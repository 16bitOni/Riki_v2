"""Whose request this is. A ContextVar, so a background run started from a request is still that person's run.

Code with no signed-in person (the heartbeat, Telegram, schedules) runs as the owner: see db.Database.session.
"""

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class Who:
    id: str
    role: str  # owner | member
    name: str

    @property
    def member(self) -> bool:
        return self.role != "owner"


_who: ContextVar[Who | None] = ContextVar("riki_who", default=None)


def set_who(who: Who | None):
    return _who.set(who)


def reset_who(token) -> None:
    try:
        _who.reset(token)
    except ValueError:  # reset from another context (a streaming response): the request is over anyway
        _who.set(None)


def who() -> Who | None:
    return _who.get()
