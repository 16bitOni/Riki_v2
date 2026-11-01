"""Built-in tools and the registry every agent draws its tools from."""

import asyncio
import fnmatch
import html
import ipaddress
import re
import textwrap
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Literal
from zoneinfo import ZoneInfo

import httpx
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field
from riki_memory import MemoryCreate

from .. import search
from ..db import Artifact, utcnow
from ..tracing import CURRENT_SPAN, RUN
from . import policy
from .policy import EXTERNAL, RISKS, SAFE, WRITE

if TYPE_CHECKING:
    from ..runtime import Runtime


_COMBINATORS = ("oneOf", "anyOf", "allOf")


def flatten_schema(schema: dict) -> dict:
    """Anthropic (also via Bedrock) rejects oneOf/anyOf/allOf at the top of a tool's input schema: merge the
    branches into one object. shortcut: branch-only `required` is dropped and $ref branches are not resolved,
    the server still validates; upgrade if a server needs them."""
    branches = [b for k in _COMBINATORS for b in schema.get(k, [])]
    if not branches:
        return schema
    props = dict(schema.get("properties", {}))
    for b in branches:
        props.update(b.get("properties", {}))
    return {**{k: v for k, v in schema.items() if k not in _COMBINATORS}, "type": "object", "properties": props}


@dataclass
class ToolEntry:
    tool: BaseTool
    risk: str
    source: str = "builtin"  # builtin | mcp:<server> | app:<app id>
    reaches_people: Callable[[dict], bool] | None = None  # for hubs: do these arguments act toward someone?
    ask_if: Callable[[dict], bool] | None = None  # safe tools that need approval for some arguments
    money: bool = False  # spends or moves money: always asks, never granted (policy.moves_money)

    def __post_init__(self) -> None:
        assert self.risk in RISKS, self.risk
        if isinstance(self.tool.args_schema, dict):  # MCP tools carry the server's raw JSON schema
            self.tool.args_schema = flatten_schema(self.tool.args_schema)

    def risk_for(self, args: dict) -> str:
        if self.reaches_people and self.reaches_people(args):
            return EXTERNAL
        if self.risk == SAFE and (self.money or (self.ask_if and self.ask_if(args))):
            return WRITE
        return self.risk


class ToolRegistry:
    def __init__(self) -> None:
        self.entries: dict[str, ToolEntry] = {}

    def add(self, entry: ToolEntry) -> None:
        if entry.tool.name in self.entries:
            raise ValueError(f"duplicate tool name: {entry.tool.name}")
        self.entries[entry.tool.name] = entry

    def remove(self, name: str) -> None:
        self.entries.pop(name, None)

    def get(self, name: str) -> ToolEntry | None:
        return self.entries.get(name)

    def expand(self, names: list[str]) -> list[str]:
        """'notion_*' means every tool of that app; plain names pass through."""
        return list(dict.fromkeys(n2 for n in names for n2 in (
            sorted(k for k in self.entries if fnmatch.fnmatchcase(k, n)) if "*" in n else [n])))

    def catalog(self, exclude: set[str]) -> str:
        """One line per built-in tool, and one line per app or MCP server (they can have dozens of tools):
        cheap enough to keep in every system prompt, which every model call re-sends."""
        lines, apps = [], {}
        for name, e in sorted(self.entries.items()):
            if name in exclude or e.risk == EXTERNAL:
                continue
            if e.source.startswith(("app:", "mcp:")):
                app = e.source.split(":", 1)[1]
                apps.setdefault(app, []).append(name.removeprefix(f"{app}_") + ("*" if e.risk == WRITE else ""))
                continue
            first = (e.tool.description or "").strip().splitlines()[0][:120] if e.tool.description else ""
            lines.append(f"- {name} [{e.risk}]: {first}")
        lines += [f"- {app}_* (load as {app}_<name>; * = asks first): {', '.join(tools)}"
                  for app, tools in apps.items()]
        return "\n".join(lines) or "(none)"


# --- fetch_url safety -------------------------------------------------------------------------

