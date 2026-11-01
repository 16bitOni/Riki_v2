import json

import httpx
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy.ext.asyncio import create_async_engine

from app.db import Artifact, Base
from app.main import app

from .conftest import tool_call


async def test_models_match_migrations(database):
    engine = create_async_engine(database)
    async with engine.connect() as conn:
        diff = await conn.run_sync(lambda c: compare_metadata(MigrationContext.configure(c), Base.metadata))
    await engine.dispose()
    assert diff == [], f"models changed without a migration: {diff}"


async def test_api_login_chat_and_trace(make_runtime):
    def respond(messages):
        if isinstance(messages[-1], ToolMessage):
            return AIMessage("It's late, go sleep [t1].")
        return AIMessage("", tool_calls=[tool_call("current_time", {})])

    app.state.rt = await make_runtime(respond)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/threads")).status_code == 401
        assert (await client.post("/api/login", json={"password": "wrong password!!"})).status_code == 401
        assert (await client.post("/api/login", json={"password": "correct horse battery"})).status_code == 200

        events = []
        async with client.stream("POST", "/api/chat", json={"message": "what time is it?"}) as r:
            assert r.status_code == 200
            async for line in r.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
        kinds = [e["type"] for e in events]
        assert kinds[0] == "thread" and kinds[-1] == "run_end" and "token" in kinds and "span_end" in kinds
        run_id, thread_id = events[0]["run_id"], events[0]["thread_id"]

        detail = (await client.get(f"/api/runs/{run_id}")).json()
        assert detail["run"]["status"] == "ok"
        assert {s["kind"] for s in detail["spans"]} == {"memory", "agent", "llm", "tool"}
        msgs = (await client.get(f"/api/threads/{thread_id}/messages")).json()
        assert [m["role"] for m in msgs] == ["user", "assistant"]
        assert (await client.post(f"/api/runs/{run_id}/feedback", json={"score": -1})).status_code == 200
        assert (await client.get("/api/metrics")).json()[-1]["thumbs_down"] == 1
        assert (await client.post("/api/chat", json={"message": ""})).status_code == 422
        assert (await client.post("/api/chat", json={"message": "x", "thread_id": "../etc"})).status_code == 422


async def test_persona_status_logs_approvals_and_heartbeat(make_runtime):
    import asyncio

    from langchain_core.tools import tool

    from app.agent import policy
    from app.agent.tools import ToolEntry
    from app.ops import beat

    @tool
    def make_file(path: str) -> str:
        """Create a file."""
        return "created"

    def respond(messages):
        if isinstance(messages[-1], ToolMessage):
            return AIMessage("done")
        return AIMessage("", tool_calls=[tool_call("make_file", {"path": "x"})])

    rt = await make_runtime(respond, extra_tools=[ToolEntry(make_file, "write")], core_tools=["make_file"])
    app.state.rt = rt
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/api/login", json={"password": "correct horse battery"})

        # persona edits rebuild the prompt and change the recorded prompt version
        before = rt.prompt_version
        r = await client.put("/api/persona", json={"name": "Momo", "soul": "Playful, uses Bengali words.",
                                                   "hue": 30, "heartbeat_minutes": 0, "heartbeat_prompt": ""})
        assert r.status_code == 200 and r.json()["name"] == "Momo"
        assert "Playful, uses Bengali words." in rt.main_system and "You are Momo" in rt.main_system
        assert rt.prompt_version != before and (await client.get("/api/me")).json()["agent_name"] == "Momo"
        assert (await client.put("/api/persona", json={"name": "", "hue": 999, "heartbeat_minutes": 0})
                ).status_code == 422

        # a pending approval shows up in the inbox and the status badge, from any page
        main = (await client.get("/api/threads/main")).json()
        assert main["kind"] == "main" and (await client.get("/api/threads/main")).json()["id"] == main["id"]
        chat = asyncio.create_task(client.post("/api/chat", json={"message": "make x", "thread_id": main["id"]}))
        for _ in range(100):
            inbox = (await client.get("/api/approvals")).json()
            if inbox:
                break
            await asyncio.sleep(0.05)
        assert inbox[0]["tool"] == "make_file" and inbox[0]["request"] == "make x" and inbox[0]["risk"] == "write"
        status = (await client.get("/api/status")).json()
        assert status["pending_approvals"] == 1 and len(status["active"]) == 1 and status["db"]["ok"]
        await policy.decide(rt.db, inbox[0]["id"], True)
        await chat
        assert (await client.get("/api/approvals")).json() == []
        assert (await client.get("/api/approvals?status=decided")).json()[0]["status"] == "approved"
        assert all(t["kind"] == "side" for t in (await client.get("/api/threads")).json())

        # heartbeats can't get approvals: the write is refused, the run is recorded
        await beat(rt, rt.persona)
        feed = (await client.get("/api/heartbeat")).json()
        assert feed[0]["source"] == "heartbeat" and feed[0]["status"] == "ok"
        detail = (await client.get(f"/api/runs/{feed[0]['id']}")).json()
        assert [s["status"] for s in detail["spans"] if s["kind"] == "tool"] == ["denied"]

        logs = (await client.get("/api/logs?level=INFO")).json()
        assert isinstance(logs, list)
        assert (await client.get("/api/logs?level=DEBUG")).status_code == 422


