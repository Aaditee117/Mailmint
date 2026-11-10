"""Persistent state: the sent log and the learned-pattern cache.

The sent log is the source of truth for "have I already mailed this person".
It is append-only during a send so an interrupted run resumes cleanly.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

FIELDNAMES = [
    "timestamp", "person_key", "name", "company", "domain", "scope", "email",
    "pattern", "confidence", "attempt", "message_id", "status", "detail",
    "replied_at", "reply_kind",
]

# Statuses that mean "this person has been contacted, do not contact again".
TERMINAL_STATUSES = {"sent", "bounced_permanent"}


@dataclass
class SentRecord:
    timestamp: str
    person_key: str
    name: str
    company: str
    domain: str
    # The learned-pattern key this send was planned under. Stored because it is
    # the CSV's domain-or-company scope, which is not always the resolved
    # domain - reconcile must rule out the pattern under the same key that
    # planning looked it up with.
    scope: str
    email: str
    pattern: str
    confidence: str
    attempt: str
    message_id: str
    status: str
    detail: str
    # Engagement is tracked separately from `status` on purpose. Flipping status
    # to "replied" would make already_contacted() stop recognising the person as
    # contacted, and a later run would mail them a second time.
    replied_at: str = ""
    reply_kind: str = ""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SentLog:
    def __init__(self, path: Path):
        self.path = path
        self.records: list[SentRecord] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        # utf-8-sig, not utf-8: opening this file in Excel and saving adds a BOM,
        # which would turn the first column name into "﻿timestamp". Every
        # row would then read as blank and dedupe would silently stop working -
        # meaning people already contacted get emailed a second time.
        with self.path.open("r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                self.records.append(
                    SentRecord(**{k: (row.get(k) or "") for k in FIELDNAMES})
                )

    def append(self, record: SentRecord) -> None:
        is_new = not self.path.exists()
        with self.path.open("a", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
            if is_new:
                writer.writeheader()
            writer.writerow(asdict(record))
        self.records.append(record)

    def rewrite(self) -> None:
        """Persist in-place edits (used by reconcile when a status changes)."""
        with self.path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
            writer.writeheader()
            for record in self.records:
                writer.writerow(asdict(record))

    def for_person(self, person_key: str) -> list[SentRecord]:
        return [r for r in self.records if r.person_key == person_key]

    def already_contacted(self, person_key: str) -> SentRecord | None:
        """The record that blocks a resend, if any.

        A permanent bounce counts as contacted for dedupe purposes but the
        caller may deliberately retry it with a different pattern; that is what
        `addresses_tried` is for.
        """
        for record in self.for_person(person_key):
            if record.status == "sent":
                return record
        return None

    def addresses_tried(self, person_key: str) -> set[str]:
        return {
            r.email.lower()
            for r in self.for_person(person_key)
            if r.status in {"sent", "bounced_permanent", "send_failed"}
        }

    def sent_on(self, day: str) -> int:
        """Count of real sends on a UTC date (YYYY-MM-DD)."""
        return sum(
            1
            for r in self.records
            if r.status == "sent" and r.timestamp.startswith(day)
        )

    def sent_today(self) -> int:
        return self.sent_on(datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    def find_by_email(self, email: str) -> SentRecord | None:
        email = email.lower()
        for record in reversed(self.records):
            if record.email.lower() == email and record.status == "sent":
                return record
        return None

    def mark_reply(self, email: str, kind: str, when: str) -> bool:
        """Record engagement against the most recent send to this address.

        A human reply always wins over an auto-reply: an out-of-office that
        lands before the real answer must not mask it.
        """
        record = self.find_by_email(email)
        if record is None:
            return False
        if record.reply_kind == "reply" and kind == "auto_reply":
            return False
        if record.reply_kind == kind:
            return False
        record.reply_kind = kind
        record.replied_at = when
        return True

    def engagement(self) -> dict[str, int]:
        """The funnel.

        `delivered` means only "sent, and no bounce came back". The statuses are
        disjoint, so it must not be derived by subtracting bounces from a total.
        """
        delivered = [r for r in self.records if r.status == "sent"]
        bounced = sum(1 for r in self.records if r.status == "bounced_permanent")
        failed = sum(1 for r in self.records if r.status == "send_failed")
        return {
            "attempted": len(delivered) + bounced + failed,
            "delivered": len(delivered),
            "bounced": bounced,
            "failed": failed,
            "auto_replied": sum(1 for r in delivered if r.reply_kind == "auto_reply"),
            "replied": sum(1 for r in delivered if r.reply_kind == "reply"),
        }

    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for record in self.records:
            tally[record.status] = tally.get(record.status, 0) + 1
        return tally


@dataclass
class DomainKnowledge:
    """What we have worked out about one company's mail domain."""

    domain: str = ""
    provider: str = "unknown"
    mx_ok: bool = False
    pattern: str | None = None
    pattern_source: str = ""
    ruled_out: list[str] = field(default_factory=list)
    updated: str = ""


class LearnedStore:
    """Per-domain findings, keyed by the CSV's domain-or-company scope."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, DomainKnowledge] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for scope, entry in (raw or {}).items():
            known = {f.name for f in DomainKnowledge.__dataclass_fields__.values()}
            self.data[scope] = DomainKnowledge(
                **{k: v for k, v in entry.items() if k in known}
            )

    def get(self, scope: str) -> DomainKnowledge:
        return self.data.setdefault(scope.lower(), DomainKnowledge())

    def save(self) -> None:
        payload = {scope: asdict(k) for scope, k in self.data.items()}
        self.path.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )

    def rule_out(self, scope: str, pattern: str) -> None:
        """Record a pattern that bounced so the next attempt does not repeat it."""
        knowledge = self.get(scope)
        if pattern and pattern not in knowledge.ruled_out:
            knowledge.ruled_out.append(pattern)
        if knowledge.pattern == pattern:
            knowledge.pattern = None
            knowledge.pattern_source = f"cleared: {pattern} bounced"
        knowledge.updated = utc_now()
