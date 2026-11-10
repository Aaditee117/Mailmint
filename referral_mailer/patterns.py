"""Name normalisation, address permutation, and confidence scoring.

The scoring model is a prior, not a measurement. Weights come from the rough
industry distribution of corporate address formats; sector and mail-provider
signals nudge them. A confirmed sample address for the domain overrides the
whole thing, which is why finding one sample is worth more than any tuning
that happens in this file.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Honorifics and suffixes that show up in scraped name lists and never appear
# in an email address.
_TITLES = {"mr", "mrs", "ms", "miss", "dr", "prof", "sri", "smt", "shri"}
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "md", "cfa"}


@dataclass(frozen=True)
class NameParts:
    """A person's name reduced to the ASCII tokens an address can be built from."""

    first: str
    middle: str
    last: str

    @property
    def f(self) -> str:
        return self.first[:1]

    @property
    def m(self) -> str:
        return self.middle[:1]

    @property
    def l(self) -> str:  # noqa: E743 - reads naturally next to .f and .m
        return self.last[:1]

    @property
    def full(self) -> str:
        return " ".join(p for p in (self.first, self.middle, self.last) if p)


def _ascii_fold(text: str) -> str:
    """Strip accents and anything an SMTP local part will not carry."""
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Za-z\-' ]", " ", ascii_only)


def _clean_token(token: str) -> str:
    """Lowercase a single name token and drop separators email addresses omit."""
    return re.sub(r"[^a-z]", "", token.lower())


def parse_name(first: str = "", last: str = "", full_name: str = "") -> NameParts:
    """Build NameParts from explicit first/last columns, or by splitting a full name.

    Explicit columns win when present: a CSV that says first=Ravi last=Kumar is
    more trustworthy than any splitting heuristic we could apply to "Ravi Kumar".
    """
    if not (first or last) and full_name:
        tokens = [t for t in _ascii_fold(full_name).split() if t]
        tokens = [t for t in tokens if _clean_token(t) not in _TITLES | _SUFFIXES]
        tokens = [_clean_token(t) for t in tokens]
        tokens = [t for t in tokens if t]
        if not tokens:
            return NameParts("", "", "")
        if len(tokens) == 1:
            return NameParts(tokens[0], "", "")
        return NameParts(tokens[0], " ".join(tokens[1:-1]), tokens[-1])

    first_tokens = [_clean_token(t) for t in _ascii_fold(first).split()]
    first_tokens = [t for t in first_tokens if t and t not in _TITLES]
    last_tokens = [_clean_token(t) for t in _ascii_fold(last).split()]
    last_tokens = [t for t in last_tokens if t and t not in _SUFFIXES]

    # "Ravi Kumar" in the first-name column means Kumar is a middle name unless
    # the last-name column is empty, in which case it is the surname.
    if first_tokens and not last_tokens and len(first_tokens) > 1:
        return NameParts(first_tokens[0], " ".join(first_tokens[1:-1]), first_tokens[-1])

    head = first_tokens[0] if first_tokens else ""
    middle = " ".join(first_tokens[1:]) if len(first_tokens) > 1 else ""
    tail = "".join(last_tokens)
    return NameParts(head, middle, tail)


# (key, human-readable shape, builder). Weights are the global base rate, in
# percent, before any sector or provider adjustment.
_PATTERN_SPECS: list[tuple[str, str, str, float]] = [
    ("first.last", "{first}.{last}", "fl_dot", 38.0),
    ("first", "{first}", "first", 17.0),
    ("f+last", "{f}{last}", "f_last", 14.0),
    ("firstlast", "{first}{last}", "firstlast", 7.0),
    ("first_last", "{first}_{last}", "fl_under", 4.0),
    ("f.last", "{f}.{last}", "f_dot_last", 4.0),
    ("last.first", "{last}.{first}", "last_first", 3.0),
    ("first+l", "{first}{l}", "first_l", 3.0),
    ("last+f", "{last}{f}", "last_f", 2.0),
    ("last", "{last}", "last", 2.0),
    ("first-last", "{first}-{last}", "fl_dash", 2.0),
    ("f+l", "{f}{l}", "f_l", 2.0),
    ("first.l", "{first}.{l}", "first_dot_l", 2.0),
    ("first.middle.last", "{first}.{middle}.{last}", "fml_dot", 1.0),
]

