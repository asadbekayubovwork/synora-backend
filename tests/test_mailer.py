"""Proves mail actually leaves over SMTP, against a throwaway local server."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncGenerator
from email import message_from_bytes
from email.message import Message

import pytest
from aiosmtpd.controller import Controller

from app.core.config import settings
from app.services import mailer


class Collector:
    """Minimal aiosmtpd handler: keeps whatever is delivered."""

    def __init__(self) -> None:
        self.messages: list[Message] = []

    async def handle_DATA(self, server, session, envelope):  # noqa: N802, ARG002
        self.messages.append(message_from_bytes(envelope.content))
        return "250 Message accepted"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
async def smtp_server(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[Collector]:
    collector = Collector()
    # `Controller.start()` proves it is up by connecting to hostname:port
    # itself, which rules out port 0 — so claim a free port first and hand it
    # over. Two runs colliding on it is far less likely than a fixed port.
    port = free_port()
    controller = Controller(collector, hostname="127.0.0.1", port=port)
    controller.start()

    monkeypatch.setattr(settings, "smtp_host", controller.hostname)
    monkeypatch.setattr(settings, "smtp_port", port)
    monkeypatch.setattr(settings, "smtp_user", None)
    monkeypatch.setattr(settings, "smtp_password", None)
    monkeypatch.setattr(settings, "smtp_starttls", False)
    monkeypatch.setattr(settings, "smtp_from", "Synora <no-reply@synora.ai>")

    try:
        yield collector
    finally:
        controller.stop()


def part(message: Message, subtype: str) -> str:
    for candidate in message.walk():
        if candidate.get_content_type() == f"text/{subtype}":
            return candidate.get_payload(decode=True).decode("utf-8")
    raise AssertionError(f"no text/{subtype} part")


async def test_the_code_is_delivered(smtp_server: Collector):
    await mailer.send_otp_email("ali@example.com", "841540", 10)

    assert len(smtp_server.messages) == 1
    sent = smtp_server.messages[0]

    assert sent["To"] == "ali@example.com"
    assert sent["From"] == "Synora <no-reply@synora.ai>"
    # The code leads the subject so it is readable from a notification.
    assert sent["Subject"].startswith("841540")


async def test_both_a_text_and_an_html_part_are_sent(smtp_server: Collector):
    await mailer.send_otp_email("ali@example.com", "841540", 10)
    sent = smtp_server.messages[0]

    assert sent.get_content_type() == "multipart/alternative"
    assert "841540" in part(sent, "plain")
    # Rendered spaced out in the HTML, so match on the digits' order.
    assert "8 4 1 5 4 0" in part(sent, "html")


async def test_deliverability_headers_are_present(smtp_server: Collector):
    await mailer.send_otp_email("ali@example.com", "841540", 10)
    sent = smtp_server.messages[0]

    # Spam filters read a missing Date or Message-ID as a bot signature.
    assert sent["Date"]
    assert sent["Message-ID"].endswith("@synora.ai>")
    assert sent["Auto-Submitted"] == "auto-generated"


async def test_a_dead_mail_server_does_not_break_the_signup(monkeypatch: pytest.MonkeyPatch):
    # Nothing is listening here; sending must log and move on, not raise.
    monkeypatch.setattr(settings, "smtp_host", "127.0.0.1")
    monkeypatch.setattr(settings, "smtp_port", 1)
    monkeypatch.setattr(settings, "smtp_starttls", False)

    await mailer.send_otp_email("ali@example.com", "841540", 10)


async def test_nothing_is_sent_when_smtp_is_unconfigured(
    smtp_server: Collector,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "smtp_host", None)

    await mailer.send_otp_email("ali@example.com", "841540", 10)

    assert smtp_server.messages == []


async def test_the_registration_flow_sends_one_email(smtp_server: Collector, client):
    response = await client.post(
        "/auth/register",
        json={"email": "ali@example.com", "password": "Str0ngPassw0rd"},
    )

    assert response.status_code == 201
    # Let the send settle before asserting on what arrived.
    await asyncio.sleep(0.1)

    assert len(smtp_server.messages) == 1
    # The mailed code is the one the API issued, not a second, unrelated one.
    assert response.json()["dev_code"] in part(smtp_server.messages[0], "plain")
