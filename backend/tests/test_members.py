"""Several people on one Riki: they must never see, touch or act through each other's things."""

import json

import httpx
import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError

from app.agent.tools import ToolEntry
from app.agent.graph import Agent  # noqa: F401  (imported so a refactor that moves it breaks this file loudly)
from app.db import Device, Run, Thread, User, utcnow
from app.main import app
from app.tenancy import Who, reset_who, set_who
from langchain_core.tools import tool

from .test_email_login import EMAIL, OWNER, inbox, no_sleep  # noqa: F401  (fixtures)


@tool
async def read_owner_mail(query: str = "") -> str:
    """The owner's Gmail, standing in for a connected app."""
    return "SECRET: the owner's mail"


async def setup(make_runtime, inbox, respond=None, **kw):
    """A runtime in email mode with no members, and the owner signed in."""
    rt = await make_runtime(respond or (lambda messages: AIMessage("hello")), extra_tools=[ToolEntry(read_owner_mail, "safe", "app:gmail")],
                            **EMAIL, **kw)
    app.state.rt = rt
    async with rt.db.session(system=True) as s:
        await s.execute(update(User).values(login=None))
        await s.execute(delete(Device))
        await s.execute(delete(User).where(User.role == "member"))
        await s.commit()
    return rt


async def browser(rt, inbox, email) -> httpx.AsyncClient:
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    assert (await c.post("/api/login/code", json={"email": email})).status_code == 200
    assert (await c.post("/api/login/verify", json={"email": email, "code": inbox[-1][1]})).status_code == 200
    return c


async def invite(owner, name):
    r = await owner.post("/api/users", json={"email": f"{name}@example.com", "name": name.title()})
    assert r.status_code == 201, r.text
    return r.json()


async def chat(client, message, thread_id=None):
    async with client.stream("POST", "/api/chat", json={"message": message, "thread_id": thread_id}) as r:
        events = [json.loads(line[6:]) async for line in r.aiter_lines() if line.startswith("data: ")]
    assert r.status_code == 200, r.status_code
    return events[0]["thread_id"], events[0]["run_id"], events


async def three(make_runtime, inbox, **kw):
    rt = await setup(make_runtime, inbox, **kw)
    owner = await browser(rt, inbox, OWNER)
    alice_id, bob_id = (await invite(owner, "alice"))["id"], (await invite(owner, "bob"))["id"]
    return rt, owner, await browser(rt, inbox, "alice@example.com"), await browser(rt, inbox, "bob@example.com"), alice_id, bob_id


async def test_people_only_see_their_own_chats_runs_and_messages(make_runtime, inbox):
    rt, owner, alice, bob, alice_id, _ = await three(make_runtime, inbox)
    t_owner, run_owner, _ = await chat(owner, "owner private chat")
    t_alice, run_alice, _ = await chat(alice, "alice private chat")

    mine = {t["id"] for t in (await alice.get("/api/threads")).json()}
    assert t_alice in mine and t_owner not in mine
    assert t_owner not in {t["id"] for t in (await bob.get("/api/threads")).json()}

    # reading, renaming, deleting someone else's chat or run: not found, and nothing changed
    assert (await alice.get(f"/api/threads/{t_owner}/messages")).json() in ([], {"messages": []}) or \
        (await alice.get(f"/api/threads/{t_owner}/messages")).status_code == 404
    assert (await bob.get(f"/api/runs/{run_alice}")).status_code == 404
    assert (await bob.patch(f"/api/threads/{t_alice}", json={"title": "hacked"})).status_code in (404, 200)
    await bob.delete(f"/api/threads/{t_alice}")
    await alice.delete(f"/api/threads/{t_owner}")
    async with rt.db.session(system=True) as s:
        titles = {t.id: t.title for t in (await s.scalars(select(Thread))).all()}
    assert t_alice in titles and titles[t_alice] != "hacked" and t_owner in titles  # both still there

    # chatting into someone else's thread id must not write into it
    stray = await alice.post("/api/chat", json={"message": "write into the owner's chat", "thread_id": t_owner})
    async with rt.db.session(system=True) as s:
        stolen = (await s.scalars(select(Run).where(Run.thread_id == t_owner, Run.input == "write into the owner's chat"))).all()
    assert stolen == [], f"a member wrote into the owner's chat (status {stray.status_code})"


