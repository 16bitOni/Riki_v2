"""Settings you can change from the app.

The environment (.env) gives the defaults. What you change in Settings is stored in the database and wins, until you press
Reset. Only a short list of safe, useful settings is editable here; models, the database and the password stay in .env.
The values are applied to the running settings object in place, so everything that holds a reference to it sees the change
at once (no restart). Changes that need more than a new value (the sandbox tool, the name in the prompt) are applied by
``after_change``.
"""

import copy
import logging
import shutil
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, select

from .config import Effort, Price
from .db import AppSetting, utcnow

log = logging.getLogger(__name__)

EDITABLE = ("cognition", "cognition_interval_h", "quiet_hours", "daily_budget_usd", "prices", "sandbox", "default_effort",
            "timezone", "user_name")


class SettingsPatch(BaseModel):
    """What the Settings page may send. Leave a field out to keep it; send null to go back to the .env default
    (for the daily budget, 0 means no cap)."""

    model_config = ConfigDict(extra="forbid")
    cognition: bool | None = None
    cognition_interval_h: float | None = Field(None, ge=0.5, le=48)
    quiet_hours: str | None = Field(None, pattern=r"^\d{1,2}-\d{1,2}$")
    daily_budget_usd: float | None = Field(None, ge=0, le=100_000)
    prices: dict[str, Price] | None = None
    sandbox: bool | None = None
    default_effort: Effort | None = None
    timezone: str | None = None
    user_name: str | None = Field(None, min_length=1, max_length=40)

    @field_validator("timezone")
    @classmethod
    def real_timezone(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                ZoneInfo(v)
            except Exception:
                raise ValueError(f"'{v}' is not a time zone name like Asia/Kolkata")
        return v

    @field_validator("quiet_hours")
    @classmethod
    def hours_in_range(cls, v: str | None) -> str | None:
        if v is not None and any(int(x) > 24 for x in v.split("-")):
            raise ValueError("hours are 0 to 24")
        return v


def snapshot_defaults(settings) -> dict:
    """The values the environment gave, taken before any override is applied."""
    return {k: copy.deepcopy(getattr(settings, k)) for k in EDITABLE}


def _encode(key: str, value):
    if key == "prices":
        return {m: p.model_dump(exclude_none=True) for m, p in value.items()}
    return value


def _decode(key: str, value):
    if key == "prices":
        return {m: Price(**p) for m, p in (value or {}).items()}
    if key == "daily_budget_usd":
        return value or None  # 0 means "no cap"
    return value


def _apply(rt, key: str, value) -> None:
    setattr(rt.settings, key, _decode(key, value))


async def load_overrides(rt) -> list[str]:
    """At start: put what you saved in Settings over the environment's values."""
    applied = []
    async with rt.db.session() as s:
        rows = {r.key: r.value for r in (await s.scalars(select(AppSetting))).all()}
    for key in EDITABLE:
        if key in rows:
            try:
                SettingsPatch(**{key: rows[key]})  # still valid under today's rules?
                _apply(rt, key, rows[key])
                applied.append(key)
            except Exception:
                log.warning("ignoring the saved setting %r: it no longer validates", key)
    return applied


async def save_patch(rt, patch: SettingsPatch) -> list[str]:
    """Store and apply what was sent. Returns the keys that changed."""
    changed = []
    async with rt.db.session() as s:
        for key in patch.model_fields_set & set(EDITABLE):
            value = getattr(patch, key)
            if value is None:  # null: back to the environment's value
                await s.execute(delete(AppSetting).where(AppSetting.key == key))
                setattr(rt.settings, key, copy.deepcopy(rt.defaults[key]))
            else:
                stored = _encode(key, value)
                await s.merge(AppSetting(key=key, value=stored, updated_at=utcnow()))
                _apply(rt, key, stored)
            changed.append(key)
        await s.commit()
    await after_change(rt, changed)
    return changed


async def after_change(rt, keys) -> None:
    keys = set(keys)
    if "sandbox" in keys:
        rt.sync_sandbox()
    if keys & {"user_name", "sandbox"}:
        await rt.reload_persona()  # the prompt names the user and lists the tools


async def get_value(db, key: str):
    async with db.session() as s:
        row = await s.get(AppSetting, key)
    return row.value if row is not None else None


async def put_value(db, key: str, value) -> None:
    async with db.session() as s:
        await s.merge(AppSetting(key=key, value=value, updated_at=utcnow()))
        await s.commit()


async def overridden(rt) -> list[str]:
    async with rt.db.session() as s:
        return [r for r in (await s.scalars(select(AppSetting.key))).all() if r in EDITABLE]


async def view(rt) -> dict:
    """Everything the Settings page needs in one call: what is in force, what .env says, and what you changed."""
    d = rt.defaults
    return {"values": current(rt),
            "defaults": {**d, "prices": {m: p.model_dump() for m, p in d["prices"].items()}},
            "overridden": await overridden(rt),
            "docker": shutil.which("docker") is not None,
            "models": {"main": rt.settings.model, "smart": rt.settings.smart_model, "embedding": rt.settings.embedding_model}}


def current(rt) -> dict:
    s = rt.settings
    return {"cognition": s.cognition, "cognition_interval_h": s.cognition_interval_h, "quiet_hours": s.quiet_hours,
            "daily_budget_usd": s.daily_budget_usd, "sandbox": s.sandbox, "default_effort": s.default_effort,
            "timezone": s.timezone, "user_name": s.user_name,
            "prices": {m: p.model_dump() for m, p in s.prices.items()}}
