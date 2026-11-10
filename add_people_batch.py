"""Add a batch of already-structured people (e.g. from a LinkedIn people-search
Apify actor) to people.csv, each with their own `notes` text.

cli.py's `add` command applies one shared --notes string to every person in a
batch, because it exists to clean up a messy human LinkedIn paste. This script
is for the opposite case: input is already structured (one name + one notes
string per person), so it mirrors `add`'s exact dedup logic (load existing
people.csv, dedupe by Person.key, write via append_people) without the
messy-paste parsing step, and without an interactive confirmation prompt -
this is meant to be called unattended.

Usage:
    python add_people_batch.py --company "Acme Corp" --domain acme.com \
        --role "Data Engineer" --job-id 12345 --sector indian_product \
        --batch-file people_batch.json

Batch file format: a JSON array of {"name": "...", "notes": "..."} objects,
e.g. [{"name": "Amy Salazar", "notes": "Technical Recruiter; Bengaluru India"}].
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from referral_mailer.config import load_config
from referral_mailer.patterns import parse_name
from referral_mailer.people import append_people, load_people, split_display_name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Add a batch of structured people (name + per-person notes) to people.csv.",
    )
    parser.add_argument("--config", help="path to config.yaml (default: alongside run.py)")
    parser.add_argument("--company", required=True, help="company name, as you want it in the email")
    parser.add_argument("--domain", help="mail domain - supply it if you know it")
    parser.add_argument("--role", help="the role you are applying for")
    parser.add_argument("--job-id", dest="job_id", help="requisition / job ID from the posting")
    parser.add_argument("--sector", help="indian_services | indian_product | us_startup | us_enterprise")
    parser.add_argument("--sample", help="a known address at the company: 'Full Name|addr@domain'")
    parser.add_argument("--batch-file", dest="batch_file", required=True,
                         help="JSON file: [{\"name\": \"...\", \"notes\": \"...\"}, ...]")
    args = parser.parse_args(argv)

    try:
        config = load_config(Path(args.config) if args.config else None)

        batch_path = Path(args.batch_file)
        items = json.loads(batch_path.read_text(encoding="utf-8"))

        existing, _ = load_people(config.people_path) if config.people_path.exists() else ([], [])
        scope = (args.domain or args.company).strip().lower()
        known = {p.key for p in existing}

        rows: list[dict[str, str]] = []
        duplicates: list[str] = []
        skipped_malformed: list[object] = []

        for item in items:
            name = (item or {}).get("name", "").strip() if isinstance(item, dict) else ""
            if not name:
                skipped_malformed.append(item)
                continue
            notes = (item.get("notes") or "").strip()

            parts = parse_name(full_name=name)
            key = f"{parts.first}|{parts.last}|{scope}"
            if key in known:
                duplicates.append(name)
                continue
            known.add(key)

            first, last = split_display_name(name)
            rows.append({
                "first": first,
                "last": last,
                "company": args.company,
                "domain": args.domain or "",
                "role": args.role or "",
                "job_id": args.job_id or "",
                "sample_email": args.sample or "",
                "sector": args.sector or "",
                "notes": notes,
            })

        if skipped_malformed:
            print(f"Ignored {len(skipped_malformed)} batch entr(y/ies) with no usable name.")
        if duplicates:
            print(f"Skipping {len(duplicates)} already in {config.people_path.name}: {', '.join(duplicates)}")

        if not rows:
            print("Nothing new to add.")
            return 0

        append_people(config.people_path, rows)
        print(f"Added {len(rows)} to {config.people_path}")
        for row in rows:
            print(f"  {row['first']} {row['last']}")
        return 0
    except PermissionError as exc:
        print(f"ERROR: cannot write {exc.filename or 'a file'}.")
        print("Close it in Excel if it is open, wait for OneDrive to finish syncing, then try again.")
        return 1
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
