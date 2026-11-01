"""Sign in with an emailed one-time code, and be remembered on that browser."""

import httpx
import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import delete, select, update

from app import mailer
from app.config import Settings
from app.db import Device, User, utcnow
from app.main import app

OWNER = "owner@example.com"
EMAIL = {"owner_email": OWNER, "smtp_host": "smtp.example.com", "smtp_user": "riki@example.com"}


@pytest.fixture
def inbox(monkeypatch):
    """Every code the app tried to email, instead of sending it."""
    sent: list[tuple[str, str]] = []

    async def send_code(_settings, to, code):
        sent.append((to, code))

    monkeypatch.setattr(mailer, "send_code", send_code)
    return sent


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_):
        pass

    monkeypatch.setattr("app.main.asyncio.sleep", instant)  # the 1 s slow-down on a wrong guess


async def make_client(make_runtime):
    app.state.rt = await make_runtime(lambda messages: AIMessage("ok"), **EMAIL)
    async with app.state.rt.db.session(system=True) as s:  # the test database outlives a test: no codes, devices or members
        await s.execute(update(User).values(login=None))
        await s.execute(delete(Device))
        await s.execute(delete(User).where(User.role == "member"))
        await s.commit()
    return app.state.rt, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def ask_code(client, inbox, email=OWNER):
    assert (await client.post("/api/login/code", json={"email": email})).status_code == 200
    return inbox[-1][1] if inbox else None


async def test_half_a_setup_is_refused_at_start():
    for half in ({"owner_email": OWNER, "smtp_host": None},
                 {"owner_email": None, "smtp_host": "smtp.example.com", "smtp_user": "a@b.c"}):
        with pytest.raises(ValueError, match="go together"):
            Settings(password="correct horse battery", **half)  # explicit: a real .env must not supply the other half


async def test_right_code_signs_in_and_the_device_is_remembered(make_runtime, inbox):
    rt, client = await make_client(make_runtime)
    async with client:
        assert (await client.get("/api/login/mode")).json() == {"email": True}
        assert (await client.get("/api/me")).status_code == 401
        code = await ask_code(client, inbox)
        assert inbox == [(OWNER, code)] and len(code) == 6
        assert (await client.post("/api/login/verify", json={"email": OWNER.upper(), "code": code})).status_code == 200
        me = (await client.get("/api/me")).json()
        assert me["email_login"] is True
        async with rt.db.session(system=True) as s:  # only a hash of the cookie is stored
            ids = (await s.scalars(select(Device.id))).all()
        assert len(ids) == 1 and client.cookies["riki_session"] not in ids

        # a code works once
        again = await client.post("/api/login/verify", json={"email": OWNER, "code": code})
        assert again.status_code == 401


async def test_nothing_is_sent_to_anyone_else_and_the_answer_is_the_same(make_runtime, inbox):
    _, client = await make_client(make_runtime)
    async with client:
        stranger = await client.post("/api/login/code", json={"email": "stranger@example.com"})
        owner = await client.post("/api/login/code", json={"email": OWNER})
        assert stranger.status_code == owner.status_code == 200 and stranger.json() == owner.json()
        assert [to for to, _ in inbox] == [OWNER]


async def test_wrong_codes_burn_the_real_one(make_runtime, inbox):
    _, client = await make_client(make_runtime)
    async with client:
        code = await ask_code(client, inbox)
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(5):
            assert (await client.post("/api/login/verify", json={"email": OWNER, "code": wrong})).status_code == 401
        # five misses and the right code no longer works either
        assert (await client.post("/api/login/verify", json={"email": OWNER, "code": code})).status_code == 401
        assert (await client.get("/api/me")).status_code == 401


async def test_an_expired_code_does_not_work(make_runtime, inbox):
    rt, client = await make_client(make_runtime)
    async with client:
        code = await ask_code(client, inbox)
        async with rt.db.session(system=True) as s:
            owner = await s.get(User, rt.settings.user_id)
            owner.login = {**owner.login, "expires": "2020-01-01T00:00:00+00:00"}
            await s.commit()
        assert (await client.post("/api/login/verify", json={"email": OWNER, "code": code})).status_code == 401


async def test_codes_are_rate_limited(make_runtime, inbox):
    _, client = await make_client(make_runtime)
    async with client:
        await ask_code(client, inbox)
        assert (await client.post("/api/login/code", json={"email": OWNER})).status_code == 429  # too soon
        assert len(inbox) == 1


async def test_a_malformed_code_is_rejected_before_it_is_checked(make_runtime, inbox):
    _, client = await make_client(make_runtime)
    async with client:
        for bad in ("12345", "1234567", "abcdef", "12 456"):
            assert (await client.post("/api/login/verify", json={"email": OWNER, "code": bad})).status_code == 422


async def test_password_login_is_off_in_email_mode(make_runtime, inbox):
    _, client = await make_client(make_runtime)
    async with client:
        assert (await client.post("/api/login", json={"password": "correct horse battery"})).status_code == 404


async def sign_in(rt, inbox):
    """A new browser, signed in. The 30 s gap between codes is waived so a test can sign in twice."""
    browser = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    async with rt.db.session(system=True) as s:
        await s.execute(update(User).where(User.id == rt.settings.user_id).values(login=None))
        await s.commit()
    await browser.post("/api/login/code", json={"email": OWNER})
    assert (await browser.post("/api/login/verify", json={"email": OWNER, "code": inbox[-1][1]})).status_code == 200
    return browser


async def test_logout_forgets_this_device(make_runtime, inbox):
    rt, _ = await make_client(make_runtime)
    browser = await sign_in(rt, inbox)
    token = browser.cookies["riki_session"]
    assert (await browser.post("/api/logout")).status_code == 200
    browser.cookies.set("riki_session", token)  # a copied cookie is dead on the server too
    assert (await browser.get("/api/me")).status_code == 401
    await browser.aclose()


async def test_sign_out_everywhere_forgets_every_browser(make_runtime, inbox):
    rt, _ = await make_client(make_runtime)
    phone, laptop = await sign_in(rt, inbox), await sign_in(rt, inbox)
    assert (await phone.get("/api/me")).status_code == 200 and (await laptop.get("/api/me")).status_code == 200
    assert (await laptop.post("/api/logout/all")).status_code == 200
    assert (await phone.get("/api/me")).status_code == 401
    async with rt.db.session(system=True) as s:
        assert (await s.scalars(select(Device.id))).all() == []
    await phone.aclose()
    await laptop.aclose()


async def test_a_device_idle_for_90_days_has_to_sign_in_again(make_runtime, inbox):
    rt, client = await make_client(make_runtime)
    async with client:
        code = await ask_code(client, inbox)
        await client.post("/api/login/verify", json={"email": OWNER, "code": code})
        assert (await client.get("/api/me")).status_code == 200
        async with rt.db.session(system=True) as s:
            await s.execute(update(Device).values(last_seen_at=utcnow().replace(year=2020)))
            await s.commit()
        assert (await client.get("/api/me")).status_code == 401
