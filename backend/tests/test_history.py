"""How much of the chat goes back to the model: what fits, cut between exchanges, never fewer than four turns."""

from datetime import datetime, timedelta, timezone

from langchain_core.messages import AIMessage

from app.db import Message
from app.turn import _history, ensure_thread


async def _chat_of(rt, turns: int, size: int) -> str:
    thread = await ensure_thread(rt, None, "history")
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    async with rt.db.session() as s:
        for i in range(turns):
            for k, role in enumerate(("user", "assistant")):
                s.add(Message(thread_id=thread, role=role, content=f"{role[0]}{i} " + "x" * size,
                              created_at=base + timedelta(seconds=2 * i + k)))
        await s.commit()
    return thread


async def test_long_messages_are_cut_by_size_but_four_turns_always_stay(make_runtime):
    rt = await make_runtime(lambda m: AIMessage("ok"), history_chars=1_000)
    thread = await _chat_of(rt, 10, 300)
    history = await _history(rt, thread)
    assert len(history) == 8
    assert history[0].content.startswith("u6") and history[-1].content.startswith("a9")  # starts on a question


async def test_short_banter_keeps_many_turns_up_to_the_cap(make_runtime):
    rt = await make_runtime(lambda m: AIMessage("ok"), history_turns=12)
    thread = await _chat_of(rt, 20, 10)
    history = await _history(rt, thread)
    assert len(history) == 24 and history[0].content.startswith("u8")