BASE_WEIGHTS: dict[str, float] = {key: w for key, _, _, w in _PATTERN_SPECS}
PATTERN_SHAPES: dict[str, str] = {key: shape for key, shape, _, _ in _PATTERN_SPECS}
ALL_PATTERNS: list[str] = [key for key, _, _, _ in _PATTERN_SPECS]


def build_local_part(pattern: str, name: NameParts) -> str | None:
    """Render one pattern for one name, or None if the name lacks the pieces."""
    first, middle, last = name.first, _clean_token(name.middle), name.last
    if pattern != "first" and pattern != "last" and not (first and last):
        return None

    builders = {
        "first.last": lambda: f"{first}.{last}",
        "first": lambda: first,
        "f+last": lambda: f"{name.f}{last}",
        "firstlast": lambda: f"{first}{last}",
        "first_last": lambda: f"{first}_{last}",
        "f.last": lambda: f"{name.f}.{last}",
        "last.first": lambda: f"{last}.{first}",
        "first+l": lambda: f"{first}{name.l}",
        "last+f": lambda: f"{last}{name.f}",
        "last": lambda: last,
        "first-last": lambda: f"{first}-{last}",
        "f+l": lambda: f"{name.f}{name.l}",
        "first.l": lambda: f"{first}.{name.l}",
        "first.middle.last": lambda: f"{first}.{middle}.{last}" if middle else None,
    }
    builder = builders.get(pattern)
    if builder is None:
        return None
    local = builder()
    if not local or len(local) < 2:
        return None
    return local


# Multipliers applied to the base weights. These encode the one thing that
# reliably shifts the distribution: who the company is.
SECTOR_ADJUST: dict[str, dict[str, float]] = {
    "indian_services": {"first.last": 2.0, "first": 0.35, "f+last": 0.6, "firstlast": 0.8},
    "indian_product": {"first.last": 1.6, "first": 0.8, "f+last": 0.9},
    "us_startup": {"first": 2.6, "first.last": 0.8, "f+last": 0.7, "firstlast": 0.9},
    "us_enterprise": {"f+last": 1.9, "first+l": 1.7, "first": 0.35, "first.last": 1.0},
    "default": {},
}

# Weaker signal: the mail provider mostly predicts company size and formality.
PROVIDER_ADJUST: dict[str, dict[str, float]] = {
    "google": {"first.last": 1.15},
    "microsoft": {"first.last": 1.20, "f+last": 1.10, "first": 0.80},
    "zoho": {"first": 1.40, "first.last": 0.85},
    "gateway": {"f+last": 1.30, "first+l": 1.20, "first": 0.50},
    "godaddy": {"first": 1.50, "first.last": 0.80},
    "forwarder": {"first": 1.60},
    "other": {},
    "unknown": {},
}

# Confidence assigned to a pattern we confirmed from a real sample address.
# Not 1.0: samples can come from a legacy alias or a since-changed convention.
LEARNED_CONFIDENCE = 0.97


@dataclass(frozen=True)
class Candidate:
    pattern: str
    email: str
    confidence: float

    @property
    def local_part(self) -> str:
        return self.email.split("@", 1)[0]


