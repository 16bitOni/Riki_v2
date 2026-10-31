"""Gmail and Google Calendar over Google's own REST APIs (not the Developer Preview MCP servers).

You sign in once with your own Google OAuth client; the scopes Google actually granted decide which tools exist, so
unticking a box on the consent screen removes those tools. Reading is safe, saving a draft asks first, and there is
no tool that sends mail or invites anyone.
"""

import asyncio
import base64
import html
import re
import time
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import httpx
from langchain_core.tools import tool

from .agent.policy import SAFE, WRITE
from .agent.tools import ToolEntry

if TYPE_CHECKING:
    from .apps import Apps

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
CALENDAR = "https://www.googleapis.com/calendar/v3/calendars/primary/events"

_G = "https://www.googleapis.com/auth/"
GMAIL_READ = {"https://mail.google.com/", _G + "gmail.modify", _G + "gmail.readonly"}
GMAIL_DRAFT = {"https://mail.google.com/", _G + "gmail.modify", _G + "gmail.compose", _G + "gmail.drafts"}
CALENDAR_READ = {_G + s for s in ("calendar", "calendar.readonly", "calendar.events", "calendar.events.readonly")}


class GoogleError(Exception):
    pass


async def _token_request(data: dict) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(TOKEN_URL, data=data)
    body = r.json()
    if r.status_code != 200:
        raise GoogleError(f"Google refused the sign-in: {body.get('error_description') or body.get('error') or r.text[:200]}")
    return body


def _tokens(body: dict, old: dict | None = None) -> dict:
    """What we keep of Google's answer. A refresh answer has no refresh_token of its own: the old one stays."""
    old = old or {}
    return {"access_token": body["access_token"], "refresh_token": body.get("refresh_token") or old.get("refresh_token"),
            "scope": body.get("scope") or old.get("scope", ""), "expires_at": time.time() + body.get("expires_in", 3600)}


async def exchange(client: dict, redirect_uri: str, code: str, verifier: str) -> dict:
    return _tokens(await _token_request({
        "grant_type": "authorization_code", "code": code, "code_verifier": verifier, "redirect_uri": redirect_uri,
        "client_id": client["client_id"], "client_secret": client["client_secret"]}))


async def _access_token(apps: "Apps", app_id: str) -> str:
    secret = await apps.secret(app_id)
    tokens = secret.get("tokens") or {}
    if tokens.get("expires_at", 0) > time.time() + 60:
        return tokens["access_token"]
    client = secret.get("client") or {}
    try:
        body = await _token_request({"grant_type": "refresh_token", "refresh_token": tokens.get("refresh_token", ""),
                                     "client_id": client.get("client_id", ""), "client_secret": client.get("client_secret", "")})
    except GoogleError as exc:
        raise GoogleError(f"{exc}. Reconnect it in the Apps tab (while your Google app is in Testing mode, "
                          "Google ends the sign-in after 7 days).") from exc
    tokens = _tokens(body, tokens)
    await apps.save_secret(app_id, tokens=tokens)
    return tokens["access_token"]


async def _call(apps: "Apps", app_id: str, method: str, url: str, **kw) -> dict:
    token = await _access_token(apps, app_id)
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kw)
    if r.status_code >= 400:
        try:
            message = r.json()["error"]["message"]
        except Exception:
            message = r.text[:300]
        raise GoogleError(f"Google answered {r.status_code}: {message}")  # says which API to enable, if that is it
    return r.json()


def _header(message: dict, name: str) -> str:
    return next((h["value"] for h in message.get("payload", {}).get("headers", []) if h["name"].lower() == name), "")


def _decode(part: dict, mime: str) -> str:
    data = (part.get("body") or {}).get("data")
    if part.get("mimeType") == mime and data:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
    return next((t for p in part.get("parts", []) if (t := _decode(p, mime))), "")


def _text(payload: dict) -> str:
    plain = _decode(payload, "text/plain")
    return plain or html.unescape(re.sub(r"<(script|style).*?</\1>|<[^>]+>", " ", _decode(payload, "text/html"), flags=re.S))


