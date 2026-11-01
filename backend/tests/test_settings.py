"""Settings changed from the app, the Telegram connection (token kept encrypted, a one-time pairing code) and the
permanent Telegram chat."""

import asyncio

import httpx
import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import delete, select

from app import channels  # noqa: F401  (the package must import)
from app.channels import telegram as tg
from app.channels.telegram import PAIR_MAX_FAILS, TelegramChannel
from app.db import AppSetting, ApprovalGrant, Message, Nudge, Thread, utcnow
from app.main import app
from app.notify import notify

AUTH = {"password": "correct horse battery"}
TOKEN = "123456789:" + "A" * 35


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _wipe(rt):
    async with rt.db.session() as s:
        await s.execute(delete(AppSetting))
        await s.execute(delete(Nudge))
        await s.execute(delete(ApprovalGrant))
        await s.commit()


@pytest.fixture
def no_network(monkeypatch):
    """No real Telegram: the bot is always "rikibot" and starting the channel does not poll."""
    async def fake_info(token):
        return {"username": "rikibot"}

    async def fake_start(self):
        if self.chat_id is None:
            self.new_code()

    monkeypatch.setattr(tg, "bot_info", fake_info)
    monkeypatch.setattr(TelegramChannel, "start", fake_start)


def _stub(channel):
    channel.sent = []

    async def send(text, keyboard=None, plain=False):
        channel.sent.append(text)
        return 1

    channel._send = send

    async def call(method, **params):
        return {}

    channel._call = call  # nothing may reach the real Telegram
    return channel


async def _settle(rt):
    if rt.background:
        await asyncio.wait(rt.background, timeout=10)


def private(text, chat=77, first="Subhadip"):
    return {"chat": {"id": chat, "type": "private"}, "from": {"first_name": first}, "text": text}


# --- settings -----------------------------------------------------------------------------------------------

async def test_a_setting_changed_in_the_app_applies_at_once_survives_a_restart_and_resets(make_runtime):
    rt = await make_runtime(lambda m: AIMessage("ok"))
    await _wipe(rt)
    app.state.rt = rt
    live = rt.settings  # everything holds this very object: it must change in place
    try:
        async with _client() as client:
            await client.post("/api/login", json=AUTH)
            before = (await client.get("/api/settings")).json()
            assert before["overridden"] == [] and before["values"]["quiet_hours"] == "22-8"

            sent = {"quiet_hours": "0-0", "daily_budget_usd": 5, "cognition": False, "user_name": "Sam",
                    "prices": {"openai:gpt-4.1-mini": {"input": 0.4, "output": 1.6, "cached": 0.1}}}
            after = (await client.put("/api/settings", json=sent)).json()
            assert rt.settings is live and live.quiet_hours == "0-0" and live.daily_budget_usd == 5 and not live.cognition
            assert live.prices["openai:gpt-4.1-mini"].output == 1.6 and "Sam" in rt.main_system
            assert set(after["overridden"]) == set(sent) and after["defaults"]["quiet_hours"] == "22-8"

            for bad in ({"timezone": "Mars/Base"}, {"quiet_hours": "30-1"}, {"daily_budget_usd": -1},
                        {"model": "x"}, {"cognition_interval_h": 0}):
                assert (await client.put("/api/settings", json=bad)).status_code == 422
            assert live.quiet_hours == "0-0"

            # the next start reads them back from the database
            await rt.stop()
            rt2 = await make_runtime(lambda m: AIMessage("ok"))
            assert rt2.settings.quiet_hours == "0-0" and rt2.settings.daily_budget_usd == 5 \
                and rt2.settings.prices["openai:gpt-4.1-mini"].input == 0.4
            app.state.rt = rt2
            reset = (await client.put("/api/settings", json={"quiet_hours": None, "prices": None, "daily_budget_usd": 0})).json()
            assert rt2.settings.quiet_hours == "22-8" and rt2.settings.prices == {} and rt2.settings.daily_budget_usd is None
            assert "quiet_hours" not in reset["overridden"] and "daily_budget_usd" in reset["overridden"]
    finally:
        await _wipe(rt)


