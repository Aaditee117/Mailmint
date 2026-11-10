"""Command line interface: discover / preview / send / reconcile / status."""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

from .bounces import ReconcileError, fetch_bounces, fetch_replies
from .config import Config, ConfigError, _resolve, load_config, load_credentials
from .dnscheck import PROVIDER_LABELS, active_resolver, configure_resolver
from .patterns import PATTERN_SHAPES
from .patterns import parse_name
from .people import append_people, load_people, parse_pasted_names, split_display_name
from .planner import PersonPlan, build_plans
from .render import Template, TemplateError, empty_placeholders, load_template, render
from .sender import GmailSender, SendError, build_message
from .store import LearnedStore, SentLog, SentRecord, utc_now

RULE = "-" * 78


def _out(text: str = "") -> None:
    print(text)


def _section(title: str) -> None:
    _out()
    _out(title)
    _out(RULE)


def _confirm(prompt: str) -> bool:
    try:
        return input(f"{prompt} [y/N]: ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        _out()
        return False


def _load_common(args: argparse.Namespace) -> tuple[Config, list, LearnedStore, SentLog]:
    config = load_config(Path(args.config) if args.config else None)
    configure_resolver(config.dns_fallback_servers, config.dns_timeout)
    people, warnings = load_people(config.people_path)
    for warning in warnings:
        _out(f"  ! {warning}")
    if not people:
        _out(f"  ! {config.people_path.name} has a header but no rows yet. Add "
             f"recipients (see README.md for the columns) or use `add`.")

    needle = (getattr(args, "company", None) or "").strip().lower()
    if needle:
        matched = [
            p for p in people
            if needle in p.company.lower() or needle in p.domain.lower()
        ]
        if not matched:
            companies = sorted({p.company for p in people if p.company})
            raise ConfigError(
                f"no rows in {config.people_path.name} match --company {needle!r}. "
                f"Present: {', '.join(companies) or '(none)'}"
            )
        _out(f"  filtered to {len(matched)} of {len(people)} rows matching '{needle}'")
        people = matched

    return config, people, LearnedStore(config.learned_path), SentLog(config.sent_log_path)


# --------------------------------------------------------------------------
# add
# --------------------------------------------------------------------------

def _read_names_blob(args: argparse.Namespace) -> str:
    if args.names_file:
        return Path(args.names_file).read_text(encoding="utf-8")
    if args.names:
        return args.names
    _out("Paste the names (one per line, straight from a LinkedIn people search).")
    _out("Headlines and locations are filtered out. Blank line when done:\n")
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line.strip():
            break
        lines.append(line)
    return "\n".join(lines)


def cmd_add(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config) if args.config else None)
    names, rejected = parse_pasted_names(_read_names_blob(args))

    # A LinkedIn people-search paste repeats the company name all over the page,
    # and "Acme Technologies" parses as a perfectly good two-token name.
    company_words = {w.lower() for w in args.company.split() if len(w) > 2}
    if company_words:
        echoes = [n for n in names if {w.lower() for w in n.split()} <= company_words]
        if echoes:
            names = [n for n in names if n not in echoes]
            rejected.extend(echoes)

    if rejected:
        _out(f"\nIgnored {len(rejected)} line(s) that do not look like names:")
        for line in rejected[:8]:
            _out(f"    {line[:70]}")
        if len(rejected) > 8:
            _out(f"    ... and {len(rejected) - 8} more")

    if not names:
        _out("\nNo names found. Nothing added.")
        return 1

    existing, _ = load_people(config.people_path) if config.people_path.exists() else ([], [])
    scope = (args.domain or args.company).strip().lower()
    known = {p.key for p in existing}

    rows: list[dict[str, str]] = []
    duplicates: list[str] = []
    for name in names:
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
            "notes": args.notes or "",
        })

    _section(f"ADDING TO {config.people_path.name}")
    _out(f"company : {args.company}")
    _out(f"domain  : {args.domain or '(will be guessed - supply one if you can)'}")
    _out(f"role    : {args.role or '(none)'}")
    _out(f"job id  : {args.job_id or '(none)'}")
    _out(f"sector  : {args.sector or config.default_sector + ' (from config)'}")
    _out(f"sample  : {args.sample or '(none - accuracy will be much lower)'}")
    _out()
    for row in rows:
        _out(f"  {row['first']} {row['last']}")
    if duplicates:
        _out(f"\n  skipping {len(duplicates)} already in the file: {', '.join(duplicates)}")
    if not rows:
        _out("\nNothing new to add.")
        return 0

    if not args.yes and not _confirm(f"\nAdd {len(rows)} people?"):
        _out("Nothing added.")
        return 1

    append_people(config.people_path, rows)
    _out(f"\nAdded {len(rows)} to {config.people_path}")
    _out(f"\nNext:  python run.py discover --company \"{args.company}\"")
    return 0


