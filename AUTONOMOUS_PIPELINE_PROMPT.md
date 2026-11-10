# Autonomous pipeline — discover jobs, discover people, outreach, track

This is what a scheduled Claude Code session executes, unattended, every time it
fires. Each run starts fresh with no memory of any previous run — everything it
needs to know about what's already been done lives in `applications_tracker.xlsx`,
`people.csv`, `sent_log.csv`, and `learned_patterns.json`. Read this document in
full and execute every step exactly, without asking anyone for confirmation at any
point. There is no human reviewing this before it acts.

Working directory: `C:\Users\kishan.payadi\OneDrive\KishanCode\Automation`. Every
command below is PowerShell syntax, run from that directory, using the project's
own venv interpreter — `.\.venv\Scripts\python.exe` — never a bare `python`.

If `mcp__Apify__*` tools are not already loaded, call `ToolSearch` first with
`select:mcp__Apify__call-actor,mcp__Apify__get-dataset-items,mcp__Apify__search-actors,mcp__Apify__fetch-actor-details`.

---

## STEP 0 — Preflight

Do one cheap Apify call (e.g. `mcp__Apify__fetch-actor-details` on
`memo23/linkedin-people-search`) before touching anything else. If it fails for
any reason, **stop the entire run right here** — do not touch `people.csv`,
`template.txt`, `sent_log.csv`, or `applications_tracker.xlsx`. Say briefly why,
and end.

---

## STEP 1 — Load state

1. `.\.venv\Scripts\python.exe tracker.py read` — parses as JSONL, one job per
   line. Build two sets from `status`:
   - **DONE** (`sent`, `skipped_not_relevant`, `skipped_pay_below_threshold`,
     `skipped_illegitimate`, `fully_exhausted`) — never reconsider these `job_key`s
     this cycle, full stop.
   - **QUEUE** (`discovered`, `needs_domain_review`) — carried over from earlier
     cycles (or added fresh in Steps 2-3 below); Step 4 works through this queue.
2. Read `job_search.yaml` for the Tier-1 `ats_companies` list and the Tier-2
   `aggregator_search` / `relevance` / `pay_threshold` settings.
3. Read `data-resume.pdf` and `software-resume.pdf` fresh — do not assume you
   remember their contents from a prior run.

---

## STEP 2 — Tier 1: fixed-company discovery

For each `{name, careers_url}` in `job_search.yaml`'s `ats_companies` (skip
entirely if the list is empty):

1. Call `khadinakbar/career-site-job-scraper` via `mcp__Apify__call-actor` with
   `startUrls: [careers_url]`, `maxResultsPerSite: 50`, `descriptionFormat: "text"`.
   Fetch results with `mcp__Apify__get-dataset-items`.
2. For each returned job, `job_key = "<company>|<jobId>"` using the job's own
   `company`/`jobId` fields from the actor's output. If already in **DONE**, skip
   it — don't even read its description.
3. Score the rest against `job_search.yaml`'s `relevance.target_roles`/`seniority`
   and what's actually in both resumes — the same judgment call
   `JOB_SEARCH_PROMPT.md` describes: entry-level/fresher (0-2 yrs) preferred, but
   surface a stretch role too if the skill match is strong. Check `postedAt`
   against `relevance.recency_days_default`; widen to `recency_days_widen_to`
   only if this company's results as a whole come back thin.
