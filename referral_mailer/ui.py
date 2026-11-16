"""Local web interface: job posting in, tailored resume + referral mails out.

Binds to 127.0.0.1 only. State-changing requests must carry a custom header,
which a page on another site cannot add, so a stray tab cannot trigger a send.
"""

from __future__ import annotations

import threading
import webbrowser
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory

from .config import ConfigError, _resolve, load_config, load_credentials
from .dnscheck import configure_resolver
from .patterns import parse_name
from .people import append_people, load_people, parse_pasted_names, split_display_name
from .planner import build_plans
from .render import empty_placeholders, load_template, render
from .resume import ResumeError, build_resume, extract_job_fields, extract_keywords, load_projects
from .store import LearnedStore, SentLog
from .tracker import STATUSES, list_applications, log_application, set_status, tracker_path

STATIC = Path(__file__).parent / "static"
HEADER = "X-Mailmint"

_send_lock = threading.Lock()
_send_state: dict = {"running": False, "total": 0, "done": 0, "sent": 0, "failed": 0,
                     "lines": [], "finished": False, "error": ""}


def _csv_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in str(value or "").split(",") if v.strip()]


def create_app(config_path: str | None = None) -> Flask:
    app = Flask(__name__, static_folder=None)

    def cfg():
        return load_config(Path(config_path) if config_path else None)

    @app.before_request
    def guard():
        host = (request.host or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            abort(403)
        if request.method == "POST" and request.headers.get(HEADER) != "1":
            abort(403)

    @app.errorhandler(ResumeError)
    @app.errorhandler(ConfigError)
    def known_error(exc):
        return jsonify(error=str(exc)), 400

    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

    @app.get("/api/info")
    def info():
        config = cfg()
        projects = load_projects(config.root / "resume" / "projects.yaml")
        companies = []
        if config.people_path.exists():
            people, _ = load_people(config.people_path)
            companies = sorted({p.company for p in people if p.company})
        return jsonify(
            projects=[{"id": p.id, "title": p.title} for p in projects],
            companies=companies,
            sender=config.sender_name,
            drive_dir=(config.raw.get("resume") or {}).get("drive_dir", ""),
        )

    @app.post("/api/keywords")
    def keywords():
        text = (request.get_json(force=True) or {}).get("jd", "")
        return jsonify(keywords=extract_keywords(cfg(), text))

    @app.post("/api/autofill")
    def autofill():
        text = (request.get_json(force=True) or {}).get("jd", "")
        return jsonify(fields=extract_job_fields(text), keywords=extract_keywords(cfg(), text))

    @app.post("/api/resume")
    def resume():
        data = request.get_json(force=True) or {}
        if not data.get("company") or not data.get("role"):
            return jsonify(error="company and role are required"), 400
        config = cfg()
        result = build_resume(
            config,
            company=data["company"].strip(),
            role=data["role"].strip(),
            keywords=_csv_list(data.get("keywords")),
            project_ids=_csv_list(data.get("projects")),
            max_projects=int(data.get("max_projects") or 3),
            summary=(data.get("summary") or None),
            max_certs=int(data.get("max_certs") or 6),
            jd=data.get("jd") or "",
        )
        tracked = ""
        try:
            tracked = str(log_application(
                config, company=data["company"].strip(), role=data["role"].strip(),
                job_id=(data.get("job_id") or "").strip(),
                apply_link=(data.get("apply_link") or "").strip(),
                description=data.get("jd") or "",
                resume_pdf=result.pdf, drive_copy=result.drive_copy,
                experience=result.experience, objective=result.objective, ats=result.ats, projects=[p.title for p in result.projects],
                keywords=_csv_list(data.get("keywords")),
            ))
        except ConfigError as exc:
            result.notes.append(f"tracker not updated: {exc}")
        return jsonify(
            tracker=tracked,
            pdf=result.pdf.name, pages=result.pages,
            projects=[p.title for p in result.projects], certs=result.certs,
            notes=result.notes, drive=str(result.drive_copy or ""),
            experience=result.experience,
        )

    @app.get("/resumes/<path:name>")
    def resume_file(name):
        config = cfg()
        out_dir = _resolve(config.root, (config.raw.get("resume") or {}).get("output_dir", "resumes"))
        return send_from_directory(out_dir, Path(name).name)

    @app.get("/resume-image/<path:name>")
    def resume_image(name):
        """The resume as a crisp PNG, so it can sit in the page itself."""
        import pymupdf
        from flask import Response

        config = cfg()
        out_dir = _resolve(config.root, (config.raw.get("resume") or {}).get("output_dir", "resumes"))
        pdf = out_dir / Path(name).name
        if not pdf.exists():
            abort(404)
        with pymupdf.open(str(pdf)) as doc:
            png = doc[0].get_pixmap(dpi=150).tobytes("png")
        return Response(png, mimetype="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/api/applications")
    def applications():
        config = cfg()
        return jsonify(rows=list_applications(config), statuses=STATUSES,
                       path=str(tracker_path(config)))

    @app.post("/api/applications/status")
    def application_status():
        d = request.get_json(force=True) or {}
        if d.get("status") not in STATUSES:
            return jsonify(error="unknown status"), 400
        set_status(cfg(), d.get("company", ""), d.get("role", ""), d.get("job_id", ""), d["status"])
        return jsonify(ok=True)

    @app.post("/api/people")
    def people():
        data = request.get_json(force=True) or {}
        config = cfg()
        company = (data.get("company") or "").strip()
        if not company:
            return jsonify(error="company is required"), 400
        names, rejected = parse_pasted_names(data.get("names") or "")
        company_words = {w.lower() for w in company.split() if len(w) > 2}
        if company_words:
            echoes = [n for n in names if {w.lower() for w in n.split()} <= company_words]
            names = [n for n in names if n not in echoes]
            rejected += echoes
        if not names:
            return jsonify(error="no names found", ignored=rejected), 400

        domain = (data.get("domain") or "").strip().lower().lstrip("@")
        existing = load_people(config.people_path)[0] if config.people_path.exists() else []
        known = {p.key for p in existing}
        scope = (domain or company).lower()
        rows, dupes = [], []
        for name in names:
            parts = parse_name(full_name=name)
            key = f"{parts.first}|{parts.last}|{scope}"
            if key in known:
                dupes.append(name)
                continue
            known.add(key)
            first, last = split_display_name(name)
            rows.append({
                "first": first, "last": last, "company": company, "domain": domain,
                "role": data.get("role", ""), "job_id": data.get("job_id", ""),
                "sample_email": data.get("sample", ""), "sector": data.get("sector", ""),
                "notes": data.get("notes", ""),
                "subject": data.get("subject", ""),
                "body": (data.get("body") or "").replace("\n", "\\n"),
            })
        if rows:
            append_people(config.people_path, rows)
        return jsonify(added=[f"{r['first']} {r['last']}".strip() for r in rows],
                       duplicates=dupes, ignored=rejected)

    def _plans(config, company, retry):
        configure_resolver(config.dns_fallback_servers, config.dns_timeout)
        people, _ = load_people(config.people_path)
        needle = company.strip().lower()
        if needle:
            people = [p for p in people
                      if needle in p.company.lower() or needle in p.domain.lower()]
        if not people:
            raise ConfigError(f"no recipients match company {company!r}")
        learned, sent_log = LearnedStore(config.learned_path), SentLog(config.sent_log_path)
        plans, _ = build_plans(people, config, learned, sent_log,
                               allow_discovery=True, retry_bounced=retry)
        learned.save()
        return plans, sent_log

    @app.post("/api/preview")
    def preview():
        data = request.get_json(force=True) or {}
        config = cfg()
        template = load_template(config.template_path)
        plans, _ = _plans(config, data.get("company", ""), bool(data.get("retry_bounced")))
        out = []
        for plan in plans:
            item = {"name": plan.person.display_name, "sendable": plan.sendable,
                    "warnings": list(plan.warnings)}
            if plan.sendable:
                subject, body = render(template, plan.person, config.sender_name)
                item.update(
                    email=plan.chosen.email, confidence=round(plan.chosen.confidence * 100),
                    subject=subject, body=body,
                    blanks=empty_placeholders(template, plan.person, config.sender_name),
                )
            else:
                item["reason"] = plan.skip_reason
            out.append(item)
        return jsonify(plans=out, sendable=sum(1 for p in out if p["sendable"]))

    @app.post("/api/send")
    def send():
        from .cli import _deliver

        data = request.get_json(force=True) or {}
        if data.get("confirm") is not True:
            return jsonify(error="confirmation required"), 400
        with _send_lock:
            if _send_state["running"]:
                return jsonify(error="a send is already running"), 409
        config = cfg()
        template = load_template(config.template_path)
        creds = load_credentials()

        resume_path = None
        if data.get("resume"):
            out_dir = _resolve(config.root, (config.raw.get("resume") or {}).get("output_dir", "resumes"))
            resume_path = out_dir / Path(data["resume"]).name
            if not resume_path.exists():
                return jsonify(error=f"resume not found: {resume_path.name}"), 400
        elif config.resume_path:
            resume_path = config.resume_path
            if not resume_path.exists():
                return jsonify(error=f"default resume missing: {resume_path}"), 400

        plans, sent_log = _plans(config, data.get("company", ""), bool(data.get("retry_bounced")))
        queue = [p for p in plans if p.sendable]
        if config.max_per_run:
            queue = queue[:config.max_per_run]
        queue = queue[:max(0, config.max_per_day - sent_log.sent_today())]
        if not queue:
            return jsonify(error="nothing to send (everyone skipped, or the daily cap is reached)"), 400

        _send_state.update(running=True, total=len(queue), done=0, sent=0, failed=0,
                           lines=[], finished=False, error="")

        def progress(index, total, status, email, detail):
            _send_state["done"] = index
            _send_state["sent" if status == "sent" else "failed"] += 1
            _send_state["lines"].append(f"{status}: {email}" + (f" ({detail})" if detail else ""))

        def work():
            try:
                _deliver(config, creds, template, queue, resume_path,
                         SentLog(config.sent_log_path), LearnedStore(config.learned_path),
                         on_progress=progress)
            except Exception as exc:  # surfaced in the UI, not swallowed
                _send_state["error"] = str(exc)
            finally:
                _send_state.update(running=False, finished=True)

        threading.Thread(target=work, daemon=True).start()
        return jsonify(started=len(queue))

    @app.get("/api/send/status")
    def send_status():
        return jsonify(_send_state)

    return app


def serve(config_path: str | None, port: int, open_browser: bool = True) -> None:
    app = create_app(config_path)
    url = f"http://127.0.0.1:{port}/"
    print(f"Mailmint UI running at {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
