# Referral mailer

Guesses corporate email addresses from names, sends referral requests one at a
time through Gmail, and reconciles the bounces so the next attempt is smarter.

## The honest limitation, up front

**There is no free way to check whether a specific mailbox exists.** DNS tells
you a *domain* accepts mail and who runs it. It cannot tell you that
`priya.nair@` exists there. The SMTP `RCPT TO` trick that tutorials show is
defeated by Google Workspace and Microsoft 365 (both accept every address at
handshake time and bounce later), usually blocked by ISPs on port 25, and gets
your IP blocklisted if you hammer it. This tool does not use it.

So the verifier here is **the bounce log** — it runs *after* the send instead of
before it. Send the top guess, wait, run `reconcile`, and Gmail's delivery
failures tell you exactly which addresses were wrong. At fewer than 20
recipients that is entirely workable.

**The single highest-leverage thing you can do is find one real address per
company.** That converts guessing (~40–70% right) into knowing (~95%+).
`discover` prints where to look.

## Setup

The project ships with a virtualenv at `.venv`. Activate it:

```bash
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks that with an execution-policy error, either run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, or skip activation
entirely and call the interpreter directly — `.\.venv\Scripts\python.exe run.py ...`
works identically.

To rebuild it from scratch:

```bash
python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Create `.env` yourself from the template — never paste the password into a chat
or commit it:

```bash
copy .env.example .env
```

`GMAIL_APP_PASSWORD` is a 16-character **App Password** from
<https://myaccount.google.com/apppasswords>, not your Google account password.
2-Step Verification must be enabled for that page to exist.

Then fill in:

- **`people.csv`** — one row per person (see columns below)
- **`template.txt`** — subject and body, with `{placeholders}`
- **`resume.pdf`** — or blank out `sender.resume` in `config.yaml`
- **`config.yaml`** — set `sender.name`

## The loop, one company at a time

This is the flow it is built around. Repeat it per company; `sent_log.csv` is
shared across all of them so nobody is ever mailed twice.

```bash
python run.py add --company "Acme Technologies" --domain acmetech.com --role "Senior Data Engineer" --job-id JR-40912 --sector indian_services
```

Run that after finding an opening, then paste the names straight from the
LinkedIn people-search results and press Enter twice. Headlines, locations,
`View profile`, pronouns, `2nd`/`3rd+` markers, honorifics and `, PMP` suffixes
are all stripped; anything it isn't sure about is listed as ignored so you can
see what it dropped. It shows you the parsed list and asks before writing.

The company-level flags are typed once and applied to every person, which is the
point — `--sample "Ravi Menon|ravi.menon@acmetech.com"` is the one worth hunting
for, because it locks the pattern for everyone at that company.

Then the same four steps, scoped to that company:

```bash
python run.py discover --company Acme
```
```bash
python run.py preview --company Acme
```
```bash
python run.py send --company Acme --live
```
```bash
python run.py reconcile
```

`--company` is a case-insensitive substring match against the company name or
the domain, and it accepts `--names-file` or `--names "A, B, C"` if you would
rather not paste interactively. `reconcile` is deliberately not scoped — it
reads your whole inbox and updates whatever it finds, so run it once after
whatever you sent that day.

## Command reference

```bash
python run.py discover
```

Resolves each company's mail domain, identifies the provider, and derives the
address pattern from any sample you supplied. Prints where to hunt for a sample
for the companies still unresolved. Writes `learned_patterns.json`.

```bash
python run.py preview
```

Renders every address and every email. Sends nothing. This is the step to
actually read — check the addresses, check the wording, check for warnings.

```bash
python run.py send --self-test
```

Sends one fully-rendered message **to yourself**. Confirms the app password
works, the attachment is attached, nothing has a leftover placeholder, and it
doesn't land in spam. Do this before any real send.

```bash
python run.py send --live
```

Sends for real, one message per recipient, with a randomized 30–90s gap. Prompts
for confirmation first. Everything is written to `sent_log.csv` as it goes, so an
interrupted run resumes cleanly — just run it again.

```bash
python run.py reconcile
```

Wait ~15 minutes after sending, then run this. Reads delivery failures from your
inbox over IMAP, marks dead addresses in the log, and rules that pattern out for
the domain. Requires IMAP enabled: Gmail → Settings → Forwarding and POP/IMAP →
Enable IMAP.

```bash
python run.py preview --retry-bounced
python run.py send --retry-bounced --live
```

Retries the bounced people at their next-best address, which is now guaranteed
to use a different pattern.

```bash
python run.py status
```

What has been sent, what bounced, what has been learned, how much of today's cap
is used.

## Metrics

`reconcile` and `status` report a funnel:

```
attempted      21
bounced         2   address does not exist
delivered      19   no bounce came back
auto-replied    3   mailbox CONFIRMED live
replied         1   a human answered
```

**There is no open tracking, deliberately.** It would require switching the mail
to HTML and embedding a remote 1x1 pixel. That is a well-known spam signal for
cold mail to a corporate domain, enterprise clients block remote images by
default so most opens never register, and Gmail proxies and caches whatever does
load - so the number you get back is noise, bought at the cost of a real
deliverability hit.