async def test_the_database_hides_other_peoples_rows_even_without_a_filter(make_runtime, inbox):
    rt, owner, alice, bob, alice_id, bob_id = await three(make_runtime, inbox)
    await chat(owner, "one")
    await chat(alice, "two")
    await chat(bob, "three")

    token = set_who(Who(alice_id, "member", "Alice"))
    try:
        async with rt.db.session() as s:  # a query with NO where clause
            seen = (await s.scalars(select(Run))).all()
            people = (await s.scalars(select(User))).all()
        assert [r.input for r in seen] == ["two"]
        assert [u.id for u in people] == [alice_id]  # not even the other accounts
        with pytest.raises(DBAPIError):  # and she cannot write a row as someone else
            async with rt.db.session() as s:
                s.add(Thread(title="forged", user_id=bob_id))
                await s.commit()
    finally:
        reset_who(token)
    async with rt.db.session(system=True) as s:
        everyone = {r.input for r in (await s.scalars(select(Run))).all()}
        assert {"one", "two", "three"} <= everyone  # the system view still sees all (sign-in, admin)


async def test_a_member_cannot_reach_owner_only_endpoints(make_runtime, inbox):
    rt, owner, alice, *_ = await three(make_runtime, inbox)
    for method, path in [("get", "/api/settings"), ("get", "/api/apps"), ("get", "/api/tools"), ("get", "/api/status"),
                         ("get", "/api/telegram"), ("get", "/api/schedules"), ("get", "/api/users"), ("get", "/api/persona"),
                         ("get", "/api/logs"), ("get", "/api/reflection"), ("get", "/api/grants"), ("get", "/api/threads/telegram"),
                         ("post", "/api/heartbeat/run"), ("get", "/api/evals"), ("get", "/api/metrics")]:
        r = await getattr(alice, method)(path)
        assert r.status_code == 403, f"{method.upper()} {path} answered {r.status_code} to a member"
    assert (await alice.post("/api/users", json={"email": "x@example.com", "name": "X"})).status_code == 403
    assert (await alice.put("/api/persona", json={"name": "Evil", "soul": "", "hue": 1, "heartbeat_minutes": 0})).status_code == 403
    assert (await owner.get("/api/users")).status_code == 200


async def test_a_member_gets_none_of_the_owners_tools_and_cannot_call_them(make_runtime, inbox, monkeypatch):
    from tests.conftest import ScriptedModel, tool_call  # the scripted model the suite already uses
    bound: list[list[str]] = []
    monkeypatch.setattr(ScriptedModel, "bind_tools", lambda self, tools, **kw: (bound.append(sorted(t.name for t in tools)), self)[1])

    calls = {"n": 0}

    def respond(messages):
        calls["n"] += 1
        if calls["n"] == 1:  # the model tries a tool it was never offered
            return AIMessage("", tool_calls=[tool_call("read_owner_mail", {"query": "passwords"})])
        return AIMessage("done")

    rt, owner, alice, *_ = await three(make_runtime, inbox, respond=respond)
    bound.clear()
    calls["n"] = 0
    _, run_id, events = await chat(alice, "what is LLM steering? look it up please")
    offered = {n for names in bound for n in names}
    assert offered, "the member run should have offered some tools"
    blocked = {"connect_app", "run_python", "schedule", "list_schedules", "cancel_schedule", "read_owner_mail"}
    assert not offered & blocked, f"a member was offered {offered & blocked}"
    tool_results = [e for e in events if e["type"] == "span_end" and e.get("span", {}).get("name") == "read_owner_mail"]
    assert all("SECRET" not in json.dumps(e) for e in events), "the owner's mail reached a member's run"
    assert tool_results == [] or all(e["span"].get("status") != "ok" for e in tool_results)


async def test_each_person_has_their_own_memory_and_prompt(make_runtime, inbox):
    rt, owner, alice, bob, alice_id, _ = await three(make_runtime, inbox)
    users_seen, prompts = [], []
    real = rt.memory.context

    async def spy(user, message, **kw):
        users_seen.append((user, kw.get("name")))
        return await real(user, message, **kw)

    rt.memory.context = spy
    real_agent_run = rt.agent.run

    async def spy_run(spec, *a, **kw):
        prompts.append(spec.system if hasattr(spec, "system") else str(spec))
        return await real_agent_run(spec, *a, **kw)

    rt.agent.run = spy_run
    await chat(owner, "tell me about my project please")
    await chat(alice, "tell me about my project please")
    assert users_seen[0][0] == rt.settings.user_id and users_seen[1] == (alice_id, "Alice")
    owner_prompt, alice_prompt = prompts[0], prompts[1]
    assert "Alice" in alice_prompt and "not available on their account" in alice_prompt
    assert "read_owner_mail" not in alice_prompt  # the owner's connected app is not in a member's tool catalog
    assert "below): none. Not connected yet: none" in alice_prompt  # no owner apps listed as connected
    assert "not available on their account" not in owner_prompt  # the owner's prompt is untouched


