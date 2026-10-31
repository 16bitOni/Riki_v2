"""Fast turns, web search through Tavily, apps the agent can ask for, and keys that stay secret.
Bugs seen in real use: "hi" took a minute (memory planner + every tool), searches for things the model knows."""

import asyncio
import base64
import hashlib
import json
import time

import httpx
import pytest
from cryptography.fernet import Fernet
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app import search
from app.apps import CATALOG, secret_box
from app.db import Connection, Message
from app.main import app
from app.tracing import redact, scrub
from app.turn import is_small_talk

from .conftest import ScriptedModel

AUTH = {"password": "correct horse battery"}
KEY = "tvly-dev-abcdefghij1234567890"


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _chat(client, message: str, thread_id: str | None = None) -> list[dict]:
    async with client.stream("POST", "/api/chat", json={"message": message, "thread_id": thread_id}) as r:
        return [json.loads(line[6:]) async for line in r.aiter_lines() if line.startswith("data: ")]


@pytest.mark.parametrize("text,small", [
    ("hi", True), ("Hey Riki!", True), ("thanks 🙂".replace(" 🙂", "!"), True), ("ok cool", True),
    ("how are you", True), ("good morning riki", True),
    ("hi, what's my sister's name?", False), ("what is LLM steering", False), ("ok book a table", False),
    ("", False), ("riki", False),
])
def test_only_small_talk_skips_memory(text, small):
    assert is_small_talk(text, "Riki") is small


async def test_small_talk_uses_no_memory_and_no_tools_but_questions_do(make_runtime, monkeypatch):
    bound: list[list[str]] = []
    monkeypatch.setattr(ScriptedModel, "bind_tools", lambda self, tools, **kw: (bound.append([t.name for t in tools]), self)[1])
    rt = await make_runtime(lambda messages: AIMessage("hey!"))
    lookups: list[str] = []
    real = rt.memory.context

    async def counting(user, message, **kw):
        lookups.append(message)
        return await real(user, message, **kw)

    rt.memory.context = counting
    app.state.rt = rt
    async with _client() as c:
        await c.post("/api/login", json=AUTH)
        events = await _chat(c, "hi riki!")
        assert lookups == [] and bound == []
        assert not [e for e in events if e["type"] == "span_start" and e["span"]["kind"] == "memory"]
        assert events[-1]["type"] == "run_end" and events[-1]["status"] == "ok"

        await _chat(c, "what did I say about chai?")
        assert lookups == ["what did I say about chai?"]
        assert {"recall_memory", "web_search", "connect_app", "spawn_agent"} <= set(bound[0])


async def test_slow_memory_never_holds_back_the_answer(make_runtime):
    rt = await make_runtime(lambda messages: AIMessage("here you go"), memory_timeout_s=0.2)

    async def hang(*a, **kw):
        await asyncio.sleep(30)

    rt.memory.context = hang
    app.state.rt = rt
    async with _client() as c:
        await c.post("/api/login", json=AUTH)
        started = time.perf_counter()
        events = await _chat(c, "what is my sister called?")
    assert time.perf_counter() - started < 5
    assert events[-1]["status"] == "ok" and events[-1]["output"] == "here you go"


def test_prompt_teaches_when_to_answer_alone_and_how_to_connect_apps():
    from app.agent.prompts import main_prompt

    p = main_prompt("Sam", "(none)", apps=", ".join(CATALOG))
    assert "answer from your own knowledge" in p and "comes from a tool in this" in p and "connect_app" in p and "tavily" in p
    assert "Never ask for a key in chat" in p and "paste a key, token or password into the chat" in p


def _tavily(monkeypatch, handler):
    transport, real = httpx.MockTransport(handler), httpx.AsyncClient
    monkeypatch.setattr(search.httpx, "AsyncClient", lambda **kw: real(**{"transport": transport, **kw}))


async def test_tavily_key_is_checked_stored_encrypted_and_used_for_search(make_runtime, monkeypatch):
    seen: list[tuple] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.headers.get("authorization") != f"Bearer {KEY}":
            return httpx.Response(401, json={"detail": "bad key"})
        if request.url.path == "/search" and json.loads(request.content)["query"] == "limit":
            return httpx.Response(429)
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": [{"title": "Result A", "url": "https://a.example", "content": "snippet A"}]})
        return httpx.Response(200, json={"account": {}})

    async def ddg(query, max_results=5):
        return "- from duckduckgo"

    _tavily(monkeypatch, handler)
    monkeypatch.setattr(search, "duckduckgo", ddg)
    rt = await make_runtime(lambda messages: AIMessage("ok"))
    app.state.rt = rt
    web_search = rt.registry.get("web_search").tool
    assert "Basic search" in await web_search.ainvoke({"query": "anything"})  # not connected yet

    async with _client() as c:
        await c.post("/api/login", json=AUTH)
        bad = (await c.post("/api/apps/tavily/connect", json={"token": "tvly-wrong-key-12345678"})).json()
        assert bad["state"] == "error" and bad["error"] == "Tavily rejected the key"
        assert "tvly-wrong" not in json.dumps(bad)

        good = (await c.post("/api/apps/tavily/connect", json={"token": KEY})).json()
        assert good["state"] == "connected" and good["tools"] == [{"name": "web_search", "risk": "safe"}]
        listing = (await c.get("/api/apps")).json()
        assert KEY not in json.dumps(listing) and next(a for a in listing["apps"] if a["id"] == "tavily")["connected"]

        out = await web_search.ainvoke({"query": "news today", "topic": "news"})
        assert "- Result A | https://a.example\n  snippet A" in out and "Basic search" not in out
        assert ("POST", "/search", f"Bearer {KEY}") in seen

        out = await web_search.ainvoke({"query": "limit"})  # Tavily's plan limit: say so, still answer
        assert "usage limit" in out and "from duckduckgo" in out

        async with rt.db.session() as db:
            row = await db.get(Connection, "tavily")
        assert KEY.encode() not in row.secret and json.loads(rt.apps.box.decrypt(row.secret))["token"] == KEY

        await c.delete("/api/apps/tavily")
        assert "Basic search" in await web_search.ainvoke({"query": "anything"})
        async with rt.db.session() as db:
            assert await db.get(Connection, "tavily") is None