def rank_candidates(
    name: NameParts,
    domain: str,
    sector: str = "default",
    provider: str = "unknown",
    learned_pattern: str | None = None,
) -> list[Candidate]:
    """Score every buildable address for this person, best first.

    When learned_pattern is set it takes LEARNED_CONFIDENCE and the remaining
    probability mass is spread over the others by their adjusted prior, so the
    fallback order stays sane if the learned pattern turns out to be wrong.
    """
    if not domain:
        return []

    sector_mult = SECTOR_ADJUST.get(sector, {})
    provider_mult = PROVIDER_ADJUST.get(provider, {})

    scored: list[tuple[str, str, float]] = []
    for pattern in ALL_PATTERNS:
        local = build_local_part(pattern, name)
        if not local:
            continue
        weight = BASE_WEIGHTS[pattern]
        weight *= sector_mult.get(pattern, 1.0)
        weight *= provider_mult.get(pattern, 1.0)
        scored.append((pattern, f"{local}@{domain}", weight))

    if not scored:
        return []

    # Two patterns can collapse to the same address (a one-letter first name
    # makes {first} and {f}{...} coincide). Keep the highest-weighted spelling.
    best_by_email: dict[str, tuple[str, str, float]] = {}
    for pattern, email, weight in scored:
        prior = best_by_email.get(email)
        if prior is None or weight > prior[2]:
            best_by_email[email] = (pattern, email, weight)
    scored = list(best_by_email.values())

    if learned_pattern and any(p == learned_pattern for p, _, _ in scored):
        others = [(p, e, w) for p, e, w in scored if p != learned_pattern]
        total_other = sum(w for _, _, w in others) or 1.0
        remaining = 1.0 - LEARNED_CONFIDENCE
        result = [
            Candidate(p, e, LEARNED_CONFIDENCE)
            for p, e, _ in scored
            if p == learned_pattern
        ]
        result += [
            Candidate(p, e, remaining * w / total_other) for p, e, w in others
        ]
    else:
        total = sum(w for _, _, w in scored) or 1.0
        result = [Candidate(p, e, w / total) for p, e, w in scored]

    return sorted(result, key=lambda c: c.confidence, reverse=True)


def infer_pattern_from_sample(
    sample_email: str, sample_name: str = ""
) -> tuple[str | None, list[str], str]:
    """Work out a domain's convention from one known address.

    Returns (confirmed_pattern, plausible_patterns, explanation). A confirmed
    pattern requires the sample owner's name; without it we can only narrow the
    field structurally, which is still useful for breaking ties.
    """
    sample_email = (sample_email or "").strip()
    if "@" not in sample_email:
        return None, [], "no usable sample address"

    local = sample_email.split("@", 1)[0].lower()
    # Strip a disambiguation suffix: rahul.sharma01@ is still {first}.{last}.
    stripped = re.sub(r"\d+$", "", local)

    if sample_name:
        parts = parse_name(full_name=sample_name)
        matches = [
            pattern
            for pattern in ALL_PATTERNS
            if build_local_part(pattern, parts) == stripped
        ]
        if matches:
            # Prefer the most specific match: {first} and {first}{l} can both fit
            # a short name, and the pattern using more of the name generalises
            # better to the rest of the company.
            best = max(matches, key=lambda p: (_SPECIFICITY.get(p, 0), BASE_WEIGHTS[p]))
            note = f"confirmed from {sample_email} against '{sample_name}'"
            if stripped != local:
                note += " (numeric suffix ignored)"
            return best, matches, note
        return None, [], f"'{sample_name}' does not fit any pattern in {sample_email}"

    # No name to match against, so read the separator instead.
    if "." in stripped:
        head, _, tail = stripped.partition(".")
        if len(head) == 1:
            return None, ["f.last"], f"dotted with single-letter head in {sample_email}"
        if tail.count(".") >= 1:
            return None, ["first.middle.last", "first.last"], f"two dots in {sample_email}"
        if len(tail) == 1:
            return None, ["first.l"], f"dotted with single-letter tail in {sample_email}"
        return None, ["first.last", "last.first"], f"dotted local part in {sample_email}"
    if "_" in stripped:
        return None, ["first_last"], f"underscore in {sample_email}"
    if "-" in stripped:
        return None, ["first-last"], f"hyphen in {sample_email}"
    return (
        None,
        ["first", "firstlast", "f+last", "first+l"],
        f"single-token local part in {sample_email} (ambiguous - "
        f"add 'Name|{sample_email}' to pin it down)",
    )


# How much of the name a pattern consumes. Used only to break ties when a
# sample address matches more than one pattern.
_SPECIFICITY: dict[str, int] = {
    "first.middle.last": 5,
    "first.last": 4,
    "first_last": 4,
    "first-last": 4,
    "firstlast": 4,
    "last.first": 4,
    "f.last": 3,
    "f+last": 3,
    "first.l": 3,
    "first+l": 3,
    "last+f": 3,
    "f+l": 2,
    "first": 1,
    "last": 1,
}


def parse_sample_field(raw: str) -> tuple[str, str]:
    """Split the CSV sample_email cell, which accepts 'Full Name|addr' or 'addr'."""
    raw = (raw or "").strip()
    if not raw:
        return "", ""
    if "|" in raw:
        name, _, email = raw.partition("|")
        return name.strip(), email.strip()
    return "", raw
