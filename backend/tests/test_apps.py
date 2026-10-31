"""Apps: risk rules, the tool catalog, and a real connect/disconnect against a local MCP server."""

import asyncio
import json
import socket
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import uvicorn
from langchain_core.messages import AIMessage
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app import apps as apps_mod
from app import google
from app.agent.policy import EXTERNAL, SAFE, WRITE
from app.apps import hub_args_reach_people, reaches_people, secret_box, tool_risk
from app.main import app


def test_people_reaching_tools_are_refused_for_every_app():
    for name in ["send_message", "reply-to-thread", "createComment", "share_file", "post_tweet", "invite_user"]:
        assert reaches_people(name), name
    for name in ["get_shared_drives", "list_comments", "search_messages", "create_draft", "place_order"]:
        assert not reaches_people(name), name
    assert hub_args_reach_people({"tools": [{"tool_slug": "GMAIL_SEND_EMAIL", "arguments": {}}]})
    assert hub_args_reach_people({"action": "SLACK_SENDS_A_MESSAGE_TO_A_SLACK_CHANNEL"})
    assert not hub_args_reach_people({"tools": [{"tool_slug": "SPOTIFY_ADD_ITEMS_TO_PLAYLIST"}]})


def test_tool_risk_trusts_hints_only_where_it_should():
    kite = apps_mod.CATALOG["kite"]
    assert tool_risk(kite, "get_holdings", False) == SAFE
    assert tool_risk(kite, "place_order", False) == WRITE
    assert tool_risk(apps_mod.CATALOG["gdrive"], "update_permission", True) == EXTERNAL
    mine = {"id": "mine", "safe_tools": []}
    assert tool_risk(mine, "list_notes", True) == SAFE
    assert tool_risk(mine, "wipe_everything", True) == WRITE  # a custom server can't call a wipe "read-only"
    assert tool_risk(apps_mod.CATALOG["notion"], "update_page", True) == SAFE  # listed apps' hints are trusted


def test_secrets_need_the_password():
    token = secret_box("correct horse battery").encrypt(b'{"token": "x"}')
    assert secret_box("correct horse battery").decrypt(token) == b'{"token": "x"}'
    with pytest.raises(Exception):
        secret_box("another password!").decrypt(token)