_TAGS = re.compile(r"<(script|style|noscript)[\s\S]*?</\1>|<[^>]+>", re.I)
_LINK = re.compile(r"""<a\s[^>]*?href=["']([^"'#][^"']*)["'][^>]*>([\s\S]*?)</a>""", re.I)
_IMG = re.compile(r"""<img\s[^>]*?src=["']([^"']+)["'][^>]*>""", re.I)
# block-level tags end a line, so a page reads as lines (headings and list items each on their own)
_BLOCK = re.compile(r"<(?:br|hr)\b[^>]*>|</(?:p|div|li|tr|h[1-6]|section|article|header|footer|ul|ol|table|"
                    r"blockquote|pre|dd|dt|nav|main|aside)\s*>", re.I)
_HEADING = re.compile(r"<h([1-6])\b[^>]*>", re.I)
_ITEM = re.compile(r"<li\b[^>]*>", re.I)
PAGE_TTL_S = 600  # how long a fetched page is kept for reading on and searching


async def assert_public_url(url: str) -> httpx.URL:
    """Block SSRF: only http(s) to globally routable addresses (no localhost, LAN, metadata IPs)."""
    u = httpx.URL(url)
    if u.scheme not in ("http", "https") or not u.host:
        raise ValueError("only http(s) URLs are allowed")
    infos = await asyncio.get_running_loop().getaddrinfo(u.host, u.port or (443 if u.scheme == "https" else 80))
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise ValueError(f"refusing to fetch non-public address {u.host}")
    # shortcut: DNS could change between this check and the request (rebinding); pin the IP if exposed publicly
    return u


def unseen_url(args: dict) -> bool:
    """A URL the model built itself can carry private data out in its query string. Only a URL shown word for word
    (in the user's message, a search result or a page read this run) goes without asking.
    shortcut: exact text match, so a bare "example.com" or a URL from an earlier message asks once."""
    return (args.get("url") or "") not in RUN.get().seen


def _abs(base: httpx.URL, ref: str) -> str | None:
    """A link or image address as an absolute http(s) URL; anything else (javascript:, data:, junk) is dropped."""
    try:
        url = str(base.join(ref.strip()))
    except Exception:
        return None
    return url if url.startswith(("http://", "https://")) else None


async def fetch_text(url: str, max_chars: int = 50_000) -> str:
    async with httpx.AsyncClient(timeout=20, follow_redirects=False,
                                 headers={"User-Agent": "Riki/0.1 (personal agent)"}) as client:
        for _ in range(4):  # follow redirects manually so every hop is checked
            r = await client.get(str(await assert_public_url(url)))
            if r.is_redirect and "location" in r.headers:
                url = str(r.url.join(r.headers["location"]))
                continue
            r.raise_for_status()
            body = r.text[: max_chars * 4]
            if "html" in r.headers.get("content-type", ""):
                # keep where links and pictures point: "text (url)", so a reply can link to them
                body = _LINK.sub(lambda m: f"{m[2]} ({u})" if (u := _abs(r.url, m[1])) else m[2], body)
                body = _IMG.sub(lambda m: f" image: {u} " if (u := _abs(r.url, m[1])) else " ", body)
                body = _HEADING.sub(lambda m: "\n" + "#" * int(m[1]) + " ", body)
                body = _BLOCK.sub("\n", _ITEM.sub("\n- ", body))
                body = html.unescape(_TAGS.sub(" ", body))
            lines = (re.sub(r"\s+", " ", ln).strip() for ln in body.splitlines())
            return "\n".join(ln for ln in lines if ln and ln not in ("-", "#", "##", "###"))[:max_chars]
    raise ValueError("too many redirects")


def page_view(head: str, lines: list[str], start_line: int, find: str, budget: int, what: str = "page") -> str:
    """One part of a long text as numbered lines: from ``start_line``, or only the lines holding all of ``find``'s words.
    ``head`` is the opening of the bracketed header (the caller adds what it knows: url, age...)."""
    if find:
        words = find.lower().split()
        hits = [f"{i + 1}: {ln}" for i, ln in enumerate(lines) if all(w in ln.lower() for w in words)]
        shown = _fit(hits, budget)
        return (f"{head} · {len(hits)} match {find!r}" + (f", first {len(shown)} shown" if len(shown) < len(hits)
                                                            else "") + "]\n" + "\n".join(shown)
                + "\n[read around a match with start_line=<its number>]")
    first = max(start_line, 1)
    if first > len(lines):
        return f"{head}; start_line {first} is past the end]"
    shown = _fit([f"{i + 1}: {lines[i]}" for i in range(first - 1, len(lines))], budget)
    end = first - 1 + len(shown)
    return (f"{head} · showing {first}-{end}]\n" + "\n".join(shown)
            + (f"\n[more: start_line={end + 1}, or find=\"words\" to search the {what}]" if end < len(lines)
               else f"\n[end of {what}]"))


