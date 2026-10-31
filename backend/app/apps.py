"""Apps: your accounts (Notion, Gmail, Zerodha, Swiggy, ...) connected from the Apps tab as remote MCP servers.

- Sign-in is the MCP OAuth flow from the MCP SDK (discovery, dynamic client registration, PKCE). The UI opens
  the app's sign-in page; the app redirects back to /api/apps/callback, which hands the code to the waiting flow.
- Tokens are stored encrypted with a key derived from RIKI_PASSWORD, so a copy of the database alone holds no
  usable tokens (changing the password means signing in to your apps again).
- Each connected app runs in its own task that owns its MCP session until you disconnect it (an anyio session
  must be closed by the task that opened it). Its tools join the registry as ``<app>_<tool>`` and leave with it.
- Risk per tool: anything that reaches other people is refused for every app; "read" mode registers only tools
  that look things up; otherwise the app's safe patterns (and, for listed apps, MCP read-only hints) decide.
"""

import asyncio
import base64
import fnmatch
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import httpx
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from sqlalchemy import select, text

from .agent.policy import EXTERNAL, SAFE, WRITE
from .agent.tools import ToolEntry
from .db import Connection, utcnow
from .search import TavilyError, check_key

if TYPE_CHECKING:
    from .runtime import Runtime

log = logging.getLogger(__name__)

_DATA = json.loads(Path(__file__).with_name("apps.json").read_text())
CATEGORIES: list[str] = _DATA["categories"]
CATALOG: dict[str, dict] = {a["id"]: a for a in _DATA["apps"]}
ROLES: dict[str, dict] = {r["id"]: r for r in _DATA["roles"]}
assert all(a in CATALOG for r in ROLES.values() for a in r["apps"]), "apps.json: a role lists an unknown app"
REGISTRY_URL = "https://registry.modelcontextprotocol.io/v0/servers"

# Words that mean "this reaches another person". Riki never does that, whichever app it is.
_PEOPLE_VERBS = {"send", "reply", "forward", "invite", "share", "publish", "post", "tweet", "notify", "dm", "email",
                 "message", "comment"}
_READ_VERBS = {"get", "list", "search", "read", "fetch", "find", "lookup", "describe", "query", "show"}
_HUB_WORDS = {"send", "sends", "reply", "replies", "forward", "invite", "share", "publish", "post", "posts", "tweet",
              "notify", "dm"}


def _words(name: str) -> list[str]:
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return [w for w in re.split(r"[^a-z0-9]+", name.lower()) if w]


def reaches_people(name: str) -> bool:
    """send_message, reply-to-thread, createComment... (the verb leads in a tool name)."""
    w = _words(name)
    return bool(w) and (w[0] in _PEOPLE_VERBS or ("comment" in w and w[0] in {"add", "create", "post"}))


def hub_args_reach_people(args: dict) -> bool:
    """Hubs (Zapier, Rube) run other apps' actions by slug, e.g. GMAIL_SEND_EMAIL; check every slug-like value."""
    blob = json.dumps(args, ensure_ascii=False)
    return any(_HUB_WORDS & set(_words(s)) for s in re.findall(r"[A-Za-z0-9]+(?:[_-][A-Za-z0-9]+)+", blob))


def tool_risk(app: dict, short: str, read_only_hint: bool) -> str:
    if reaches_people(short) or any(fnmatch.fnmatchcase(short, p) for p in app.get("external_tools", [])):
        return EXTERNAL
    # A listed app's read-only hints are trusted; a server you added yourself must also name a read verb.
    trusted_hint = read_only_hint and (app["id"] in CATALOG or (_words(short) or [""])[0] in _READ_VERBS)
    if trusted_hint or any(fnmatch.fnmatchcase(short, p) for p in app.get("safe_tools", [])):
        return SAFE
    return WRITE


def secret_box(password: str) -> MultiFernet:
    """Encrypts with a key stretched from the password (scrypt); still opens secrets saved with the older plain-hash
    key. # shortcut: fixed salt, because stealing the database alone already gets nothing; upgrade to a random
    per-install salt if the database is ever stored apart from the password."""
    pw = password.encode()
    keys = (hashlib.scrypt(pw, salt=b"riki-apps-v2", n=2**15, r=8, p=1, dklen=32, maxmem=64 * 2**20),
            hashlib.sha256(b"riki-apps-v1\0" + pw).digest())
    return MultiFernet([Fernet(base64.urlsafe_b64encode(k)) for k in keys])