async def test_failed_run_tells_the_user_why(make_runtime):
    def respond(messages):
        raise RuntimeError("model rejected the request")

    app.state.rt = await make_runtime(respond)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/api/login", json={"password": "correct horse battery"})).status_code == 200
        async with client.stream("POST", "/api/chat", json={"message": "hi"}) as r:
            events = [json.loads(line[6:]) async for line in r.aiter_lines() if line.startswith("data: ")]
        end = events[-1]
        assert end["type"] == "run_end" and end["status"] == "error" and "model rejected" in end["error"]
        msgs = (await client.get(f"/api/threads/{events[0]['thread_id']}/messages")).json()
        assert "model rejected" in msgs[-1]["error"]


async def test_avatar_upload_is_validated_by_content(make_runtime):
    app.state.rt = await make_runtime(lambda m: AIMessage("hi"))
    gif = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.put("/api/persona/avatar", content=gif)).status_code == 401
        await client.post("/api/login", json={"password": "correct horse battery"})
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        assert (await client.put("/api/persona/avatar", content=svg,
                                 headers={"Content-Type": "image/png"})).status_code == 415
        assert (await client.put("/api/persona/avatar", content=b"GIF89a" + b"0" * (5 * 1024 * 1024))
                ).status_code == 413
        assert (await client.put("/api/persona/avatar", content=gif)).status_code == 200
        url = (await client.get("/api/me")).json()["avatar"]
        r = await client.get(url)
        assert r.status_code == 200 and r.content == gif and r.headers["content-type"] == "image/gif"
        assert (await client.delete("/api/persona/avatar")).status_code == 200
        assert (await client.get("/api/me")).json()["avatar"] is None
        assert (await client.get("/api/persona/avatar")).status_code == 404


async def test_today_threads_activity_corrections_and_export(make_runtime):
    from app.ops import in_quiet_hours

    def respond(messages):
        if isinstance(messages[-1], ToolMessage):
            return AIMessage("Noted.")
        if "hi" == messages[-1].content.split("\n\n")[-1]:
            return AIMessage("Hey!")
        return AIMessage("", tool_calls=[tool_call("remember", {"fact": "Subhadip likes chai"})])

    rt = await make_runtime(respond)
    app.state.rt = rt
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/api/login", json={"password": "correct horse battery"})

        # a side chat that starts with "hi" gets retitled by its first real message
        async def say(message, thread=None):
            async with client.stream("POST", "/api/chat", json={"message": message, "thread_id": thread}) as r:
                lines = [json.loads(line[6:]) async for line in r.aiter_lines() if line.startswith("data: ")]
            return lines[0]["thread_id"]

        thread_id = await say("hi")
        await say("remember that I like chai", thread_id)
        side = next(t for t in (await client.get("/api/threads")).json() if t["id"] == thread_id)
        assert side["title"] == "remember that I like chai" and side["preview"] == "Noted."
        assert (await client.patch(f"/api/threads/{thread_id}", json={"title": "Chai"})).status_code == 200
        assert (await client.get("/api/threads")).json()[0]["title"] == "Chai"
        assert (await client.patch(f"/api/threads/{thread_id}", json={"title": ""})).status_code == 422

        today = (await client.get("/api/today")).json()
        assert today["continue"][0]["id"] == thread_id and today["learned"]["count"] == 1
        assert today["today"]["messages"] >= 2 and today["needs_you"] == []

        activity = (await client.get("/api/activity")).json()
        assert {e["type"] for e in activity} == {"action", "memory", "run"}
        run_ = next(e for e in activity if e["type"] == "run")
        assert run_["request"] == "remember that I like chai" and run_["steps"] == 1 and run_["status"] == "ok"
        assert (await client.get("/api/activity?q=zzz-nothing")).json() == []

        mem_id = next(e["id"] for e in activity if e["type"] == "memory")
        fixed = (await client.patch(f"/api/memories/{mem_id}", json={"content": "Subhadip likes masala chai"})).json()
        assert fixed["content"] == "Subhadip likes masala chai"

        async with app.state.rt.db.session() as db:
            db.add(Artifact(thread_id=thread_id, kind="md", format="md", title="Plan", content="# Plan"))
            await db.commit()
        export = await client.get("/api/export")
        assert "attachment" in export.headers["content-disposition"]
        body = export.json()
        assert body["memories"][0]["content"] == "Subhadip likes masala chai"
        assert any(c["id"] == thread_id and len(c["messages"]) == 4 for c in body["chats"])
        assert "avatar" not in body["persona"]
        assert [(f["title"], f["content"]) for f in body["files"]] == [("Plan", "# Plan")]

    assert in_quiet_hours(23, "22-8") and in_quiet_hours(3, "22-8") and not in_quiet_hours(8, "22-8")
    assert in_quiet_hours(13, "12-14") and not in_quiet_hours(5, "0-0")


async def test_a_single_message_can_be_deleted(make_runtime):
    app.state.rt = await make_runtime(lambda m: AIMessage("hi there"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/api/login", json={"password": "correct horse battery"})
        events = []
        async with client.stream("POST", "/api/chat", json={"message": "hello"}) as r:
            async for line in r.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
        run_id, thread_id = events[0]["run_id"], events[0]["thread_id"]
        assert (await client.delete(f"/api/runs/{run_id}/messages/assistant")).status_code == 200
        msgs = (await client.get(f"/api/threads/{thread_id}/messages")).json()
        assert [m["role"] for m in msgs] == ["user"]
        assert (await client.delete(f"/api/runs/{run_id}/messages/assistant")).status_code == 404
        assert (await client.delete(f"/api/runs/{run_id}/messages/system")).status_code == 422
        assert (await client.get(f"/api/runs/{run_id}")).status_code == 200  # the trace stays