def _resume_or_fail(config: Config, override: str | None = None) -> Path | None:
    """Resolve the attachment, refusing to send silently without a configured one."""
    path = _resolve(config.root, override) if override else config.resume_path
    if path is None:
        return None
    if not path.exists():
        raise ConfigError(
            f"resume path {path} does not exist. Put the file there, pass a "
            "valid --resume, or blank out sender.resume to send without an "
            "attachment."
        )
    return path


# --------------------------------------------------------------------------
# discover
# --------------------------------------------------------------------------

def cmd_discover(args: argparse.Namespace) -> int:
    config, people, learned, sent_log = _load_common(args)
    resolver, resolver_note = active_resolver()
    _out(f"DNS: {resolver_note}")
    _, domain_plans = build_plans(people, config, learned, sent_log, allow_discovery=True)

    _section("DOMAINS")
    for scope, plan in sorted(domain_plans.items()):
        count = sum(
            1 for p in people if (p.domain or p.company).strip().lower() == scope
        )
        _out(f"{scope}  ({count} recipient{'s' if count != 1 else ''})")
        if not plan.domain:
            _out("   domain    : NOT FOUND - add a `domain` column value for this company")
        else:
            _out(f"   domain    : {plan.domain}")
            if plan.mx and plan.mx.ok:
                _out(f"   mail      : accepts mail via {PROVIDER_LABELS.get(plan.provider, plan.provider)}")
                _out(f"   mx        : {', '.join(plan.mx.exchanges[:3])}")
            elif plan.mx and plan.mx.rejects_mail:
                _out(f"   mail      : NO - {plan.mx.error}")
            elif plan.mx:
                _out(f"   mail      : UNVERIFIED - {plan.mx.error}")

        if plan.pattern:
            shape = PATTERN_SHAPES.get(plan.pattern, plan.pattern)
            _out(f"   pattern   : {plan.pattern}  {shape}")
            _out(f"   source    : {plan.pattern_source}")
        elif plan.plausible:
            _out(f"   pattern   : narrowed to {', '.join(plan.plausible)} (not confirmed)")
        else:
            _out("   pattern   : unknown - falling back to the sector prior")
        for note in plan.notes:
            _out(f"   ! {note}")
        _out()

    learned.save()
    _out(f"Saved findings to {config.learned_path.name}")

    unconfirmed = [s for s, p in domain_plans.items() if not p.pattern]
    if unconfirmed:
        _section("HOW TO CONFIRM THE REMAINING PATTERNS")
        _out("Any one real address at the company settles it. Fastest sources:")
        _out("  1. GitHub commits - clone a public repo of theirs and run:")
        _out("       git log --format=%ae | sort -u | findstr /i \"<domain>\"")
        _out("  2. Google: site:<domain> \"@<domain>\"   or   filetype:pdf \"@<domain>\"")
        _out("  3. Press releases and contact pages - media contacts are published.")
        _out("  4. Any recruiter from that company who has already emailed you.")
        _out()
        _out("Then put it in people.csv as:  sample_email = Full Name|addr@domain")
        _out("(the name is what lets the tool derive the pattern with certainty)")
        _out()
        _out(f"Still unconfirmed: {', '.join(unconfirmed)}")
    return 0


# --------------------------------------------------------------------------
# preview
# --------------------------------------------------------------------------