What is measured instead is reliable:

- **bounced** - a delivery failure notice naming that address. Definitive.
- **auto-replied** - an out-of-office or similar. This is *stronger* evidence
  than an open would be: a real mailbox received and processed the message.
- **replied** - a human typed something back. For referrals this is the only
  metric that converts.

Replies are matched two ways: the sender address matches one you mailed, or the
message threads onto your `Message-ID`. Both are needed, since Gmail sometimes
rewrites the Message-ID and people sometimes reply from a different address.

A human reply always outranks an auto-reply, so an out-of-office arriving after
a real answer never masks it.

**No bounce and no reply means "not provably dead" - nothing more.** Corporate
filters quarantine legitimate mail without generating a bounce, and most people
simply do not reply.

## people.csv columns

`add` writes these for you. Edit by hand only when you want to override
something for one person.


| Column | Required | Notes |
|---|---|---|
| `first` | yes | or supply `full_name` instead |
| `last` | | without it only single-token guesses are possible |
| `full_name` | | alternative to `first`/`last` |
| `company` | yes* | used for the `{company}` placeholder and domain guessing |
| `domain` | | the mail domain. Supplying it removes a whole layer of guessing |
| `role` | | `{role}` placeholder |
| `job_id` | | `{job_id}` placeholder |
| `sample_email` | | **the important one** — see below |
| `sector` | | `indian_services`, `indian_product`, `us_startup`, `us_enterprise` |
| `notes` | | `{notes}` placeholder |
| `email` | | a known-good address; skips guessing entirely for that person |

\* `company` or `domain`, at least one.

### sample_email is what makes this accurate

Two accepted forms:

```
ravi.menon@acme.com                 # narrows the pattern structurally
Ravi Menon|ravi.menon@acme.com      # CONFIRMS the pattern outright
```

Use the second form. With the owner's name the tool tests every pattern against
the address and derives the convention with certainty, then applies it to
everyone else at that company. One sample fixes a whole company.

### Finding a sample address (free, best yield first)

1. **GitHub commit authorship** — corporate addresses leak into commit metadata
   constantly. Clone a public repo of theirs and run:
   ```bash
   git log --format=%ae | sort -u | grep -i acme.com
   ```
2. **Press releases / newsroom** — media contacts are published addresses:
   `site:acme.com "@acme.com"`
3. **PDFs** — `filetype:pdf "@acme.com"` finds brochures and investor decks with
   named contacts.
4. **Any recruiter from that company who already emailed you** — their signature
   is a confirmed sample.
5. **Apply to one posting** — the confirmation often comes from a real corporate
   address.

## How the guess is ranked

In descending order of value:

1. A confirmed sample for that domain — overrides everything (97% confidence).
2. **Sector prior.** Indian IT services run `first.last@` far above the global
   base rate; US startups skew `first@`; large US enterprises skew toward short
   aliases like `flast@`. Set `sector` per row.
3. **Global base rates** — roughly `first.last@` 38%, `first@` 17%, `flast@` 14%,
   then a long tail.
4. **Mail provider** from the MX records — a weak tiebreaker that mostly
   predicts company size and formality.

Patterns ruled out by a previous bounce are excluded.

## Safeguards

- `send` is a **dry run** unless you pass `--live`, and prompts before sending.
- One message per recipient. There is no code path that puts two recipients on
  one message — a visible CC list ends a referral conversation instantly.
- `max_per_day` (default 40) sits far under Gmail's ~500/day so a bad run cannot
  cost you the account.
- Nobody is mailed twice: `sent_log.csv` is checked before every send.
- If `sender.resume` points at a missing file, the run stops rather than sending
  a referral request with no resume attached.
- Empty placeholders are flagged in `preview` and their artifacts (`()`, doubled
  spaces) are cleaned before sending.
- The app password is read only from `.env`, is masked in tracebacks, and is
  never written to the log.

## Things worth knowing

- **A generic blast gets nothing.** The referral emails that work name a
  specific thing you built. Rewrite the middle paragraph of `template.txt` per
  company if you can be bothered — it is the whole mechanism.
- **Silence is not success.** Corporate filters quarantine plenty of legitimate
  mail without ever generating a bounce. A clean `reconcile` means "not
  provably dead", not "read".
- **LinkedIn beats email for referrals.** A connection request with a note has
  no guessing problem at all. Email is the fallback channel.
- **Guessed domains are unverified.** `discover` will happily find that
  `northwind.com` accepts mail; that is no proof it belongs to the Northwind
  Labs you meant. It warns, but the check is yours to do.
- **Under 20 people, manual lookup wins.** Two minutes each on LinkedIn plus one
  GitHub check per company gets near-100% accuracy. Automation pays off at
  scale, and 20 is not scale.
- **This project lives in OneDrive.** A file mid-sync, or one left open in
  Excel, is locked for writing and `add`/`send` will refuse with a clear
  message. Close Excel, let sync finish, retry.

## Tests

```bash
python test_referral.py
```

Covers name parsing, address construction, pattern inference, ranking,
rendering, domain slugs, provider classification, and bounce parsing — the parts
that fail silently rather than loudly.
