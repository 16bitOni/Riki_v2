"""Sends the sign-in code. Standard library only: one SMTP connection per code, always encrypted."""

import asyncio
import smtplib
import ssl
from email.message import EmailMessage


def _send(s, to: str, code: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = f"{code} is your Riki sign-in code"
    msg["From"] = s.smtp_from or s.smtp_user
    msg["To"] = to
    msg.set_content(f"Your Riki sign-in code is {code}\n\nIt works once, for 10 minutes. If you didn't ask for it, "
                    "ignore this email: nobody can sign in without the code.")
    ctx = ssl.create_default_context()
    implicit = s.smtp_port == 465
    with (smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=20, context=ctx) if implicit
          else smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20)) as smtp:
        if not implicit:
            smtp.starttls(context=ctx)
        if s.smtp_user and s.smtp_password:
            smtp.login(s.smtp_user, s.smtp_password.get_secret_value())
        smtp.send_message(msg)


async def send_code(s, to: str, code: str) -> None:
    await asyncio.to_thread(_send, s, to, code)
