"""Telegram: talk to Riki, approve changes and answer its questions from your phone.

Long polling, so Riki needs no public address and stays on localhost. It answers and messages exactly ONE chat; everyone
else is ignored. The recipient is fixed when you pair, never chosen by the model, which is what keeps "Riki never contacts
other people" true here too. Your phone and the app share one conversation: the permanent Telegram chat in the app.

Connecting from the app: you paste the bot's token (stored encrypted), the app shows a one-time code and a link, and the first
private chat that sends that code becomes the one chat. The code lives ten minutes, in memory only, and five wrong guesses
lock it until you ask for a new one. Until a chat is paired, every message is ignored without an answer.
"""

import asyncio
import base64
import hmac
import html
import json
import logging
import re
import secrets
import time
from typing import TYPE_CHECKING

import httpx

from ..agent import policy
from ..turn import start_run

if TYPE_CHECKING:
    from ..runtime import Runtime

log = logging.getLogger(__name__)

LIMIT = 4096  # characters per Telegram message
POLL_S = 25
EDIT_EVERY_S = 1.2  # a streamed reply is one message edited at most this often (Telegram rate-limits edits)
MAX_PHOTO_BYTES = 3_000_000
PAIR_TTL_S = 600
PAIR_MAX_FAILS = 5


async def bot_info(token: str) -> dict:
    """Ask Telegram who this token belongs to; a readable ValueError when it is wrong or Telegram is unreachable."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"https://api.telegram.org/bot{token}/getMe")
            data = r.json()
    except (httpx.HTTPError, ValueError):
        raise ValueError("Could not reach Telegram. Check the internet connection and try again.")
    if not data.get("ok"):
        raise ValueError("Telegram does not recognise that token. Copy it again from @BotFather.")
    return data["result"]


def md_to_html(text: str) -> str:
    """The Markdown a model writes, as Telegram HTML. Code is escaped untouched; everything else is escaped first, so
    nothing the model or a web page wrote can inject a tag."""
    out = []
    for i, seg in enumerate(re.split(r"(```(?:[a-z]*\n)?.*?```)", text, flags=re.DOTALL)):
        if i % 2 == 1:
            code = re.sub(r"^```[a-z]*\n?", "", seg).rstrip("`").strip()
            out.append("<pre>" + html.escape(code) + "</pre>")
            continue
        seg = html.escape(seg)
        seg = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", seg, flags=re.MULTILINE)
        seg = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", seg)
        seg = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<i>\1</i>", seg)
        seg = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", seg)
        seg = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', seg)
        out.append(seg)
    return "".join(out)


def strip_refs(text: str) -> str:
    """[t3] and [m:1fdab01f] are for the trace panel; on a phone they are noise."""
    return re.sub(r"\s*\[(?:t\d+|m:[0-9a-f]{8})\]", "", text)


def split_message(text: str, limit: int = LIMIT) -> list[str]:
    parts = []
    while len(text) > limit:
        cut = max(text.rfind("\n\n", 0, limit), text.rfind("\n", 0, limit), text.rfind(" ", 0, limit))
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return parts + ([text] if text else [])


def approval_keyboard(approval: dict) -> list[list[dict]]:
    """Buttons for a request: Allow / Deny (+ a 30-minute grant, never for money), or the choices of a question."""
    if approval["tool"] == policy.ASK:
        options = (approval.get("args") or {}).get("options") or []
        return [[{"text": o[:60], "callback_data": f"as:{approval['id']}:{i}"}] for i, o in enumerate(options)] + \
               [[{"text": "Skip", "callback_data": f"as:{approval['id']}:x"}]]
    row = [{"text": "Allow", "callback_data": f"ap:{approval['id']}:y"}, {"text": "Deny", "callback_data": f"ap:{approval['id']}:n"}]
    if not approval.get("money"):
        row.append({"text": f"Allow {policy.GRANT_MINUTES} min", "callback_data": f"ap:{approval['id']}:g"})
    return [row]


def approval_text(approval: dict, agent: str) -> str:
    if approval["tool"] == policy.ASK:
        return (approval.get("args") or {}).get("question", "A question for you")
    args = json.dumps(approval.get("args") or {}, ensure_ascii=False, default=str)
    money = "This moves money. " if approval.get("money") else ""
    return f"{agent} wants to run {approval['tool']}.\n{money}{args[:700]}"


class TelegramChannel:
    def __init__(self, rt: "Runtime", token: str, chat_id: int | None, bot: str = "", who: str = "") -> None:
        self.rt, self.chat_id, self.bot, self.who = rt, chat_id, bot, who
        self.code: str | None = None  # the pairing code, while no chat is paired
        self.code_until = 0.0
        self.failures = 0
        self.api = f"https://api.telegram.org/bot{token}"
        self.file_api = f"https://api.telegram.org/file/bot{token}"
        self.client = httpx.AsyncClient(timeout=POLL_S + 15)
        self.task: asyncio.Task | None = None
        self.offset = 0
        self.last_error = ""

    # -- plumbing ---------------------------------------------------------------------------------------

    async def _call(self, method: str, **params):
        r = await self.client.post(f"{self.api}/{method}", json=params)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data.get('description', r.status_code)}")
        return data["result"]

    async def _send(self, text: str, keyboard: list | None = None, plain: bool = False) -> int | None:
        sent = None
        if self.chat_id is None:
            raise RuntimeError("no Telegram chat is paired yet")
        for i, part in enumerate(split_message(text)):
            params = {"chat_id": self.chat_id, "text": part if plain else md_to_html(part), "link_preview_options": {"is_disabled": True}}
            if not plain:
                params["parse_mode"] = "HTML"
            if keyboard and i == 0:
                params["reply_markup"] = {"inline_keyboard": keyboard}
            try:
                sent = (await self._call("sendMessage", **params))["message_id"]
            except RuntimeError:  # a reply Telegram cannot parse as HTML still gets through, as plain text
                params.pop("parse_mode", None)
                params["text"] = part
                sent = (await self._call("sendMessage", **params))["message_id"]
        return sent

    async def notify(self, text: str) -> None:
        await self._send(strip_refs(text))

    async def on_approval(self, approval: dict) -> None:
        """A request is waiting (from any chat): put the buttons on your phone."""
        if self.chat_id is None:
            return
        await self._send(approval_text(approval, self.rt.persona.name), approval_keyboard(approval), plain=True)

    # -- lifecycle --------------------------------------------------------------------------------------

    # -- pairing ----------------------------------------------------------------------------------------

    def new_code(self) -> str:
        self.code, self.code_until, self.failures = secrets.token_hex(5), time.time() + PAIR_TTL_S, 0
        return self.code

    def unpair(self) -> None:
        """Forget the paired chat (keeps the bot); a new code starts a new pairing."""
        self.chat_id, self.who = None, ""
        self.new_code()

    def status(self) -> dict:
        pairing = self.chat_id is None
        live = pairing and self.code is not None and time.time() < self.code_until
        locked = pairing and self.failures >= PAIR_MAX_FAILS
        return {"connected": True, "paired": not pairing, "bot": self.bot, "who": self.who, "error": self.last_error,
                "code": self.code if live and not locked else None,
                "link": f"https://t.me/{self.bot}?start={self.code}" if live and not locked and self.bot else None,
                "expires_in": max(0, int(self.code_until - time.time())) if live else 0, "locked": locked}

    async def _pair(self, msg: dict) -> None:
        """Before any chat is paired the only thing that is read is a private message holding the code."""
        chat = msg["chat"]
        guess = (msg.get("text") or "").strip().removeprefix("/start").strip().lower()
        if chat.get("type") != "private" or not guess or self.code is None or time.time() > self.code_until \
                or self.failures >= PAIR_MAX_FAILS:
            return
        if not hmac.compare_digest(guess.encode(), self.code.encode()):
            self.failures += 1
            log.warning("telegram: wrong pairing code from chat %s (%s of %s)", chat["id"], self.failures, PAIR_MAX_FAILS)
            return
        user = msg.get("from") or {}
        self.chat_id, self.who, self.code = chat["id"], (user.get("first_name") or "")[:40], None
        await self.rt.telegram_paired(self.chat_id, self.who)
        await self._send(f"Connected. This chat is now the Telegram channel in {self.rt.persona.name}'s app: what you write "
                         "here shows up there, and the other way round. Only this chat is ever answered.", plain=True)

    async def mirror(self, ctx) -> None:
        """A message written in the app's Telegram chat: the answer also goes to your phone."""
        async for event in ctx.follow(0):
            if event and event["type"] == "run_end":
                final = strip_refs(event.get("output") or "")
                if final and self.chat_id is not None:
                    await self._send(final)
                return

    async def start(self) -> None:
        if self.chat_id is None:
            self.new_code()
            try:
                await self._call("deleteWebhook")  # polling and a webhook cannot both be set
            except RuntimeError as exc:
                self.last_error = str(exc)[:200]
        policy.NOTIFIERS.append(self.on_approval)
        self.task = asyncio.create_task(self._poll(), name="telegram")

    async def stop(self) -> None:
        if self.on_approval in policy.NOTIFIERS:
            policy.NOTIFIERS.remove(self.on_approval)
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.client.aclose()

    async def _poll(self) -> None:
        backoff = 1.0
        while True:
            try:
                updates = await self._call("getUpdates", offset=self.offset, timeout=POLL_S,
                                           allowed_updates=["message", "callback_query"])
                backoff, self.last_error = 1.0, ""
                for u in updates:
                    self.offset = u["update_id"] + 1
                    self.rt.spawn(self._handle(u))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("telegram polling failed: %s", self.last_error)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    # -- incoming ---------------------------------------------------------------------------------------

    async def _handle(self, update: dict) -> None:
        try:
            if "callback_query" in update:
                await self._callback(update["callback_query"])
            elif "message" in update:
                await self._message(update["message"])
        except Exception:
            log.exception("telegram update failed")

    def _mine(self, chat_id: int) -> bool:
        if self.chat_id is None:
            return False
        if chat_id == self.chat_id:
            return True
        log.warning("telegram: ignored a message from chat %s (only RIKI_TELEGRAM_CHAT_ID is answered)", chat_id)
        return False

    async def _callback(self, cq: dict) -> None:
        if not self._mine(cq["message"]["chat"]["id"]):
            return
        kind, approval_id, arg = (cq.get("data") or "::").split(":", 2)
        db = self.rt.db
        if kind == "ap":
            ok = await policy.decide(db, approval_id, arg in ("y", "g"), grant=arg == "g")
            note = {"y": "Allowed", "g": f"Allowed for {policy.GRANT_MINUTES} min", "n": "Denied"}.get(arg, "")
        else:
            row = await self._ask_options(approval_id)
            if arg == "x" or row is None or not arg.isdigit() or int(arg) >= len(row):
                ok, note = await policy.decide(db, approval_id, False), "Skipped"
            else:
                ok, note = await policy.decide(db, approval_id, True, answer=row[int(arg)]), row[int(arg)]
        await self._call("answerCallbackQuery", callback_query_id=cq["id"], text=note if ok else "No longer waiting")
        if ok:
            await self._call("editMessageReplyMarkup", chat_id=self.chat_id, message_id=cq["message"]["message_id"],
                             reply_markup={"inline_keyboard": []})

    async def _ask_options(self, approval_id: str) -> list[str] | None:
        from ..db import Approval

        async with self.rt.db.session() as s:
            row = await s.get(Approval, approval_id)
        return list((row.args or {}).get("options") or []) if row is not None else None

    async def _photo(self, file_id: str) -> str | None:
        info = await self._call("getFile", file_id=file_id)
        if info.get("file_size", 0) > MAX_PHOTO_BYTES:
            return None
        r = await self.client.get(f"{self.file_api}/{info['file_path']}")
        if r.status_code != 200 or len(r.content) > MAX_PHOTO_BYTES:
            return None
        return "data:image/jpeg;base64," + base64.b64encode(r.content).decode()

    async def _message(self, msg: dict) -> None:
        if self.chat_id is None:
            await self._pair(msg)
            return
        if not self._mine(msg["chat"]["id"]):
            return
        rt = self.rt
        text = (msg.get("text") or msg.get("caption") or "").strip()
        thread = await telegram_thread(rt)
        if text.lower() in ("/stop", "stop"):
            busy = rt.active_run(thread)
            if busy is not None:
                rt.runs[busy.run_id][1].cancel()
            return
        if text.lower() in ("/start", "/help"):
            await self._send("I'm here. Write to me like you do in the app: this chat is the Telegram channel there, "
                             "and it is kept in sync. /stop stops what I'm doing.", plain=True)
            return
        images = []
        if msg.get("photo"):
            photo = await self._photo(msg["photo"][-1]["file_id"])
            images = [photo] if photo else []
        if not text and not images:
            return
        if rt.active_run(thread) is not None:
            await self._send("Still working on your last message. /stop ends it.", plain=True)
            return
        ctx = start_run(rt, thread, text or "(a picture)", images=images or None)
        await self._stream(ctx)

    async def _stream(self, ctx) -> None:
        """One message that grows while Riki writes, then the final text. Approvals arrive as their own messages."""
        message_id, shown, last_edit, buffer = None, "", 0.0, ""
        async for event in ctx.follow(0):
            if event is None:
                continue
            if event["type"] == "token":
                buffer += event["text"]
                if time.monotonic() - last_edit > EDIT_EVERY_S and buffer.strip() and buffer != shown:
                    try:
                        if message_id is None:
                            message_id = await self._send(strip_refs(buffer)[:LIMIT - 50] + " …", plain=True)
                        else:
                            await self._call("editMessageText", chat_id=self.chat_id, message_id=message_id,
                                             text=strip_refs(buffer)[:LIMIT - 50] + " …")
                        shown, last_edit = buffer, time.monotonic()
                    except RuntimeError:
                        last_edit = time.monotonic()
            elif event["type"] == "run_end":
                final = strip_refs(event.get("output") or "")
                if message_id is not None:  # replace the streamed draft by the formatted final text
                    try:
                        await self._call("deleteMessage", chat_id=self.chat_id, message_id=message_id)
                    except RuntimeError:
                        pass
                if final:
                    await self._send(final)
                return


async def telegram_thread(rt) -> str:
    """The permanent chat in the app that everything said over Telegram lands in (created on first use)."""
    from ..ops import own_thread

    return await own_thread(rt, "telegram", "Telegram")
