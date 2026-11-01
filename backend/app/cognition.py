"""Riki thinking about you between chats: a living read on you (reflection), lessons from how chats went, and a
thinking cycle that may end in one specific message (a nudge).

Kept small on purpose. The code decides *whether* anything is worth a model call (new input, quiet hours, the budget,
backoff when you ignore nudges); the models only write. A cycle with nothing new costs nothing.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from riki_memory import MemoryCreate
from sqlalchemy import func, select, text, update

from .db import Message, Nudge, Reflection, Run, Schedule, Thread, utcnow
from .jobs import call, job_run
from .notify import notify
from .spend import budget_state

log = logging.getLogger(__name__)

REFLECTION_PROMPT = """You are {agent}, {name}'s personal agent. Below are the memories you hold about {name}.
Write a private, honest synthesis: your current read on this person. It is not shown to anyone but {name}, on request.
Write like a thoughtful friend making private notes, not like a report. Under 400 words, in exactly this shape:

**Right now:** 2-3 sentences on what seems to occupy their mind lately.
**Open loops:** specific things they mentioned but never resolved: commitments, worries, plans that went quiet. Name the
thing. If nothing stands out, write "Nothing obvious."
**Questions I'm sitting with:** 1-3 specific questions you are genuinely curious about, e.g. "Did the Pune trip get
rebooked?" Not generic. If none, write "None right now."
**Patterns:** behaviour you have noticed, 2-3 sentences.