def _plan_line(plan: PersonPlan) -> str:
    person = plan.person
    if plan.chosen:
        confidence = f"{plan.chosen.confidence * 100:4.0f}%"
        return (
            f"  {person.display_name:<22} {plan.chosen.email:<38} "
            f"{confidence}  {plan.chosen.pattern}"
        )
    return f"  {person.display_name:<22} {'-- SKIPPED --':<38}        {plan.skip_reason}"


def _render_preview(
    plans: list[PersonPlan], template: Template, config: Config, show_bodies: int
) -> list[str]:
    lines: list[str] = []
    sendable = [p for p in plans if p.sendable]
    skipped = [p for p in plans if not p.sendable]

    lines.append("")
    lines.append(f"WILL SEND TO {len(sendable)} of {len(plans)}")
    lines.append(RULE)
    for plan in plans:
        lines.append(_plan_line(plan))
        for warning in plan.warnings:
            lines.append(f"      ! {warning}")
        if plan.sendable and plan.alternatives:
            alternates = ", ".join(
                f"{c.email} ({c.confidence * 100:.0f}%)" for c in plan.alternatives[:3]
            )
            lines.append(f"      next guesses if it bounces: {alternates}")

    if skipped:
        lines.append("")
        lines.append(f"{len(skipped)} skipped - see reasons above.")

    for plan in sendable[:show_bodies]:
        subject, body = render(template, plan.person, config.sender_name)
        blanks = empty_placeholders(template, plan.person, config.sender_name)
        lines.append("")
        lines.append(RULE)
        lines.append(f"To      : {plan.chosen.email}")
        lines.append(f"Subject : {subject}")
        if blanks:
            lines.append(f"WARNING : empty placeholder(s): {', '.join(blanks)}")
        lines.append(RULE)
        lines.append(body.rstrip())
    return lines


