"""IMAP bounce reconciliation.

With DNS-only verification, the bounce log IS the verifier - it just runs after
the send instead of before it. This module reads delivery-status notifications
out of the sending account's inbox and reports which guessed addresses failed.

Requires IMAP to be enabled on the Gmail account
(Settings -> Forwarding and POP/IMAP -> Enable IMAP).
"""

from __future__ import annotations

import email
import email.header
import imaplib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import parseaddr

from .config import Config, Credentials

_ADDRESS_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

_BOUNCE_SENDERS = ("mailer-daemon", "postmaster")
_BOUNCE_SUBJECTS = (
    "Delivery Status Notification",
    "Undelivered Mail Returned to Sender",
    "Undeliverable",
    "Address not found",
    "Delivery incomplete",
    "failure notice",
)


class ReconcileError(RuntimeError):
    pass


@dataclass
class Bounce:
    address: str
    status_code: str
    permanent: bool
    subject: str
    detail: str

    @property
    def kind(self) -> str:
        return "permanent" if self.permanent else "temporary"


def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = email.header.decode_header(value)
    out = []
    for text, charset in parts:
        if isinstance(text, bytes):
            out.append(text.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out).strip()


def _plain_text(message: Message) -> str:
    chunks: list[str] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_content_type() in ("text/plain", "message/delivery-status", "message/rfc822"):
            try:
                payload = part.get_payload(decode=True)
            except (LookupError, ValueError):
                continue
            if payload:
                charset = part.get_content_charset() or "utf-8"
                chunks.append(payload.decode(charset, errors="replace"))
            elif part.get_content_type() == "message/delivery-status":
                # delivery-status parts are structured, not encoded payloads.
                for sub in part.get_payload():
                    if isinstance(sub, Message):
                        chunks.append(str(sub))
    return "\n".join(chunks)


def parse_bounce(message: Message) -> list[Bounce]:
    """Pull failed recipients out of one delivery-status notification."""
    subject = _decode(message.get("Subject"))
    text = _plain_text(message)
    results: list[Bounce] = []

    # Preferred path: the machine-readable DSN block (RFC 3464).
    blocks = re.split(r"\n\s*\n", text)
    for block in blocks:
        if "final-recipient" not in block.lower() and "original-recipient" not in block.lower():
            continue
        recipient = re.search(
            r"(?:Final|Original)-Recipient:\s*rfc822;\s*([^\s;]+)", block, re.I
        )
        if not recipient:
            continue
        address = recipient.group(1).strip().strip("<>").lower()
        status = re.search(r"Status:\s*([245]\.\d+\.\d+)", block, re.I)
        action = re.search(r"Action:\s*(\w+)", block, re.I)
        code = status.group(1) if status else ""
        act = (action.group(1) if action else "").lower()

        permanent = code.startswith("5") or act == "failed"
        if act in ("delayed", "delivered", "relayed", "expanded") and not code.startswith("5"):
            permanent = False
        if act == "delivered":
            continue

        diagnostic = re.search(r"Diagnostic-Code:\s*(.+)", block, re.I)
        detail = (diagnostic.group(1).strip() if diagnostic else subject)[:300]
        results.append(Bounce(address, code, permanent, subject, detail))

    if results:
        return results

    # Fallback: no parseable DSN block, so scrape addresses out of the body and
    # let the caller match them against the sent log.
    if any(s.lower() in subject.lower() for s in _BOUNCE_SUBJECTS):
        permanent = "not found" in subject.lower() or "undeliver" in subject.lower()
        seen: set[str] = set()
        for match in _ADDRESS_RE.findall(text):
            address = match.lower()
            if address in seen or "mailer-daemon" in address or "postmaster" in address:
                continue
            seen.add(address)
            results.append(Bounce(address, "", permanent, subject, subject[:300]))
    return results


# --------------------------------------------------------------------------
# Replies
# --------------------------------------------------------------------------

# RFC 3834 and the de-facto headers vendors actually send.
_AUTO_HEADERS = {
    "auto-submitted": lambda v: v.lower().startswith("auto-"),
    "x-autoreply": lambda v: True,
    "x-autorespond": lambda v: True,
    "x-auto-response-suppress": lambda v: True,
    "precedence": lambda v: v.lower() in ("auto_reply", "bulk", "junk"),
}

_AUTO_SUBJECTS = (
    "automatic reply", "auto reply", "auto:", "out of office", "out-of-office",
    "away from", "autoreply", "vacation", "on leave", "maternity", "paternity",
)

_REPLY_HEADERS = (
    "FROM SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES "
    "AUTO-SUBMITTED X-AUTOREPLY X-AUTORESPOND X-AUTO-RESPONSE-SUPPRESS PRECEDENCE"
)


@dataclass
class Reply:
    address: str
    subject: str
    date: str
    auto: bool

    @property
    def kind(self) -> str:
        return "auto_reply" if self.auto else "reply"