async def test_the_sandbox_tool_follows_the_switch_without_a_restart(make_runtime, monkeypatch):
    rt = await make_runtime(lambda m: AIMessage("ok"))
    await _wipe(rt)
    app.state.rt = rt
    monkeypatch.setattr("app.sandbox.shutil.which", lambda name: "/usr/bin/docker")
    try:
        async with _client() as client:
            await client.post("/api/login", json=AUTH)
            assert rt.registry.get("run_python") is None
            await client.put("/api/settings", json={"sandbox": True})
            assert rt.registry.get("run_python") is not None
            await client.put("/api/settings", json={"sandbox": False})
            assert rt.registry.get("run_python") is None
    finally:
        await _wipe(rt)


# --- pairing --------------------------------------------------------------------------------------------------

async def test_only_the_chat_that_sends_the_code_is_paired_and_guessing_locks_it(make_runtime):
    rt = await make_runtime(lambda m: AIMessage("hi"))
    await _wipe(rt)
    channel = _stub(TelegramChannel(rt, TOKEN, None, "rikibot"))
    try:
        code = channel.new_code()
        await channel._message(private("hello there"))           # no code: silence
        await channel._message(private("/start"))                # a bare /start: silence
        await channel._message({**private(code), "chat": {"id": -5, "type": "group"}})  # right code, wrong kind of chat
        assert channel.chat_id is None and channel.sent == []

        for i in range(PAIR_MAX_FAILS):
            await channel._message(private(f"/start {'0' * 10}", chat=99))
        assert channel.status()["locked"] and channel.status()["code"] is None
        await channel._message(private(code))                   # the right code after too many wrong ones: still no
        assert channel.chat_id is None

        code = channel.new_code()                                 # asking for a new code starts over
        await channel._message(private(f"/start {code}"))
        assert channel.chat_id == 77 and channel.who == "Subhadip" and channel.code is None
        assert len(channel.sent) == 1 and "Connected" in channel.sent[0]
        await channel._message(private("a stranger", chat=1234))  # only the paired chat counts from now on
        assert len(channel.sent) == 1
    finally:
        await channel.client.aclose()
        await _wipe(rt)


async def test_pairing_code_expires(make_runtime):
    rt = await make_runtime(lambda m: AIMessage("hi"))
    channel = _stub(TelegramChannel(rt, TOKEN, None, "rikibot"))
    try:
        code = channel.new_code()
        channel.code_until -= 3600
        await channel._message(private(code))
        assert channel.chat_id is None and channel.status()["code"] is None
    finally:
        await channel.client.aclose()


async def test_connect_from_the_app_keeps_the_token_encrypted_and_the_link_never_leaks_it(make_runtime, no_network):
    rt = await make_runtime(lambda m: AIMessage("hi"))
    await _wipe(rt)
    app.state.rt = rt
    try:
        async with _client() as client:
            await client.post("/api/login", json=AUTH)
            assert (await client.get("/api/telegram")).json()["connected"] is False
            assert (await client.post("/api/telegram", json={"token": "not a token"})).status_code == 422

            st = (await client.post("/api/telegram", json={"token": TOKEN})).json()
            assert st["connected"] and not st["paired"] and st["bot"] == "rikibot"
            assert st["link"] == f"https://t.me/rikibot?start={st['code']}" and len(st["code"]) == 10
            assert TOKEN not in str((await client.get("/api/telegram")).json())
            async with rt.db.session() as s:
                stored = (await s.get(AppSetting, "telegram")).value
            assert TOKEN not in str(stored) and stored["chat_id"] is None  # encrypted at rest

            _stub(rt.telegram)
            await rt.telegram._message(private(f"/start {st['code']}", chat=4242))
            assert (await client.get("/api/me")).json()["telegram"] is True
            done = (await client.get("/api/telegram")).json()
            assert done["paired"] and done["who"] == "Subhadip" and done["code"] is None and done["link"] is None
            assert (await client.post("/api/telegram/test")).status_code == 200 and "Hello from" in rt.telegram.sent[-1]

            # a restart opens the saved connection: same bot, same chat, no new pairing
            await rt.telegram_stop()
            await rt.telegram_load()
            assert rt.telegram.chat_id == 4242 and rt.telegram.bot == "rikibot"

            # another phone: the bot stays, the chat is forgotten, a new code appears
            again = (await client.post("/api/telegram/pair")).json()
            assert not again["paired"] and again["code"] and (await client.get("/api/me")).json()["telegram"] is False

            await client.delete("/api/telegram")
            assert (await client.get("/api/telegram")).json()["connected"] is False and rt.telegram is None
            await rt.telegram_load()  # disconnected stays disconnected, even if .env still names a bot
            assert rt.telegram is None
            assert (await client.post("/api/telegram/test")).status_code == 400
    finally:
        await rt.telegram_stop()
        await _wipe(rt)


