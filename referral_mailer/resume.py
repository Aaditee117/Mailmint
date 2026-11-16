"""Tailored one-page LaTeX resumes: fixed layout, per-job project selection.

The layout lives in resume/template.tex and never changes. Per job, this picks
projects from resume/projects.yaml, renders the LaTeX, compiles it with
Tectonic, checks it is one page (trimming if not), names the PDF
Name_Role_Company.pdf and copies it to a Google Drive folder.
"""

from __future__ import annotations

import re
from datetime import date
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

from .config import Config, ConfigError, _resolve


class ResumeError(RuntimeError):
    pass


_LATEX_SPECIALS = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
    "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def tex(text: str) -> str:
    """Escape plain text for LaTeX; **double stars** become bold."""
    text = str(text or "")
    parts = re.split(r"\*\*(.+?)\*\*", text)
    out = []
    for i, part in enumerate(parts):
        escaped = "".join(_LATEX_SPECIALS.get(c, c) for c in part)
        out.append(r"\textbf{" + escaped + "}" if i % 2 else escaped)
    return "".join(out)


def slug(text: str) -> str:
    words = re.sub(r"[^A-Za-z0-9]+", " ", text or "").split()
    return "_".join(w if w.isupper() else w.capitalize() for w in words)


def output_name(name: str, role: str, company: str) -> str:
    return f"{slug(name)}_{slug(role)}_{slug(company)}"


@dataclass
class Project:
    id: str
    title: str
    tech: str
    tags: list[str]
    bullets: list[str]
    dates: str = ""  # never stored; filled in at build time by assign_dates


def _load_yaml(path: Path):
    if not path.exists():
        raise ResumeError(f"missing {path}")
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_projects(path: Path) -> list[Project]:
    data = _load_yaml(path) or []
    projects = []
    for item in data:
        if not item.get("id") or not item.get("title"):
            raise ResumeError(f"{path}: every project needs an 'id' and a 'title'")
        projects.append(Project(
            id=str(item["id"]),
            title=item["title"],
            tech=item.get("tech", "") or "",
            tags=[str(t).lower() for t in item.get("tags") or []],
            bullets=list(item.get("bullets") or []),
        ))
    return projects


# Role-title words that say nothing about skills; matching on them pulls in
# unrelated items ("developer" would match every web certification).
GENERIC_WORDS = {
    "developer", "engineer", "engineering", "software", "senior", "junior", "sr", "jr",
    "associate", "trainee", "intern", "lead", "specialist", "executive", "graduate",
    "fresher", "data", "analyst", "consultant", "manager", "and", "the", "of", "for", "with",
}


def _hit(word: str, tag: str) -> bool:
    """Whole-word/phrase match in either direction (so "bi" never hits "mobile")."""
    if tag in GENERIC_WORDS or word in GENERIC_WORDS:
        return False
    return bool(
        re.search(rf"(?<!\w){re.escape(tag)}(?!\w)", word)
        or re.search(rf"(?<!\w){re.escape(word)}(?!\w)", tag)
    )


def job_terms(keywords: list[str], role: str) -> list[str]:
    """Explicit keywords, the full role title, and its non-generic words."""
    terms = [k.strip().lower() for k in keywords or [] if k.strip()]
    terms.append(role.strip().lower())
    terms += [w for w in re.split(r"\W+", role.lower()) if w and w not in GENERIC_WORDS]
    return terms


def rank_projects(projects: list[Project], keywords: list[str]) -> list[Project]:
    """Order by how many keywords appear in a project's tags/title/bullets.

    Tag hits count triple - they are the curated signal - and anything that
    merely appears in the text counts once. Ties keep file order.
    """
    words = {k.strip().lower() for k in keywords if len(k.strip()) > 1}

    def score(p: Project) -> int:
        text = (p.title + " " + " ".join(p.bullets)).lower()
        total = 0
        for w in words:
            if any(_hit(w, t) for t in p.tags):
                total += 3
            elif _hit(w, text):
                total += 1
        return total

    return sorted(projects, key=lambda p: -score(p))


