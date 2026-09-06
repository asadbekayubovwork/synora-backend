"""Email delivery.

With no `SMTP_HOST` configured the code is logged to the console instead of
sent, so the whole signup flow works locally without a mailbox.

The HTML is table-based with inline styles on purpose: Outlook renders through
Word, which ignores flexbox, grid and `<style>` blocks. Anything that would not
survive that is layered on top of a plain fallback — the gradient header sits on
a `bgcolor`, so clients that drop it still get a solid brand bar.
"""

from __future__ import annotations

import logging
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

import aiosmtplib

from app.core.config import settings

logger = logging.getLogger("synora.mailer")

BRAND = "Synora-AI"

# Brand bar. `BRAND_FALLBACK` is what non-gradient clients paint instead.
BRAND_FALLBACK = "#2f6fed"
BRAND_GRADIENT = "linear-gradient(115deg, #2f6fed 0%, #6a3fe0 55%, #b23fd0 100%)"

FONT_STACK = "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO_STACK = "'SF Mono', SFMono-Regular, ui-monospace, Menlo, Consolas, monospace"


def _sender_domain() -> str:
    """The domain the From address claims, for a Message-ID that matches it."""
    _, address = parseaddr(settings.smtp_from)
    _, _, domain = address.partition("@")
    return domain or "synora.ai"


async def _send(to: str, subject: str, text: str, html: str) -> None:
    if not settings.smtp_host:
        logger.warning("SMTP is not configured, printing the message instead.\nTo: %s\n%s", to, text)
        return

    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to
    message["Subject"] = subject

    # Spam filters treat a missing Date or Message-ID as a bot signature, and
    # neither the library nor most relays add them for you.
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=_sender_domain())

    # A transactional code is not a mailing list, but bulk classifiers look for
    # this before deciding a no-reply sender is one.
    message["Auto-Submitted"] = "auto-generated"

    message.set_content(text)
    message.add_alternative(html, subtype="html")

    try:
        await aiosmtplib.send(
            message,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_user,
            password=settings.smtp_password,
            start_tls=settings.smtp_starttls,
            timeout=15,
        )
        logger.info("Verification code sent to %s", to)
    except (aiosmtplib.SMTPException, OSError) as exc:
        # A dead mail server must not turn a valid signup into a 500 — the user
        # can resend. The code is already stored either way.
        logger.error("Could not deliver mail to %s: %s", to, exc)


def _otp_html(code: str, minutes_valid: int, heading: str, intro: str, footer: str) -> str:
    spaced_code = " ".join(code)

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>{heading}</title>
</head>
<body style="margin:0;padding:0;background-color:#f2f2f3;">
  <!-- Preview line: what the inbox list shows next to the subject. -->
  <div style="display:none;max-height:0;overflow:hidden;opacity:0;">
    Your {BRAND} code is {code}. It expires in {minutes_valid} minutes.
  </div>

  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
         style="background-color:#f2f2f3;padding:32px 16px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
               style="max-width:480px;background-color:#ffffff;border-radius:16px;overflow:hidden;">

          <tr>
            <td bgcolor="{BRAND_FALLBACK}"
                style="background-color:{BRAND_FALLBACK};background-image:{BRAND_GRADIENT};padding:28px 32px;">
              <span style="font-family:{FONT_STACK};font-size:20px;font-weight:700;
                           letter-spacing:-0.02em;color:#ffffff;">{BRAND}</span>
            </td>
          </tr>

          <tr>
            <td style="padding:32px;">
              <h1 style="margin:0 0 10px;font-family:{FONT_STACK};font-size:26px;
                         font-weight:700;letter-spacing:-0.02em;color:#0a0a0a;">
                {heading}
              </h1>

              <p style="margin:0 0 28px;font-family:{FONT_STACK};font-size:15px;
                        line-height:1.55;color:#6b6b6b;">
                {intro} It expires in {minutes_valid} minutes.
              </p>

              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td align="center" bgcolor="#0a0a0a"
                      style="background-color:#0a0a0a;border-radius:12px;padding:22px 16px;">
                    <span style="font-family:{MONO_STACK};font-size:30px;font-weight:700;
                                 letter-spacing:6px;color:#ffffff;">{spaced_code}</span>
                  </td>
                </tr>
              </table>

              <div style="height:1px;background-color:#e6e6e6;margin:28px 0 0;line-height:1px;">&nbsp;</div>

              <p style="margin:20px 0 0;font-family:{FONT_STACK};font-size:13px;
                        line-height:1.55;color:#8a8a8a;">
                {footer}
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


async def send_otp_email(to: str, code: str, minutes_valid: int) -> None:
    """The registration code."""
    # The code leads the subject so it is readable from a phone notification
    # without opening the message.
    subject = f"{code} is your {BRAND} verification code"
    footer = f"If you didn't create a {BRAND} account, you can safely ignore this email."
    text = (
        f"Your {BRAND} verification code is {code}.\n\n"
        f"It expires in {minutes_valid} minutes.\n\n{footer}"
    )
    html = _otp_html(
        code,
        minutes_valid,
        heading="Verify your email",
        intro=f"Enter this code in {BRAND} to finish creating your account.",
        footer=footer,
    )
    await _send(to, subject, text, html)


async def send_password_reset_email(to: str, code: str, minutes_valid: int) -> None:
    """The password-reset code."""
    subject = f"{code} is your {BRAND} password reset code"
    # Worth stating plainly: this mail is the only warning an account owner gets
    # that someone is trying to take the account over.
    footer = (
        "If you didn't ask to reset your password, ignore this email — "
        "your password has not changed."
    )
    text = (
        f"Your {BRAND} password reset code is {code}.\n\n"
        f"It expires in {minutes_valid} minutes.\n\n{footer}"
    )
    html = _otp_html(
        code,
        minutes_valid,
        heading="Reset your password",
        intro=f"Enter this code in {BRAND} to choose a new password.",
        footer=footer,
    )
    await _send(to, subject, text, html)