async def test_a_token_that_cannot_be_opened_is_reported_not_crashed(make_runtime, no_network):
    rt = await make_runtime(lambda m: AIMessage("hi"))
    await _wipe(rt)
    try:
        async with rt.db.session() as s:
            s.add(AppSetting(key="telegram", value={"token": "garbage", "chat_id": 5, "bot": "b", "who": ""}))
            await s.commit()
        await rt.telegram_load()
        assert rt.telegram is None and "Connect it again" in rt.telegram_status()["error"]
    finally:
        await _wipe(rt)


# --- the permanent Telegram chat -----------------------------------------------------------------------------

async def test_the_telegram_chat_is_permanent_and_the_phone_and_app_share_it(make_runtime, no_network):
    rt = await make_runtime(lambda m: AIMessage("Sure thing."))
    await _wipe(rt)
    app.state.rt = rt
    channel = rt.telegram = _stub(TelegramChannel(rt, TOKEN, 77, "rikibot"))
    try:
        async with _client() as client:
            await client.post("/api/login", json=AUTH)
            t = (await client.get("/api/threads/telegram")).json()
            assert t["kind"] == "telegram" and t["id"] == (await client.get("/api/threads/telegram")).json()["id"]
            assert t["id"] not in [x["id"] for x in (await client.get("/api/threads")).json()]  # not a topic chat

            # from the phone: the exchange lands in this chat
            await channel._message(private("what is two plus two", chat=77))
            await _settle(rt)
            texts = [m["content"] for m in (await client.get(f"/api/threads/{t['id']}/messages")).json()]
            assert texts[:2] == ["what is two plus two", "Sure thing."]
            assert channel.sent[-1] == "Sure thing."

            # from the app, in this chat: the answer also reaches the phone
            channel.sent.clear()
            await client.post("/api/chat", json={"message": "and from the app", "thread_id": t["id"]})
            await _settle(rt)
            assert channel.sent == ["Sure thing."]

            # a message Riki starts itself is kept here too
            await notify(rt, "Reminder: call the bank", in_app=False)
            last = (await client.get(f"/api/threads/{t['id']}/messages")).json()[-1]
            assert last["content"] == "Reminder: call the bank" and channel.sent[-1] == "Reminder: call the bank"

            # "delete" clears it but never removes it
            gone = (await client.delete(f"/api/threads/{t['id']}")).json()
            assert gone["cleared"] and (await client.get("/api/threads/telegram")).json()["id"] == t["id"]
            assert (await client.get(f"/api/threads/{t['id']}/messages")).json() == []
    finally:
        rt.telegram = None
        await channel.client.aclose()
        async with rt.db.session() as s:
            await s.execute(delete(Message).where(Message.thread_id == t["id"]))
            await s.commit()
        await _wipe(rt)


# --- grants and nudges ---------------------------------------------------------------------------------------

async def test_running_grants_can_be_ended_and_nudges_are_listed(make_runtime):
    from datetime import timedelta

    rt = await make_runtime(lambda m: AIMessage("ok"))
    await _wipe(rt)
    app.state.rt = rt
    try:
        async with rt.db.session() as s:
            live = ApprovalGrant(thread_id="a" * 32, tool="make_file", expires_at=utcnow() + timedelta(minutes=20))
            old = ApprovalGrant(thread_id="a" * 32, tool="old_tool", expires_at=utcnow() - timedelta(minutes=1))
            s.add_all([live, old, Nudge(topic="bank", text="Call the bank today?")])
            await s.commit()
        async with _client() as client:
            await client.post("/api/login", json=AUTH)
            assert [g["tool"] for g in (await client.get("/api/grants")).json()] == ["make_file"]
            assert (await client.delete(f"/api/grants/{live.id}")).status_code == 200
            assert (await client.get("/api/grants")).json() == []
            assert (await client.delete(f"/api/grants/{live.id}")).status_code == 404
            assert [n["topic"] for n in (await client.get("/api/nudges")).json()] == ["bank"]
    finally:
        await _wipe(rt)
