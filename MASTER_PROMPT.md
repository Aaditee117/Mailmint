# Master prompt — populate people.csv and template.txt for a new company

Paste everything between the `=== PROMPT START ===` and `=== PROMPT END ===` markers
into a new Claude Code session opened in this project, filling in the INPUT block
first. Written to be followed literally, so a smaller model can execute it without
inferring anything.

---

=== PROMPT START ===

You are working in the `referral-mailer` project. Your job is to overwrite exactly
two files — `people.csv` and `template.txt` — so that `python run.py preview` renders
correctly. Overwrite them completely; do not append to what is already there.

## INPUT

COMPANY NAME:
<!-- e.g. Ingram Micro -->

JOB DESCRIPTION (paste the full posting, including the job ID / requisition number and locations):
<!-- paste here -->

PEOPLE (paste the LinkedIn people-search results raw — names, headlines, locations, all of it):
<!-- paste here -->

RESUME FILE (the PDF in the project root to write the email from):
<!-- e.g. software-resume.pdf -->

## STEP 1 — Read the project before writing anything

Read these four files. They define the schema you must obey; do not rely on memory
or on what the existing `people.csv` / `template.txt` look like, because those may
already be wrong.

- `referral_mailer/people.py` — the `KNOWN_COLUMNS` and `CANONICAL_COLUMNS` lists
- `referral_mailer/render.py` — the `PLACEHOLDERS` list
- `config.yaml` — `sender.name`, `sender.resume`, `guess.default_sector`
- `README.md` — the columns table and the "How the guess is ranked" section

Read the resume PDF named in the INPUT block. Every factual claim in the email must
come from it.

Also read `sent_log.csv` and check whether anyone in the INPUT list was already
mailed. If so, say which, and leave those rows out.

## STEP 2 — Find the company's email pattern

Run these web searches:

1. `<COMPANY> email format` — target Clay, RocketReach, LeadIQ, Hunter, SignalHire
2. `<COMPANY> email address pattern @<domain>`
3. `site:<domain> "@<domain>"` — press releases and newsroom pages publish real addresses
4. `filetype:pdf "@<domain>"` — brochures and investor decks name real contacts

Record: the mail domain, the reported dominant pattern, and the reported percentage.

**A percentage from an aggregator is a claim, not a confirmed sample.** If, and only
if, you find a real address published alongside its owner's full name, put it in the
`sample_email` cell of every row in the form `Full Name|address@domain.com`. That one
value raises the tool's confidence from roughly 60% to 97% for the whole company.

**Never invent a sample_email.** A fabricated sample locks a wrong pattern in at 97%
confidence for every recipient, which is worse than guessing. If you did not find a
real one, leave `sample_email` empty on every row and say so in your report.

## STEP 3 — Write people.csv

The header must be exactly this line, and the file must contain no other columns:

```
first,last,company,domain,role,job_id,sample_email,sector,notes
```

Any other column name (`email`, `location`, `title`, `priority`, `status`, `sent_date`,
…) is silently discarded by the loader and only makes the file misleading. Do not add
columns for any reason, including if the user's pasted data seems to call for them.

Column rules:

| Column | What goes in it |
|---|---|
| `first` | Given name, plus any middle name, exactly as spelled on LinkedIn |
| `last` | Surname only |
| `company` | The company name from INPUT, identical on every row |
| `domain` | The mail domain you confirmed in step 2, identical on every row |
| `role` | The job title being applied for, identical on every row |
| `job_id` | The requisition ID from the JD, identical on every row |
| `sample_email` | `Full Name\|address@domain` if genuinely found; otherwise empty |
| `sector` | Leave empty so `config.yaml`'s `guess.default_sector` applies. Set it only if that default is clearly wrong for this company: `indian_services`, `indian_product`, `us_startup`, `us_enterprise` |
| `notes` | Their job title and city, semicolon-separated. Short and factual |

**Leave the `email` column out entirely.** A filled `email` cell means "this address is
known-good, skip guessing", which switches off pattern ranking, the fallback ladder,
and bounce learning for that person. Guessed addresses must never go there — the whole
point of the tool is that it generates and ranks them itself.

`notes` is a live template placeholder, so it can end up in a sent email. Keep it to
facts you would not mind the recipient reading. No strategy language, no priority
ratings, no "PRIMARY TARGET".

Who to include, and in what order:

- Order rows by usefulness, best first, because `max_per_run` truncates from the top:
  1. Talent Acquisition / recruiters at the hiring location
  2. HR Business Partners at the hiring location
  3. Engineers or managers in the same function as the role
  4. Senior HR leadership in the same country
  5. Everyone else, with other-continent HR last
- Flag rather than silently keep: a surname that is only an initial (e.g. "Emma R.")
  produces a junk guess like `emma.r@`. Keep the row but call it out in your report.

## STEP 4 — Write template.txt

Only these eight placeholders exist. Any other `{name}` makes `load_template` raise
`TemplateError` and the entire tool stops:

```
{first_name} {last_name} {full_name} {company} {role} {job_id} {notes} {sender_name}
```

