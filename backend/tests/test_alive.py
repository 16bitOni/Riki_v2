"""Riki between chats: a read on you, lessons, a thinking cycle that may end in one nudge, reminders, files, the daily
budget, the phone channel and the sandbox."""

import io
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import delete, func, select, update

from app import spend
from app.channels.telegram import (TelegramChannel, approval_keyboard, approval_text, md_to_html, split_message,
                                   strip_refs)
from app.cognition import (ignored_streak, latest_reflection, learn_lessons, nudge_allowed, reflection_block,
                           refresh_reflection, run_cycle, save_edit)
from app.db import Approval, Feedback, Message, Nudge, Reflection, Run, Schedule, Thread, utcnow
from app.files import Unreadable, extract_text
from app.main import app
from app.sandbox import docker_command, sandbox_tools
from app.schedules import fire_due, next_occurrence, parse_when
from app.turn import ensure_thread

from .conftest import tool_call
from .test_agent import run

AUTH = {"password": "correct horse battery"}
IST = "Asia/Kolkata"


async def _clean(rt):
    async with rt.db.session() as s:
        for table in (Reflection, Nudge, Schedule):
            await s.execute(delete(table))
        await s.commit()


def _memory(i, when=None):
    return SimpleNamespace(id=f"{i:032x}", content=f"fact {i}", kind="fact", importance=3, event_at=None,
                           created_at=when or datetime.now(timezone.utc), source_key=None, tags=[])


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# --- reflection, lessons ----------------------------------------------------------------------------------

async def test_the_read_on_you_is_written_shown_and_your_edit_is_ground_truth(make_runtime):
    prompts = []

    def respond(messages):
        prompts.append(messages[0].content)
        return AIMessage("**Right now:** busy building Riki.")

    rt = await make_runtime(respond)
    await _clean(rt)
    rt.memory.items = [_memory(i) for i in range(4)]
    assert await refresh_reflection(rt) is True
    assert "busy building Riki" in await reflection_block(rt)
    assert await refresh_reflection(rt) is False  # nothing new, no model call
    assert len(prompts) == 1

    await save_edit(rt, "**Right now:** I changed jobs. Not building Riki any more.")
    assert (await latest_reflection(rt)).source == "edit"
    rt.memory.items += [_memory(100 + i) for i in range(10)]
    assert await refresh_reflection(rt) is True  # ten new memories
    assert "EDITED BY THE USER" in prompts[-1] and "changed jobs" in prompts[-1]


async def test_quiet_chats_yield_lessons_as_preference_memories(make_runtime):
    def respond(messages):
        return AIMessage("", tool_calls=[tool_call("Lessons", {"lessons": ["Sub wants shorter answers about code."]})])

    rt = await make_runtime(respond)
    thread = await ensure_thread(rt, None, "a long chat")
    old = utcnow() - timedelta(hours=8)
    async with rt.db.session() as s:
        for i in range(6):
            s.add(Message(thread_id=thread, role="user" if i % 2 == 0 else "assistant", content=f"message {i}",
                          created_at=old + timedelta(minutes=i)))
        await s.commit()
    assert await learn_lessons(rt) == 1
    lesson = rt.memory.items[-1]
    assert lesson.kind == "preference" and "shorter answers" in lesson.content and lesson.source_key == f"lesson/{thread}"
    assert await learn_lessons(rt) == 0  # the same messages are not read twice


# --- nudges ---------------------------------------------------------------------------------------------

def _nudge(hours_ago, topic="pune trip", reaction=None, now=None):
    now = now or datetime(2026, 11, 2, 6, 30, tzinfo=timezone.utc)
    return Nudge(topic=topic, text="x", sent_at=now - timedelta(hours=hours_ago), reaction=reaction)


