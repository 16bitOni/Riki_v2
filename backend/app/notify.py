"""Riki saying something on its own initiative: a reminder, a nudge, a scheduled task's result.

In the app it lands in the check-in thread (Home shows it); on the phone it is a Telegram message to your one chat.
"""

import logging

from .db import Message

log = logging.getLogger(__name__)


async def notify(rt, text: str, *, run_id: str | None = None, in_app: bool = True) -> None:
    from .ops import own_thread  # ops imports this module; avoid the cycle at import time

    if in_app:
        thread = await own_thread(rt, "heartbeat", "Check-ins")
        async with rt.db.session() as s:
            s.add(Message(thread_id=thread, role="assistant", content=text, run_id=run_id))
            await s.commit()
    tg = getattr(rt, "telegram", None)
    if tg is not None and tg.chat_id is not None:
        try:
            from .channels.telegram import telegram_thread

            async with rt.db.session() as s:  # the Telegram chat in the app keeps what was sent to the phone
                s.add(Message(thread_id=await telegram_thread(rt), role="assistant", content=text, run_id=run_id))
                await s.commit()
            await tg.notify(text)
        except Exception:
            log.exception("could not send the message to Telegram")