4. **Immediately** (don't batch this for later):
   - Fails relevance → `.\.venv\Scripts\python.exe tracker.py upsert --job-key
     "<key>" --company "..." --role "..." --job-id "..." --location "..."
     --source "<ats-name>" --link "<applyUrl>" --status skipped_not_relevant
     --notes "<one-line reason>"`.
   - Passes → `tracker.py upsert ... --status discovered` (same fields, no
     `--resume-used` yet — Step 4 sets that). This is what makes it show up in
     **QUEUE** even if this cycle never gets to it in Step 4.

---

## STEP 3 — Tier 2: broader search

Using `apricot_blackberry/job-board-aggregator` via `mcp__Apify__call-actor`,
once per `{keyword, location}` pair from `job_search.yaml`'s `aggregator_search`
(cross product of the two lists is fine for a small list):

1. Dedup key here is `job_key = "<company_normalized>|<title_normalized>|<location>"`
   (lowercased, whitespace-collapsed) — there's no stable requisition ID from an
   aggregator listing, unlike Tier 1. Skip anything already in **DONE**.
2. Apply the same relevance/recency rules as Step 2, **plus** the pay-threshold
   and legitimacy checks from `JOB_SEARCH_PROMPT.md`:
   - Internship stipend below `pay_threshold.internship_min_stipend_inr_per_month`
     → fails, *unless* `isRemote` is true and
     `internship_min_stipend_waived_if_fully_remote` is set.
   - Undisclosed pay → cross-reference AmbitionBox/Glassdoor for that company's
     typical pay for a similar role/level (WebSearch/WebFetch) as a credibility
     proxy; a company with zero public footprint anywhere is a caution flag.
3. Same immediate-upsert rule as Step 2: `skipped_pay_below_threshold` /
   `skipped_illegitimate` / `skipped_not_relevant` for failures (pick whichever
   reason actually applies), `discovered` for passes.

---

## STEP 4 — Work the queue

Build the actual work list from **QUEUE** (`tracker.py read`'s `discovered` and
`needs_domain_review` rows) — this includes both this cycle's fresh finds and
anything carried over from earlier cycles that didn't get reached. Process
Tier-1 (has a real `job_id`) rows before Tier-2 rows, oldest `date_first_found`
first within each tier. For each job in order:

1. **Resume selection.** Data Engineer / Data Analyst / data-pipeline-flavored JD
   → `data-resume.pdf`. General Software Engineer JD → `software-resume.pdf`.
   Same judgment call as the manual process — read the JD, pick the resume whose
   actual listed skills match it best.

2. **People discovery.** Call `memo23/linkedin-people-search` with `mode:
   "public"` **always** — never pass a `cookie`, and never use any LinkedIn
   account, real or otherwise, for this. Target the same priority order
   `MASTER_PROMPT.md` already specifies: recruiters and Talent Acquisition first,
   then HR Business Partners, then engineers/managers in the same function as the
   role, then senior HR leadership, then everyone else. In practice this usually
   means one call with `company: "<name>"` plus a `title`/`jobFunctions` filter
   aimed at recruiting/HR, and — if that comes back thin — a second call aimed at
   the relevant engineering function. `maxResults` around 15-20 per company is
   usually enough; there's no need to exhaust the search.

3. **Row construction.** The actor's `name` field is one string (needs
   splitting) and there is no clean `jobTitle` field — the person's actual title
   lives inside the free-text `about`/`summary`. For each person, write a short,
   factual `notes` phrase in the same style already used in `people.csv` (their
   job title and city, semicolon-separated — e.g. `"Senior Data Engineer;
   Bengaluru India"`), derived from `about`/`summary`/`location`. Write these as
   a JSON array to a scratch file (e.g. `_scratch\<company>_batch.json`, create
   the `_scratch` folder if it doesn't exist) as
   `[{"name": "...", "notes": "..."}, ...]`, then:
   ```
   .\.venv\Scripts\python.exe add_people_batch.py --company "<name>" ^
     --domain "<domain if already known, else omit>" --role "<role>" ^
     --job-id "<job_id if any>" --sector indian_services ^
     --batch-file "_scratch\<company>_batch.json"
   ```
   (omit `--sector` to fall back to `config.yaml`'s `guess.default_sector`, which
   is already `indian_services`). This script handles dedup against everyone
   already in `people.csv` — you don't need to check for duplicates yourself.
   **Never** write a guessed address into a `sample_email` — only a real,
   independently-found address; leave it blank otherwise (per `MASTER_PROMPT.md`:
   a fabricated one locks in a wrong pattern at 97% confidence).

4. `.\.venv\Scripts\python.exe run.py discover --company "<name>"`.

5. **Rewrite `template.txt` completely** (overwrite, don't append) for this
   company/role, following `MASTER_PROMPT.md` Step 4's content rules in full:
   only the 8 valid placeholders (`{first_name} {last_name} {full_name}
   {company} {role} {job_id} {notes} {sender_name}`), subject line
   `Subject: Referral Request - {role} ({job_id})`, `Hi {first_name},` greeting,
   100-160 word body, banned openers ("I hope this finds you well" and
   variants), 3-5 bullets mapping the *chosen resume's actual content* to this
   JD's stated requirements in the JD's own vocabulary, one sentence of concrete
   value-add before the ask, a specific low-friction ask, sign-off `Thanks,` plus
   literal phone/email/LinkedIn lines. Every claim must be traceable to the
   resume you picked in step 1 — if the JD wants something the resume doesn't
   have, don't claim it.

6. `.\.venv\Scripts\python.exe run.py preview --company "<name>" --resume
   "<chosen-resume>.pdf"`. Verify against `MASTER_PROMPT.md` Step 5's checklist
   (no unrecognised-column warning, no `TemplateError`, ranked addresses present,
   no leftover `{...}` or stray `()`/doubled spaces, attachment line matches the
   resume you chose). If anything fails, fix it and re-run preview — do not send
   on unverified output.

   **Domain-confidence judgment call:** if `discover` could only guess the
   domain from the company name (no sample_email found, no corroboration from
   the company's own site/careers page), and the company name is generic enough
   that the guess feels genuinely shaky, prefer marking this job
   `needs_domain_review` over sending blind — this is a judgment call, not a
   hard rule, and shouldn't block sending for ordinary, clearly-identified
   companies.

7. `.\.venv\Scripts\python.exe run.py send --company "<name>" --live --yes
   --resume "<chosen-resume>.pdf"`. `--yes` is mandatory — there is no human to
   answer the confirmation prompt. Read the output:
   - If it reports the daily cap already reached (`max_per_day`), **stop
     processing further jobs for the rest of this cycle** — do not attempt any
     more sends this run; everything remaining in the queue carries over to the
     next scheduled cycle automatically (their tracker status is still
     `discovered`/`needs_domain_review`). Go straight to Step 5.
   - If it sent N messages, `tracker.py upsert --job-key "<key>" --status sent
     --resume-used "<chosen-resume>.pdf" --people-contacted-delta N --link
     "<applyUrl>"`.
   - If nothing valid to send (no ranked address, everyone already contacted,
     etc.), `tracker.py upsert --job-key "<key>" --status needs_domain_review
     --notes "<why>"` and continue to the next job.

---

## STEP 5 — End of cycle

`.\.venv\Scripts\python.exe run.py reconcile` (unscoped — covers everything sent
today or earlier), then `.\.venv\Scripts\python.exe run.py status` (purely
informational). Nothing else to do — end the run.

---

## Notes carried over from the manual process, unchanged

- `send.max_per_day` / `send.max_per_run` in `config.yaml` are the send-volume
  safety net. Do not add a second cap on top of them.
- `sent_log.csv` already prevents ever re-mailing the same person twice, at any
  company, for any role — you don't need to think about person-level dedup
  yourself; `run.py`'s own `build_plans()` handles it.
- Never fill a person's `email` column with a guess — that column means
  "known-good, skip guessing entirely." Guesses only ever go through the normal
  ranking machinery.
