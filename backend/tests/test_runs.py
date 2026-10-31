"""Runs live on the server: a client can leave and come back, a chat takes one message at a time, a run can
be stopped, and chats can be deleted. These are the bugs seen in real use (status vanished, no reply, late
approvals, no way to delete a chat)."""

import asyncio
import json

import httpx
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from sqlalchemy import select

from app.agent.tools import ToolEntry
from app.db import Approval, Message, Run, Thread, new_id
from app.main import app
from app.ops import interrupt_stale_runs
from app.tracing import clean

from .conftest import tool_call

AUTH = {"password": "correct horse battery"}


async def _runtime(make_runtime):
    @tool
    def make_file(path: str) -> str:
        """Create a file."""
        return "created"

    def respond(messages):
        if isinstance(messages[-1], ToolMessage):
            return AIMessage("done")
        if "make" not in str(messages[-1].content):
            return AIMessage("just chatting")
        return AIMessage("", tool_calls=[tool_call("make_file", {"path": "x"})])

    rt = await make_runtime(respond, extra_tools=[ToolEntry(make_file, "write")], core_tools=["make_file"])
    app.state.rt = rt
    return rt


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _events(client, url, **kw) -> list[dict]:
    async with client.stream(kw.pop("method", "GET"), url, **kw) as r:
        return [json.loads(line[6:]) async for line in r.aiter_lines() if line.startswith("data: ")]


async def _wait_for_ask(client) -> dict:
    for _ in range(100):
        asks = (await client.get("/api/live")).json()["asks"]
        if asks:
            return asks[0]
        await asyncio.sleep(0.05)
    raise AssertionError("no approval appeared")


async def test_run_survives_the_client_and_can_be_followed_again(make_runtime):
    rt = await _runtime(make_runtime)
    async with _client() as client:
        await client.post("/api/login", json=AUTH)
        thread = (await client.get("/api/threads/main")).json()["id"]
        # start a run but never read its stream, like a tab that was closed or navigated away
        chat = asyncio.create_task(client.post("/api/chat", json={"message": "make x", "thread_id": thread}))
        ask = await _wait_for_ask(client)
        live = (await client.get("/api/live")).json()
        assert [a["thread_id"] for a in live["active"]] == [thread] and live["asks"][0]["request"] == "make x"

        # a second message in the same chat is refused and says which run is busy
        busy = await client.post("/api/chat", json={"message": "again", "thread_id": thread})
        assert busy.status_code == 409 and busy.json()["detail"]["run_id"] == live["active"][0]["run_id"]

        # answering from anywhere finishes the run; a late client replays everything from the start
        assert (await client.post(f"/api/approvals/{ask['id']}", json={"approve": True})).status_code == 200
        await chat
        run_id = live["active"][0]["run_id"]
        replay = await _events(client, f"/api/runs/{run_id}/stream")
        kinds = [e["type"] for e in replay]
        assert "approval" in kinds and kinds[-1] == "run_end" and replay[-1]["status"] == "ok"
        # ...or only what it missed
        tail = await _events(client, f"/api/runs/{run_id}/stream?from={len(replay) - 1}")
        assert [e["type"] for e in tail] == ["run_end"]
        assert (await client.get("/api/live")).json() == {"active": [], "asks": []}
        assert (await client.get(f"/api/runs/{run_id}/stream?from=0&x=1")).status_code == 200
        assert (await client.get(f"/api/runs/{new_id()}/stream")).status_code == 404
        # the chat is free again
        assert rt.active_run(thread) is None