def test_nudge_guards_live_in_code():
    now = datetime(2026, 11, 2, 6, 30, tzinfo=timezone.utc)  # 12:00 in Kolkata
    ok = lambda nudges=(), chat_hours=None, topic="exam", at=now: nudge_allowed(  # noqa: E731
        at, IST, "22-8", list(nudges), None if chat_hours is None else at - timedelta(hours=chat_hours), topic)
    assert ok() == (True, "")
    assert ok(at=datetime(2026, 11, 2, 18, 30, tzinfo=timezone.utc))[1] == "quiet hours"  # 00:00 in Kolkata
    assert ok(chat_hours=1)[1] == "you were just chatting"
    assert ok([_nudge(10, "x")])[1].startswith("last nudge was less than 24h")
    assert ok([_nudge(30, "pune trip planning")], topic="pune trip")[1] == "already nudged about that this week"
    assert ok([_nudge(30, "something else")])[0] is True


def test_three_ignored_nudges_in_a_row_double_the_wait():
    ignored = [_nudge(30 + 24 * i, f"t{i}", "ignored") for i in range(3)]
    assert ignored_streak(ignored) == 3
    assert ignored_streak([_nudge(30, "a", "replied"), *ignored]) == 0  # a reply (newest) resets it
    now = datetime(2026, 11, 2, 6, 30, tzinfo=timezone.utc)
    assert nudge_allowed(now, IST, "22-8", ignored, None, "new")[1].startswith("last nudge was less than 48h")
    assert nudge_allowed(now, IST, "22-8", [_nudge(30, "a", "replied")], None, "new")[0] is True


async def test_a_thinking_cycle_can_end_in_one_specific_nudge_and_costs_nothing_when_nothing_is_new(make_runtime):
    def respond(messages):
        if "thinking privately" in messages[0].content:
            return AIMessage("", tool_calls=[tool_call("Think", {"monologue": "The trip went quiet.",
                                                                 "reflection": "**Right now:** the trip went quiet."})])
        return AIMessage("", tool_calls=[tool_call("Act", {"send": True, "text": "Did the Pune trip ever get rebooked?",
                                                           "topic": "pune trip", "kind": "open_loop"})])

    rt = await make_runtime(respond, quiet_hours="0-0", cognition_interval_h=0.000001)
    await _clean(rt)
    rt.memory.items = [_memory(1)]
    assert await run_cycle(rt, force=True) == "nudge sent"
    async with rt.db.session() as s:
        nudge = await s.scalar(select(Nudge))
        heartbeat = await s.scalar(select(Thread).where(Thread.kind == "heartbeat"))
        said = (await s.scalars(select(Message.content).where(Message.thread_id == heartbeat.id))).all()
    assert nudge.topic == "pune trip" and "Did the Pune trip ever get rebooked?" in said
    last = await latest_reflection(rt)
    assert last.source == "cycle" and "The trip went quiet." in last.monologue and last.run_id == nudge.run_id

    async with rt.db.session() as s:
        before = await s.scalar(select(func.count()).select_from(Run).where(Run.source == "cognition"))
    assert await run_cycle(rt) == "nothing new"  # no new memory, chat or schedule: not even a model call
    async with rt.db.session() as s:
        assert await s.scalar(select(func.count()).select_from(Run).where(Run.source == "cognition")) == before


async def test_cycles_pause_in_quiet_hours_and_when_the_budget_is_nearly_used(make_runtime):
    hour = datetime.now(ZoneInfo(IST)).hour
    rt = await make_runtime(lambda m: AIMessage("x"), quiet_hours=f"{hour}-{(hour + 1) % 24}", timezone=IST,
                            daily_budget_usd=1.0)  # this very hour is quiet
    assert await run_cycle(rt) == "quiet hours"
    rt.settings = rt.settings.model_copy(update={"quiet_hours": "0-0"})
    async with rt.db.session() as s:
        s.add(Run(source="chat", status="ok", input="x", model="m", prompt_version="p", cost_usd=0.9))
        await s.commit()
    spend._CACHE.clear()
    assert await run_cycle(rt) == "budget"


# --- schedules -------------------------------------------------------------------------------------------

def test_repeating_schedules_keep_their_local_time():
    prev = datetime(2026, 11, 6, 3, 30, tzinfo=timezone.utc)  # Friday 09:00 in Kolkata
    now = datetime(2026, 11, 6, 3, 31, tzinfo=timezone.utc)
    at = lambda rep: (next_occurrence(prev, rep, IST, now) or prev).astimezone(ZoneInfo(IST)).strftime("%a %H:%M")  # noqa: E731
    assert next_occurrence(prev, "none", IST, now) is None
    assert (at("daily"), at("weekdays"), at("weekly")) == ("Sat 09:00", "Mon 09:00", "Fri 09:00")