class NeedsSignIn(Exception):
    pass


# mcp logs every aborted OAuth flow with a traceback; ours abort on purpose and show "sign in again" in the Apps tab
logging.getLogger("mcp.client.auth.oauth2").addFilter(
    lambda r: not (r.exc_info and isinstance(r.exc_info[1], NeedsSignIn)))


class _Store:
    """The MCP SDK's TokenStorage, backed by the app's encrypted secret."""

    def __init__(self, apps: "Apps", app_id: str) -> None:
        self.apps, self.app_id = apps, app_id

    async def get_tokens(self) -> OAuthToken | None:
        t = (await self.apps.secret(self.app_id)).get("tokens")
        return OAuthToken.model_validate(t) if t else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        await self.apps.save_secret(self.app_id, tokens=tokens.model_dump(mode="json", exclude_none=True))

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        c = (await self.apps.secret(self.app_id)).get("client")
        return OAuthClientInformationFull.model_validate(c) if c else None

    async def set_client_info(self, info: OAuthClientInformationFull) -> None:
        await self.apps.save_secret(self.app_id, client=info.model_dump(mode="json", exclude_none=True))


def _root_cause(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:  # anyio wraps the real cause
        exc = next((e for e in exc.exceptions if isinstance(e, NeedsSignIn)), exc.exceptions[0])
    return exc


class Apps:
    def __init__(self, rt: "Runtime") -> None:
        self.rt = rt
        self.box = secret_box(rt.settings.password.get_secret_value())
        self.live: dict[str, tuple[asyncio.Task, asyncio.Event]] = {}
        self.state: dict[str, dict] = {}  # app id -> {"state", "error", "auth_url", "tools": [(name, risk)]}
        self.flows: dict[str, asyncio.Future] = {}  # OAuth state -> (code, state) from the callback

    # -- the app definition: listed apps come from apps.json, apps you added from their row --------------

    def definition(self, row: Connection) -> dict:
        return CATALOG.get(row.id) or {"id": row.id, "name": row.name, "url": row.url, "auth": row.auth,
                                       "category": "Added by you", "blurb": row.url, "mode": "ask"}

    async def rows(self) -> list[Connection]:
        async with self.rt.db.session() as s:
            return list((await s.scalars(select(Connection).order_by(Connection.created_at))).all())

    async def row(self, app_id: str) -> Connection | None:
        async with self.rt.db.session() as s:
            return await s.get(Connection, app_id)

    # -- encrypted secrets ---------------------------------------------------------------------------------

    async def secret(self, app_id: str) -> dict:
        row = await self.row(app_id)
        if row is None or not row.secret:
            return {}
        try:
            return json.loads(self.box.decrypt(row.secret))
        except InvalidToken:  # the password changed: sign in again
            return {}

    async def save_secret(self, app_id: str, **changes) -> None:
        data = {k: v for k, v in {**await self.secret(app_id), **changes}.items() if v is not None}
        async with self.rt.db.session() as s:
            row = await s.get(Connection, app_id)
            row.secret, row.updated_at = self.box.encrypt(json.dumps(data).encode()), utcnow()
            await s.commit()

    def _status(self, app_id: str, **fields) -> None:
        self.state.setdefault(app_id, {}).update(fields)

    # -- lifecycle -----------------------------------------------------------------------------------------

    async def start(self) -> None:
        """Reconnect apps you connected before, in the background (a slow app must not delay startup)."""
        await self._rekey()
        for row in await self.rows():
            if row.enabled:
                self._launch(row, interactive=False)

    async def _rekey(self) -> None:
        """Move secrets saved under the older key to the current one. Ones the password no longer opens stay as is."""
        async with self.rt.db.session() as s:
            for row in (await s.scalars(select(Connection).where(Connection.secret.is_not(None)))).all():
                try:
                    row.secret = self.box.rotate(row.secret)
                except InvalidToken:
                    continue
            await s.commit()

    async def stop(self) -> None:
        for app_id in list(self.live):
            await self.disconnect(app_id)

    async def connect(self, app_id: str, interactive: bool = True, wait_s: float = 25) -> dict:
        """(Re)connect. Returns as soon as it is connected, failed, or needs you to sign in (with the URL)."""
        await self.disconnect(app_id)
        row = await self.row(app_id)
        auth_url: asyncio.Future = asyncio.get_running_loop().create_future()
        ready: asyncio.Future = asyncio.get_running_loop().create_future()
        self._launch(row, interactive, ready, auth_url)
        await asyncio.wait([ready, auth_url], timeout=wait_s, return_when=asyncio.FIRST_COMPLETED)
        return self.view_state(app_id)

    async def disconnect(self, app_id: str) -> None:
        task, stop = self.live.pop(app_id, (None, None))
        if task is None:
            return
        stop.set()  # a connected app closes its session cleanly
        if self.state.get(app_id, {}).get("state") != "connected" or not (await asyncio.wait([task], timeout=5))[0]:
            task.cancel()  # still connecting or waiting for a sign-in
        await asyncio.gather(task, return_exceptions=True)
        self._status(app_id, state="off", error=None, auth_url=None, tools=[])

    def view_state(self, app_id: str) -> dict:
        st = self.state.get(app_id, {})
        return {"state": st.get("state", "off"), "error": st.get("error"), "auth_url": st.get("auth_url"),
                "tools": [{"name": n, "risk": r} for n, r in st.get("tools", [])]}

    def _launch(self, row: Connection, interactive: bool, ready=None, auth_url=None) -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(self._serve(row, interactive, stop, ready, auth_url))
        self.live[row.id] = (task, stop)

    async def _serve(self, row: Connection, interactive: bool, stop: asyncio.Event, ready, auth_url) -> None:
        app, names = self.definition(row), []
        self._status(row.id, state="connecting", error=None, auth_url=None, tools=[])
        try:
            if app.get("native"):  # a built-in tool, not an MCP server: check the key, then wait to be disconnected
                key = (await self.secret(row.id)).get("token")
                if not key:
                    raise NeedsSignIn()
                await check_key(key)
                self._status(row.id, state="connected", error=None, auth_url=None, tools=[(app["native"], SAFE)])
                if ready is not None and not ready.done():
                    ready.set_result(True)
                await stop.wait()
                return
            connection = await self._connection(app, row, interactive, auth_url)
            async with MultiServerMCPClient({row.id: connection}).session(row.id) as session:
                tools = await load_mcp_tools(session, server_name=row.id, tool_name_prefix=True)
                kept = []
                for t in tools:
                    short = t.name.removeprefix(f"{row.id}_")
                    risk = tool_risk(app, short, bool((t.metadata or {}).get("readOnlyHint")))
                    if row.mode == "read" and risk == WRITE:
                        kept.append((short, "off"))  # read-only: changing tools are never even registered
                        continue
                    self.rt.registry.add(ToolEntry(t, risk, f"app:{row.id}",
                                                   hub_args_reach_people if app.get("inspect_args") else None))
                    names.append(t.name)
                    kept.append((short, risk))
                await self.rt.reload_persona()
                self._status(row.id, state="connected", error=None, auth_url=None, tools=kept)
                log.info("app %r connected: %d of %d tools", row.id, len(names), len(tools))
                if ready is not None and not ready.done():
                    ready.set_result(True)
                await stop.wait()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # one broken app must not take Riki down
            cause = _root_cause(exc)
            if isinstance(cause, NeedsSignIn):
                self._status(row.id, state="signin", error="Sign in again to reconnect.", auth_url=None)
            else:
                log.warning("app %r failed: %s: %s", row.id, type(cause).__name__, cause)
                self._status(row.id, state="error", error=(str(cause) if isinstance(cause, TavilyError)
                                                           else f"{type(cause).__name__}: {cause}")[:300])
            if ready is not None and not ready.done():
                ready.set_result(False)
        finally:
            for n in names:
                self.rt.registry.remove(n)
            if names:
                await self.rt.reload_persona()
            if self.live.get(row.id, (None,))[0] is asyncio.current_task():
                del self.live[row.id]

    async def _connection(self, app: dict, row: Connection, interactive: bool, auth_url) -> dict:
        url = app["url"]
        conn: dict = {"transport": "sse" if url.rstrip("/").endswith("/sse") else "streamable_http", "url": url}
        secret = await self.secret(row.id)
        if app["auth"] == "token":
            if not secret.get("token"):
                raise NeedsSignIn()
            header = app.get("header") or secret.get("header") or "Authorization"
            conn["headers"] = {header: ("Bearer " if header == "Authorization" else "") + secret["token"]}
        elif app["auth"] in ("oauth", "google"):
            conn["auth"] = self._oauth(app, row.id, secret, interactive, auth_url)
        return conn

    def _oauth(self, app: dict, app_id: str, secret: dict, interactive: bool, auth_url) -> OAuthClientProvider:
        flow: dict = {}

        async def redirect(url: str) -> None:
            if not interactive:  # reconnecting at startup or a token that can't refresh: you have to sign in
                raise NeedsSignIn()
            flow["state"] = parse_qs(urlparse(url).query).get("state", [""])[0]
            self.flows[flow["state"]] = asyncio.get_running_loop().create_future()
            self._status(app_id, state="signin", error=None, auth_url=url)
            if auth_url is not None and not auth_url.done():
                auth_url.set_result(url)

        async def callback() -> tuple[str, str | None]:
            try:
                return await asyncio.wait_for(self.flows[flow["state"]], 600)
            finally:
                self.flows.pop(flow["state"], None)

        redirect_uri = secret.get("redirect_uri", "http://localhost:8000/api/apps/callback")
        metadata = OAuthClientMetadata(
            client_name="Riki (personal agent)", redirect_uris=[redirect_uri], scope=app.get("scope"),
            grant_types=["authorization_code", "refresh_token"], response_types=["code"],
            token_endpoint_auth_method="client_secret_post" if app["auth"] == "google" else "none")
        return OAuthClientProvider(app["url"], metadata, _Store(self, app_id), redirect, callback)

    def finish_sign_in(self, state: str, code: str | None, error: str | None) -> bool:
        """Called by /api/apps/callback. False if no sign-in is waiting for this state."""
        future = self.flows.get(state)
        if future is None or future.done():
            return False
        if error or not code:
            future.set_exception(NeedsSignIn(error or "no code"))
        else:
            future.set_result((code, state))
        return True

    async def token(self, app_id: str) -> str | None:
        """A connected app's saved key, for the built-in tools it switches on. None while it isn't connected."""
        if self.state.get(app_id, {}).get("state") != "connected":
            return None
        return (await self.secret(app_id)).get("token")

    async def uses_this_week(self) -> dict[str, int]:
        async with self.rt.db.session() as s:
            rows = (await s.execute(text(
                "select name, count(*) from spans where kind = 'tool' and started_at > now() - interval '7 days' "
                "group by name"))).all()
        counts: dict[str, int] = {}
        live = {e.tool.name: e.source.removeprefix("app:") for e in self.rt.registry.entries.values()
                if e.source.startswith("app:")}
        live |= {a["native"]: a["id"] for a in CATALOG.values()
                 if a.get("native") and self.state.get(a["id"], {}).get("state") == "connected"}
        for name, n in rows:
            app_id = live.get(name) or next((a for a in self.state if name.startswith(f"{a}_")), None)
            if app_id:
                counts[app_id] = counts.get(app_id, 0) + n
        return counts


async def search_directory(q: str) -> list[dict]:
    """Remote servers from the official MCP Registry that a browser sign-in or a pasted key can reach."""
    # shortcut: parses the registry's server.json shape (servers[].server.remotes); not exercised live from CI
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(REGISTRY_URL, params={"search": q, "limit": 40, "version": "latest"})
        r.raise_for_status()
    out, seen = [], set()
    for item in r.json().get("servers", []):
        s = item.get("server", item)
        remote = next((x for x in s.get("remotes") or [] if x.get("type") in ("streamable-http", "sse")
                       and str(x.get("url", "")).startswith("https://") and "{" not in x["url"]), None)
        if remote is None or remote["url"] in seen:
            continue
        seen.add(remote["url"])
        headers = [h.get("name") for h in remote.get("headers") or [] if h.get("isRequired")]
        out.append({"name": s.get("title") or s.get("name", "").split("/")[-1], "registry_name": s.get("name"),
                    "description": (s.get("description") or "")[:200], "url": remote["url"],
                    "header": headers[0] if headers else None,
                    "website": s.get("websiteUrl") or (s.get("repository") or {}).get("url")})
    return out
