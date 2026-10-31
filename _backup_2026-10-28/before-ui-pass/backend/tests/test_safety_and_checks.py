import pytest

from app.agent.mcp import risk_for
from app.agent.tools import assert_public_url
from app.checks import citation_check, drop_found, parse_unsupported
from app.tracing import redact
from evals.run import check_case


def test_redact_removes_secrets_and_keeps_json_valid():
    out = redact({"password": "hunter2hunter2", "key": "AKIAIOSFODNN7EXAMPLE", "auth": "Bearer abcdefghijklmnopqrstu",
                  "gh": "ghp_" + "a" * 36, "note": "hello", "tokens_in": 12345})
    assert out["password"] == "[REDACTED]" and out["key"] == "[REDACTED]" and out["auth"] == "[REDACTED]"
    assert out["gh"] == "[REDACTED]" and out["note"] == "hello" and out["tokens_in"] == 12345
    assert redact("x" * 30_000).endswith("chars]")


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://localhost:8000/", "http://10.0.0.5/x",
                                 "http://169.254.169.254/latest/meta-data", "http://[::1]/", "file:///etc/passwd",
                                 "ftp://example.com/"])
async def test_fetch_blocks_private_and_non_http(url):
    with pytest.raises(ValueError):
        await assert_public_url(url)


def test_mcp_risk_defaults_to_write():
    server = {"safe_tools": ["read_*", "get_*"], "external_tools": ["send_message", "get_*_comment*"]}
    assert risk_for(server, "read_file") == "safe"
    assert risk_for(server, "get_issue_comments") == "external"  # external wins
    assert risk_for(server, "send_message") == "external"
    assert risk_for(server, "write_file") == "write"
    with pytest.raises(ValueError):
        risk_for({"default_risk": "yolo"}, "x")


def _span(kind, name, status="ok", ref=None, output=None, risk="safe"):
    return {"kind": kind, "name": name, "status": status, "ref": ref, "output": output, "meta": {"risk": risk}}


def test_citation_check():
    spans = [_span("memory", "recall", output="... (id: abcdef0123...)"), _span("tool", "web_search", ref="t1"),
             _span("tool", "fetch_url", status="error", ref="t2")]
    assert citation_check("a [t1] b [m:abcdef01]", spans) == {"citations": 2, "invalid_citations": []}
    assert citation_check("a [t2] [t3] [m:99999999]", spans)["invalid_citations"] == ["t2", "t3", "m:99999999"]


def test_check_case():
    spans = [_span("agent", "Riki"), _span("tool", "current_time", ref="t1"),
             _span("tool", "send_email", status="denied", risk="external")]
    assert check_case({"tools_called": ["current_time"], "cites": True, "tools_not_executed": ["*email*"],
                       "no_executed_writes": True}, "It is Monday [t1].", spans, 100, 100) == []
    fails = check_case({"tools_called": ["web_search"], "max_tool_calls": 1, "must_match": ["Tuesday"],
                        "max_latency_ms": 50, "min_sub_agents": 1}, "Monday", spans, 100, 100)
    assert len(fails) == 5


async def test_mcp_servers_with_missing_env_are_skipped(tmp_path, caplog):
    from contextlib import AsyncExitStack

    from app.agent.mcp import load_mcp

    cfg = tmp_path / "mcp.json"
    cfg.write_text('{"servers": {"x": {"transport": "stdio", "command": "${RIKI_SURELY_UNSET_VAR}"},'
                   ' "off": {"enabled": false, "transport": "stdio", "command": "nope"}}}')
    async with AsyncExitStack() as stack:
        assert await load_mcp(cfg, stack) == []
    assert "RIKI_SURELY_UNSET_VAR" in caplog.text


def test_custom_openai_endpoint_reaches_chat_and_embeddings():
    from app.config import Settings
    from app.runtime import _embeddings, _model

    s = Settings(_env_file=None, password="x" * 12, model="openai:my-chat", embedding_model="openai:my-emb",
                 base_url="http://llm.local/v1", api_key="k1", embedding_base_url="http://emb.local/v1")
    chat, emb = _model(s.model, s), _embeddings(s)
    assert (chat.model_name, chat.openai_api_base, chat.openai_api_key.get_secret_value()) == \
        ("my-chat", "http://llm.local/v1", "k1")
    assert (emb.model, emb.openai_api_base, emb.openai_api_key.get_secret_value()) == \
        ("my-emb", "http://emb.local/v1", "k1")  # key falls back to RIKI_API_KEY
    assert emb.check_embedding_ctx_length is False


def test_temperature_none_leaves_the_field_out(monkeypatch):
    # Bug seen in real use: Bedrock's Kimi K3 rejects any request that has a temperature field.
    from app.config import Settings
    from app.runtime import _model

    monkeypatch.setenv("RIKI_TEMPERATURE", "none")
    s = Settings(_env_file=None, password="x" * 12, model="openai:kimi", api_key="k1")
    assert s.temperature is None
    assert "temperature" not in _model(s.model, s)._default_params
    assert _model(s.model, Settings(_env_file=None, password="x" * 12, api_key="k1", temperature=0)).temperature == 0


def test_plain_preview_strips_markdown_and_refs():
    from app.main import plain_preview

    md = "Here's the **short** version:\n\n| | FastAPI | Django |\n|---|---|---|\n| Async | native [t4] | [docs](http://x.y) [m:abcdef01] |"
    assert plain_preview(md) == "Here's the short version: FastAPI Django Async native docs"


def test_grounding_flags_only_tagged_lines_and_not_items_found_in_the_evidence():
    raw = ("Let me think: the page lists 11.\nUNSUPPORTED: 17 requests in total\n"
           "- UNSUPPORTED: The $15-an-Hour Expert — Brad Flora\nUNSUPPORTED: AI for Energy – Diana Hu")
    claims = parse_unsupported(raw)
    assert claims == ["17 requests in total", "The $15-an-Hour Expert — Brad Flora", "AI for Energy – Diana Hu"]
    proof = "Winter 2027 ... The $15-an-Hour Expert By Brad Flora ... Own Your Intelligence By Jon Xu"
    assert drop_found(claims, proof) == ["17 requests in total", "AI for Energy – Diana Hu"]
    assert parse_unsupported("NONE") == []
