"""Configuration and credential loading.

Credentials come from .env only. They are never read from config.yaml, never
written to the sent log, and never printed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    """Raised for anything the user can fix by editing config.yaml or .env."""


@dataclass
class Credentials:
    user: str
    app_password: str

    def __repr__(self) -> str:  # keep the secret out of tracebacks and logs
        return f"Credentials(user={self.user!r}, app_password=<hidden>)"


@dataclass
class Config:
    raw: dict
    root: Path

    # sender
    sender_name: str
    reply_to: str
    resume_path: Path | None

    # send
    min_delay: float
    max_delay: float
    max_per_day: int
    max_per_run: int
    smtp_host: str
    smtp_port: int

    # reconcile
    imap_host: str
    lookback_days: int

    # dns
    dns_fallback_servers: list[str]
    dns_timeout: float

    # guess
    default_sector: str
    domain_tlds: list[str]

    # paths
    people_path: Path
    template_path: Path
    sent_log_path: Path
    learned_path: Path


def _resolve(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def load_config(config_path: Path | None = None) -> Config:
    root = PROJECT_ROOT
    path = config_path or (root / "config.yaml")
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")

    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    sender = raw.get("sender") or {}
    send = raw.get("send") or {}
    reconcile = raw.get("reconcile") or {}
    dns_cfg = raw.get("dns") or {}
    guess = raw.get("guess") or {}
    paths = raw.get("paths") or {}

    resume_value = (sender.get("resume") or "").strip()
    resume_path = _resolve(root, resume_value) if resume_value else None

    min_delay = float(send.get("min_delay_seconds", 30))
    max_delay = float(send.get("max_delay_seconds", 90))
    if max_delay < min_delay:
        raise ConfigError("send.max_delay_seconds must be >= send.min_delay_seconds")

    tlds = guess.get("domain_tlds") or [".com", ".in", ".co.in", ".io"]
    tlds = [t if t.startswith(".") else f".{t}" for t in tlds]

    return Config(
        raw=raw,
        root=root,
        sender_name=(sender.get("name") or "").strip(),
        reply_to=(sender.get("reply_to") or "").strip(),
        resume_path=resume_path,
        min_delay=min_delay,
        max_delay=max_delay,
        max_per_day=int(send.get("max_per_day", 40)),
        max_per_run=int(send.get("max_per_run", 20)),
        smtp_host=send.get("smtp_host", "smtp.gmail.com"),
        smtp_port=int(send.get("smtp_port", 587)),
        imap_host=reconcile.get("imap_host", "imap.gmail.com"),
        lookback_days=int(reconcile.get("lookback_days", 7)),
        dns_fallback_servers=list(dns_cfg.get("fallback_servers") or ["8.8.8.8", "1.1.1.1"]),
        dns_timeout=float(dns_cfg.get("timeout_seconds", 2.5)),
        default_sector=guess.get("default_sector", "default"),
        domain_tlds=tlds,
        people_path=_resolve(root, paths.get("people", "people.csv")),
        template_path=_resolve(root, paths.get("template", "template.txt")),
        sent_log_path=_resolve(root, paths.get("sent_log", "sent_log.csv")),
        learned_path=_resolve(root, paths.get("learned", "learned_patterns.json")),
    )


def load_credentials() -> Credentials:
    """Read Gmail credentials from .env. Only called by commands that need them."""
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        raise ConfigError(
            f".env not found at {env_path}\n"
            "Copy .env.example to .env and fill in GMAIL_USER and "
            "GMAIL_APP_PASSWORD (a 16-character App Password, not your Google "
            "account password)."
        )
    # utf-8-sig, not utf-8: Notepad and PowerShell's `-Encoding utf8` both write
    # a BOM, which otherwise turns the first key into "﻿GMAIL_USER" and
    # produces a baffling "missing GMAIL_USER".
    load_dotenv(env_path, override=True, encoding="utf-8-sig")

    user = (os.getenv("GMAIL_USER") or "").strip()
    password = (os.getenv("GMAIL_APP_PASSWORD") or "").strip().replace(" ", "")

    missing = [n for n, v in (("GMAIL_USER", user), ("GMAIL_APP_PASSWORD", password)) if not v]
    if missing:
        raise ConfigError(f"missing in .env: {', '.join(missing)}")
    if "@" not in user:
        raise ConfigError(f"GMAIL_USER does not look like an address: {user}")
    if len(password) != 16:
        raise ConfigError(
            f"GMAIL_APP_PASSWORD is {len(password)} characters; a Gmail App "
            "Password is 16. Generate one at "
            "https://myaccount.google.com/apppasswords"
        )
    return Credentials(user, password)