- `{first_name}`, `{last_name}`, `{full_name}` are the **recipient**.
- `{sender_name}` is **you** — it comes from `sender.name` in `config.yaml`.
- There is no placeholder for your phone, your email, your university, or your
  LinkedIn. Write those as literal text, taken from the resume.
- Do not use `{notes}` in the body.

File structure:

- Lines starting with `#` are stripped. Use them at the top to record the placeholder
  list for whoever edits the file next.
- The first non-comment line must start with `Subject:`.
- Everything after it is the body.

Content rules:

- Subject line format: `Subject: Referral Request - {role} ({job_id})`. Keep "Referral
  Request" literal so it's scannable in an inbox — don't replace it with jargon like
  "expertise" or a tech-stack list.
- Greeting is `Hi {first_name},` — never `Dear {first_name},`. "Dear" reads as a form
  letter.
- Body between 100 and 160 words. Short and crisp, not a cover letter. Trimming length
  means cutting throat-clearing, not cutting the skills mapping — the bullets mapping
  resume to JD stay in.
- **Banned openers**: "I hope this finds you well", "I hope you're doing well", "I hope
  this email finds you in good health", or any variant. They read as bot-generated and
  add zero information. Open directly with the role/company/job ID instead.
- One short sentence on current position and qualification, then 3–5 tight bullets
  mapping the resume to the JD's stated requirements, in the JD's own vocabulary. Bullets
  should be phrases, not full sentences with subject/verb padding.
- Before the closing ask, add one short sentence stating the concrete value you'd bring
  to their specific team or project — the "so what" behind the bullets, not a repeat of
  them (e.g. "I can start contributing to production data pipelines from day one").
- Close with a specific, easy ask — pass the profile along, or point to the right
  person — not a generic "please consider me". Where the tone fits, also invite the
  recipient to connect on LinkedIn or reach out directly by phone/WhatsApp — an
  additional, low-friction channel alongside the forward-to-HR ask, never instead of it.
- **Every claim must appear in the resume PDF.** If the JD asks for Java and the resume
  does not have Java, do not write Java. List what the resume actually has that
  satisfies the requirement.
- Sign with `{sender_name}`, then literal phone and email lines, plus a LinkedIn profile
  link when one is available. Mark the phone line "(call/WhatsApp)" when inviting a
  direct reach-out there. Sign-off is `Thanks,` — not `Best regards,` (reads too formal
  against the crisp "Hi" opener) and not `Thank you for your time and consideration.` as
  its own paragraph.

## STEP 5 — Verify

Run:

```
.\.venv\Scripts\python.exe run.py preview --company <first word of company name>
```

The output is correct only if all of these hold:

- No `ignoring unrecognised column(s)` warning
- No `TemplateError` and no unknown-placeholder error
- Every row shows a ranked address plus fallback guesses
- The rendered body has no leftover `{...}` and no stray `()` or doubled spaces
- The last line reads `Attachment: <your resume>.pdf`

If anything fails, fix the file and run it again. Do not report success on unverified
output, and do not run `send`.

## STEP 6 — Report

Tell the user, briefly:

1. Row count written, and anyone excluded (already in `sent_log.csv`, or not actually
   an employee)
2. The pattern found, the confidence `preview` gave, and — stated plainly — whether a
   confirmed `sample_email` was found or not
3. Any row with a weak guess, and why
4. Any JD requirement the resume does not meet (location, degree, graduation year,
   a named language). State it; do not paper over it in the email
5. The next command: `python run.py send --self-test`

=== PROMPT END ===

---

## Why these rules exist

Each one is a failure this workflow actually hit:

- **Extra CSV columns** — `people.py` validates headers against `KNOWN_COLUMNS` and
  discards the rest. A file with `location`, `contact_priority` and `status` columns
  looks informative and carries none of it into the send.
- **Guessed addresses in `email`** — the loader treats a filled `email` as authoritative
  and skips ranking entirely. Sixteen guesses sitting in that column disabled the
  feature the tool exists to provide.
- **Invented placeholders** — `render.py` raises on any name outside `PLACEHOLDERS`, so
  a template using `{university}` or `{your_phone_number}` cannot send at all, and the
  error surfaces only when you run the tool.
- **`{full_name}` in the signature** — it renders the recipient's name, so the email
  arrives signed by the person receiving it.
- **Fabricated `sample_email`** — `LEARNED_CONFIDENCE` is 0.97 and overrides every other
  signal. Guessing wrong there is worse than not guessing.
- **Resume drift** — an email claiming languages the attached resume does not list is
  caught by the first person who opens both.

## Follow-up prompts

Retarget an existing list at a new posting at the same company:

> Read `people.csv`. Keep the rows, update `role` and `job_id` to the new posting
> below, and rewrite `template.txt` against it using only the eight valid
> placeholders. Then run `preview` and show me the first rendered email.

After a send, once the bounces are in:

> Run `reconcile`, then `status`. Tell me which addresses bounced, what pattern the
> tool ruled out, and what the retry addresses would be.
