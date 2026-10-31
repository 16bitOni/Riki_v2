"""Every run records what its model calls cost, from a price list you provide. No price, no number (never a guess)."""

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.config import Price
from app.db import Run
from app.tracing import run_cost

from .test_agent import run


def span(name="m", tin=1000, tout=100, cached=0, kind="llm"):
    return {"kind": kind, "name": name, "tokens_in": tin, "tokens_out": tout, "tokens_cached": cached}


def test_cached_tokens_are_billed_at_the_cached_rate():
    prices = {"m": Price(input=2.0, cached=0.5, output=8.0)}
    cost, unpriced = run_cost([span(tin=1000, tout=100, cached=400)], prices)
    assert cost == pytest.approx((600 * 2.0 + 400 * 0.5 + 100 * 8.0) / 1e6)
    assert unpriced == []


def test_no_cached_price_means_no_discount():
    cost, _ = run_cost([span(tin=1000, tout=0, cached=400)], {"m": Price(input=2.0, output=8.0)})
    assert cost == pytest.approx(1000 * 2.0 / 1e6)


def test_a_model_without_a_price_is_named_and_the_priced_ones_still_count():
    prices = {"cheap": Price(input=1.0, output=1.0)}
    cost, unpriced = run_cost([span("cheap", 1000, 0), span("mystery", 1000, 0), span("mystery", 5, 5),
                               span("tool-x", kind="tool")], prices)
    assert cost == pytest.approx(1000 / 1e6)
    assert unpriced == ["mystery"]


def test_nothing_priced_is_none_not_zero():
    assert run_cost([span()], {}) == (None, ["m"])
    assert run_cost([], {"m": Price(input=1, output=1)}) == (None, [])


async def test_a_run_stores_its_cost(make_runtime):
    rt = await make_runtime(lambda m: AIMessage("hey!"))
    rt.settings = rt.settings.model_copy(update={"prices": {rt.settings.model: Price(input=0.4, cached=0.1, output=1.6)}})
    ctx, _ = await run(rt, "hi")  # the scripted model reports 100 tokens in, 10 out per call
    async with rt.db.session() as s:
        row = await s.scalar(select(Run).where(Run.id == ctx.run_id))
    assert row.cost_usd == pytest.approx((100 * 0.4 + 10 * 1.6) / 1e6)
    assert "unpriced_models" not in row.checks


async def test_an_unpriced_run_is_flagged_not_zeroed(make_runtime):
    rt = await make_runtime(lambda m: AIMessage("hey!"))
    ctx, _ = await run(rt, "hi")
    async with rt.db.session() as s:
        row = await s.scalar(select(Run).where(Run.id == ctx.run_id))
    assert row.cost_usd is None
    assert row.checks["unpriced_models"] == [rt.settings.model]