Rules: only what the memories show; real names, dates and places; if memories are sparse, say so in the section.
{previous}"""

LESSONS_PROMPT = """You are {agent}, {name}'s personal agent. Read this conversation and write what you should do differently
or keep doing the next time, as at most 3 short lessons about how {name} likes to be talked to and helped.
Only specific, earned lessons: a correction they made, something that clearly landed, a preference they stated about
style, length or approach. Write each as one sentence about {name} ("{name} wants ...", "{name} dislikes ...").
No facts about their life (those are saved separately), no generic advice. If there is nothing, return an empty list."""

THINK_PROMPT = """You are {agent}, thinking privately about {name} between conversations. Nobody reads this. It is {now}.
You get your last reflection, your recent private thoughts, and what is new since you last thought. Write:
1. monologue: 80-150 words, first person. Build on your earlier thoughts, note what changed, what you are unsure about.
2. reflection: the updated read on {name} in the same four parts as before (Right now / Open loops / Questions I'm sitting
with / Patterns), under 400 words, only what the sources show. If the previous reflection was edited by {name}, their
edits are ground truth: keep them."""

ACT_PROMPT = """You are {agent}, {name}'s personal agent. You just finished thinking about {name}. It is {now}.
Decide whether to send ONE message to {name} right now. Most of the time the answer is NO.
Send only if there is something specific: an open loop that has gone quiet, a deadline or reminder coming, a pattern worth
naming. Write 1-2 sentences, casual and specific, like a friend who remembered. Never generic ("how are you?", "just
checking in"), never a topic in the recent nudges, never work pressure late in the evening.
Set send=false if nothing specific, the time is wrong, or it can wait."""


class Lessons(BaseModel):
    lessons: list[str] = Field(default_factory=list, max_length=3)


class Think(BaseModel):
    monologue: str
    reflection: str


class Act(BaseModel):
    send: bool = False
    text: str = ""
    topic: str = ""
    kind: Literal["open_loop", "curiosity", "checkin", "deadline"] = "open_loop"


def _when(m) -> str:
    return (getattr(m, "event_at", None) or m.created_at).strftime("%Y-%m-%d")


def memory_lines(memories) -> str:
    return "\n".join(f"- [{getattr(m, 'kind', 'fact')}, {_when(m)}] {m.content}" for m in memories) or "(none yet)"


# --- reflection ---------------------------------------------------------------------------------------------

async def latest_reflection(rt) -> Reflection | None:
    async with rt.db.session() as s:
        return await s.scalar(select(Reflection).order_by(Reflection.created_at.desc()).limit(1))


async def reflection_block(rt) -> str:
    """The latest read on you, for the prompt. Empty until there is one."""
    r = await latest_reflection(rt)
    if r is None or not r.content.strip():
        return ""
    return f"## Riki's current read on {rt.settings.user_name} (private notes, use them like a friend who pays attention)\n{r.content.strip()[:1800]}"


def previous_note(last: Reflection | None) -> str:
    if last is None:
        return ""
    edited = " It was EDITED BY THE USER: their wording is ground truth, keep what they corrected." if last.source == "edit" else ""
    return f"\nYour previous version:{edited}\n{last.content}"


async def refresh_reflection(rt, *, force: bool = False) -> bool:
    """Write a new reflection when enough has changed (10 new memories, or the first one at 3). Returns whether it did."""
    memories = await rt.memory.list_memories(rt.settings.user_id, limit=200)
    last = await latest_reflection(rt)
    new = sum(1 for m in memories if last is None or m.created_at > last.created_at)
    if not force and (len(memories) < 3 or (last is not None and new < 10)):
        return False

    async def work(ctx):
        body = await call(rt, "smart", REFLECTION_PROMPT.format(agent=rt.persona.name, name=rt.settings.user_name,
                                                                  previous=previous_note(last)),
                          f"Memories ({len(memories)}):\n{memory_lines(memories)}", purpose="reflection")
        async with rt.db.session() as s:
            s.add(Reflection(content=body.strip(), memory_count=len(memories), source="auto", run_id=ctx.run_id))
            await s.commit()
        return "reflection updated"

    _, result = await job_run(rt, "reflection", "update the read on you", work)
    return result is not None


async def save_edit(rt, content: str) -> Reflection:
    """You corrected the read: your wording becomes the newest version and is treated as ground truth next time."""
    async with rt.db.session() as s:
        last = await s.scalar(select(Reflection).order_by(Reflection.created_at.desc()).limit(1))
        r = Reflection(content=content.strip(), monologue=last.monologue if last else "", source="edit",
                       memory_count=last.memory_count if last else 0)
        s.add(r)
        await s.commit()
    return r


# --- lessons ------------------------------------------------------------------------------------------------

_LESSON_THREADS = text("""
    select t.id from threads t
    where t.kind in ('main', 'side')
      and (select max(created_at) from messages where thread_id = t.id) < now() - interval '6 hours'
      and (select count(*) from messages m where m.thread_id = t.id
           and m.created_at > coalesce(t.lessons_at, 'epoch')) >= 6
    order by t.created_at limit 2""")


async def learn_lessons(rt) -> int:
    """Chats that went quiet: draw up to 3 lessons about how you like to be helped, saved as preference memories."""
    async with rt.db.session() as s:
        ids = [r[0] for r in (await s.execute(_LESSON_THREADS)).all()]
    saved = 0
    for thread_id in ids:
        async with rt.db.session() as s:
            t = await s.get(Thread, thread_id)
            since = t.lessons_at or datetime(1970, 1, 1, tzinfo=timezone.utc)
            msgs = (await s.scalars(select(Message).where(Message.thread_id == thread_id, Message.created_at > since)
                                    .order_by(Message.created_at.desc()).limit(40))).all()
        convo = "\n".join(f"{rt.settings.user_name if m.role == 'user' else rt.persona.name}: {m.content[:600]}"
                          for m in reversed(msgs))

        async def work(ctx, convo=convo, thread_id=thread_id):
            out = await call(rt, "fast", LESSONS_PROMPT.format(agent=rt.persona.name, name=rt.settings.user_name),
                             convo, schema=Lessons, purpose="lessons")
            n = 0
            for lesson in out.lessons[:3]:
                if len(lesson.strip()) >= 10:
                    await rt.memory.save(MemoryCreate(user_id=rt.settings.user_id, content=lesson.strip()[:300],
                                                      kind="preference", importance=3, source_key=f"lesson/{thread_id}"))
                    n += 1
            return n

        _, n = await job_run(rt, "lessons", f"lessons from chat {thread_id[:8]}", work)
        saved += n or 0
        async with rt.db.session() as s:  # also when nothing was learned: do not read the same messages again
            await s.execute(update(Thread).where(Thread.id == thread_id).values(lessons_at=utcnow()))
            await s.commit()
    return saved


async def feedback_lesson(rt, run_id: str, comment: str) -> None:
    """A thumbs-down with a comment is the clearest lesson there is."""
    comment = comment.strip()
    if len(comment) >= 4:
        await rt.memory.save(MemoryCreate(user_id=rt.settings.user_id, kind="preference", importance=4,
                                          content=f"{rt.settings.user_name} marked a reply as bad: {comment[:280]}",
                                          source_key=f"feedback/{run_id}"))


# --- nudges -------------------------------------------------------------------------------------------------

NUDGE_MIN_GAP_H = 24
QUIET_AFTER_CHAT_H = 3
TOPIC_COOLDOWN_D = 7
REACTION_WINDOW_H = 12


def ignored_streak(nudges: list[Nudge]) -> int:
    """How many of the newest nudges in a row were ignored (a reply or a still-open nudge ends the streak)."""
    n = 0
    for x in sorted(nudges, key=lambda x: x.sent_at, reverse=True):
        if x.reaction != "ignored":
            break
        n += 1
    return n


def nudge_allowed(now: datetime, tz: str, quiet: str, nudges: list[Nudge], last_user_at: datetime | None,
                  topic: str) -> tuple[bool, str]:
    """The guards that live in code, not in the prompt. Returns (ok, why not)."""
    from .ops import in_quiet_hours

    if in_quiet_hours(now.astimezone(ZoneInfo(tz)).hour, quiet):
        return False, "quiet hours"
    if last_user_at and now - last_user_at < timedelta(hours=QUIET_AFTER_CHAT_H):
        return False, "you were just chatting"
    newest = max((n.sent_at for n in nudges), default=None)
    gap = NUDGE_MIN_GAP_H * 2 ** (ignored_streak(nudges) // 3)  # three ignored in a row: leave twice as long
    if newest and now - newest < timedelta(hours=gap):
        return False, f"last nudge was less than {gap}h ago"
    t = topic.strip().lower()
    for n in nudges:
        old = n.topic.strip().lower()
        if now - n.sent_at < timedelta(days=TOPIC_COOLDOWN_D) and t and (t in old or old in t):
            return False, "already nudged about that this week"
    return True, ""


async def mark_reactions(rt) -> None:
    """A nudge you answered within 12 hours was a hit; one you did not was ignored (this teaches it to back off)."""
    now = utcnow()
    async with rt.db.session() as s:
        old = (await s.scalars(select(Nudge).where(Nudge.reaction.is_(None),
                                                   Nudge.sent_at < now - timedelta(hours=REACTION_WINDOW_H)))).all()
        for n in old:
            replied = await s.scalar(select(Message.id).where(
                Message.role == "user", Message.created_at > n.sent_at,
                Message.created_at < n.sent_at + timedelta(hours=REACTION_WINDOW_H)).limit(1))
            n.reaction = "replied" if replied else "ignored"
        await s.commit()


async def _last_cycle_at(rt) -> datetime | None:
    async with rt.db.session() as s:
        return await s.scalar(select(func.max(Run.created_at)).where(Run.source == "cognition"))


async def run_cycle(rt, *, force: bool = False) -> str:
    """One thinking cycle. Returns what happened, in a few words (it is also the run's output)."""
    from .ops import in_quiet_hours

    st = rt.settings
    now = utcnow()
    if not st.cognition and not force:
        return "off"
    if not force:
        if in_quiet_hours(now.astimezone(ZoneInfo(st.timezone)).hour, st.quiet_hours):
            return "quiet hours"
        if await budget_state(rt) != "ok":
            return "budget"
        last_cycle = await _last_cycle_at(rt)
        if last_cycle and now - last_cycle < timedelta(hours=st.cognition_interval_h):
            return "too soon"
    else:
        last_cycle = await _last_cycle_at(rt)

    since = last_cycle or now - timedelta(days=2)
    memories = await rt.memory.list_memories(st.user_id, limit=100)
    fresh = [m for m in memories if m.created_at > since]
    async with rt.db.session() as s:
        recent = (await s.scalars(select(Message).where(Message.role == "user", Message.created_at > since)
                                  .order_by(Message.created_at.desc()).limit(12))).all()
        soon = (await s.scalars(select(Schedule).where(Schedule.status == "active", Schedule.next_at < now + timedelta(hours=48))
                                .order_by(Schedule.next_at).limit(10))).all()
    last = await latest_reflection(rt)
    if not (fresh or recent or soon) and last is not None and not force:
        return "nothing new"  # no model call at all

    await mark_reactions(rt)
    async with rt.db.session() as s:
        nudges = (await s.scalars(select(Nudge).where(Nudge.sent_at > now - timedelta(days=30))
                                  .order_by(Nudge.sent_at.desc()))).all()
        thoughts = (await s.scalars(select(Reflection).where(Reflection.monologue != "")
                                    .order_by(Reflection.created_at.desc()).limit(3))).all()
        last_user_at = await s.scalar(select(func.max(Message.created_at)).where(Message.role == "user"))
    local_now = now.astimezone(ZoneInfo(st.timezone)).strftime("%A %d %B %Y, %H:%M")

    async def work(ctx):
        think = await call(
            rt, "smart", THINK_PROMPT.format(agent=rt.persona.name, name=st.user_name, now=local_now),
            f"## Your last reflection\n{(last.content if last else '(none yet)')}{previous_note(last) if last and last.source == 'edit' else ''}\n\n"
            "## Your recent private thoughts\n" + ("\n\n".join(t.monologue for t in reversed(thoughts)) or "(none yet)") +
            f"\n\n## New memories since you last thought ({len(fresh)})\n{memory_lines(fresh)}\n\n"
            "## What they said in chats since\n" + ("\n".join(f"- {m.content[:200]}" for m in reversed(recent)) or "(nothing)") +
            "\n\n## Coming up\n" + ("\n".join(f"- {s.next_at.astimezone(ZoneInfo(st.timezone)):%a %d %b %H:%M}: {s.text[:120]}" for s in soon) or "(nothing scheduled)"),
            schema=Think, purpose="think")
        async with rt.db.session() as s:
            s.add(Reflection(content=think.reflection.strip(), monologue=think.monologue.strip(), source="cycle",
                             memory_count=len(memories), run_id=ctx.run_id))
            await s.commit()
        act = await call(
            rt, "fast", ACT_PROMPT.format(agent=rt.persona.name, name=st.user_name, now=local_now),
            f"## Your reflection\n{think.reflection}\n\n## Recent nudges (never repeat these topics)\n" +
            ("\n".join(f"- {n.sent_at:%d %b}: [{n.reaction or 'open'}] {n.topic}" for n in nudges[:10]) or "(none)") +
            "\n\n## Coming up\n" + ("\n".join(f"- {s.next_at.astimezone(ZoneInfo(st.timezone)):%a %d %b %H:%M}: {s.text[:120]}" for s in soon) or "(nothing)"),
            schema=Act, purpose="act")
        if not act.send or len(act.text.strip()) < 5:
            return "thought, nothing to say"
        ok, why = nudge_allowed(now, st.timezone, st.quiet_hours, list(nudges), last_user_at, act.topic or act.text[:40])
        if not ok and not force:
            return f"wanted to say something, held back: {why}"
        async with rt.db.session() as s:
            s.add(Nudge(topic=(act.topic or act.text[:40])[:120], text=act.text.strip(), kind=act.kind, run_id=ctx.run_id))
            await s.commit()
        await notify(rt, act.text.strip(), run_id=ctx.run_id)
        return "nudge sent"

    _, result = await job_run(rt, "cognition", "thinking cycle", work)
    return result or "failed"
