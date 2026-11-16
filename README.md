# Mailmint

A local job-application assistant. For each job posting it:

1. builds a **tailored one-page, ATS-friendly resume** (LaTeX, fixed format),
2. writes the **career objective** from the job description,
3. **tracks the application** in Excel,
4. guesses corporate email addresses for the people you want a **referral** from,
5. sends each person a personalised mail through Gmail, and
6. reads the bounces afterwards so the next attempt is smarter.

Everything runs on your own machine. Your Gmail app password never leaves it.

---

## Quick start

1. **Install** (one time):

   ```bash
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   copy .env.example .env
   ```

   Edit `.env` and set `GMAIL_USER` and `GMAIL_APP_PASSWORD` (a 16-character
   **App Password** from <https://myaccount.google.com/apppasswords>, not your
   Google password; 2-Step Verification must be on). Never paste it in chat or
   commit it.

2. **Get the LaTeX compiler** (one time). Resumes compile with
   [Tectonic](https://tectonic-typesetting.github.io): put `tectonic.exe` in
   `tools/tectonic/` (or anywhere on `PATH`). The first build downloads LaTeX
   packages, so it needs internet and takes a little longer.

3. **Edit your details**:
   - `resume/profile.yaml` - contact info, experience, skills, certifications
   - `resume/projects.yaml` - your project pool
   - `config.yaml` - `sender.name`, Drive folder, send limits
   - `template.txt` - the default email

4. **Run the interface**: double-click `start_ui.bat` (or
   `python run.py ui`). A page opens at <http://127.0.0.1:5000>.

If PowerShell blocks activating the venv, skip activation and call the
interpreter directly: `.\.venv\Scripts\python.exe run.py ...`

---

## The web interface

Five parts, top to bottom:

1. **Job posting** - company, role, job ID, apply link, mail domain, and the job
   description. Paste the description and press **Auto-fill from description**:
   it fills company, role, job ID, apply link and the skills it finds (it never
   overwrites something you typed).
2. **Tailored resume** - builds the PDF and shows it as a full page right there:
   chosen experience, projects and certificates, the objective, and an **ATS
   keyword match** (e.g. "9 of 10 job skills appear in the resume" plus which are
   missing). Download it or open it in a new tab.
3. **Applications tracker** - every build is logged; change the status
   (Resume built / Applied / Interview / Offer / Rejected / No response) from the
   dropdown.
4. **People to email** - paste names from a LinkedIn people search; optionally
   add a custom subject and body for the batch.
5. **Preview and send** - renders each email, then **Send for real** after a
   confirmation. The resume you just built is attached. Keep the window open
   while a send runs (messages are spaced out on purpose).

The server only accepts requests from your own machine, and nothing is sent
without your confirmation. `reconcile` and `status` are still command-line.

---

## Tailored resume

The layout lives in `resume/template.tex` and never changes between jobs (a
single-column ATS-safe layout based on Jake Gutierrez's MIT-licensed template).
Only the content changes:

- **Projects** are ranked by how well their `tags` match the job's keywords and
  role title; the top 3 are used. Force specific ones with `--projects`.
- **Experience** has variants in `profile.yaml`. Data roles use your original
  Optimus bullets; software, full-stack and GenAI roles use the OptiStudio
  bullets (and OptiStudio is then left out of Projects so it isn't repeated).
  Force one with `--experience NAME`.
- **Certificates** are chosen per job by tag match, never padded: a role with no
  relevant certificates shows no Certifications section.
- **Career objective** is written from the posting using only skills you list in
  `profile.yaml`, so it never claims something you don't have. A `--summary`
  you supply overrides it.
- **One page**: if it overflows, spare certificates go first, then bullets, then
  the lowest-ranked project.
- **Dates**: projects have no stored dates; each build assigns dates from the last
  six months, most relevant project most recent.
- **ATS**: real text (no images), standard section names, visible link URLs that
  are also clickable, correct PDF title/author, and a keyword check on every
  build.
- **Output**: `resumes/Aaditee_Agrawal_<Role>_<Company>.pdf` and `.tex`, copied
  to your Google Drive folder if `resume.drive_dir` is set.

```bash
python run.py resume --company "CGI" --role "Data Engineer" --keywords "pyspark,sql" --jd-file jd.txt
python run.py resume --company "Acme" --role "BI Developer" --projects medallion-lakehouse,cdc-ingestion
python run.py resume --list-projects
```

Other flags: `--job-id`, `--apply-link`, `--max-projects`, `--max-certs`,
`--summary`, `--experience`.

**Overleaf**: paste any generated `.tex` into a new Overleaf project to edit it
by hand; it compiles there as-is.

### Adding to your pool

- **Project**: copy a block in `resume/projects.yaml`. Give it an `id`, `title`,
  `tags` (lowercase skills a job might mention) and 1-4 `bullets`, strongest
  first (the last ones are trimmed when space runs out).
- **Certificate**: add `{name: "...", tags: [...]}` under `certifications` in
  `profile.yaml`. Add `pin: true` to force one onto every resume.
- **Experience variant**: add a block under `variants:` with `tags`, `bullets`,
  and optionally `objective_focus`, `skills` and `exclude_projects`.

### Google Drive

Install Google Drive for desktop, then set the folder in `config.yaml`:

```yaml
resume:
  output_dir: resumes
  drive_dir: "G:/My Drive/resumes"
```

The PDF and `.tex` are copied there on every build (same company and role
overwrites the earlier file). Leave it blank to skip.

---

## Application tracker

Every resume build adds or updates a row in `applications.xlsx` (path set under
`tracker.path` in `config.yaml`): date, company, role, job ID, apply link,
status, resume file, Drive location, experience used, projects used, skills, the
full job description, and notes. Rebuilding for the same company + role + job ID
updates that row and keeps your status and notes.

Close the file in Excel before building, or the save is refused (the tool tells
you).

---

## Custom mails (not just the one template)

1. **One-off mail to any address** - no guessing, no `people.csv`:

   ```bash
   python run.py compose --to someone@acme.com --subject "Quick question" --body-file note.txt --attach Data-resume.pdf
   ```

   Omit `--body` to type it (finish with a line containing only `.`). Several
   recipients: `--to a@x.com,b@y.com`. Dry run unless `--live`; every recipient
   gets a separate message.
2. **Per-person text** - fill `subject` and/or `body` columns in `people.csv`;
   they replace the template for that person. Use a literal `\n` for line breaks
   or a quoted multi-line cell. Placeholders work.
3. **A different template per batch** - `send --template other.txt` (also on
   `preview`).

Any extra column in `people.csv` (say `project`) becomes a `{project}`
placeholder.

Placeholders: `{first_name}`, `{last_name}`, `{full_name}`, `{company}`, `{role}`,
`{job_id}`, `{notes}`, `{sender_name}`.

---

## Referral mail workflow

### The honest limitation

**There is no free way to check whether a specific mailbox exists.** DNS tells
you a *domain* accepts mail, not that `priya.nair@` exists there. The SMTP
`RCPT TO` trick is defeated by Google Workspace and Microsoft 365 (they accept
everything at handshake time and bounce later), is often blocked on port 25, and
gets your IP blocklisted. This tool does not use it.

So the verifier is **the bounce log**, which runs *after* sending: send the top
guess, wait, run `reconcile`, and the delivery failures show which addresses
were wrong. At fewer than 20 recipients that is entirely workable.

**The highest-leverage thing you can do is find one real address per company.**
That turns guessing (~40-70% right) into knowing (~95%+). `discover` prints
where to look.

### One company at a time

```bash
python run.py add --company "Acme Technologies" --domain acmetech.com --role "Senior Data Engineer" --job-id JR-40912 --sector indian_services
```

Then paste names straight from LinkedIn people-search results and press Enter
twice. Headlines, locations, `View profile`, pronouns, `2nd`/`3rd+` markers,
honorifics and `, PMP` suffixes are stripped; anything it isn't sure about is
listed as ignored. It shows the parsed list and asks before writing. (The web
interface does the same thing in part 4.)

`--sample "Ravi Menon|ravi.menon@acmetech.com"` is the flag worth hunting for: it
locks the address pattern for everyone at that company.

Then, scoped to that company:

```bash
python run.py discover --company Acme
python run.py preview --company Acme
python run.py send --company Acme --live
python run.py reconcile
```

`--company` is a case-insensitive substring match on company or domain.
`reconcile` is deliberately not scoped: it reads your whole inbox, so run it once
after whatever you sent that day.

### Command reference

| Command | What it does |
|---|---|
| `ui` | Open the web interface |
| `resume` | Build a tailored one-page resume |
| `add` | Add one company's people to `people.csv` |
| `discover` | Resolve each company's mail domain, provider and address pattern; writes `learned_patterns.json` |
| `preview` | Render every address and every email; sends nothing - **the step to actually read** |
| `send --self-test` | Send one fully rendered message **to yourself** (checks the password, attachment, placeholders, spam placement) |
| `send --live` | Send for real, one message per recipient, randomised 15-60s gap (set in `config.yaml`); prompts first; interrupted runs resume |
| `compose` | Send a free-form custom mail to any address |
| `reconcile` | ~15 min after sending: read bounces and replies over IMAP, mark dead addresses, rule out that pattern. Needs IMAP enabled in Gmail |
| `preview --retry-bounced` / `send --retry-bounced --live` | Retry bounced people at their next-best address |
| `status` | What was sent, bounced, learned, and how much of today's cap is used |

### Metrics

`reconcile` and `status` report a funnel: attempted, bounced, delivered,
auto-replied (mailbox confirmed live), replied (a human answered).

There is deliberately **no open tracking**: it needs HTML mail with a remote
pixel (a spam signal for cold mail), corporate clients block remote images, and
Gmail proxies what loads, so the number would be noise bought at a deliverability
cost. What is measured is reliable:

- **bounced** - a delivery failure naming that address. Definitive.
- **auto-replied** - out-of-office etc. *Stronger* than an open: a real mailbox
  received it.
- **replied** - a human wrote back. The only metric that converts.

Replies are matched by sender address or by threading onto your `Message-ID`. A
human reply always outranks an auto-reply. **No bounce and no reply means "not
provably dead" - nothing more.**

### people.csv columns

`add` writes these for you; edit by hand only to override something.

| Column | Required | Notes |
|---|---|---|
| `first` | yes | or supply `full_name` |
| `last` | | without it only single-token guesses are possible |
| `full_name` | | alternative to `first`/`last` |
| `company` | yes* | for `{company}` and domain guessing |
| `domain` | | the mail domain; supplying it removes a layer of guessing |
| `role` | | `{role}` |
| `job_id` | | `{job_id}` |
| `sample_email` | | **the important one** - see below |
| `sector` | | `indian_services`, `indian_product`, `us_startup`, `us_enterprise` |
| `notes` | | `{notes}` |
| `email` | | a known-good address; skips guessing for that person |
| `subject`, `body` | | custom text for that person |

\* `company` or `domain`, at least one.

**`sample_email`** takes two forms:

```
ravi.menon@acme.com                 # narrows the pattern structurally
Ravi Menon|ravi.menon@acme.com      # CONFIRMS the pattern outright
```

Use the second: with the owner's name every pattern is tested against the
address and the convention is derived with certainty, then applied to everyone
at that company.

**Finding a sample address (free, best yield first):**

1. **GitHub commit authorship** - clone a public repo of theirs and run
   `git log --format=%ae | sort -u | grep -i acme.com`
2. **Press releases / newsroom** - `site:acme.com "@acme.com"`
3. **PDFs** - `filetype:pdf "@acme.com"`
4. **A recruiter from that company who already emailed you** - their signature
   is a confirmed sample.
5. **Apply to one posting** - the confirmation often comes from a real address.

### How the guess is ranked

1. A confirmed sample for that domain - overrides everything (97% confidence).
2. **Sector prior** - Indian IT services run `first.last@` far above the base
   rate; US startups skew `first@`; large US enterprises skew toward `flast@`.
3. **Global base rates** - roughly `first.last@` 38%, `first@` 17%, `flast@` 14%.
4. **Mail provider** from the MX records - a weak tiebreaker.

Patterns ruled out by a previous bounce are excluded.

---

## Safeguards

- `send` is a **dry run** unless you pass `--live`, and prompts first (the web
  interface asks you to confirm).
- One message per recipient. No code path puts two recipients on one message.
- `send.max_per_day` and `send.max_per_run` in `config.yaml` sit far under
  Gmail's ~500/day so a bad run cannot cost you the account.
- Nobody is mailed twice: `sent_log.csv` is checked before every send.
- If the attachment is missing, the run stops rather than sending without it.
- Empty placeholders are flagged in `preview`; their artefacts (`()`, doubled
  spaces) are cleaned before sending.
- The app password is read only from `.env`, masked in tracebacks, never logged.

## Things worth knowing

- **A generic blast gets nothing.** Referral emails that work name something
  specific you built. Per-person or per-batch custom text is the whole mechanism.
- **Silence is not success.** Corporate filters quarantine legitimate mail without
  a bounce. A clean `reconcile` means "not provably dead", not "read".
- **LinkedIn beats email for referrals.** A connection request with a note has no
  guessing problem. Email is the fallback channel.
- **Guessed domains are unverified.** `discover` may find that `northwind.com`
  accepts mail; that doesn't prove it's the Northwind you meant. It warns; the
  check is yours.
- **Under 20 people, manual lookup wins.** Two minutes each on LinkedIn plus one
  GitHub check per company gets near-100% accuracy.
- **This project lives in OneDrive.** A file mid-sync, or open in Excel (including
  `applications.xlsx` and `people.csv`), is locked for writing. Close it, let sync
  finish, retry.
- **Back up your history.** `sent_log.csv`, `people.csv`, `learned_patterns.json`
  and `applications.xlsx` are your state (all git-ignored). OneDrive covers them.

## Files

```
run.py                     entry point
start_ui.bat               double-click to open the web interface
config.yaml                sender, send limits, DNS, Drive folder, tracker path
template.txt               default email (Subject: line + body)
people.csv                 recipients
sent_log.csv               every send attempt (dedupe + bounce matching)
learned_patterns.json      address patterns learned per company
applications.xlsx          application tracker
resume/                    template.tex, profile.yaml, projects.yaml
resumes/                   generated PDFs and .tex files
referral_mailer/           the code (cli, ui, resume, tracker, planner, ...)
tools/tectonic/            LaTeX compiler (not committed)
test_referral.py           tests
```

## Tests

```bash
python test_referral.py
```

Covers name parsing, address construction, pattern inference, ranking,
rendering, domain slugs, provider classification and bounce parsing - the parts
that fail silently rather than loudly. The resume, tracker and web-interface code
are not covered by it.
