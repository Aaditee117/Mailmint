"""Gmail SMTP delivery.

One message per recipient, always. There is no code path here that puts two
recipients on the same message - a visible CC list ends a referral conversation
before it starts.
"""

from __future__ import annotations

import mimetypes
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

from .config import Config, Credentials


class SendError(RuntimeError):
    pass


def build_message(
    config: Config,
    creds: Credentials,
    to_address: str,
    subject: str,
    body: str,
    attachment: Path | None = None,
) -> EmailMessage:
    """Compose a plain-text message with an optional resume attachment.

    Plain text on purpose: a one-to-one note from a person does not arrive as
    HTML, and HTML-only mail scores worse with corporate spam filters.
    """
    message = EmailMessage()
    message["From"] = formataddr((config.sender_name or None, creds.user))
    message["To"] = to_address
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    # Our own Message-ID gives reconcile an exact handle on the message. Gmail
    # may replace it, which is why bounce matching falls back to the address.
    message["Message-ID"] = make_msgid(domain=creds.user.split("@", 1)[1])
    message["Reply-To"] = config.reply_to or creds.user
    message.set_content(body)

    if attachment:
        if not attachment.exists():
            raise SendError(f"attachment not found: {attachment}")
        guessed, _ = mimetypes.guess_type(attachment.name)
        maintype, _, subtype = (guessed or "application/octet-stream").partition("/")
        message.add_attachment(
            attachment.read_bytes(),
            maintype=maintype,
            subtype=subtype or "octet-stream",
            filename=attachment.name,
        )
    return message


class GmailSender:
    """Context manager holding one authenticated SMTP session for the whole run."""

    def __init__(self, config: Config, creds: Credentials):
        self.config = config
        self.creds = creds
        self._smtp: smtplib.SMTP | None = None

    def __enter__(self) -> "GmailSender":
        try:
            smtp = smtplib.SMTP(self.config.smtp_host, self.config.smtp_port, timeout=30)
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            try:
                smtp.login(self.creds.user, self.creds.app_password)
            except smtplib.SMTPServerDisconnected as exc:
                # smtplib retries AUTH LOGIN after Gmail's 535 on AUTH PLAIN, and
                # Gmail hangs up on that retry - so a dropped login is a rejection.
                raise smtplib.SMTPAuthenticationError(
                    535, b"connection dropped during login (Gmail 535 bad credentials)"
                ) from exc
        except smtplib.SMTPAuthenticationError as exc:
            raise SendError(
                "Gmail rejected the login. Check that GMAIL_USER is correct and "
                "that GMAIL_APP_PASSWORD is a 16-character App Password from "
                "https://myaccount.google.com/apppasswords (2-Step Verification "
                f"must be on). Server said: {exc.smtp_error!r}"
            ) from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise SendError(f"could not connect to {self.config.smtp_host}: {exc}") from exc
        self._smtp = smtp
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._smtp is not None:
            try:
                self._smtp.quit()
            except smtplib.SMTPException:
                pass
            self._smtp = None

    def send(self, message: EmailMessage) -> str:
        """Send one message, returning its Message-ID. Raises SendError on failure."""
        if self._smtp is None:
            raise SendError("sender is not connected")
        try:
            self._smtp.send_message(message)
        except smtplib.SMTPRecipientsRefused as exc:
            refused = "; ".join(f"{a}: {c} {m}" for a, (c, m) in exc.recipients.items())
            raise SendError(f"recipient refused at send time - {refused}") from exc
        except smtplib.SMTPSenderRefused as exc:
            raise SendError(f"sender refused: {exc.smtp_code} {exc.smtp_error!r}") from exc
        except smtplib.SMTPDataError as exc:
            hint = ""
            if exc.smtp_code in (421, 450, 452, 550) and b"limit" in (exc.smtp_error or b"").lower():
                hint = " - this looks like Gmail's daily sending limit; stop and resume tomorrow"
            raise SendError(f"Gmail refused the message: {exc.smtp_code} {exc.smtp_error!r}{hint}") from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise SendError(f"send failed: {exc}") from exc
        return message["Message-ID"] or ""