async def test_live_shows_only_your_own_runs(make_runtime, inbox):
    rt, owner, alice, bob, alice_id, bob_id = await three(make_runtime, inbox)
    now = utcnow()
    rt.active.update({"a" * 32: {"run_id": "a" * 32, "thread_id": None, "source": "chat", "input": "alice's", "started_at": now, "user_id": alice_id},
                      "b" * 32: {"run_id": "b" * 32, "thread_id": None, "source": "chat", "input": "bob's", "started_at": now, "user_id": bob_id}})
    try:
        assert [a["input"] for a in (await alice.get("/api/live")).json()["active"]] == ["alice's"]
        assert [a["input"] for a in (await bob.get("/api/live")).json()["active"]] == ["bob's"]
        assert (await owner.get("/api/live")).json()["active"] == []
    finally:
        rt.active.clear()


async def test_disabling_signs_a_person_out_at_once_and_removing_deletes_their_data(make_runtime, inbox, monkeypatch):
    rt, owner, alice, bob, alice_id, _ = await three(make_runtime, inbox)
    thread, _, _ = await chat(alice, "remember this")
    assert (await alice.get("/api/threads")).status_code == 200

    assert (await owner.patch(f"/api/users/{alice_id}", json={"disabled": True})).status_code == 200
    assert (await alice.get("/api/threads")).status_code == 401  # their next request, not their next sign-in
    assert (await alice.post("/api/login/code", json={"email": "alice@example.com"})).status_code == 200
    assert (await alice.post("/api/login/verify", json={"email": "alice@example.com", "code": "000000"})).status_code == 401

    cleared = []

    async def delete_user(user_id):
        cleared.append(user_id)
        return True

    monkeypatch.setattr(rt.memory, "delete_user", delete_user, raising=False)
    assert (await owner.delete(f"/api/users/{alice_id}")).status_code == 200
    assert cleared == [alice_id]
    async with rt.db.session(system=True) as s:
        assert await s.get(Thread, thread) is None  # their chats went with them
    assert (await bob.get("/api/threads")).status_code == 200  # nobody else was touched


async def test_the_owner_cannot_be_disabled_or_removed_through_the_api(make_runtime, inbox):
    rt, owner, *_ = await three(make_runtime, inbox)
    # the owner's id is not a 32-hex member id, so these never match a route that could touch it
    assert (await owner.patch(f"/api/users/{rt.settings.user_id}", json={"disabled": True})).status_code == 422
    assert (await owner.delete(f"/api/users/{rt.settings.user_id}")).status_code == 422
    async with rt.db.session(system=True) as s:
        assert (await s.get(User, rt.settings.user_id)).disabled is False


async def test_inviting_needs_email_sign_in(make_runtime):
    app.state.rt = await make_runtime(lambda messages: AIMessage("ok"))  # password mode
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        await c.post("/api/login", json={"password": "correct horse battery"})
        assert (await c.post("/api/users", json={"email": "a@example.com", "name": "A"})).status_code == 409


async def test_signing_out_everywhere_only_affects_your_own_browsers(make_runtime, inbox):
    rt, owner, alice, bob, *_ = await three(make_runtime, inbox)
    assert (await alice.post("/api/logout/all")).status_code == 200
    assert (await alice.get("/api/threads")).status_code == 401
    assert (await owner.get("/api/threads")).status_code == 200 and (await bob.get("/api/threads")).status_code == 200


async def test_a_member_can_open_every_page_they_are_shown_and_sees_only_their_own(make_runtime, inbox):
    rt, owner, alice, *_ = await three(make_runtime, inbox)
    await chat(owner, "the owner's private question about their salary")
    await chat(alice, "alice's question")
    for path in ["/api/me", "/api/today", "/api/activity", "/api/spend", "/api/memories", "/api/threads", "/api/threads/main",
                 "/api/approvals", "/api/live", "/api/artifacts", "/api/export"]:
        r = await alice.get(path)
        assert r.status_code == 200, f"GET {path} gave a member {r.status_code}: {r.text[:200]}"
        assert "salary" not in r.text and OWNER not in r.text, f"{path} showed a member something of the owner's"
    me = (await alice.get("/api/me")).json()
    assert me["account"] == "member" and me["agent_name"] == "Riki" and me["telegram"] is False and me["model"] == ""
    assert (await owner.get("/api/me")).json()["account"] == "owner"