def select_certs(profile: dict, keywords: list[str], max_certs: int) -> tuple[list[str], int]:
    """Pick certifications: pinned ones always, then best keyword matches.

    A cert is a plain string (pinned) or {name, tags, pin}. Returns
    (names in display order, number of pinned).
    """
    pinned: list[str] = []
    pool: list[tuple[str, list[str]]] = []
    for item in profile.get("certifications") or []:
        if isinstance(item, str):
            pinned.append(item)
        elif item.get("pin"):
            pinned.append(item["name"])
        else:
            pool.append((item["name"], [str(t).lower() for t in item.get("tags") or []]))

    words = {k.strip().lower() for k in keywords if len(k.strip()) > 1}

    def score(entry: tuple[str, list[str]]) -> int:
        name, tags = entry
        total = 0
        for w in words:
            if any(_hit(w, t) for t in tags):
                total += 3
            elif _hit(w, name.lower()):
                total += 1
        return total

    ranked = sorted(pool, key=lambda e: -score(e))
    room = max(0, max_certs - len(pinned))
    # Only fill spare slots with certs that actually matched something.
    extra = [name for name, tags in ranked[:room] if score((name, tags)) > 0]
    return pinned + extra, len(pinned)


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _month_label(index: int) -> str:
    """index counts months since year 0 (year*12 + month0)."""
    return f"{_MONTHS[index % 12]} {index // 12}"