def test_catalog_is_well_formed():
    for a in apps_mod.CATALOG.values():
        assert a["category"] in apps_mod.CATEGORIES and a["auth"] in ("oauth", "google", "token", "none"), a["id"]
        assert a["url"].startswith("https://") and a.get("mode", "ask") in ("read", "ask"), a["id"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def notes_server():
    port = _free_port()
    srv = FastMCP("notes", host="127.0.0.1", port=port)

    @srv.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def list_notes() -> str:
        """List my notes."""
        return "milk; call mom"

    @srv.tool()
    def add_note(text: str) -> str:
        """Add a note."""
        return "added"

    @srv.tool()
    def send_note(to: str) -> str:
        """Send a note to someone."""
        return "sent"

    server = uvicorn.Server(uvicorn.Config(srv.streamable_http_app(), host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    await task


async def test_connect_modes_and_disconnect(make_runtime, notes_server):
    rt = await make_runtime(lambda m: AIMessage("hi"))
    app.state.rt = rt
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/apps")).status_code == 401
        await client.post("/api/login", json={"password": "correct horse battery"})
        listed = (await client.get("/api/apps")).json()
        assert "Money" in listed["categories"] and any(a["id"] == "kite" for a in listed["apps"])
        assert (await client.post("/api/apps/custom", json={"name": "x", "url": "http://10.0.0.1/mcp"})).status_code == 422

        r = (await client.post("/api/apps/custom", json={"name": "My Notes", "url": notes_server})).json()
        assert r["id"] == "my_notes" and r["state"] == "connected", r
        risks = {t["name"]: t["risk"] for t in r["tools"]}
        assert risks == {"list_notes": SAFE, "add_note": WRITE, "send_note": EXTERNAL}
        assert rt.registry.get("my_notes_list_notes").risk == SAFE
        assert "my_notes_* (load as my_notes_<name>" in rt.main_system and "add_note*" in rt.main_system
        assert "send_note" not in rt.main_system  # never offered
        assert rt.registry.expand(["my_notes_*"]) == ["my_notes_add_note", "my_notes_list_notes", "my_notes_send_note"]
        assert "milk" in json.dumps(await rt.registry.get("my_notes_list_notes").tool.ainvoke({}))

        # read-only: changing tools aren't even registered
        r = (await client.patch("/api/apps/my_notes", json={"mode": "read"})).json()
        assert {t["name"]: t["risk"] for t in r["tools"]} == {"list_notes": SAFE, "add_note": "off", "send_note": EXTERNAL}
        assert rt.registry.get("my_notes_add_note") is None
        mine = next(a for a in (await client.get("/api/apps")).json()["apps"] if a["id"] == "my_notes")
        assert mine["category"] == "Added by you" and mine["mode"] == "read" and mine["connected"]

        # tokens are never exported; disconnecting removes the tools and the row
        export = (await client.get("/api/export")).json()
        assert export["apps"] == [{"id": "my_notes", "name": "My Notes", "mode": "read", "connected": True}]
        assert (await client.delete("/api/apps/my_notes")).json() == {"ok": True}
        assert rt.registry.get("my_notes_list_notes") is None and "my_notes" not in rt.main_system

        # a token app can't connect without its token; a sign-in nobody started is refused
        assert (await client.post("/api/apps/github/connect", json={})).status_code == 400
        await client.delete("/api/apps/github")

        # a Google app only takes a real OAuth client ID (Google answers anything else "invalid_client")
        bad = await client.post("/api/apps/gmail/connect", json={"client_id": "ya29.not-a-client-id", "client_secret": "x"})
        assert bad.status_code == 400 and "client ID" in bad.json()["detail"]
        await client.delete("/api/apps/gmail")
        assert (await client.get("/api/apps/callback?state=nope&code=x")).status_code == 400


async def test_directory_search_keeps_only_reachable_remotes(monkeypatch):
    body = {"servers": [
        {"server": {"name": "io.example/notes", "title": "Notes", "description": "Notes app",
                    "remotes": [{"type": "streamable-http", "url": "https://notes.example/mcp"}]}},
        {"server": {"name": "io.example/local", "packages": [{"registryType": "npm", "identifier": "x"}]}},
        {"server": {"name": "io.example/tenant", "remotes": [{"type": "sse", "url": "https://{tenant}.x.io/sse"}]}},
        {"server": {"name": "io.example/keyed", "remotes": [{"type": "sse", "url": "https://k.example/sse",
                                                              "headers": [{"name": "X-Api-Key", "isRequired": True}]}]}},
    ]}
    real = httpx.AsyncClient
    monkeypatch.setattr(apps_mod.httpx, "AsyncClient", lambda **kw: real(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body)), **kw))
    found = await apps_mod.search_directory("notes")
    assert [(f["name"], f["header"]) for f in found] == [("Notes", None), ("keyed", "X-Api-Key")]


class _AuthServer:
    """Just enough of an OAuth authorization server for the MCP SDK: DCR, codes, tokens."""

    def __init__(self):
        self.clients, self.codes, self.tokens = {}, {}, {}

    async def get_client(self, client_id):
        return self.clients.get(client_id)

    async def register_client(self, info):
        self.clients[info.client_id] = info

    async def authorize(self, client, params):
        from mcp.server.auth.provider import AuthorizationCode, construct_redirect_uri

        code = f"code-{len(self.codes)}"
        self.codes[code] = AuthorizationCode(
            code=code, scopes=[], expires_at=9e9, client_id=client.client_id, code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri, redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource)
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(self, client, code):
        return self.codes.get(code)

    async def exchange_authorization_code(self, client, code):
        from mcp.server.auth.provider import AccessToken
        from mcp.shared.auth import OAuthToken

        self.codes.pop(code.code)
        token = f"tok-{len(self.tokens)}"
        self.tokens[token] = AccessToken(token=token, client_id=client.client_id, scopes=[], resource=code.resource)
        return OAuthToken(access_token=token, token_type="Bearer", expires_in=3600)

    async def load_access_token(self, token):
        return self.tokens.get(token)

    async def load_refresh_token(self, client, refresh_token):
        return None

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        raise NotImplementedError

    async def revoke_token(self, token):
        self.tokens.pop(getattr(token, "token", token), None)


async def test_sign_in_flow_stores_tokens_and_reconnects_without_you(make_runtime):
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    srv = FastMCP("diary", host="127.0.0.1", port=port, auth_server_provider=_AuthServer(),
                  auth=AuthSettings(issuer_url=base, resource_server_url=f"{base}/mcp",
                                    client_registration_options=ClientRegistrationOptions(enabled=True),
                                    validate_token_resource=False))

    @srv.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def read_diary() -> str:
        """Today's diary."""
        return "gym at 7"

    server = uvicorn.Server(uvicorn.Config(srv.streamable_http_app(), host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    rt = await make_runtime(lambda m: AIMessage("hi"))
    app.state.rt = rt
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            await client.post("/api/login", json={"password": "correct horse battery"})
            r = (await client.post("/api/apps/custom", json={"name": "Diary", "url": f"{base}/mcp"},
                                   headers={"Origin": "http://localhost:5173"})).json()
            assert r["state"] == "signin" and r["auth_url"].startswith(f"{base}/authorize"), r

            # the browser: the app's sign-in page redirects back to Riki's callback
            async with httpx.AsyncClient() as browser:
                back = (await browser.get(r["auth_url"])).headers["location"]
            assert back.startswith("http://localhost:5173/api/apps/callback?")
            assert (await client.get(back.removeprefix("http://localhost:5173"))).status_code == 200
            for _ in range(100):
                if rt.apps.view_state("diary")["state"] == "connected":
                    break
                await asyncio.sleep(0.05)
            assert rt.apps.view_state("diary")["tools"] == [{"name": "read_diary", "risk": SAFE}]
            row = await rt.apps.row("diary")
            assert b"tok-" not in row.secret  # stored encrypted

            # restart-style reconnect: stored tokens, no sign-in
            assert (await rt.apps.connect("diary", interactive=False))["state"] == "connected"
            assert "gym" in json.dumps(await rt.registry.get("diary_read_diary").tool.ainvoke({}))
            await client.delete("/api/apps/diary")
    finally:
        server.should_exit = True
        await task


async def test_a_server_that_lists_tools_without_a_token_still_gets_you_signed_in(make_runtime):
    # Bug seen in real use (Gmail): initialize and tools/list work without a token, only a call answers 401. The app
    # showed "connected", never offered a sign-in, and every call then waited out the tool timeout.
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    srv = FastMCP("mail", host="127.0.0.1", port=port, stateless_http=True, auth_server_provider=_AuthServer(),
                  auth=AuthSettings(issuer_url=base, resource_server_url=f"{base}/mcp",
                                    client_registration_options=ClientRegistrationOptions(enabled=True),
                                    validate_token_resource=False))

    @srv.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def read_mail() -> str:
        """Today's mail."""
        return "one new message"

    inner = srv.streamable_http_app()
    listing = {"name": "read_mail", "description": "Today's mail.", "inputSchema": {"type": "object", "properties": {}},
               "annotations": {"readOnlyHint": True}}

    async def gate(scope, receive, send):  # answer the handshake and the tool list without a token, like Google
        if scope["type"] != "http" or scope["path"] != "/mcp" or b"authorization" in dict(scope["headers"]):
            return await inner(scope, receive, send)
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        rpc = json.loads(body or b"{}")
        result = {"initialize": {"protocolVersion": rpc.get("params", {}).get("protocolVersion", "2025-03-26"),
                                 "capabilities": {"tools": {}}, "serverInfo": {"name": "mail", "version": "1"}},
                  "tools/list": {"tools": [listing]}}.get(rpc.get("method"))
        if result is None and not rpc.get("method", "").startswith("notifications/"):
            replay = iter([{"type": "http.request", "body": body}])  # a tool call: the real server answers 401
            return await inner(scope, lambda: _next(replay), send)
        status, payload = (200, json.dumps({"jsonrpc": "2.0", "id": rpc["id"], "result": result}).encode()) if result \
            else (202, b"")
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": payload})

    async def _next(it):
        return next(it)

    server = uvicorn.Server(uvicorn.Config(gate, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    rt = await make_runtime(lambda m: AIMessage("hi"))
    app.state.rt = rt
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            await client.post("/api/login", json={"password": "correct horse battery"})
            r = (await client.post("/api/apps/custom", json={"name": "Mail", "url": f"{base}/mcp"},
                                   headers={"Origin": "http://localhost:5173"})).json()
            assert r["state"] == "signin" and r["auth_url"].startswith(f"{base}/authorize"), r
            assert rt.registry.get("mail_read_mail") is None  # nothing to call until you are signed in

            async with httpx.AsyncClient() as browser:
                back = (await browser.get(r["auth_url"])).headers["location"]
            assert (await client.get(back.removeprefix("http://localhost:5173"))).status_code == 200
            for _ in range(100):
                if rt.apps.view_state("mail")["state"] == "connected":
                    break
                await asyncio.sleep(0.05)
            assert rt.apps.view_state("mail")["tools"] == [{"name": "read_mail", "risk": SAFE}]
            await client.delete("/api/apps/mail")
    finally:
        server.should_exit = True
        await task


async def test_gmail_signs_in_with_googles_own_api_and_gets_only_the_tools_the_token_allows(make_runtime, monkeypatch):
    # Bug seen in real use: Google's Gmail MCP server is a Developer Preview and answered 403 to a normal project.
    rt = await make_runtime(lambda m: AIMessage("hi"))
    app.state.rt = rt
    tokens_asked, gmail_status = [], [200]

    def google_says(req: httpx.Request) -> httpx.Response:
        if req.url.host == "oauth2.googleapis.com":
            grant = parse_qs(req.content.decode())["grant_type"][0]
            tokens_asked.append(grant)
            body = {"access_token": f"at{len(tokens_asked)}", "expires_in": 3600,
                    "scope": "https://www.googleapis.com/auth/gmail.readonly"}  # compose left unticked
            return httpx.Response(200, json=body | ({"refresh_token": "rt"} if grant == "authorization_code" else {}))
        assert req.headers["authorization"] == f"Bearer at{len(tokens_asked)}"
        if gmail_status[0] != 200:
            return httpx.Response(gmail_status[0], json={"error": {"message": "Gmail API has not been used in project 1"}})
        if req.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "m1"}]})
        return httpx.Response(200, json={"id": "m1", "snippet": "your invoice", "payload": {"headers": [
            {"name": "From", "value": "Ana <ana@example.com>"}, {"name": "Subject", "value": "Invoice"},
            {"name": "Date", "value": "Sat, 31 Oct 2026"}]}})

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        real = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(**({"transport": httpx.MockTransport(google_says)} | kw)))
        try:
            await client.post("/api/login", json={"password": "correct horse battery"})
            r = (await client.post("/api/apps/gmail/connect", headers={"Origin": "http://localhost:5173"},
                                   json={"client_id": "1-a.apps.googleusercontent.com", "client_secret": "s"})).json()
            assert r["state"] == "signin" and r["auth_url"].startswith(google.AUTH_URL), r
            q = parse_qs(urlparse(r["auth_url"]).query)
            assert q["access_type"] == ["offline"] and q["scope"] == [apps_mod.CATALOG["gmail"]["scope"]]

            assert (await client.get(f"/api/apps/callback?state={q['state'][0]}&code=abc")).status_code == 200
            for _ in range(100):
                if rt.apps.view_state("gmail")["state"] == "connected":
                    break
                await asyncio.sleep(0.05)
            names = {t["name"] for t in rt.apps.view_state("gmail")["tools"]}
            assert names == {"search", "read", "list_labels"}  # no draft tool: compose was not granted
            assert "Invoice" in await rt.registry.get("gmail_search").tool.ainvoke({"query": "is:unread"})

            await rt.apps.save_secret("gmail", tokens={**(await rt.apps.secret("gmail"))["tokens"], "expires_at": 0})
            await rt.registry.get("gmail_search").tool.ainvoke({})  # an expired token is refreshed, not a new sign-in
            assert tokens_asked == ["authorization_code", "refresh_token"]
            assert (await rt.apps.secret("gmail"))["tokens"]["refresh_token"] == "rt"

            gmail_status[0] = 403  # Google's own words reach the model: which API is off
            with pytest.raises(google.GoogleError, match="has not been used in project 1"):
                await rt.registry.get("gmail_search").tool.ainvoke({})
        finally:
            await client.delete("/api/apps/gmail")