def cmd_preview(args: argparse.Namespace) -> int:
    config, people, learned, sent_log = _load_common(args)
    template = load_template(config.template_path)
    plans, _ = build_plans(
        people, config, learned, sent_log,
        allow_discovery=True, retry_bounced=args.retry_bounced,
    )
    learned.save()

    resume = _resolve(config.root, args.resume) if args.resume else config.resume_path
    lines = _render_preview(plans, template, config, args.bodies)
    lines.append("")
    lines.append(RULE)
    lines.append(
        f"Attachment: {resume.name if resume and resume.exists() else 'NONE'}"
        + ("" if not resume or resume.exists() else f"  (MISSING: {resume})")
    )
    lines.append("Nothing was sent. Run `send --live` when this looks right.")

    text = "\n".join(lines)
    _out(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        _out(f"\nWritten to {args.out}")
    return 0


# --------------------------------------------------------------------------
# send
# --------------------------------------------------------------------------

def cmd_send(args: argparse.Namespace) -> int:
    config, people, learned, sent_log = _load_common(args)
    template = load_template(config.template_path)
    creds = load_credentials()
    resume = _resume_or_fail(config, args.resume)

    plans, _ = build_plans(
        people, config, learned, sent_log,
        allow_discovery=True, retry_bounced=args.retry_bounced,
    )
    learned.save()

    queue = [p for p in plans if p.sendable]
    limit = args.limit if args.limit is not None else config.max_per_run
    if limit and len(queue) > limit:
        _out(f"  ! limiting this run to {limit} of {len(queue)} (send.max_per_run)")
        queue = queue[:limit]

    already_today = sent_log.sent_today()
    remaining_today = max(0, config.max_per_day - already_today)
    if len(queue) > remaining_today:
        _out(
            f"  ! daily cap: {already_today} already sent today, "
            f"{remaining_today} left of {config.max_per_day}"
        )
        queue = queue[:remaining_today]

    if args.self_test:
        return _self_test(config, creds, template, plans, resume)

    if not queue:
        _out("\nNothing to send.")
        return 0

    live = args.live
    _section("LIVE SEND" if live else "DRY RUN (no mail will leave your account)")
    _out(f"From       : {config.sender_name} <{creds.user}>")
    _out(f"Recipients : {len(queue)}")
    _out(f"Attachment : {resume.name if resume else 'none'}")
    _out(f"Throttle   : {config.min_delay:.0f}-{config.max_delay:.0f}s between messages")
    _out()
    for plan in queue:
        subject, _ = render(template, plan.person, config.sender_name)
        _out(f"  {plan.chosen.email:<40} {subject[:60]}")

    if live:
        _out()
        estimate = len(queue) * (config.min_delay + config.max_delay) / 2 / 60
        _out(f"This will send {len(queue)} real emails (about {estimate:.0f} min with throttling).")
        if not args.yes and not _confirm("Send them?"):
            _out("Aborted. Nothing sent.")
            return 1
    else:
        _out("\nDry run complete. Add --live to actually send.")
        return 0

    return _deliver(config, creds, template, queue, resume, sent_log, learned)


def _deliver(
    config: Config,
    creds,
    template: Template,
    queue: list[PersonPlan],
    resume: Path | None,
    sent_log: SentLog,
    learned: LearnedStore,
) -> int:
    sent = failed = 0
    _section("SENDING")
    try:
        with GmailSender(config, creds) as sender:
            for index, plan in enumerate(queue, start=1):
                person, candidate = plan.person, plan.chosen
                subject, body = render(template, person, config.sender_name)
                attempt = len(sent_log.for_person(person.key)) + 1
                status, detail, message_id = "sent", "", ""

                try:
                    message = build_message(
                        config, creds, candidate.email, subject, body, resume
                    )
                    message_id = sender.send(message)
                    sent += 1
                    _out(f"  [{index}/{len(queue)}] sent     {candidate.email}")
                except SendError as exc:
                    status, detail = "send_failed", str(exc)
                    failed += 1
                    _out(f"  [{index}/{len(queue)}] FAILED   {candidate.email}: {exc}")

                sent_log.append(
                    SentRecord(
                        timestamp=utc_now(),
                        person_key=person.key,
                        name=person.display_name,
                        company=person.company,
                        domain=plan.domain_plan.domain,
                        scope=plan.domain_plan.scope,
                        email=candidate.email,
                        pattern=candidate.pattern,
                        confidence=f"{candidate.confidence:.3f}",
                        attempt=str(attempt),
                        message_id=message_id,
                        status=status,
                        detail=detail[:500],
                    )
                )

                if status == "send_failed" and "limit" in detail.lower():
                    _out("\n  Stopping: Gmail is rate-limiting this account.")
                    break
                if index < len(queue):
                    delay = random.uniform(config.min_delay, config.max_delay)
                    time.sleep(delay)
    except KeyboardInterrupt:
        _out("\n  Interrupted. Everything sent so far is in the log; rerun to continue.")
    except SendError as exc:
        _out(f"\n  {exc}")
        return 1

    learned.save()
    _section("DONE")
    _out(f"sent: {sent}   failed: {failed}")
    _out()
    _out("Wait ~15 minutes, then run `reconcile` to see which addresses bounced.")
    return 0 if failed == 0 else 1


def _self_test(
    config: Config, creds, template: Template, plans: list[PersonPlan], resume: Path | None
) -> int:
    """Send one fully-rendered message to yourself. Not written to the sent log."""
    sample = next((p for p in plans if p.chosen), None)
    if sample is None:
        _out("No person could be rendered, so there is nothing to test.")
        return 1

    subject, body = render(template, sample.person, config.sender_name)
    _out(f"Sending a test copy of the message for {sample.person.display_name} "
         f"(would go to {sample.chosen.email}) to {creds.user}")
    try:
        with GmailSender(config, creds) as sender:
            message = build_message(
                config, creds, creds.user, f"[TEST] {subject}", body, resume
            )
            sender.send(message)
    except SendError as exc:
        _out(f"FAILED: {exc}")
        return 1
    _out("Sent. Check your inbox: attachment present, formatting intact, "
         "no leftover placeholders, and confirm it did not land in spam.")
    return 0


# --------------------------------------------------------------------------
# reconcile
# --------------------------------------------------------------------------

def cmd_reconcile(args: argparse.Namespace) -> int:
    config, people, learned, sent_log = _load_common(args)
    creds = load_credentials()

    _out(f"Scanning the last {args.days or config.lookback_days} days of "
         f"{creds.user} for delivery failures...")
    bounces = fetch_bounces(config, creds, args.days)

    matched: list[tuple] = []
    for bounce in bounces:
        record = sent_log.find_by_email(bounce.address)
        if record:
            matched.append((bounce, record))

    _section("BOUNCES")
    dead = 0
    if not matched:
        _out(f"  none. ({len(bounces)} bounce notice(s) in the inbox, "
             f"none matching {config.sent_log_path.name})")
        if bounces:
            addresses = ", ".join(sorted({b.address for b in bounces}))
            _out(f"  unmatched: {addresses[:200]}")
    for bounce, record in matched:
        if bounce.permanent and record.status == "sent":
            record.status = "bounced_permanent"
            record.detail = f"{bounce.status_code} {bounce.detail}".strip()
            # Prefer the scope recorded at send time; fall back for rows written
            # before the column existed.
            scope = (record.scope or record.domain or record.company).strip().lower()
            learned.rule_out(scope, record.pattern)
            dead += 1
            verdict = "PERMANENT - address does not exist"
        elif bounce.permanent:
            verdict = f"permanent (already recorded as {record.status})"
        else:
            verdict = "temporary - may still be delivered, do not resend yet"
        _out(f"  {record.email:<40} {verdict}")
        if bounce.detail:
            _out(f"      {bounce.detail[:120]}")

    engaged = _scan_replies(config, creds, sent_log, args.days)

    if dead or engaged:
        sent_log.rewrite()
        learned.save()

    funnel = sent_log.engagement()
    _section("FUNNEL")
    _out(f"  attempted      {funnel['attempted']}")
    _out(f"  bounced        {funnel['bounced']}   address does not exist")
    _out(f"  delivered      {funnel['delivered']}   no bounce came back")
    _out(f"  auto-replied   {funnel['auto_replied']}   mailbox CONFIRMED live")
    _out(f"  replied        {funnel['replied']}   a human answered")
    if funnel["failed"]:
        _out(f"  send failed    {funnel['failed']}")

    _section("WHAT THIS TELLS YOU")
    if dead:
        _out(f"{dead} address(es) confirmed dead, and the pattern each used is now")
        _out("ruled out for that domain. Retry them at their next-best address:")
        _out("    python run.py preview --retry-bounced")
        _out("    python run.py send --retry-bounced --live")
        _out()
    _out("An auto-reply is the strongest delivery proof you can get - it means a")
    _out("real mailbox received it. No bounce and no reply means only 'not")
    _out("provably dead': corporate filters quarantine plenty of legitimate mail")
    _out("without ever generating a bounce, and most people simply do not reply.")
    return 0


def _scan_replies(config: Config, creds, sent_log: SentLog, days: int | None = None) -> int:
    """Match inbox messages against what we sent. Returns how many rows changed."""
    sent = [r for r in sent_log.records if r.status == "sent"]
    addresses = {r.email for r in sent}
    message_ids = {r.message_id for r in sent if r.message_id}
    if not addresses:
        return 0

    replies = fetch_replies(config, creds, addresses, message_ids, days)

    _section("REPLIES")
    if not replies:
        _out("  none yet")
        return 0

    changed = 0
    for reply in sorted(replies, key=lambda r: (r.auto, r.address)):
        label = "auto-reply" if reply.auto else "REPLY"
        if sent_log.mark_reply(reply.address, reply.kind, utc_now()):
            changed += 1
        _out(f"  {reply.address:<40} {label:<11} {reply.subject[:40]}")
    return changed


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> int:
    config, people, learned, sent_log = _load_common(args)

    _section("FUNNEL")
    if not sent_log.records:
        _out(f"  no log yet at {config.sent_log_path.name}")
    else:
        funnel = sent_log.engagement()
        _out(f"  attempted      {funnel['attempted']}")
        _out(f"  bounced        {funnel['bounced']}   address does not exist")
        _out(f"  delivered      {funnel['delivered']}   no bounce came back")
        _out(f"  auto-replied   {funnel['auto_replied']}   mailbox confirmed live")
        _out(f"  replied        {funnel['replied']}   a human answered")
        if funnel["failed"]:
            _out(f"  send failed    {funnel['failed']}")
        _out()
        _out(f"  sent today     {sent_log.sent_today()} of {config.max_per_day} allowed")

    _section("RECIPIENTS")
    for person in people:
        records = sent_log.for_person(person.key)
        if not records:
            state = "not contacted"
        else:
            last = records[-1]
            state = f"{last.status} -> {last.email} ({last.timestamp[:10]})"
            if last.reply_kind == "reply":
                state += "  [REPLIED]"
            elif last.reply_kind == "auto_reply":
                state += "  [auto-reply]"
        _out(f"  {person.display_name:<24} {state}")

    _section("LEARNED PATTERNS")
    if not learned.data:
        _out("  none yet - run `discover`")
    for scope, knowledge in sorted(learned.data.items()):
        pattern = knowledge.pattern or "unknown"
        shape = PATTERN_SHAPES.get(knowledge.pattern or "", "")
        ruled = f"  ruled out: {', '.join(knowledge.ruled_out)}" if knowledge.ruled_out else ""
        _out(f"  {scope:<24} {knowledge.domain or '?':<24} {pattern} {shape}{ruled}")
    return 0


# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="Guess corporate email addresses from names, send referral "
                    "requests through Gmail, and reconcile the bounces.",
    )
    parser.add_argument("--config", help="path to config.yaml (default: alongside run.py)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p = subparsers.add_parser("add", help="add one company's people to people.csv")
    p.add_argument("--company", required=True, help="company name, as you want it in the email")
    p.add_argument("--domain", help="mail domain - supply it if you know it")
    p.add_argument("--role", help="the role you are applying for")
    p.add_argument("--job-id", dest="job_id", help="requisition / job ID from the posting")
    p.add_argument("--sector", help="indian_services | indian_product | us_startup | us_enterprise")
    p.add_argument("--sample", help="a known address at the company: 'Full Name|addr@domain'")
    p.add_argument("--notes", help="free text, available as {notes}")
    p.add_argument("--names", help="names, newline or comma separated (omit to paste interactively)")
    p.add_argument("--names-file", dest="names_file", help="read the names from a file")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.set_defaults(func=cmd_add)

    p = subparsers.add_parser("discover", help="resolve domains and address patterns")
    p.add_argument("--company", help="only this company (substring match)")
    p.set_defaults(func=cmd_discover)

    p = subparsers.add_parser("preview", help="render every email; send nothing")
    p.add_argument("--company", help="only this company (substring match)")
    p.add_argument("--bodies", type=int, default=2, help="how many full bodies to print (default 2)")
    p.add_argument("--out", help="also write the preview to this file")
    p.add_argument("--retry-bounced", action="store_true",
                   help="include people whose earlier address failed, using the next guess")
    p.add_argument("--resume", help="override sender.resume for this invocation")
    p.set_defaults(func=cmd_preview)

    p = subparsers.add_parser("send", help="send the queue (dry run unless --live)")
    p.add_argument("--company", help="only this company (substring match)")
    p.add_argument("--live", action="store_true", help="actually send")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.add_argument("--limit", type=int, help="cap this run (overrides send.max_per_run)")
    p.add_argument("--self-test", action="store_true",
                   help="send one rendered message to yourself and stop")
    p.add_argument("--retry-bounced", action="store_true",
                   help="include people whose earlier address failed, using the next guess")
    p.add_argument("--resume", help="override sender.resume for this invocation")
    p.set_defaults(func=cmd_send)

    p = subparsers.add_parser("reconcile", help="read bounces over IMAP and update the log")
    p.add_argument("--days", type=int, help="how far back to scan (default: config)")
    p.set_defaults(func=cmd_reconcile)

    p = subparsers.add_parser("status", help="show what has been sent and learned")
    p.add_argument("--company", help="only this company (substring match)")
    p.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, TemplateError, ReconcileError, SendError) as exc:
        _out(f"\nERROR: {exc}")
        return 1
    except FileNotFoundError as exc:
        _out(f"\nERROR: {exc}")
        return 1
    except PermissionError as exc:
        # This project lives under OneDrive, where a syncing file - or one left
        # open in Excel - is locked for writing.
        _out(f"\nERROR: cannot write {exc.filename or 'a file'}.")
        _out("Close it in Excel if it is open, wait for OneDrive to finish "
             "syncing, then try again.")
        return 1
    except KeyboardInterrupt:
        _out("\nInterrupted.")
        return 130