def test_when_must_be_a_future_iso_time_in_your_zone():
    now = datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)
    assert parse_when("2026-11-02T09:00", IST, now) == datetime(2026, 11, 2, 3, 30, tzinfo=timezone.utc)
    for bad in ("tomorrow", "2020-01-01T09:00", "2030-01-01T09:00"):
        with pytest.raises(ValueError):
            parse_when(bad, IST, now)


async def test_reminders_are_scheduled_fire_once_and_repeat(make_runtime):
    rt = await make_runtime(lambda m: AIMessage("ok"))
    await _clean(rt)
    schedule, listing, cancel = (rt.registry.get(n).tool for n in ("schedule", "list_schedules", "cancel_schedule"))
    when = (datetime.now(ZoneInfo(rt.settings.timezone)) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    assert "Scheduled remind" in await schedule.ainvoke({"when": when, "text": "call mom", "repeat": "daily"})
    assert "Scheduled remind" in await schedule.ainvoke({"when": when, "text": "once only"})
    assert "ISO" in await schedule.ainvoke({"when": "tomorrow", "text": "x"})
    assert "call mom" in await listing.ainvoke({})

    async with rt.db.session() as s:  # both come due
        await s.execute(update(Schedule).values(next_at=utcnow() - timedelta(minutes=1)))
        await s.commit()
    assert await fire_due(rt) == 2
    assert await fire_due(rt) == 0  # claimed: never fired twice
    async with rt.db.session() as s:
        rows = {r.text: r for r in (await s.scalars(select(Schedule))).all()}
        heartbeat = await s.scalar(select(Thread).where(Thread.kind == "heartbeat"))
        said = (await s.scalars(select(Message.content).where(Message.thread_id == heartbeat.id))).all()
    assert rows["once only"].status == "done"
    assert rows["call mom"].status == "active" and rows["call mom"].next_at > utcnow()
    assert "Reminder: call mom" in said and "Reminder: once only" in said
    assert "Cancelled" in await cancel.ainvoke({"id": rows["call mom"].id[:8]})
    assert await listing.ainvoke({}) == "Nothing scheduled."


async def test_a_scheduled_task_is_a_read_only_run_whose_answer_lands_in_the_check_in_thread(make_runtime):
    rt = await make_runtime(lambda m: AIMessage("Three things on today."))
    await _clean(rt)
    async with rt.db.session() as s:
        s.add(Schedule(kind="task", text="what is on today?", next_at=utcnow() - timedelta(minutes=1)))
        await s.commit()
    assert await fire_due(rt) == 1
    import asyncio
    await asyncio.gather(*rt.background)
    async with rt.db.session() as s:
        source = await s.scalar(select(Run.source).where(Run.input == "Scheduled task: what is on today?"))
        heartbeat = await s.scalar(select(Thread).where(Thread.kind == "heartbeat"))
        said = (await s.scalars(select(Message.content).where(Message.thread_id == heartbeat.id))).all()
    assert source == "schedule" and "Three things on today." in said


# --- files -----------------------------------------------------------------------------------------------

def test_files_become_readable_text():
    assert extract_text("notes.txt", "héllo\nworld".encode()) == "héllo\nworld"
    from docx import Document
    from openpyxl import Workbook
    from pypdf import PdfWriter

    doc = Document()
    doc.add_paragraph("Quarterly plan")
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text, table.rows[0].cells[1].text = "owner", "Priya"
    buf = io.BytesIO()
    doc.save(buf)
    assert "Quarterly plan" in extract_text("plan.docx", buf.getvalue()) and "owner\tPriya" in extract_text("plan.docx", buf.getvalue())
    wb = Workbook()
    wb.active.append(["month", "spend"])
    wb.active.append(["jan", 120])
    buf = io.BytesIO()
    wb.save(buf)
    assert "jan\t120" in extract_text("budget.xlsx", buf.getvalue())
    pdf = PdfWriter()
    pdf.add_blank_page(100, 100)
    buf = io.BytesIO()
    pdf.write(buf)
    with pytest.raises(Unreadable, match="scanned"):
        extract_text("scan.pdf", buf.getvalue())
    with pytest.raises(Unreadable, match="cannot read .exe"):
        extract_text("tool.exe", b"MZ")


async def test_an_uploaded_file_is_read_a_part_at_a_time(make_runtime):
    rt = await make_runtime(lambda m: AIMessage("ok"))
    app.state.rt = rt
    body = "\n".join(f"row {i},{i * 3}" for i in range(400)).encode()
    async with _client() as client:
        await client.post("/api/login", json=AUTH)
        r = await client.post("/api/uploads?name=numbers.csv", content=body)
        assert r.status_code == 200 and r.json()["name"] == "numbers.csv" and r.json()["lines"] == 400
        file_id = r.json()["id"]
        assert (await client.post("/api/uploads?name=tool.exe", content=b"MZ")).status_code == 415
        assert file_id not in [a["id"] for a in (await client.get("/api/artifacts")).json()]  # not a document of Riki's
    read_file = rt.registry.get("read_file").tool
    first = await read_file.ainvoke({"id": file_id})
    assert "[numbers.csv · 400 lines · showing 1-" in first and "more: start_line=" in first
    assert "399: row 398,1194" in await read_file.ainvoke({"id": file_id, "find": "row 398"})
    assert "No attached file" in await read_file.ainvoke({"id": "0" * 32})


# --- the daily budget ---------------------------------------------------------------------------------------

def test_budget_states():
    assert [spend.state_for(x, 10.0) for x in (0, 7.9, 8.0, 9.99, 10.0, 12)] == ["ok", "ok", "soft", "soft", "hard", "hard"]
    assert spend.state_for(5, None) == "ok"


async def test_over_the_daily_budget_chat_answers_briefly_and_says_so_once(make_runtime):
    rt = await make_runtime(lambda m: AIMessage("sure."), daily_budget_usd=0.5)
    async with rt.db.session() as s:
        s.add(Run(source="chat", status="ok", input="x", model="m", prompt_version="p", cost_usd=1.0))
        await s.commit()
    spend._CACHE.clear()
    ctx, first = await run(rt, "hello there friend")
    assert ctx.effort == "low" and first.startswith("(Today's budget is used up")
    _, second = await run(rt, "hello again friend")
    assert second == "sure."
    app.state.rt = rt
    async with _client() as client:
        await client.post("/api/login", json=AUTH)
        got = (await client.get("/api/spend")).json()
    assert got["state"] == "hard" and got["budget_usd"] == 0.5 and got["spent_usd"] >= 1.0


# --- the phone ---------------------------------------------------------------------------------------------

def test_telegram_formatting_and_buttons():
    assert md_to_html("**hi** <b>x</b> `a<b` [l](https://e.com)") == (
        '<b>hi</b> &lt;b&gt;x&lt;/b&gt; <code>a&lt;b</code> <a href="https://e.com">l</a>')
    assert strip_refs("Zerodha [m:1fdab01f] and 11 titles [t3].") == "Zerodha and 11 titles."
    assert all(len(p) <= 4096 for p in split_message("word " * 3000))
    write = {"id": "a1", "tool": "make_file", "args": {"path": "x"}, "money": False}
    assert [b["text"] for b in approval_keyboard(write)[0]] == ["Allow", "Deny", "Allow 30 min"]
    assert [b["text"] for b in approval_keyboard({**write, "money": True})[0]] == ["Allow", "Deny"]  # money: never granted
    ask = {"id": "a2", "tool": "ask_user", "args": {"question": "Which?", "options": ["8th", "15th"]}}
    assert [r[0]["text"] for r in approval_keyboard(ask)] == ["8th", "15th", "Skip"] and approval_text(ask, "Riki") == "Which?"
    assert "This moves money" in approval_text({**write, "money": True}, "Riki")


async def test_taps_on_the_phone_decide_approvals_and_only_your_chat_counts(make_runtime):
    rt = await make_runtime(lambda m: AIMessage("hi"))
    channel = TelegramChannel(rt, "123:abc", 42)
    calls = []

    async def fake(method, **params):
        calls.append((method, params))
        return {}

    channel._call = fake
    try:
        async with rt.db.session() as s:
            plain = Approval(run_id="r" * 32, span_id="s" * 32, tool="make_file", args={}, thread_id="t" * 32)
            ask = Approval(run_id="r" * 32, span_id="s" * 32, tool="ask_user", thread_id="t" * 32,
                           args={"question": "Which?", "options": ["8th", "15th"]})
            stranger = Approval(run_id="r" * 32, span_id="s" * 32, tool="make_file", args={})
            s.add_all([plain, ask, stranger])
            await s.commit()

        tap = lambda data, chat=42: {"id": "1", "data": data, "message": {"chat": {"id": chat}, "message_id": 7}}  # noqa: E731
        await channel._callback(tap(f"ap:{stranger.id}:y", chat=99))  # someone else's chat: nothing happens
        await channel._callback(tap(f"ap:{plain.id}:y"))
        await channel._callback(tap(f"as:{ask.id}:1"))
        async with rt.db.session() as s:
            assert (await s.get(Approval, stranger.id)).status == "pending"
            assert (await s.get(Approval, plain.id)).status == "approved"
            answered = await s.get(Approval, ask.id)
        assert answered.status == "approved" and answered.result == "15th"
        assert [m for m, _ in calls].count("answerCallbackQuery") == 2
    finally:
        await channel.client.aclose()
        async with rt.db.session() as s:  # the database is shared by every test: leave no pending request behind
            await s.execute(delete(Approval).where(Approval.run_id == "r" * 32))
            await s.commit()


# --- the sandbox ---------------------------------------------------------------------------------------------

def test_the_sandbox_is_off_by_default_and_locked_down_when_on():
    cmd = docker_command("c1", "python:3.12-slim", "/tmp/work")
    for flag in ("--network", "none", "--read-only", "--cap-drop", "ALL", "--memory", "512m", "no-new-privileges"):
        assert flag in cmd
    assert cmd[cmd.index("-v") + 1] == "/tmp/work:/work:ro" and cmd[-3:] == ["python", "-I", "/work/main.py"]


async def test_run_python_is_only_offered_when_switched_on(make_runtime):
    rt = await make_runtime(lambda m: AIMessage("ok"))
    assert rt.registry.get("run_python") is None and sandbox_tools(rt) == []


# --- the API for all of it ------------------------------------------------------------------------------------

async def test_api_reflection_feedback_lessons_and_message_costs(make_runtime):
    rt = await make_runtime(lambda m: AIMessage("done. [t1]"))
    await _clean(rt)
    app.state.rt = rt
    async with _client() as client:
        await client.post("/api/login", json=AUTH)
        assert (await client.get("/api/reflection")).json() is None
        put = await client.put("/api/reflection", json={"content": "**Right now:** I am learning Rust."})
        assert put.status_code == 200 and put.json()["source"] == "edit"
        assert (await client.get("/api/reflection")).json()["content"].endswith("learning Rust.")

        chat = await client.post("/api/chat", json={"message": "look at the numbers please 42"})
        run_id = next(line for line in chat.text.splitlines() if '"run_id"' in line).split('"run_id": "')[1].split('"')[0]
        await client.post(f"/api/runs/{run_id}/feedback", json={"score": -1, "comment": "too wordy for code questions"})
        assert any("too wordy for code questions" in m.content and m.kind == "preference" for m in rt.memory.items)
        async with rt.db.session() as db:  # shared database: the Health metrics of other tests count thumbs-down
            await db.execute(delete(Feedback).where(Feedback.run_id == run_id))
            await db.commit()

        main = (await client.get("/api/threads/main")).json()["id"]
        assert (await client.get("/api/schedules")).json() == []
        assert isinstance((await client.get("/api/cost/models")).json(), list)
        assert {"spent_usd", "budget_usd", "state"} <= set((await client.get("/api/spend")).json())
        assert (await client.get("/api/me")).json()["telegram"] is False
        assert isinstance(main, str)
