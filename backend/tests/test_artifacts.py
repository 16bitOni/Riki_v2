"""The two file tools: Markdown files and designed HTML pages, and the sandbox the page is shown in."""

import httpx
from langchain_core.messages import AIMessage

from app.db import new_id
from app.main import app
from app.tracing import RUN, RunContext

AUTH = {"password": "correct horse battery"}


async def test_markdown_and_page_are_saved_revised_and_the_page_is_sandboxed(make_runtime):
    rt = await make_runtime(lambda messages: AIMessage("ok"))
    app.state.rt = rt
    RUN.set(RunContext(run_id=new_id()))
    save_md, save_page = rt.registry.get("save_markdown").tool, rt.registry.get("save_artifact").tool

    out = await save_md.ainvoke({"title": "Notes", "content": "# Notes\n\n- one"})
    md_id = out.split("(id ")[1].split(")")[0]
    out = await save_page.ainvoke({"title": "Report", "html": "<!doctype html><h1>Report</h1>"})
    page_id = out.split("(id ")[1].split(")")[0]
    assert "No html file" in await save_page.ainvoke({"title": "x", "html": "x", "id": md_id})  # kinds never mix
    await save_md.ainvoke({"title": "Notes v2", "content": "# Notes\n\n- two", "id": md_id})

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get(f"/api/artifacts/{md_id}")).status_code == 401
        await c.post("/api/login", json=AUTH)
        md = (await c.get(f"/api/artifacts/{md_id}")).json()
        assert md["title"] == "Notes v2" and md["content"].endswith("- two") and md["kind"] == "md"
        assert (await c.get(f"/api/artifacts/{md_id}/view")).status_code == 404  # only pages are served as HTML
        view = await c.get(f"/api/artifacts/{page_id}/view")
        assert "<h1>Report</h1>" in view.text and view.headers["content-security-policy"].startswith("sandbox ")
        assert "allow-same-origin" not in view.headers["content-security-policy"]
