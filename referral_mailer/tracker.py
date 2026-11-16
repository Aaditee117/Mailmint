"""Application tracker: one Excel row per job you prepared a resume for.

Rows are keyed on company + role + job id, so rebuilding a resume for the same
posting updates that row (new resume file, keywords...) instead of adding a
duplicate. Status and Notes are yours: a rebuild never overwrites them.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .config import Config, ConfigError, _resolve

COLUMNS = [
    "Date Added", "Last Updated", "Company", "Role", "Job ID", "Apply Link", "Status",
    "Resume File", "Resume Location (Drive)", "Experience Used", "Projects Used",
    "Skills / Keywords", "Job Description", "Notes",
]
STATUSES = ["Resume built", "Applied", "Interview", "Offer", "Rejected", "No response"]
WIDTHS = {"Date Added": 17, "Last Updated": 17, "Company": 22, "Role": 26, "Job ID": 14,
          "Apply Link": 34, "Status": 14, "Resume File": 38, "Resume Location (Drive)": 38,
          "Experience Used": 16, "Projects Used": 44, "Skills / Keywords": 36,
          "Job Description": 60, "Notes": 30}
CELL_LIMIT = 32000  # Excel's hard limit is 32767 characters per cell

_PRESERVED = {"Date Added", "Status", "Notes"}


def tracker_path(config: Config) -> Path:
    value = (config.raw.get("tracker") or {}).get("path", "applications.xlsx")
    return _resolve(config.root, value)


def _key(company: str, role: str, job_id: str) -> str:
    return "|".join(x.strip().lower() for x in (company, role, job_id))


def _open(path: Path):
    if path.exists():
        wb = load_workbook(path)
        ws = wb.active
        header = [c.value for c in ws[1]]
        if header[: len(COLUMNS)] != COLUMNS:
            raise ConfigError(
                f"{path.name} has unexpected columns. Move or rename it and a fresh "
                "tracker will be created."
            )
        return wb, ws
    wb = Workbook()
    ws = wb.active
    ws.title = "Applications"
    ws.append(COLUMNS)
    for index, name in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=index)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="0F9D74")
        cell.alignment = Alignment(vertical="center")
        ws.column_dimensions[get_column_letter(index)].width = WIDTHS[name]
    ws.freeze_panes = "A2"
    return wb, ws


def _find_row(ws, key: str) -> int | None:
    for row in range(2, ws.max_row + 1):
        if _key(str(ws.cell(row, 3).value or ""), str(ws.cell(row, 4).value or ""),
                str(ws.cell(row, 5).value or "")) == key:
            return row
    return None


def _save(wb, path: Path) -> None:
    try:
        wb.save(path)
    except PermissionError as exc:
        raise ConfigError(
            f"cannot write {path.name}: close it in Excel (or wait for OneDrive to "
            "finish syncing) and try again."
        ) from exc


def log_application(
    config: Config,
    company: str,
    role: str,
    job_id: str = "",
    apply_link: str = "",
    description: str = "",
    resume_pdf: Path | None = None,
    drive_copy: Path | None = None,
    experience: str = "",
    projects: list[str] | None = None,
    keywords: list[str] | None = None,
) -> Path:
    path = tracker_path(config)
    wb, ws = _open(path)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    key = _key(company, role, job_id)
    row = _find_row(ws, key)
    is_new = row is None
    if is_new:
        row = ws.max_row + 1

    values = {
        "Date Added": now, "Last Updated": now, "Company": company, "Role": role,
        "Job ID": job_id, "Apply Link": apply_link, "Status": "Resume built",
        "Resume File": resume_pdf.name if resume_pdf else "",
        "Resume Location (Drive)": str(drive_copy) if drive_copy else "",
        "Experience Used": experience, "Projects Used": "; ".join(projects or []),
        "Skills / Keywords": ", ".join(keywords or []),
        "Job Description": (description or "")[:CELL_LIMIT], "Notes": "",
    }
    for index, name in enumerate(COLUMNS, start=1):
        if not is_new and name in _PRESERVED:
            continue
        # Keep an existing link/description if this rebuild did not supply one.
        if not is_new and name in ("Apply Link", "Job Description") and not values[name]:
            continue
        cell = ws.cell(row=row, column=index, value=values[name])
        cell.alignment = Alignment(vertical="top", wrap_text=name == "Job Description")
        if name == "Apply Link" and str(values[name]).startswith("http"):
            cell.hyperlink = values[name]
            cell.font = Font(color="0563C1", underline="single")
        if name == "Resume File" and resume_pdf:
            cell.hyperlink = str(resume_pdf)
            cell.font = Font(color="0563C1", underline="single")
    ws.row_dimensions[row].height = 30
    _save(wb, path)
    return path


def list_applications(config: Config) -> list[dict]:
    path = tracker_path(config)
    if not path.exists():
        return []
    ws = _open(path)[1]
    rows = []
    for row in range(2, ws.max_row + 1):
        item = {name: ("" if ws.cell(row, i).value is None else str(ws.cell(row, i).value))
                for i, name in enumerate(COLUMNS, start=1)}
        if item["Company"] or item["Role"]:
            item.pop("Job Description")
            rows.append(item)
    return list(reversed(rows))


def set_status(config: Config, company: str, role: str, job_id: str, status: str) -> None:
    path = tracker_path(config)
    if not path.exists():
        raise ConfigError("no tracker file yet")
    wb, ws = _open(path)
    row = _find_row(ws, _key(company, role, job_id))
    if row is None:
        raise ConfigError("that application is not in the tracker")
    ws.cell(row, 7, status)
    ws.cell(row, 2, datetime.now().strftime("%Y-%m-%d %H:%M"))
    _save(wb, path)
