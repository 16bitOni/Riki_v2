"""Setup: pick what you use Riki for, tell it about yourself, let it read your apps (read only) and keep the facts
you tick."""

import asyncio

import httpx
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from sqlalchemy import select

from app.agent.policy import SAFE, WRITE
from app.agent.tools import ToolEntry
from app.config import Settings
from app.db import Run, Span
from app.main import app

from .conftest import tool_call


@tool
def gmail_search(query: str) -> str:
    """Search mail."""
    return "Calendar invite: Riki standup with Priya (manager), every Monday 10:00."


@tool
def gmail_create_draft(to: str, body: str) -> str:
    """Make a draft."""
    return "draft made"


async def test_setup_learns_only_what_you_tick_and_its_scan_can_change_nothing(make_runtime):
    def respond(messages):
        last = messages[-1]
        if isinstance(last, ToolMessage):
            return AIMessage("- Subhadip has a Riki standup with Priya, his manager, every Monday [t1].")
        if "Get to know me" in str(last.content):  # the scan tries a change too: it must be refused in code
            return AIMessage("", tool_calls=[tool_call("gmail_search", {"query": "from:me"}),
                                             tool_call("gmail_create_draft", {"to": "priya@x.com", "body": "hi"}, 1)])
        return AIMessage("ok")

    core = [*Settings.model_fields["core_tools"].default, "gmail_search", "gmail_create_draft"]
    rt = await make_runtime(respond, core_tools=core,
                            extra_tools=[ToolEntry(gmail_search, SAFE), ToolEntry(gmail_create_draft, WRITE)])
    app.state.rt = rt
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/api/login", json={"password": "correct horse battery"})
        me = (await client.get("/api/me")).json()
        assert me["onboarded"] is False and me["role"] == ""

        assert (await client.post("/api/onboarding", json={"role": "hacker"})).status_code == 422
        got = (await client.post("/api/onboarding", json={
            "role": "developer", "about": "I'm building Riki in Python. key sk-abcdefghijklmnopqrstuv"})).json()
        assert got["role"] == "developer" and not got["done"] and any(r["id"] == "developer" for r in got["roles"])
        assert rt.persona.heartbeat_prompt.startswith("Check my open pull requests")
        await asyncio.gather(*rt.background)
        assert rt.memory.remembered[-1][1] == "I'm building Riki in Python. key [secret removed]"

        run_id = (await client.post("/api/onboarding/scan")).json()["run_id"]
        output = await rt.runs[run_id][1]
        assert output.startswith("- Subhadip has a Riki standup")
        async with rt.db.session() as s:
            run = await s.get(Run, run_id)
            spans = {sp.name: sp.status for sp in (await s.scalars(select(Span).where(Span.run_id == run_id))).all()}
        assert run.source == "onboarding"
        assert spans["gmail_search"] == "ok" and spans["gmail_create_draft"] == "denied"
        assert rt.memory.items == []  # nothing is learned until you pick

        r = await client.post("/api/onboarding/facts", json={"run_id": run_id, "facts": [
            "Subhadip has a Riki standup with Priya, his manager, every Monday.",
            "Subhadip has a Riki standup with Priya, his manager,   every Monday.",  # same fact twice
            "Subhadip's token is ghp_abcdefghijklmnopqrstuvwxyz0123"]})
        assert r.json() == {"saved": 1}
        assert rt.memory.items[0].source_key == f"{run.thread_id}/{run_id}"
        assert (await client.post("/api/onboarding/facts", json={"facts": ["a fact"] * 31})).status_code == 422

        assert (await client.get("/api/today")).json()["suggest"][0] == "What needs me on GitHub today?"
        await client.post("/api/onboarding", json={"done": True})
        assert (await client.get("/api/me")).json()["onboarded"] is True