def classify_reply(headers: Message) -> bool:
    """True when this looks machine-generated rather than typed by a person."""
    for name, predicate in _AUTO_HEADERS.items():
        value = headers.get(name)
        if value and predicate(str(value).strip()):
            return True
    subject = _decode(headers.get("Subject")).lower()
    return any(marker in subject for marker in _AUTO_SUBJECTS)


def fetch_replies(
    config: Config,
    creds: Credentials,
    known_addresses: set[str],
    known_message_ids: set[str],
    lookback_days: int | None = None,
) -> list[Reply]:
    """Find inbox messages that answer something we sent.

    Matched two ways: the sender is an address we mailed, or the message
    threads onto one of our Message-IDs. Both are needed - Gmail sometimes
    rewrites the Message-ID we set, and people sometimes reply from a
    different address than the one we guessed.
    """
    days = lookback_days if lookback_days is not None else config.lookback_days
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
    wanted = {a.lower() for a in known_addresses if a}
    threads = {m.strip() for m in known_message_ids if m.strip()}
    if not wanted and not threads:
        return []

    try:
        imap = imaplib.IMAP4_SSL(config.imap_host, timeout=30)
    except OSError as exc:
        raise ReconcileError(f"could not connect to {config.imap_host}: {exc}") from exc

    try:
        try:
            imap.login(creds.user, creds.app_password)
        except imaplib.IMAP4.error as exc:
            raise ReconcileError(f"Gmail rejected the IMAP login: {exc}") from exc

        status, _ = imap.select("INBOX", readonly=True)
        if status != "OK":
            raise ReconcileError("could not open INBOX")

        status, data = imap.search(None, f"(SINCE {since})")
        if status != "OK" or not data or not data[0]:
            return []
        message_ids = data[0].split()

        replies: list[Reply] = []
        seen: set[str] = set()
        # Headers only, in batches - downloading whole bodies for an inbox-wide
        # scan would be slow and buys nothing.
        for start in range(0, len(message_ids), 200):
            batch = b",".join(message_ids[start:start + 200]).decode()
            status, data = imap.fetch(batch, f"(BODY.PEEK[HEADER.FIELDS ({_REPLY_HEADERS})])")
            if status != "OK" or not data:
                continue
            for item in data:
                if not (isinstance(item, tuple) and item[1]):
                    continue
                headers = email.message_from_bytes(item[1])
                sender = parseaddr(_decode(headers.get("From")))[1].lower()

                refs = f"{headers.get('In-Reply-To', '')} {headers.get('References', '')}"
                threaded = any(mid in refs for mid in threads) if threads else False
                if sender not in wanted and not threaded:
                    continue
                if sender in seen:
                    continue
                seen.add(sender)
                replies.append(Reply(
                    address=sender,
                    subject=_decode(headers.get("Subject"))[:200],
                    date=_decode(headers.get("Date"))[:64],
                    auto=classify_reply(headers),
                ))
        return replies
    finally:
        try:
            imap.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def fetch_bounces(config: Config, creds: Credentials, lookback_days: int | None = None) -> list[Bounce]:
    """Scan the sending account's inbox for delivery failures."""
    days = lookback_days if lookback_days is not None else config.lookback_days
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")

    try:
        imap = imaplib.IMAP4_SSL(config.imap_host, timeout=30)
    except OSError as exc:
        raise ReconcileError(f"could not connect to {config.imap_host}: {exc}") from exc

    try:
        try:
            imap.login(creds.user, creds.app_password)
        except imaplib.IMAP4.error as exc:
            raise ReconcileError(
                "Gmail rejected the IMAP login. The App Password is the same one "
                "used for SMTP, but IMAP must also be switched on in Gmail: "
                f"Settings -> Forwarding and POP/IMAP -> Enable IMAP. ({exc})"
            ) from exc

        status, _ = imap.select("INBOX", readonly=True)
        if status != "OK":
            raise ReconcileError("could not open INBOX")

        message_ids: set[bytes] = set()
        queries = [f'(SINCE {since} FROM "{s}")' for s in _BOUNCE_SENDERS]
        queries += [f'(SINCE {since} SUBJECT "{s}")' for s in _BOUNCE_SUBJECTS]
        for query in queries:
            try:
                status, data = imap.search(None, query)
            except imaplib.IMAP4.error:
                continue
            if status == "OK" and data and data[0]:
                message_ids.update(data[0].split())

        bounces: list[Bounce] = []
        for message_id in sorted(message_ids):
            status, data = imap.fetch(message_id, "(RFC822)")
            if status != "OK" or not data:
                continue
            for item in data:
                if isinstance(item, tuple) and item[1]:
                    bounces.extend(parse_bounce(email.message_from_bytes(item[1])))
        return bounces
    finally:
        try:
            imap.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
