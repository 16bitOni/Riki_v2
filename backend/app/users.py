"""The people who can use this Riki: the owner (from the environment) and the members the owner invites."""

import re

from sqlalchemy import select

from .db import Device, User, new_id

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def norm(email: str) -> str:
    return email.strip().lower()


async def ensure_owner(rt) -> None:
    """At start: the owner's row exists and matches RIKI_OWNER_EMAIL / RIKI_USER_NAME (the migration made a placeholder)."""
    s = rt.settings
    async with rt.db.session(system=True) as db:
        owner = await db.get(User, s.user_id)
        if owner is None:
            owner = User(id=s.user_id, email=f"{s.user_id}@local", name=s.user_name, role="owner")
            db.add(owner)
        owner.role, owner.disabled, owner.name = "owner", False, s.user_name
        if s.owner_email:
            owner.email = norm(s.owner_email)
        await db.commit()


async def by_email(rt, email: str) -> User | None:
    async with rt.db.session(system=True) as db:
        return await db.scalar(select(User).where(User.email == norm(email), User.disabled.is_(False)))


async def invite(rt, email: str, name: str) -> User:
    async with rt.db.session(system=True) as db:
        user = User(id=new_id(), email=norm(email), name=name.strip(), role="member")
        db.add(user)
        await db.commit()
        return user


async def sign_out_everywhere_for(rt, user_id: str) -> None:
    async with rt.db.session(system=True) as db:
        await db.execute(Device.__table__.delete().where(Device.user_id == user_id))
        await db.commit()