def assign_dates(projects: list[Project], today: date | None = None) -> list[tuple[Project, str]]:
    """Give each project a date range inside the last six months.

    Nothing is stored: the window slides with today's date. The window is split
    evenly (at most 3 months each); the first project - the most relevant - gets
    the most recent slot.
    """
    today = today or date.today()
    now = today.year * 12 + today.month - 1
    n = max(1, len(projects))
    span = max(1, min(3, 6 // n))
    out = []
    for i, project in enumerate(projects):
        end = now - i * span
        start = end - span + 1
        label = (_month_label(start) + " -- " + _month_label(end)) if span > 1 else _month_label(end)
        out.append((project, label))
    return out


def apply_experience_variant(
    profile: dict, terms: list[str], force: str | None = None
) -> tuple[dict, str, set[str]]:
    """Swap each experience entry's bullets for the variant that best fits the job.

    Returns (profile copy, variant name used or "default", project ids to leave
    out of the Projects section because the variant already covers them).
    """
    import copy

    profile = copy.deepcopy(profile)
    used, excluded = "default", set()
    for entry in profile.get("experience") or []:
        variants = entry.get("variants") or []
        best, best_score = None, 0
        for variant in variants:
            if force:
                score = 1 if variant.get("name") == force else 0
            else:
                tags = [str(t).lower() for t in variant.get("tags") or []]
                score = sum(1 for term in terms if any(_hit(term, t) for t in tags))
            if score > best_score:
                best, best_score = variant, score
        if force and force != "default" and best is None:
            raise ResumeError(
                f"unknown experience variant {force!r}. Available: default, "
                + ", ".join(v.get("name", "?") for v in variants)
            )
        if best:
            entry["bullets"] = best["bullets"]
            used = best.get("name", "variant")
            if best.get("objective_focus"):
                profile["_focus"] = best["objective_focus"]
            if best.get("skills"):
                profile["_extra_skills"] = list(best["skills"])
            excluded |= set(best.get("exclude_projects") or [])
        entry.pop("variants", None)
    return profile, used, excluded


def _section(title: str, body: str) -> str:
    return f"\\section{{{title}}}\n{body}\n\\vspace{{-10pt}}\n" if body.strip() else ""



def _skill_items(profile: dict) -> list[str]:
    """Individual skills from profile.skills (commas inside parentheses are kept)."""
    items = []
    for value in (profile.get("skills") or {}).values():
        for part in re.split(r",\s*(?![^()]*\))", str(value)):
            name = re.sub(r"\s*\(.*?\)", "", part).strip()
            if name:
                items.append(name)
    items.extend(str(s) for s in profile.get("_extra_skills") or [])
    return items


def _contains(text: str, term: str) -> bool:
    return bool(re.search(rf"(?<![\w+#]){re.escape(term.lower())}(?![\w+#])", text.lower()))


def generate_objective(
    profile: dict, company: str, role: str, jd: str, keywords: list[str]
) -> str | None:
    """Write the career objective from the posting.

    Only skills you actually list in profile.yaml can appear, picked by how early
    the job description mentions them, so the text never claims something you
    don't have. Returns None when too little matches (the default objective is
    used instead).
    """
    haystack = (jd or "") + "\n" + ", ".join(keywords)
    found = []
    for skill in dict.fromkeys(_skill_items(profile)):
        if _contains(haystack, skill):
            position = haystack.lower().find(skill.lower())
            found.append((position if position >= 0 else 10**9, skill))
    skills = [s for _, s in sorted(found)][:6]
    focus = profile.get("_focus") or profile.get("objective_focus") or "building reliable software solutions"
    title = role.strip()[:1].upper() + role.strip()[1:]
    if len(skills) > 2:
        listed = ", ".join(skills[:-1]) + ", and " + skills[-1]
    else:
        listed = " and ".join(skills)
    where = f" at {company.strip()}" if company.strip() else ""
    working = f", working with {listed}" if skills else ""
    return (
        f"{title} with hands-on experience {focus}{working}. "
        f"Seeking to apply this experience to deliver reliable, high-quality solutions{where}."
    )


def ats_report(config: Config, pdf: Path, keywords: list[str], jd: str) -> dict:
    """Which of the posting's skills made it into the resume text an ATS reads."""
    from pypdf import PdfReader

    text = " ".join((page.extract_text() or "") for page in PdfReader(str(pdf)).pages)
    wanted = {k.strip().lower() for k in keywords if len(k.strip()) > 1}
    if jd:
        wanted |= set(extract_keywords(config, jd))
    wanted = {w for w in wanted if w not in GENERIC_WORDS}
    matched = sorted(w for w in wanted if _contains(text, w))
    missing = sorted(w for w in wanted if w not in matched)
    return {"matched": matched, "missing": missing}


def _link(url: str, label: str) -> str:
    """A clickable link that also looks like one (the template hides link boxes)."""
    return rf"\href{{{url}}}{{\textcolor{{blue}}{{\underline{{{label}}}}}}}"


def _header(profile: dict) -> str:
    c = profile.get("contact") or {}
    line1 = [tex(c[k]) for k in ("phone", "location") if c.get(k)]
    if c.get("email"):
        line1.append(_link(f"mailto:{c['email']}", tex(c["email"])))
    # Show the URL itself as text: an ATS reads text, not link annotations.
    line2 = []
    for link in profile.get("links") or []:
        shown = re.sub(r"^https?://(www\.)?", "", link["url"]).rstrip("/")
        line2.append(_link(link["url"], tex(shown)))
    second = (" \\\\\n    \\small " + " $|$ ".join(line2)) if line2 else ""
    return (
        "\\begin{center}\n"
        f"    \\textbf{{\\Huge \\scshape {tex(profile.get('name'))}}} \\\\ \\vspace{{2pt}}\n"
        f"    \\small {' $|$ '.join(line1)}{second}\n"
        "\\end{center}\n\\vspace{-8pt}"
    )


def _text_block(lines: list[str]) -> str:
    """A label-less list: one escaped line each, joined with \\\\."""
    inner = " \\\\\n     ".join(lines)
    return (
        "\\begin{itemize}[leftmargin=0.15in, label={}]\n"
        f"    \\small{{\\item{{\n     {inner}\n    }}}}\n"
        "\\end{itemize}"
    )


def _items(bullets: list[str], limit: int | None) -> str:
    bullets = [b for b in bullets if str(b).strip()][:limit]
    body = "\n".join(f"        \\resumeItem{{{tex(b)}}}" for b in bullets)
    return f"    \\resumeItemListStart\n{body}\n    \\resumeItemListEnd"


def render_latex(
    template: str,
    profile: dict,
    projects: list[Project],
    summary: str | None = None,
    max_bullets: int | None = None,
    exp_bullets: int | None = None,
    certs: list[str] | None = None,
    today: date | None = None,
) -> str:
    if certs is None:
        certs = select_certs(profile, [], 999)[0]
    dated_projects = []
    for p, dates in assign_dates(projects, today):
        dated_projects.append(replace(p, dates=dates))
    objective = (summary if summary is not None else profile.get("objective", "")) or ""
    objective_block = ""
    if objective.strip():
        objective_block = _section(
            "Career Objective", _text_block([tex(objective)])
        )

    edu = "".join(
        f"    \\resumeSubheading\n      {{{tex(e.get('school'))}}}{{{tex(e.get('location'))}}}\n"
        f"      {{{tex(e.get('degree')).replace('|', '$|$')}}}{{{tex(e.get('dates'))}}}\n"
        for e in profile.get("education") or []
    )
    education = _section(
        "Education", f"\\resumeSubHeadingListStart\n{edu}\\resumeSubHeadingListEnd"
    )

    skills = _section("Technical Skills", _text_block([
        f"\\textbf{{{tex(k)}}}{{: {tex(v)}}}" for k, v in (profile.get("skills") or {}).items()
    ]) if profile.get("skills") else "")

    exp = "".join(
        f"\\resumeProjectHeading\n    {{\\textbf{{{tex(e.get('title'))}}}}}{{{tex(e.get('dates'))}}}\n"
        + _items(e.get("bullets") or [], exp_bullets) + "\n"
        for e in profile.get("experience") or []
    )
    experience = _section(
        "Experience", f"\\resumeSubHeadingListStart\n{exp}\\resumeSubHeadingListEnd"
    ) if exp else ""

    proj = "".join(
        f"\\resumeProjectHeading\n    {{\\textbf{{{tex(p.title)}}}"
        + (f" $|$ \\textit{{{tex(p.tech)}}}" if p.tech else "")
        + f"}}{{{tex(p.dates)}}}\n"
        + _items(p.bullets, max_bullets) + "\n"
        for p in dated_projects
    )
    projects_block = _section(
        "Projects", f"\\resumeSubHeadingListStart\n{proj}\\resumeSubHeadingListEnd"
    )

    certs = _section("Certifications", _text_block([tex(c) for c in certs]) if certs else "")
    achievements = _section("Achievements", _text_block(
        [tex(a) for a in profile.get("achievements") or []]
    ) if profile.get("achievements") else "")
    # The last section needs no trailing negative space.
    achievements = achievements.replace("\\vspace{-10pt}\n", "")

    fills = {
        "NAME": tex(profile.get("name")),
        "HEADER": _header(profile),
        "OBJECTIVE": objective_block,
        "EDUCATION": education,
        "SKILLS": skills,
        "EXPERIENCE": experience,
        "PROJECTS": projects_block,
        "CERTS": certs,
        "ACHIEVEMENTS": achievements,
    }
    for key, value in fills.items():
        template = template.replace(f"<<{key}>>", value)
    return template


def extract_keywords(config: Config, text: str) -> list[str]:
    """Skills mentioned in a job description, found by matching the known tag
    vocabulary (every tag on your projects and certifications) as whole words."""
    base = config.root / "resume"
    vocab: set[str] = set()
    for project in load_projects(base / "projects.yaml"):
        vocab.update(project.tags)
    profile = _load_yaml(base / "profile.yaml") or {}
    for item in profile.get("certifications") or []:
        if isinstance(item, dict):
            vocab.update(str(t).lower() for t in item.get("tags") or [])
    for value in (profile.get("skills") or {}).values():
        vocab.update(x.strip().lower() for x in re.split(r"[,/]", str(value)) if x.strip())
    lowered = text.lower()
    found = [
        v for v in vocab
        if v not in GENERIC_WORDS and len(v) > 1
        and re.search(rf"(?<!\w){re.escape(v)}(?!\w)", lowered)
    ]
    return sorted(found)


def extract_job_fields(text: str) -> dict:
    """Best-effort company / role / job id from a pasted job description."""
    out = {"company": "", "role": "", "job_id": "", "apply_link": ""}
    urls = re.findall(r"https?://[^\s<>\")]+", text)
    preferred = [u for u in urls if re.search(r"apply|career|job|position|requisition", u, re.I)]
    if preferred or urls:
        out["apply_link"] = (preferred or urls)[0].rstrip(".,;")
    lines = [ln.strip(" \t*#-•|") for ln in text.splitlines() if ln.strip()]

    m = re.search(
        r"(?:job\s*id|job\s*code|job\s*number|req(?:uisition)?\s*(?:id|no\.?|number|#)?|"
        r"requisition|reference\s*(?:id|number|code)?)\s*[:#\-]?\s*([A-Za-z0-9][A-Za-z0-9\-_/]{3,})",
        text, re.I)
    if m and re.search(r"\d", m.group(1)):
        out["job_id"] = m.group(1).strip("-_/")

    m = re.search(r"(?:job\s*title|position(?:\s*title)?|role)\s*[:\-]\s*(.+)", text, re.I)
    if m:
        out["role"] = m.group(1).strip()[:80]
    elif lines and len(lines[0]) <= 80 and not lines[0].endswith((".", ",")):
        out["role"] = lines[0]

    m = re.search(r"(?:company|organi[sz]ation|employer)\s*[:\-]\s*(.+)", text, re.I)
    if m:
        out["company"] = m.group(1).strip()[:60]
    else:
        m = re.search(r"\b(?i:about)[ \t]+([A-Z][\w&.\-]*(?:[ \t]+[A-Z][\w&.\-]*){0,3})", text)
        if not m:
            m = re.search(r"\b(?i:at|join)[ \t]+([A-Z][\w&.\-]*(?:[ \t]+[A-Z][\w&.\-]*){0,3})", "\n".join(lines[:6]))
        if m:
            out["company"] = m.group(1).strip()

    # A role line like "Data Engineer at CGI" holds both.
    m = re.match(r"(.+?)\s+(?:at|@|-|–|\|)\s+([A-Z][\w&.\- ]{1,40})$", out["role"])
    if m:
        out["role"] = m.group(1).strip()
        out["company"] = out["company"] or m.group(2).strip()
    return out


def find_tectonic(config: Config) -> str:
    local = config.root / "tools" / "tectonic" / "tectonic.exe"
    if local.exists():
        return str(local)
    found = shutil.which("tectonic")
    if found:
        return found
    raise ResumeError(
        "Tectonic (LaTeX compiler) not found. Put tectonic.exe in "
        "tools/tectonic/ or on PATH (https://tectonic-typesetting.github.io)."
    )


def compile_pdf(config: Config, tex_path: Path) -> Path:
    cmd = [find_tectonic(config), "-X", "compile", "--keep-logs", "--outdir",
           str(tex_path.parent), str(tex_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired as exc:
        raise ResumeError("LaTeX compile timed out (first run downloads packages; "
                          "check your internet connection and retry)") from exc
    pdf = tex_path.with_suffix(".pdf")
    if proc.returncode != 0 or not pdf.exists():
        tail = "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-15:])
        raise ResumeError(f"LaTeX compile failed:\n{tail}")
    return pdf


def page_count(pdf: Path) -> int:
    from pypdf import PdfReader
    return len(PdfReader(str(pdf)).pages)


@dataclass
class ResumeResult:
    pdf: Path
    tex: Path
    projects: list[Project]
    pages: int
    drive_copy: Path | None
    notes: list[str]
    certs: list[str]
    experience: str = "default"
    objective: str = ""
    ats: dict | None = None


def build_resume(
    config: Config,
    company: str,
    role: str,
    keywords: list[str] | None = None,
    project_ids: list[str] | None = None,
    max_projects: int = 3,
    summary: str | None = None,
    max_certs: int = 6,
    experience: str | None = None,
    jd: str = "",
) -> ResumeResult:
    base = config.root / "resume"
    settings = config.raw.get("resume") or {}
    out_dir = _resolve(config.root, settings.get("output_dir", "resumes"))
    drive_value = (settings.get("drive_dir") or "").strip()

    profile = _load_yaml(base / "profile.yaml") or {}
    template = (base / "template.tex").read_text(encoding="utf-8")
    pool = load_projects(base / "projects.yaml")
    by_id = {p.id: p for p in pool}
    profile, exp_variant, excluded = apply_experience_variant(
        profile, job_terms(keywords or [], role), experience
    )

    notes: list[str] = []
    if project_ids:
        missing = [i for i in project_ids if i not in by_id]
        if missing:
            raise ResumeError(f"unknown project id(s): {', '.join(missing)}. "
                              f"Available: {', '.join(by_id)}")
        chosen = [by_id[i] for i in project_ids]
    else:
        ranked = [p for p in rank_projects(pool, job_terms(keywords or [], role))
                  if p.id not in excluded]
        chosen = ranked[:max_projects]
    if not chosen:
        raise ResumeError("no projects available - add some to resume/projects.yaml")

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = output_name(profile.get("name", "Resume"), role, company)
    tex_path = out_dir / f"{stem}.tex"

    # Fit to one page: trim bullets first, then drop the lowest-ranked project.
    # Explicitly requested projects are never dropped - that would silently
    # ignore the request - so those just get a warning.
    # Order of sacrifice: spare certifications, then bullets, then projects.
    cert_keywords = job_terms(keywords or [], role)
    # An explicit summary wins; otherwise write one from the posting.
    objective = summary
    if objective is None:
        objective = generate_objective(profile, company, role, jd, keywords or [])
    final_objective = objective if objective is not None else (profile.get("objective") or "")
    full_certs, pinned_n = select_certs(profile, cert_keywords, max_certs)
    cert_options = [full_certs]
    if len(full_certs) > pinned_n:
        cert_options.append(full_certs[:pinned_n])
    attempts = []
    for count in range(len(chosen), 0, -1):
        for cert_list in cert_options:
            for bullets in (None, 3, 2, 1):
                attempts.append((chosen[:count], bullets, cert_list))
    result_projects, pages, result_certs = chosen, 0, full_certs
    for subset, bullets, cert_list in attempts:
        if project_ids and len(subset) < len(chosen):
            break
        tex_path.write_text(
            render_latex(template, profile, subset, objective, bullets, None, cert_list),
            encoding="utf-8",
        )
        result_certs = cert_list
        pdf = compile_pdf(config, tex_path)
        pages = page_count(pdf)
        result_projects = subset
        if pages <= 1:
            if bullets or len(subset) < len(chosen) or len(cert_list) < len(full_certs):
                notes.append(
                    f"trimmed to fit one page ({len(subset)} project(s)"
                    + (f", max {bullets} bullets each" if bullets else "") + ")"
                )
            break
    if pages > 1:
        notes.append(f"STILL {pages} PAGES - shorten profile.yaml or the chosen projects")

    for junk in (".log", ".aux", ".out"):
        tex_path.with_suffix(junk).unlink(missing_ok=True)

    drive_copy = None
    if drive_value:
        drive_dir = Path(drive_value).expanduser()
        if drive_dir.parent.exists():
            drive_dir.mkdir(parents=True, exist_ok=True)
            drive_copy = drive_dir / pdf.name
            shutil.copy2(pdf, drive_copy)
            shutil.copy2(tex_path, drive_dir / tex_path.name)
        else:
            notes.append(f"Drive folder not available ({drive_dir.parent} missing) - "
                         "install/sign in to Google Drive for desktop; not uploaded")
    else:
        notes.append("resume.drive_dir not set in config.yaml - not copied to Drive")

    return ResumeResult(pdf, tex_path, result_projects, pages, drive_copy, notes, result_certs, exp_variant,
                        final_objective, ats_report(config, pdf, keywords or [], jd))
