"""Application tracker storage: one folder per application with a state.json (+ generated files)."""
from __future__ import annotations

import csv
import io
import json
import os
import re
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from . import config
from .models import ApplicationState
from .textutil import slugify

STATUSES = ["drafted", "sent", "replied", "interview", "offer", "rejected", "withdrawn"]
_ID = re.compile(r"^[a-z0-9][a-z0-9\-]{2,120}$")
_ALLOWED_FILES = re.compile(r"^[A-Za-z0-9_\-]+\.(pdf|tex|txt|eml|json|log)$")
_lock = threading.RLock()


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def new_id(company: str, role: str, today: Optional[date] = None) -> str:
    today = today or date.today()
    base = f"{today:%Y%m%d}-{slugify(company or 'company', 24)}-{slugify(role or 'application', 24)}"
    with _lock:
        cand, i = base, 2
        while (config.APPS_DIR / cand).exists():
            cand, i = f"{base}-{i}", i + 1
        return cand


def valid_id(app_id: str) -> bool:
    return bool(_ID.match(app_id or ""))


def app_dir(app_id: str) -> Path:
    if not valid_id(app_id):
        raise KeyError("invalid application id")
    return config.APPS_DIR / app_id


def safe_file(app_id: str, filename: str) -> Path:
    if not _ALLOWED_FILES.match(filename or ""):
        raise KeyError("invalid file name")
    path = app_dir(app_id) / filename
    if not path.is_file():
        raise FileNotFoundError(filename)
    return path


def save_state(state: ApplicationState) -> None:
    with _lock:
        d = app_dir(state.id)
        d.mkdir(parents=True, exist_ok=True)
        if not state.created_at:
            state.created_at = now_iso()
        state.updated_at = now_iso()
        tmp = d / "state.json.tmp"
        tmp.write_text(json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, d / "state.json")


def load_state(app_id: str) -> ApplicationState:
    path = app_dir(app_id) / "state.json"
    if not path.exists():
        raise KeyError(app_id)
    return ApplicationState.model_validate_json(path.read_text(encoding="utf-8"))


def delete_app(app_id: str) -> None:
    import shutil

    d = app_dir(app_id)
    if d.exists():
        shutil.rmtree(d)


def summary(s: ApplicationState) -> dict:
    return {
        "id": s.id, "kind": s.kind, "company": s.company, "role": s.role, "status": s.status, "lang": s.lang,
        "created_at": s.created_at, "sent_at": s.sent_at, "follow_up_on": s.follow_up_on, "url": s.url,
        "contact": s.contact.get("name", ""), "note": s.note,
        "has_pdf": any(k.endswith(".pdf") for k in s.files),
    }


def list_apps() -> list[dict]:
    out = []
    if not config.APPS_DIR.exists():
        return out
    for d in sorted(config.APPS_DIR.iterdir(), reverse=True):
        if d.is_dir() and (d / "state.json").exists():
            try:
                out.append(summary(load_state(d.name)))
            except Exception:
                out.append({"id": d.name, "kind": "?", "company": "(unreadable state.json)", "role": "", "status": "?",
                            "created_at": "", "sent_at": "", "follow_up_on": "", "url": "", "contact": "", "note": "",
                            "lang": "", "has_pdf": False})
    return sorted(out, key=lambda x: x.get("created_at", ""), reverse=True)


def set_status(app_id: str, status: str, *, note: Optional[str] = None, follow_up_on: Optional[str] = None,
               sent_on: Optional[str] = None, today: Optional[date] = None) -> ApplicationState:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    today = today or date.today()
    with _lock:
        s = load_state(app_id)
        previous = s.status
        s.status = status
        if status == "sent" and previous != "sent":
            s.sent_at = sent_on or today.isoformat()
            if follow_up_on is None:
                days = config.load_settings().tracker.follow_up_days
                s.follow_up_on = (date.fromisoformat(s.sent_at) + timedelta(days=days)).isoformat()
        if status in ("replied", "interview", "offer", "rejected", "withdrawn") and follow_up_on is None:
            s.follow_up_on = ""
        if follow_up_on is not None:
            s.follow_up_on = follow_up_on
        if sent_on is not None:
            s.sent_at = sent_on
        if note is not None:
            s.note = note
        save_state(s)
        return s


CSV_COLUMNS = ["created", "type", "company", "role", "contact", "contact_email", "language", "status", "sent_on",
               "follow_up_on", "url", "note", "id"]


def export_csv() -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for a in list_apps():
        try:
            s = load_state(a["id"])
        except Exception:
            continue
        w.writerow([s.created_at[:10], s.kind, s.company, s.role, s.contact.get("name", ""), s.contact.get("email", ""),
                    s.lang, s.status, s.sent_at, s.follow_up_on, s.url, s.note.replace("\n", " "), s.id])
    return buf.getvalue()
