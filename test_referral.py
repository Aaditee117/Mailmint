"""Self-checks for the parts that fail silently.

Address guessing and bounce parsing are the two places where a bug produces
plausible-looking output instead of an error, so they are what gets tested.
Run with:  python test_referral.py
"""

from __future__ import annotations

import email
import sys

from referral_mailer.bounces import classify_reply, parse_bounce
from referral_mailer.store import SentRecord
from referral_mailer.dnscheck import classify_provider, company_slugs
from referral_mailer.patterns import (
    build_local_part,
    infer_pattern_from_sample,
    parse_name,
    parse_sample_field,
    rank_candidates,
)
from referral_mailer.render import Template, humanise, render
from referral_mailer.people import (
    Person,
    clean_pasted_name,
    parse_pasted_names,
    split_display_name,
)

FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{label}\n    expected: {expected!r}\n    actual:   {actual!r}")


def check_true(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        FAILURES.append(f"{label}{('  ' + detail) if detail else ''}")


# ---------------------------------------------------------------- name parsing

def test_name_parsing() -> None:
    simple = parse_name("Kishan", "Payadi")
    check("simple first/last", (simple.first, simple.last), ("kishan", "payadi"))

    accented = parse_name("José", "Muñoz")
    check("accents folded to ascii", (accented.first, accented.last), ("jose", "munoz"))

    titled = parse_name(full_name="Dr. Priya Nair")
    check("title stripped", (titled.first, titled.last), ("priya", "nair"))

    suffixed = parse_name("John", "Smith Jr.")
    check("suffix stripped", (suffixed.first, suffixed.last), ("john", "smith"))

    three = parse_name(full_name="Ravi Kumar Nair")
    check("three-part name", (three.first, three.middle, three.last), ("ravi", "kumar", "nair"))

    # A two-token first-name column with an empty last-name column: the last
    # token has to become the surname or every guess is wrong.
    merged = parse_name("Ravi Kumar", "")
    check("split when last is empty", (merged.first, merged.last), ("ravi", "kumar"))

    hyphenated = parse_name("Mary-Jane", "Watson-Smith")
    check(
        "hyphens dropped from tokens",
        (hyphenated.first, hyphenated.last),
        ("maryjane", "watsonsmith"),
    )

    single = parse_name(full_name="Madonna")
    check("single token", (single.first, single.last), ("madonna", ""))

    empty = parse_name("", "", "")
    check("empty input", (empty.first, empty.last), ("", ""))


# ------------------------------------------------------------- local part build

def test_local_parts() -> None:
    name = parse_name("Priya", "Nair")
    cases = {
        "first.last": "priya.nair",
        "first": "priya",
        "f+last": "pnair",
        "firstlast": "priyanair",
        "first_last": "priya_nair",
        "f.last": "p.nair",
        "last.first": "nair.priya",
        "first+l": "priyan",
        "last+f": "nairp",
        "last": "nair",
        "first-last": "priya-nair",
        "f+l": "pn",
        "first.l": "priya.n",
    }
    for pattern, expected in cases.items():
        check(f"build {pattern}", build_local_part(pattern, name), expected)

    middle = parse_name(full_name="Ravi Kumar Nair")
    check("build first.middle.last", build_local_part("first.middle.last", middle), "ravi.kumar.nair")
    check(
        "first.middle.last needs a middle name",
        build_local_part("first.middle.last", name),
        None,
    )

    only_first = parse_name("Madonna", "")
    check("last-requiring pattern without a last name", build_local_part("first.last", only_first), None)
    check("first works without a last name", build_local_part("first", only_first), "madonna")


# ----------------------------------------------------------- pattern inference

def test_infer_from_sample() -> None:
    confirmed, _, _ = infer_pattern_from_sample("ravi.menon@acme.com", "Ravi Menon")
    check("confirm first.last from a named sample", confirmed, "first.last")

    confirmed, _, _ = infer_pattern_from_sample("rmenon@acme.com", "Ravi Menon")
    check("confirm f+last", confirmed, "f+last")

    confirmed, _, _ = infer_pattern_from_sample("ravi@acme.com", "Ravi Menon")
    check("confirm first", confirmed, "first")

    # A numeric disambiguation suffix must not defeat the match.
    confirmed, _, note = infer_pattern_from_sample("rahul.sharma01@acme.com", "Rahul Sharma")
    check("numeric suffix ignored", confirmed, "first.last")
    check_true("numeric suffix is mentioned", "suffix" in note, note)

    # No name to check against: narrow structurally, never claim certainty.
    confirmed, plausible, _ = infer_pattern_from_sample("ravi.menon@acme.com")
    check("unnamed sample confirms nothing", confirmed, None)
    check_true("dotted sample narrows to dotted patterns", "first.last" in plausible, str(plausible))

    confirmed, plausible, _ = infer_pattern_from_sample("rmenon@acme.com")
    check("single-token sample confirms nothing", confirmed, None)
    check_true("single token stays ambiguous", len(plausible) >= 3, str(plausible))

    confirmed, plausible, note = infer_pattern_from_sample("someone@acme.com", "Ravi Menon")
    check("mismatched name yields no pattern", confirmed, None)
    check_true("mismatch is explained", "does not fit" in note, note)

    check("empty sample", infer_pattern_from_sample("")[0], None)
    check("garbage sample", infer_pattern_from_sample("not-an-address")[0], None)


def test_sample_field_parsing() -> None:
    check("name|addr", parse_sample_field("Ravi Menon|ravi@acme.com"), ("Ravi Menon", "ravi@acme.com"))
    check("bare addr", parse_sample_field("ravi@acme.com"), ("", "ravi@acme.com"))
    check("blank", parse_sample_field(""), ("", ""))
    check("whitespace tolerated", parse_sample_field("  Ravi Menon | ravi@acme.com "), ("Ravi Menon", "ravi@acme.com"))


# --------------------------------------------------------------------- ranking

def test_ranking() -> None:
    name = parse_name("Priya", "Nair")

    ranked = rank_candidates(name, "acme.com", sector="indian_services")
    check("indian services favours first.last", ranked[0].pattern, "first.last")
    check_true(
        "confidences sum to 1",
        abs(sum(c.confidence for c in ranked) - 1.0) < 1e-6,
        str(sum(c.confidence for c in ranked)),
    )

    ranked = rank_candidates(name, "acme.com", sector="us_startup")
    check("us startup favours first", ranked[0].pattern, "first")

    ranked = rank_candidates(name, "acme.com", sector="us_enterprise")
    check_true(
        "us enterprise favours a short alias",
        ranked[0].pattern in ("f+last", "first+l", "first.last"),
        ranked[0].pattern,
    )

    ranked = rank_candidates(name, "acme.com", sector="us_startup", learned_pattern="first.last")
    check("a learned pattern overrides the sector prior", ranked[0].pattern, "first.last")
    check_true("learned confidence is high", ranked[0].confidence > 0.9, str(ranked[0].confidence))
    check_true(
        "fallbacks still ranked sensibly under a learned pattern",
        ranked[1].confidence < ranked[0].confidence,
        f"{ranked[1].confidence} vs {ranked[0].confidence}",
    )

    check("no domain yields no candidates", rank_candidates(name, ""), [])
    check_true("no name yields no candidates", rank_candidates(parse_name("", ""), "acme.com") == [])

    # A one-letter first name collapses several patterns onto one address.
    collapsed = rank_candidates(parse_name("K", "Payadi"), "acme.com")
    addresses = [c.email for c in collapsed]
    check("collapsed duplicates removed", len(addresses), len(set(addresses)))

    ranked = rank_candidates(name, "acme.com")
    check("every candidate is on the right domain", {c.email.split("@")[1] for c in ranked}, {"acme.com"})


# -------------------------------------------------------------------- rendering

def _person(**overrides) -> Person:
    base = dict(
        row=2, first="Priya", last="Nair", company="Acme", domain="acme.com",
        role="Data Engineer", job_id="JR-1", sample_name="", sample_email="",
        sector="", notes="", known_email="",
    )
    base.update(overrides)
    return Person(**base)


def test_rendering() -> None:
    template = Template(
        subject="Referral request - {role} ({job_id}) at {company}",
        body="Hi {first_name},\n\nThe {role} role at {company} ({job_id}).\n\n{sender_name}\n",
    )

    subject, body = render(template, _person(), "Kishan")
    check("subject renders", subject, "Referral request - Data Engineer (JR-1) at Acme")
    check_true("body renders", "Hi Priya," in body, body)

    # The empty-job_id case: the reader must not see a dangling "()".
    subject, body = render(template, _person(job_id=""), "Kishan")
    check("empty placeholder cleaned from subject", subject, "Referral request - Data Engineer at Acme")
    check_true("empty placeholder cleaned from body", "()" not in body, body)
    check_true("no doubled space left in body", "  " not in body, repr(body))

    indented = Template(subject="S {company}", body="Hi {first_name},\n\n    indented line\n")
    _, body = render(indented, _person(), "Kishan")
    check_true("leading indentation preserved", "\n    indented line" in body, repr(body))

    # A hand-written or model-generated CSV often carries lowercase names, and
    # "Hi priya," must never go out.
    _, body = render(template, _person(first="priya", last="nair"), "Kishan")
    check_true("lowercase name capitalised", "Hi Priya," in body, body)

    check("humanise fixes lowercase", humanise("priya nair"), "Priya Nair")
    check("humanise leaves deliberate casing alone", humanise("Ronan McDonald"), "Ronan McDonald")
    check("humanise leaves O'Neill alone", humanise("Sean O'Neill"), "Sean O'Neill")
    check("humanise on empty", humanise(""), "")


# ---------------------------------------------------------------------- domains

def test_domains() -> None:
    check("noise words dropped", company_slugs("Optimus Info Pvt Ltd")[0], "optimusinfo")
    check_true("initials offered", "oi" in company_slugs("Optimus Info Pvt Ltd"))
    check_true(
        "full token form kept as a fallback",
        "optimusinfopvtltd" in company_slugs("Optimus Info Pvt Ltd"),
    )
    check("all-noise name falls back to its tokens", company_slugs("Tech Solutions")[0], "techsolutions")
    check("empty company", company_slugs(""), [])

    check("google detected", classify_provider(["aspmx.l.google.com"]), "google")
    check("m365 detected", classify_provider(["acme-com.mail.protection.outlook.com"]), "microsoft")
    check("zoho detected", classify_provider(["mx.zoho.in"]), "zoho")
    check("gateway wins over the vendor behind it", classify_provider(["mx.pphosted.com"]), "gateway")
    check("unknown host", classify_provider(["mail.acme.com"]), "other")
    check("no hosts", classify_provider([]), "unknown")


# ------------------------------------------------------- pasted LinkedIn names

def test_pasted_name_cleaning() -> None:
    check("plain name", clean_pasted_name("Priya Nair"), "Priya Nair")
    check("credential suffix", clean_pasted_name("Arjun Sharma, PMP"), "Arjun Sharma")
    check("honorific", clean_pasted_name("Dr. Rahul Krishnan Iyer"), "Rahul Krishnan Iyer")
    check("pronouns", clean_pasted_name("Sneha Reddy (She/Her)"), "Sneha Reddy")
    check("degree marker", clean_pasted_name("Ravi Menon 3rd+"), "Ravi Menon")
    check("bullet and degree", clean_pasted_name("Sneha Reddy (She/Her) - 2nd"), "Sneha Reddy")
    check("pipe headline", clean_pasted_name("Priya Nair | Senior Data Engineer"), "Priya Nair")

    # Everything that is NOT a person must be rejected.
    for line in [
        "Senior Data Engineer at Acme Technologies",
        "Principal Architect at Acme",
        "Engineering Manager | Platform",
        "Greater Bengaluru Area",
        "Bengaluru",
        "View profile",
        "Message",
        "500+ connections",
        "",
        "   ",
    ]:
        check(f"rejects {line!r}", clean_pasted_name(line), "")


def test_pasted_block() -> None:
    blob = (
        "Priya Nair\n"
        "Senior Data Engineer at Acme Technologies\n"
        "Bengaluru, Karnataka, India\n"
        "Arjun Sharma, PMP\n"
        "Priya Nair\n"          # duplicate further down the page
    )
    names, rejected = parse_pasted_names(blob)
    check("names extracted in order", names, ["Priya Nair", "Arjun Sharma"])
    check_true("duplicates collapsed", names.count("Priya Nair") == 1)
    check_true("noise reported rather than dropped silently", len(rejected) >= 3, str(rejected))

    # A single comma-separated line is the other shape people paste.
    names, _ = parse_pasted_names("Priya Nair, Arjun Sharma, Sneha Reddy")
    check("comma separated list", names, ["Priya Nair", "Arjun Sharma", "Sneha Reddy"])

    # One comma is a credential, not a separator.
    names, _ = parse_pasted_names("Arjun Sharma, PMP")
    check("one comma is a suffix", names, ["Arjun Sharma"])


def test_display_name_split() -> None:
    check("two tokens", split_display_name("Priya Nair"), ("Priya", "Nair"))
    # The middle name belongs in `first` so parse_name reads it as a middle
    # name; putting it in `last` would build "krishnaniyer".
    check("three tokens", split_display_name("Rahul Krishnan Iyer"), ("Rahul Krishnan", "Iyer"))
    parts = parse_name(*split_display_name("Rahul Krishnan Iyer"))
    check("round-trips to the right parts", (parts.first, parts.middle, parts.last),
          ("rahul", "krishnan", "iyer"))
    check("guess uses first and last, not the middle",
          build_local_part("first.last", parts), "rahul.iyer")
    check("middle still available",
          build_local_part("first.middle.last", parts), "rahul.krishnan.iyer")


# ---------------------------------------------------------------------- bounces

_DSN = """From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>
Subject: Delivery Status Notification (Failure)
Content-Type: multipart/report; report-type=delivery-status; boundary="B"

--B
Content-Type: text/plain; charset=UTF-8

Address not found. Your message wasn't delivered to arjun.sharma@acme.com.

--B
Content-Type: message/delivery-status

Reporting-MTA: dns; googlemail.com

Final-Recipient: rfc822; arjun.sharma@acme.com
Action: failed
Status: 5.1.1
Diagnostic-Code: smtp; 550 5.1.1 The email account that you tried to reach does not exist.

--B--
"""

_DELAYED = _DSN.replace("Action: failed", "Action: delayed").replace("Status: 5.1.1", "Status: 4.4.7")


def test_bounce_parsing() -> None:
    bounces = parse_bounce(email.message_from_string(_DSN))
    check("one recipient parsed", len(bounces), 1)
    check("address extracted", bounces[0].address, "arjun.sharma@acme.com")
    check("status code extracted", bounces[0].status_code, "5.1.1")
    check_true("5.x.x is permanent", bounces[0].permanent)
    check_true("diagnostic captured", "does not exist" in bounces[0].detail, bounces[0].detail)

    delayed = parse_bounce(email.message_from_string(_DELAYED))
    check("delayed notice parsed", len(delayed), 1)
    check_true("4.x.x is not permanent", not delayed[0].permanent, str(delayed[0]))

    ordinary = email.message_from_string(
        "From: someone@acme.com\nSubject: Re: your note\n\nThanks, will take a look.\n"
    )
    check("a normal reply is not a bounce", parse_bounce(ordinary), [])


# ---------------------------------------------------------------------- replies

def _headers(raw: str):
    return email.message_from_string(raw)


def test_reply_classification() -> None:
    human = _headers(
        "From: Priya Nair <priya.nair@arm.com>\n"
        "Subject: Re: Referral request - Software Engineer\n\n"
    )
    check_true("a typed reply is not auto", not classify_reply(human))

    for raw, label in [
        ("From: a@arm.com\nAuto-Submitted: auto-replied\nSubject: Re: x\n\n", "Auto-Submitted"),
        ("From: a@arm.com\nSubject: Automatic reply: Referral request\n\n", "subject marker"),
        ("From: a@arm.com\nSubject: Out of Office: back Monday\n\n", "out of office"),
        ("From: a@arm.com\nX-Autoreply: yes\nSubject: Re: x\n\n", "X-Autoreply"),
        ("From: a@arm.com\nPrecedence: bulk\nSubject: Re: x\n\n", "Precedence"),
    ]:
        check_true(f"detects auto-reply via {label}", classify_reply(_headers(raw)))

    # "Re: ... vacation policy" must not be mistaken for an out-of-office.
    tricky = _headers("From: a@arm.com\nSubject: Re: Referral request\n\n")
    check_true("plain Re: is human", not classify_reply(tricky))


def _record(email_addr: str, **kw) -> SentRecord:
    base = dict(
        timestamp="2026-08-08T10:00:00+00:00", person_key=f"k|{email_addr}",
        name="X", company="Arm", domain="arm.com", scope="arm.com",
        email=email_addr, pattern="first.last", confidence="0.9", attempt="1",
        message_id="<m@gmail.com>", status="sent", detail="",
    )
    base.update(kw)
    return SentRecord(**base)


def test_engagement_tracking() -> None:
    from referral_mailer.store import SentLog

    log = SentLog.__new__(SentLog)  # no file I/O for this check
    log.records = [
        _record("a@arm.com"),
        _record("b@arm.com"),
        _record("c@arm.com", status="bounced_permanent"),
    ]

    check_true("marks a reply", log.mark_reply("a@arm.com", "reply", "now"))
    check_true("marks an auto-reply", log.mark_reply("b@arm.com", "auto_reply", "now"))
    check_true("unknown address is ignored", not log.mark_reply("zz@arm.com", "reply", "now"))
    check_true("re-marking the same kind is a no-op", not log.mark_reply("a@arm.com", "reply", "now"))

    # An out-of-office arriving after a real answer must not overwrite it.
    check_true("auto-reply cannot mask a human reply",
               not log.mark_reply("a@arm.com", "auto_reply", "later"))
    check("human reply survives", log.records[0].reply_kind, "reply")
    # ...but a human reply after an auto-reply is an upgrade.
    check_true("human reply upgrades an auto-reply",
               log.mark_reply("b@arm.com", "reply", "later"))

    log.records[1].reply_kind = "auto_reply"
    funnel = log.engagement()
    # delivered and bounced are disjoint sets, so attempted is their sum - not
    # a total that bounces get subtracted from.
    check("funnel counts every attempt", funnel["attempted"], 3)
    check("funnel counts delivered", funnel["delivered"], 2)
    check("funnel counts bounced", funnel["bounced"], 1)
    check("funnel counts replies", funnel["replied"], 1)
    check("funnel counts auto-replies", funnel["auto_replied"], 1)

    # A bounced address must never be counted as engaged.
    check_true("bounced rows excluded from replies",
               not log.mark_reply("c@arm.com", "reply", "now"))


# -------------------------------------------------------------------------- run

def main() -> int:
    tests = [
        test_name_parsing,
        test_local_parts,
        test_infer_from_sample,
        test_sample_field_parsing,
        test_ranking,
        test_rendering,
        test_domains,
        test_pasted_name_cleaning,
        test_pasted_block,
        test_display_name_split,
        test_bounce_parsing,
        test_reply_classification,
        test_engagement_tracking,
    ]
    for test in tests:
        try:
            test()
        except Exception as exc:  # a crash is a failure like any other
            FAILURES.append(f"{test.__name__} raised {type(exc).__name__}: {exc}")

    if FAILURES:
        print(f"FAILED ({len(FAILURES)})\n")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print(f"OK - {len(tests)} test groups passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