def _iso(value: str, tz: str) -> str:
    d = datetime.fromisoformat(value)
    return (d if d.tzinfo else d.replace(tzinfo=ZoneInfo(tz))).isoformat()


def tools(apps: "Apps", app_id: str, granted: list[str], tz: str) -> list[ToolEntry]:
    """The tools of one Google app that the granted scopes allow."""
    scopes, out = set(granted), []
    source = f"app:{app_id}"

    def call(method: str, url: str, **kw):
        return _call(apps, app_id, method, url, **kw)

    if app_id == "gmail" and scopes & GMAIL_READ:
        @tool
        async def gmail_search(query: str = "", max_results: int = 10) -> str:
            """Search mail with Gmail's own syntax (from:ana, subject:invoice, is:unread, newer_than:2d, label:x,
            after:2026/10/31). Empty query = newest. One line per message: id | date | sender | subject | snippet."""
            listing = await call("GET", f"{GMAIL}/messages", params={"q": query, "maxResults": min(max(max_results, 1), 25)})
            ids = [m["id"] for m in listing.get("messages", [])]
            if not ids:
                return "No messages match."
            found = await asyncio.gather(*(call("GET", f"{GMAIL}/messages/{i}", params={
                "format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]}) for i in ids))
            return "\n".join(f"{m['id']} | {_header(m, 'date')} | {_header(m, 'from')} | {_header(m, 'subject')} | "
                             f"{m.get('snippet', '')}" for m in found)

        @tool
        async def gmail_read(message_id: str) -> str:
            """The headers and text of one message, by the id gmail_search gave."""
            m = await call("GET", f"{GMAIL}/messages/{message_id}", params={"format": "full"})
            head = "\n".join(f"{n}: {_header(m, n.lower())}" for n in ("From", "To", "Cc", "Date", "Subject"))
            return f"{head}\n\n{_text(m.get('payload', {})).strip()[:20_000]}"

        @tool
        async def gmail_list_labels() -> str:
            """The names of the user's Gmail labels (for label: searches)."""
            return ", ".join(sorted(l["name"] for l in (await call("GET", f"{GMAIL}/labels")).get("labels", [])))

        out += [ToolEntry(t, SAFE, source) for t in (gmail_search, gmail_read, gmail_list_labels)]

    if app_id == "gmail" and scopes & GMAIL_DRAFT:
        @tool
        async def gmail_create_draft(to: str, subject: str, body: str) -> str:
            """Save a draft in the user's Gmail Drafts. It is never sent: the user reads and sends it themselves."""
            message = EmailMessage()
            message["To"], message["Subject"] = to, subject
            message.set_content(body)
            raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
            draft = await call("POST", f"{GMAIL}/drafts", json={"message": {"raw": raw}})
            return f"Draft saved in Gmail Drafts (id {draft['id']}). The user sends it."

        out.append(ToolEntry(gmail_create_draft, WRITE, source))

    if app_id == "gcal" and scopes & CALENDAR_READ:
        @tool
        async def gcal_list_events(time_min: str = "", time_max: str = "", query: str = "", max_results: int = 25) -> str:
            """Events on the user's main calendar, soonest first, with start, end, title and place. time_min/time_max
            are ISO dates or datetimes in the user's timezone (2026-11-01 or 2026-11-01T09:00); default: now to 7 days."""
            now = datetime.now(ZoneInfo(tz))
            params = {"timeMin": _iso(time_min, tz) if time_min else now.isoformat(),
                      "timeMax": _iso(time_max, tz) if time_max else (now + timedelta(days=7)).isoformat(),
                      "singleEvents": "true", "orderBy": "startTime", "maxResults": min(max(max_results, 1), 100)}
            if query:
                params["q"] = query
            events = (await call("GET", CALENDAR, params=params)).get("items", [])
            when = lambda e: e.get("dateTime") or e.get("date", "")  # noqa: E731
            return "\n".join(f"{when(e['start'])} to {when(e['end'])} | {e.get('summary', '(no title)')}"
                             + (f" | {e['location']}" if e.get("location") else "") for e in events) or "No events."

        out.append(ToolEntry(gcal_list_events, SAFE, source))
    return out