async def test_connect_app_tool_shows_a_card_and_only_the_main_agent_has_it(make_runtime):
    rt = await make_runtime(lambda messages: AIMessage("ok"))
    connect = rt.registry.get("connect_app").tool
    assert "Listed apps" in await connect.ainvoke({"app": "nope"})
    asked = await connect.ainvoke({"app": "tavily", "reason": "better search"})
    assert "card to connect Tavily" in asked and "you never see it" in asked
    rt.apps.state["tavily"] = {"state": "connected"}
    assert "already connected" in await connect.ainvoke({"app": "tavily"})
    assert rt.agent.offerable("connect_app", 0) and not rt.agent.offerable("connect_app", 1)
    assert "- connect_app" not in rt.sub_system and "tavily" in rt.main_system


async def test_a_key_pasted_in_chat_never_reaches_the_model_or_the_database(make_runtime):
    seen: list[str] = []

    def respond(messages):
        seen.extend(str(m.content) for m in messages)
        return AIMessage("I removed that key. Paste it in the card instead.")

    rt = await make_runtime(respond)
    app.state.rt = rt
    async with _client() as c:
        await c.post("/api/login", json=AUTH)
        events = await _chat(c, f"my tavily key is {KEY} use it")
    assert events[-1]["status"] == "ok"
    assert not any(KEY in text for text in seen) and any("[secret removed]" in text for text in seen)
    async with rt.db.session() as db:
        rows = (await db.scalars(select(Message.content))).all()
    assert not any(KEY in r for r in rows)
    assert rt.memory.remembered and KEY not in rt.memory.remembered[-1][1]


async def test_secrets_are_not_echoed_back_or_shown_in_typed_addresses(make_runtime):
    rt = await make_runtime(lambda messages: AIMessage("ok"))
    app.state.rt = rt
    mine = "https://mcp.example.com/s/SECRETPATH/mcp?key=abc123"
    async with rt.db.session() as db:
        db.add(Connection(id="mine", name="Mine", url=mine, auth="oauth", mode="ask", enabled=False))
        await db.commit()
    try:
        async with _client() as c:
            await c.post("/api/login", json=AUTH)
            too_long = await c.post("/api/apps/tavily/connect", json={"token": "x" * 5000})
            assert too_long.status_code == 422 and "xxxxx" not in too_long.text
            listing = (await c.get("/api/apps")).json()["apps"]
    finally:  # the database is shared with the other tests
        async with rt.db.session() as db:
            await db.delete(await db.get(Connection, "mine"))
            await db.commit()
    mine_view = next(a for a in listing if a["id"] == "mine")
    assert mine_view["url"] == mine_view["blurb"] == "https://mcp.example.com"
    assert "SECRETPATH" not in json.dumps(listing) and "abc123" not in json.dumps(listing)


@pytest.mark.parametrize("secret", [
    KEY, "sk-abcdefghijklmnopqrstuv", "sk_live_abcdefghijklmnop1234", "hf_" + "a" * 34, "ghp_" + "b" * 24,
    "Bearer abcdefghijklmnopqrstuvwxyz",
])
def test_known_key_shapes_are_scrubbed(secret):
    assert secret not in scrub(f"use {secret} now") and secret not in json.dumps(redact({"note": f"x {secret}"}))
    assert scrub("plain text, tvly is a word") == "plain text, tvly is a word"


def test_secrets_saved_under_the_old_key_still_open_and_move_to_the_new_one():
    old = Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"riki-apps-v1\0" + b"correct horse battery").digest()))
    legacy = old.encrypt(b'{"token": "t"}')
    box = secret_box("correct horse battery")
    assert box.decrypt(legacy) == b'{"token": "t"}'
    moved = box.rotate(legacy)
    with pytest.raises(Exception):
        old.decrypt(moved)
    assert box.decrypt(moved) == b'{"token": "t"}'