async def test_stop_ends_a_run_waiting_for_approval_and_says_so(make_runtime):
    rt = await _runtime(make_runtime)
    async with _client() as client:
        await client.post("/api/login", json=AUTH)
        thread = (await client.get("/api/threads/main")).json()["id"]
        chat = asyncio.create_task(client.post("/api/chat", json={"message": "make x", "thread_id": thread}))
        await _wait_for_ask(client)
        run_id = (await client.get("/api/live")).json()["active"][0]["run_id"]
        assert (await client.post(f"/api/runs/{run_id}/stop")).status_code == 200
        await chat
        assert (await client.post(f"/api/runs/{run_id}/stop")).status_code == 409  # already over

        live = (await client.get("/api/live")).json()
        assert live == {"active": [], "asks": []}  # the card nobody can answer is gone
        async with rt.db.session() as s:
            run = await s.get(Run, run_id)
            assert run.status == "stopped"
            assert (await s.scalars(select(Approval).where(Approval.run_id == run_id))).one().status == "expired"
            last = (await s.scalars(select(Message).where(Message.thread_id == thread)
                                    .order_by(Message.created_at.desc()))).first()
            assert last.role == "assistant" and "stopped" in last.content.lower()
        # a stopped run is visible in Activity, with what was waiting
        runs = [e for e in (await client.get("/api/activity")).json() if e["type"] == "run"]
        assert runs[0]["id"] == run_id and runs[0]["status"] == "stopped"


async def test_delete_a_chat_clear_the_main_one_and_never_during_a_run(make_runtime):
    rt = await _runtime(make_runtime)
    async with _client() as client:
        await client.post("/api/login", json=AUTH)
        events = await _events(client, "/api/chat", method="POST", json={"message": "hello there"})
        thread = events[0]["thread_id"]
        assert events[-1]["type"] == "run_end"

        assert (await client.delete(f"/api/threads/{thread}")).json() == {"ok": True, "cleared": False}
        assert (await client.get(f"/api/threads/{thread}/messages")).json() == []
        assert thread not in [t["id"] for t in (await client.get("/api/threads")).json()]
        assert (await client.delete(f"/api/threads/{thread}")).status_code == 404

        main = (await client.get("/api/threads/main")).json()["id"]
        await _events(client, "/api/chat", method="POST", json={"message": "hi main", "thread_id": main})
        assert (await client.delete(f"/api/threads/{main}")).json() == {"ok": True, "cleared": True}
        assert (await client.get(f"/api/threads/{main}/messages")).json() == []
        assert (await client.get("/api/threads/main")).json()["id"] == main  # still there, now empty

        chat = asyncio.create_task(client.post("/api/chat", json={"message": "make x", "thread_id": main}))
        ask = await _wait_for_ask(client)
        assert (await client.delete(f"/api/threads/{main}")).status_code == 409
        await client.post(f"/api/approvals/{ask['id']}", json={"approve": False})
        await chat


async def test_nul_characters_cannot_break_saving_a_reply(make_runtime):
    """Postgres text/JSONB reject U+0000; one stray byte from a web page used to lose the whole reply."""
    assert clean("a\x00b") == "ab"
    rt = await make_runtime(lambda m: AIMessage("fine\x00 reply"))
    app.state.rt = rt
    async with _client() as client:
        await client.post("/api/login", json=AUTH)
        events = await _events(client, "/api/chat", method="POST", json={"message": "hi\x00 there"})
        assert events[-1]["type"] == "run_end" and events[-1]["status"] == "ok"
        msgs = (await client.get(f"/api/threads/{events[0]['thread_id']}/messages")).json()
        assert [m["content"] for m in msgs] == ["hi there", "fine reply"]


async def test_runs_left_running_by_a_restart_are_closed_with_a_message(make_runtime):
    rt = await _runtime(make_runtime)
    async with rt.db.session() as s:
        thread = Thread(title="old")
        s.add(thread)
        await s.flush()
        s.add(Run(id=new_id(), thread_id=thread.id, source="chat", input="was working", status="running",
                     model="m", prompt_version="v"))
        await s.commit()
    await interrupt_stale_runs(rt.db)
    async with rt.db.session() as s:
        run = (await s.scalars(select(Run).where(Run.thread_id == thread.id))).one()
        assert run.status == "error" and "restarted" in run.error
        reply = (await s.scalars(select(Message).where(Message.thread_id == thread.id))).one()
        assert reply.role == "assistant" and "restarted" in reply.content
