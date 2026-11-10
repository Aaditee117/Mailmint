"""Loading and validating the recipient list."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from .patterns import NameParts, parse_name, parse_sample_field

KNOWN_COLUMNS = {
    "first", "last", "full_name", "company", "domain", "role",
    "job_id", "sample_email", "sector", "notes", "email",
}

# The column order written by `add` when creating people.csv.
CANONICAL_COLUMNS = [
    "first", "last", "company", "domain", "role",
    "job_id", "sample_email", "sector", "notes",
]


@dataclass
class Person:
    row: int
    first: str
    last: str
    company: str
    domain: str
    role: str
    job_id: str
    sample_name: str
    sample_email: str
    sector: str
    notes: str
    # Set when the CSV supplies a known-good address directly; guessing is then
    # skipped entirely for this person.
    known_email: str
    name: NameParts = field(init=False)

    def __post_init__(self) -> None:
        self.name = parse_name(self.first, self.last, f"{self.first} {self.last}".strip())

    @property
    def display_name(self) -> str:
        return " ".join(p for p in (self.first, self.last) if p) or self.name.full

    @property
    def key(self) -> str:
        """Stable identity for dedupe across runs, independent of guessed address."""
        scope = (self.domain or self.company).strip().lower()
        return f"{self.name.first}|{self.name.last}|{scope}"

    def problems(self) -> list[str]:
        issues: list[str] = []
        if not self.name.first:
            issues.append("no first name")
        if not self.company and not self.domain:
            issues.append("no company and no domain")
        if not self.name.last and not self.known_email:
            issues.append("no last name (only single-token guesses possible)")
        if self.name.middle and not self.known_email:
            issues.append(
                f"multi-part name ('{self.name.full}') - check whether the company "
                f"uses '{self.name.first}.{self.name.last}' or includes the middle name"
            )
        return issues


def load_people(path: Path) -> tuple[list[Person], list[str]]:
    """Read people.csv. Returns (people, warnings)."""
    if not path.exists():
        raise FileNotFoundError(f"recipient list not found: {path}")

    warnings: list[str] = []
    people: list[Person] = []

    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise ValueError(f"{path} has no header row")

        headers = {(h or "").strip().lower() for h in reader.fieldnames}
        unknown = headers - KNOWN_COLUMNS - {""}
        if unknown:
            warnings.append(f"ignoring unrecognised column(s): {', '.join(sorted(unknown))}")
        if not headers & {"first", "full_name"}:
            raise ValueError(f"{path} needs a 'first' or 'full_name' column")

        for offset, raw_row in enumerate(reader, start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw_row.items()}
            if not any(row.values()):
                continue

            first, last = row.get("first", ""), row.get("last", "")
            if not (first or last) and row.get("full_name"):
                # Split preserving the original casing. parse_name() lowercases
                # for address building, and these two values are also what the
                # greeting renders from - deriving them from it produced
                # "Hi priya,".
                cleaned = clean_pasted_name(row["full_name"]) or row["full_name"].strip()
                first, last = split_display_name(cleaned)

            sample_name, sample_email = parse_sample_field(row.get("sample_email", ""))
            domain = row.get("domain", "").strip().lower().lstrip("@")
            known_email = row.get("email", "").strip().lower()
            if known_email and "@" in known_email and not domain:
                domain = known_email.split("@", 1)[1]

            people.append(
                Person(
                    row=offset,
                    first=first,
                    last=last,
                    company=row.get("company", ""),
                    domain=domain,
                    role=row.get("role", ""),
                    job_id=row.get("job_id", ""),
                    sample_name=sample_name,
                    sample_email=sample_email,
                    sector=row.get("sector", ""),
                    notes=row.get("notes", ""),
                    known_email=known_email,
                )
            )

    seen: dict[str, int] = {}
    for person in people:
        if person.key in seen:
            warnings.append(
                f"row {person.row}: duplicate of row {seen[person.key]} "
                f"({person.display_name}) - only the first will be mailed"
            )
        else:
            seen[person.key] = person.row

    return people, warnings


# --------------------------------------------------------------------------
# Ingesting names pasted out of a LinkedIn people search
# --------------------------------------------------------------------------

# Post-nominals that trail a name and never appear in an address.
_CREDENTIALS = {
    "pmp", "phd", "md", "mba", "cfa", "cpa", "pe", "msc", "bsc", "ma", "ba",
    "bs", "ms", "cissp", "csm", "cscp", "cpim", "psm", "safe", "aws", "gcp",
    "jr", "sr", "ii", "iii", "iv",
}

# Honorifics that lead a name on LinkedIn but never appear in an address.
_TITLES = {"dr", "mr", "mrs", "ms", "miss", "prof", "professor", "sri", "shri", "smt"}

# Words that mark a pasted line as a headline, location, or UI chrome rather
# than a person's name. A line containing any of these is rejected outright,
# which is why they are all words that would be extraordinary as a surname.
_NOT_A_NAME = {
    # LinkedIn chrome
    "view", "profile", "message", "follow", "following", "connect", "connection",
    "connections", "member", "linkedin", "status", "reachable", "shared",
    "mutual", "company", "page", "people", "also", "viewed", "see", "more",
    "others", "premium", "open", "hiring", "impressions", "followers",
    # job titles
    "engineer", "engineering", "manager", "director", "senior", "junior",
    "lead", "head", "officer", "president", "analyst", "developer",
    "consultant", "specialist", "recruiter", "recruiting", "principal",
    "architect", "staff", "chief", "founder", "cofounder", "partner",
    "associate", "executive", "scientist", "designer", "product", "program",
    "project", "technical", "software", "platform", "solutions", "delivery",
    "quality", "devops", "sre", "intern", "trainee", "coordinator",
    "administrator", "advisor", "talent", "acquisition", "resources",
    "sourcer", "staffing", "operations", "strategy", "marketing", "sales",
    # locations and filler
    "area", "region", "greater", "metropolitan", "district", "remote",
    "hybrid", "onsite", "india", "usa", "states", "united", "kingdom",
    "at", "of", "the", "and",
}

_SEPARATORS = re.compile(r"\s*(?:\||•|·|—|–|\s-\s|\bat\b)\s*", re.I)
_PARENTHETICAL = re.compile(r"\([^)]*\)")
_DEGREE = re.compile(r"\b\d+(?:st|nd|rd|th)\+?\b|\bdegree\b", re.I)
_NON_NAME_CHARS = re.compile(r"[^\w\s.'\-]", re.UNICODE)


def clean_pasted_name(line: str) -> str:
    """Reduce one pasted line to a name, or return '' if it is not one.

    LinkedIn search results paste as three lines per person - name, headline,
    location - plus assorted chrome. This keeps the name lines and drops the
    rest. It is deliberately conservative: `add` shows you what it parsed and
    asks before writing, so a false reject costs one manual edit while a false
    accept would email a stranger called "Bengaluru".
    """
    text = _PARENTHETICAL.sub(" ", line or "")
    text = _DEGREE.sub(" ", text)
    text = _SEPARATORS.split(text)[0]
    text = text.split(",")[0]
    text = _NON_NAME_CHARS.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" .-'")

    if not text or any(char.isdigit() for char in text):
        return ""

    tokens = [t for t in text.split() if t.strip(".'-")]
    tokens = [t for t in tokens if t.lower().strip(".") not in _CREDENTIALS]
    while tokens and tokens[0].lower().strip(".") in _TITLES:
        tokens.pop(0)
    # Two to four tokens: one token is usually a city, five-plus is a headline.
    if not 2 <= len(tokens) <= 4:
        return ""
    if any(t.lower() in _NOT_A_NAME for t in tokens):
        return ""
    if not all(re.fullmatch(r"[^\W\d_][\w.'\-]*", t, re.UNICODE) for t in tokens):
        return ""
    return " ".join(tokens)


def split_display_name(name: str) -> tuple[str, str]:
    """Split a cleaned name into the CSV's first and last columns.

    Middle names stay in the `first` column on purpose: parse_name() then reads
    them as a middle name, which keeps `first.middle.last` available as a
    pattern. Putting them in `last` would produce "krishnaniyer" instead.
    """
    tokens = name.split()
    if len(tokens) < 2:
        return name, ""
    return " ".join(tokens[:-1]), tokens[-1]


def parse_pasted_names(blob: str) -> tuple[list[str], list[str]]:
    """Split a pasted block into (names, rejected lines), preserving order."""
    names: list[str] = []
    rejected: list[str] = []
    seen: set[str] = set()

    raw_lines: list[str] = []
    for line in (blob or "").splitlines():
        # Two or more commas means a comma-separated list of names; one comma is
        # far more likely to be a trailing credential ("Priya Nair, PMP").
        if line.count(",") >= 2:
            raw_lines.extend(line.split(","))
        else:
            raw_lines.append(line)

    for line in raw_lines:
        if not line.strip():
            continue
        name = clean_pasted_name(line)
        if not name:
            rejected.append(line.strip())
        elif name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    return names, rejected


def append_people(path: Path, rows: list[dict[str, str]]) -> int:
    """Append rows to people.csv, creating or widening the header as needed."""
    existing_rows: list[dict[str, str]] = []
    fieldnames = list(CANONICAL_COLUMNS)

    if path.exists():
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            fieldnames = [f for f in (reader.fieldnames or []) if f]
            existing_rows = [dict(row) for row in reader]

        needed = [c for c in CANONICAL_COLUMNS if c not in fieldnames]
        if needed:
            # A column the new rows need is missing, so the whole file has to be
            # rewritten with the wider header rather than silently dropping data.
            fieldnames = fieldnames + needed
            with path.open("w", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                for row in existing_rows:
                    writer.writerow({k: (row.get(k) or "") for k in fieldnames})

    is_new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        if is_new:
            writer.writeheader()
        for row in rows:
            writer.writerow({k: (row.get(k) or "") for k in fieldnames})
    return len(rows)


def group_by_domain_scope(people: list[Person]) -> dict[str, list[Person]]:
    """Group by the thing a pattern is learned for: the company's mail domain."""
    groups: dict[str, list[Person]] = {}
    for person in people:
        scope = (person.domain or person.company).strip().lower()
        groups.setdefault(scope, []).append(person)
    return groups