def wrap_lines(text: str, width: int = 300) -> list[str]:
    return [w for ln in text.splitlines() for w in textwrap.wrap(ln, width)]


def _fit(lines: list[str], budget: int) -> list[str]:
    """As many lines as fit in ``budget`` characters, at least one."""
    out, used = [], 0
    for ln in lines:
        if out and used + len(ln) + 1 > budget:
            break
        out.append(ln)
        used += len(ln) + 1
    return out


# --- built-in tools ---------------------------------------------------------------------------

def builtin_tools(rt: "Runtime") -> list[ToolEntry]:
    s = rt.settings

    @tool
    def current_time() -> str:
        """Current date, time and weekday in the user's timezone."""
        return datetime.now(ZoneInfo(s.timezone)).strftime("%A %d %B %Y, %H:%M %Z")

    @tool
    async def web_search(query: str, max_results: int = 5,
                         topic: Literal["general", "news", "finance"] = "general", images: bool = False) -> str:
        """Search the web for current or obscure facts. Returns title, URL and snippet per result.
        images=true also returns picture URLs (needs the Tavily app)."""
        key, note = await rt.apps.token("tavily"), "(Basic search: no Tavily key is connected.)\n"
        if key:
            try:
                return await search.search(key, query, max_results, topic, images)
            except search.TavilyError as exc:  # fall back, and tell the model why
                note = f"({exc}; basic search used instead.)\n"
        return note + await search.duckduckgo(query, max_results)

    @tool
    async def connect_app(app: str, reason: str = "") -> str:
        """Show a secure card in the chat to connect an app: sign in, or paste its key into the card, never into the
        chat. `app` is an id from the list of apps that can be connected, e.g. tavily, gmail, notion."""
        from ..apps import CATALOG

        if app not in CATALOG:
            return f"No listed app '{app}'. Listed apps: {', '.join(CATALOG)}."
        name = CATALOG[app]["name"]
        if rt.apps.state.get(app, {}).get("state") == "connected":
            return f"{name} is already connected."
        return (f"A card to connect {name} ({CATALOG[app]['blurb']}) is now in the chat. {s.user_name} signs in or "
                "pastes the key there and you never see it. Tell them in one line what it is for, then stop.")

    # shortcut: kept in process memory per Riki instance; move to the database if pages must survive a restart
    pages: dict[str, tuple[float, list[str]]] = {}  # url -> (fetched at, numbered lines)

    @tool
    async def fetch_url(url: str, start_line: int = 1, find: str = "") -> str:
        """Read a public web page as numbered lines of clean text, one part at a time. The page is kept for a few
        minutes: to read on, call again with the start_line it gives; to jump to something, pass find="words" to get
        only the lines that contain all of them, with their numbers."""
        at, lines = pages.get(url, (None, []))
        if at is None or time.monotonic() - at > PAGE_TTL_S:  # monotonic starts near 0 at boot: never compare to 0
            lines = wrap_lines(await fetch_text(url))
            at = time.monotonic()
            pages[url] = (at, lines)
            while len(pages) > 32:
                pages.pop(next(iter(pages)))
        if not lines:
            return f"[{url}] has no readable text (it may need JavaScript to show its content)."
        head = f"[{url} · fetched {int(time.monotonic() - at)}s ago · {len(lines)} lines"
        budget = max(s.tool_output_chars - 300, 300)  # room for the notes, so the generic truncation never fires
        return page_view(head, lines, start_line, find, budget)

    class AskArgs(BaseModel):
        question: str = Field(max_length=300)
        options: list[str] = Field(min_length=2, max_length=4, description="2-4 short answers the user can tap.")

    @tool(args_schema=AskArgs)
    async def ask_user(question: str, options: list[str]) -> str:
        """Ask the user ONE question with 2-4 tappable choices. Use it only when a wrong guess would be expensive or
        could not be undone (money, a booking, deleting, a date that matters). For anything cheap, state your assumption
        and carry on. It waits for the answer, which can take hours; never ask something you could look up."""
        return await policy.ask(rt.db, question, [o[:60] for o in options], CURRENT_SPAN.get() or "",
                                s.approval_timeout_s)

    @tool
    async def recall_memory(query: str) -> str:
        """Search long-term memory about the user. Each line carries its memory id for citation."""
        memories, _ = await rt.memory.recall(RUN.get().memory_user or s.user_id, query)
        if not memories:
            return "Nothing relevant in memory."
        return "\n".join(f"- [m:{m.id[:8]}] ({m.kind}, {(m.event_at or m.created_at):%Y-%m-%d}) {m.content}"
                         for m in memories)

    @tool
    async def remember(fact: str, importance: int = 3) -> str:
        """Save one durable fact about the user, written as a self-contained sentence."""
        m = await rt.memory.save(MemoryCreate(user_id=RUN.get().memory_user or s.user_id, content=fact, kind="fact",
                                              importance=max(1, min(5, importance))))
        return f"Saved as m:{m.id[:8]}."

    async def _save(kind: str, title: str, content: str, id: str) -> str:
        if len(content) > 200_000:
            return "Too long: keep it under 200,000 characters, or split it into several."
        ctx = RUN.get()
        async with rt.db.session() as db:
            a = await db.get(Artifact, id) if id else None
            if id and (a is None or a.kind != kind):
                return f"No {kind} file with id {id}. Leave id empty to make a new one."
            if a is None:
                a = Artifact(thread_id=ctx.thread_id, kind=kind, format="md" if kind == "md" else "pdf")
                db.add(a)
            a.title, a.content = title.strip()[:200] or "Untitled", content
            a.run_id, a.updated_at = ctx.run_id, utcnow()
            await db.commit()
        return (f"Saved '{a.title}' (id {a.id}). {s.user_name} sees it as a card with the download. "
                "Reply in one line; do not paste it into the chat.")

    @tool
    async def save_markdown(title: str, content: str, id: str = "") -> str:
        """Save a Markdown file the user asked for (notes, a README, a plan, a report to keep). They see it
        rendered and can download the .md. Start with '# Title'; use headings, lists and tables. Never use emoji.
        To revise one, pass its id and the full new content."""
        return await _save("md", title, content, id)

    @tool
    async def save_artifact(title: str, html: str, id: str = "") -> str:
        """Make a designed page the user asked for (report, one-pager, brochure, poster, slides) as one complete,
        self-contained HTML document with all CSS inline in a <style> tag. It is shown full screen and saved as a
        PDF, so design it for print: A4 (@page { size: A4; margin: 0 }), real type hierarchy, generous spacing,
        a restrained colour palette, print-safe backgrounds (print-color-adjust: exact). Slides: one 16:9 section
        per slide with break-after: page. No external scripts; Google Fonts links are fine. Never use emoji.
        Every fact must come from your sources. To revise one, pass its id and the full new HTML."""
        return await _save("html", title, html, id)

    return [
        ToolEntry(current_time, SAFE), ToolEntry(web_search, SAFE), ToolEntry(fetch_url, SAFE, ask_if=unseen_url),
        ToolEntry(recall_memory, SAFE), ToolEntry(connect_app, SAFE), ToolEntry(ask_user, SAFE),
        # a run that has read the web, a file or an app may have been told what to "remember": that needs your yes
        ToolEntry(remember, SAFE, ask_if=lambda _: RUN.get().tainted),
        ToolEntry(save_markdown, SAFE), ToolEntry(save_artifact, SAFE),  # write only Riki's own file store
    ]


__all__ = ["ToolEntry", "ToolRegistry", "builtin_tools", "assert_public_url", "fetch_text", "unseen_url", "SAFE", "WRITE",
           "EXTERNAL"]
