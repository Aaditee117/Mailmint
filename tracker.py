"""Read/update applications_tracker.xlsx - the sole record of which jobs have
been discovered, skipped, or contacted by the autonomous pipeline.

Two subcommands:
    python tracker.py read
        Prints every tracked job as one JSON line each (JSONL), for the
        autonomous pipeline to load as its "already handled" set.
    python tracker.py upsert --job-key "..." [--company ...] [--role ...] ...
        Creates the row if job_key is new, otherwise updates only the fields
        passed. date_first_found is set once, on creation, and never changed
        after. last_updated is bumped on every call. --people-contacted-delta
        adds to the running count and stamps date_contacted.

This is the only thing that touches applications_tracker.xlsx's byte format -
everything else (the autonomous pipeline, a human) goes through this CLI.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.worksheet import Worksheet

TRACKER_PATH = Path(__file__).resolve().parent / "applications_tracker.xlsx"
SHEET_NAME = "Applications"

# Column order as written to the sheet. job_key is always column 1 - the
# lookup key for upsert.
COLUMNS = [
    "job_key",
    "company",
    "role",
    "job_id",
    "location",
    "source_board",
    "application_link",
    "date_first_found",
    "date_contacted",
    "people_contacted",
    "resume_used",
    "status",
    "notes",
    "last_updated",
]


def _open_or_create() -> tuple[Workbook, Worksheet]:
    if TRACKER_PATH.exists():
        wb = load_workbook(TRACKER_PATH)
        ws = wb[SHEET_NAME] if SHEET_NAME in wb.sheetnames else wb.active
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = SHEET_NAME
        ws.append(COLUMNS)
    return wb, ws


def _row_as_dict(ws: Worksheet, row_idx: int) -> dict:
    return {col: ws.cell(row=row_idx, column=i + 1).value for i, col in enumerate(COLUMNS)}


def _find_row(ws: Worksheet, job_key: str) -> int | None:
    for row_idx in range(2, ws.max_row + 1):
        if ws.cell(row=row_idx, column=1).value == job_key:
            return row_idx
    return None


def cmd_read(args: argparse.Namespace) -> int:
    if not TRACKER_PATH.exists():
        return 0
    wb = load_workbook(TRACKER_PATH, read_only=True)
    ws = wb[SHEET_NAME] if SHEET_NAME in wb.sheetnames else wb.active
    for row_idx in range(2, ws.max_row + 1):
        row = {col: ws.cell(row=row_idx, column=i + 1).value for i, col in enumerate(COLUMNS)}
        if row["job_key"] is None:
            continue
        print(json.dumps(row, default=str))
    wb.close()
    return 0


def cmd_upsert(args: argparse.Namespace) -> int:
    wb, ws = _open_or_create()
    row_idx = _find_row(ws, args.job_key)
    now = datetime.now().isoformat(timespec="seconds")
    today = date.today().isoformat()

    if row_idx is None:
        row_idx = ws.max_row + 1
        existing = {col: None for col in COLUMNS}
        existing["job_key"] = args.job_key
        existing["people_contacted"] = 0
    else:
        existing = _row_as_dict(ws, row_idx)

    for field, attr in (
        ("company", "company"),
        ("role", "role"),
        ("job_id", "job_id"),
        ("location", "location"),
        ("source_board", "source"),
        ("application_link", "link"),
        ("status", "status"),
        ("resume_used", "resume_used"),
        ("notes", "notes"),
    ):
        value = getattr(args, attr)
        if value is not None:
            existing[field] = value

    if not existing.get("date_first_found"):
        existing["date_first_found"] = today

    if args.people_contacted_delta:
        existing["people_contacted"] = int(existing.get("people_contacted") or 0) + args.people_contacted_delta
        existing["date_contacted"] = today

    existing["last_updated"] = now

    for i, col in enumerate(COLUMNS):
        ws.cell(row=row_idx, column=i + 1, value=existing.get(col))

    wb.save(TRACKER_PATH)
    print(f"upserted {args.job_key}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tracker.py",
        description="Read or update applications_tracker.xlsx.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p = subparsers.add_parser("read", help="print every tracked job as one JSON line each")
    p.set_defaults(func=cmd_read)

    p = subparsers.add_parser("upsert", help="create or update one job's row")
    p.add_argument("--job-key", dest="job_key", required=True,
                    help="company|job_id, or company|title|location if no job_id exists")
    p.add_argument("--company")
    p.add_argument("--role")
    p.add_argument("--job-id", dest="job_id")
    p.add_argument("--location")
    p.add_argument("--source", help="greenhouse/lever/ashby/.../indeed/glassdoor/ziprecruiter/linkedin")
    p.add_argument("--link", help="application link")
    p.add_argument("--status", help="discovered|skipped_not_relevant|skipped_pay_below_threshold|"
                                     "skipped_illegitimate|needs_domain_review|sent|fully_exhausted")
    p.add_argument("--resume-used", dest="resume_used", help="data-resume.pdf or software-resume.pdf")
    p.add_argument("--people-contacted-delta", dest="people_contacted_delta", type=int,
                    help="add this many to the running people_contacted count")
    p.add_argument("--notes")
    p.set_defaults(func=cmd_upsert)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except PermissionError as exc:
        print(f"ERROR: cannot write {exc.filename or TRACKER_PATH}.")
        print("Close it in Excel if it is open, wait for OneDrive to finish syncing, then try again.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
