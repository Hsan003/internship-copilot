"""FastAPI app: JSON API + static single-page UI. Binds to localhost only by default."""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, config, editor, fetch, fit, latex, letter, llm, pipeline, retrieve, store, techvocab
from . import profile as prof
from .models import ApplicationState
from .tasks import TaskManager
from .textutil import slugify

tasks = TaskManager()
_status_cache: dict[str, Any] = {"t": 0.0, "v": None}

DEFAULT_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


# ---- request bodies (module level: FastAPI cannot resolve classes defined inside create_app)
class EditorBody(BaseModel):
    text: str

class ModelBody(BaseModel):
    model: str

class PreviewBody(BaseModel):
    lang: str = "en"

class FetchBody(BaseModel):
    url: str

class IngestBody(BaseModel):
    url: str = ""
    title: str = ""
    company: str = ""
    text: str = ""
    selection: str = ""
    jsonld: Optional[Dict[str, Any]] = None
    source: str = "bookmarklet"  # bookmarklet | paste

class DuplicateBody(BaseModel):
    source: str
    name: str


class RegenBody(BaseModel):
    paragraph_ids: List[str] = Field(default_factory=lambda: ["*"])
    extra: str = ""
    model: str = ""

class StatusBody(BaseModel):
    status: str
    note: Optional[str] = None
    follow_up_on: Optional[str] = None
    sent_on: Optional[str] = None


def allowed_hosts() -> set[str]:
    extra = {h.strip().lower() for h in os.environ.get("COPILOT_ALLOWED_HOSTS", "").split(",") if h.strip()}
    return DEFAULT_HOSTS | extra


def _host_only(hostport: str) -> str:
    h = (hostport or "").strip().lower()
    if h.startswith("["):
        return h.split("]")[0] + "]"
    return h.split(":")[0]


class NoCacheStatic(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


def create_app(allow_any_host: Optional[bool] = None) -> FastAPI:
    config.ensure_data_dir()
    any_host = os.environ.get("COPILOT_ALLOW_ANY_HOST") == "1" if allow_any_host is None else allow_any_host
    app = FastAPI(title="Internship Copilot", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)

    # ---- DNS-rebinding / CSRF protection (this app can read your CV and write files: keep it local) ----
    @app.middleware("http")
    async def guard(request: Request, call_next):
        if not any_host:
            host = _host_only(request.headers.get("host", ""))
            if host not in allowed_hosts():
                return PlainTextResponse(
                    f"Blocked: unexpected Host “{host}”. Open the app at http://localhost:<port>, or start it with "
                    f"--allow-any-host / COPILOT_ALLOWED_HOSTS if you really need another host name.", status_code=400)
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            origin = request.headers.get("origin")
            if origin and origin != "null":
                if _host_only(urlparse(origin).netloc) != _host_only(request.headers.get("host", "")) and not any_host:
                    return PlainTextResponse("Blocked: cross-site request.", status_code=403)
        return await call_next(request)

    # ------------------------------------------------------------------ status
    @app.get("/api/status")
    def status() -> dict:
        now = time.time()
        if _status_cache["v"] is None or now - _status_cache["t"] > 2.5:
            st = config.load_settings()
            l = llm.get_llm().status()
            eng = latex.find_engine(st.latex.engine)
            try:
                p = prof.load_profile()
                profile_info = {"ok": True, "example": p.example, "name": p.identity.name, "errors": [],
                                "projects": len(p.projects), "experience": len(p.experience)}
            except prof.ConfigFileError as exc:
                profile_info = {"ok": False, "example": False, "name": "", "errors": exc.errors[:8]}
            except Exception as exc:
                profile_info = {"ok": False, "example": False, "name": "", "errors": [str(exc)]}
            _status_cache["v"] = {
                "version": __version__, "demo": st.llm.backend == "fake", "llm": l,
                "latex": {"engine": eng[0] if eng else None, "path": eng[1] if eng else None},
                "playwright": fetch.playwright_available(), "profile": profile_info, "data_dir": str(config.DATA_DIR),
                "settings": {"model": st.llm.model, "base_url": st.llm.base_url, "backend": st.llm.backend},
                "config_error": config.config_error,
            }
            _status_cache["t"] = now
        return {**_status_cache["v"], "busy": tasks.busy()}

    @app.get("/api/domains")
    def domains() -> list[dict]:
        return [{"key": k, "en": v["en"], "fr": v["fr"], "short": techvocab.DOMAIN_SHORT.get(k, v)["en"]}
                for k, v in techvocab.DOMAIN_LABELS.items() if k != "other"]

    @app.get("/api/blueprints")
    def blueprints() -> dict:
        return {kind: {"default": f"{kind}.yaml", "items": prof.list_blueprints(kind)} for kind in ("letter", "email")}

    @app.post("/api/blueprints/duplicate")
    def blueprints_duplicate(body: DuplicateBody) -> dict:
        try:
            name = editor.duplicate_blueprint(body.source, body.name)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return {"file": name, "key": f"bp:{name}"}

    @app.get("/api/sample/{lang}")
    def sample(lang: str) -> dict:
        if lang not in ("fr", "en"):
            raise HTTPException(404)
        text = (config.DEFAULTS_DIR / "samples" / f"job_{lang}.txt").read_text(encoding="utf-8")
        return {"text": text}

    # ------------------------------------------------------------------ editable files
    @app.get("/api/editor")
    def editor_list() -> list[dict]:
        keys = list(config.EDITABLE)
        main = {"letter.yaml", "email.yaml"}
        for kind in ("letter", "email"):
            keys += [f"bp:{n}" for n in config.blueprint_files(kind) if n not in main]
        return [editor.info(k) for k in keys]

    @app.get("/api/editor/{key}")
    def editor_get(key: str) -> dict:
        if not config.valid_key(key):
            raise HTTPException(404, "unknown file")
        return editor.read(key)


    @app.post("/api/editor/{key}/validate")
    def editor_validate(key: str, body: EditorBody) -> dict:
        if not config.valid_key(key):
            raise HTTPException(404, "unknown file")
        errors, warnings = editor.validate(key, body.text)
        return {"ok": not errors, "errors": errors, "warnings": warnings}

    @app.put("/api/editor/{key}")
    def editor_put(key: str, body: EditorBody) -> JSONResponse:
        if not config.valid_key(key):
            raise HTTPException(404, "unknown file")
        ok, errors, warnings = editor.write(key, body.text)
        _status_cache["v"] = None
        return JSONResponse({"ok": ok, "errors": errors, "warnings": warnings}, status_code=200 if ok else 422)

    @app.post("/api/editor/{key}/reset")
    def editor_reset(key: str) -> dict:
        if not config.valid_key(key):
            raise HTTPException(404, "unknown file")
        try:
            editor.reset(key)
        except FileNotFoundError:
            raise HTTPException(422, "This form was created by you, so there is no shipped default to go back to.")
        _status_cache["v"] = None
        return editor.read(key)


    @app.post("/api/settings/model")
    def settings_model(body: ModelBody) -> dict:
        try:
            editor.set_llm_model(body.model)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        _status_cache["v"] = None
        return {"ok": True, "model": body.model}


    @app.post("/api/cv/preview")
    def cv_preview(body: PreviewBody) -> dict:
        """Compile the CV template with your profile (no job) - the fastest way to test a template."""
        lang = body.lang if body.lang in ("en", "fr") else "en"
        try:
            profile = prof.load_profile()
        except prof.ConfigFileError as exc:
            return {"ok": False, "message": "Profile error: " + "; ".join(exc.errors[:3])}
        st = config.load_settings()
        plan = retrieve.plan_cv(profile, retrieve.JobTerms(), lang, st.cv)
        outdir = config.DATA_DIR / "preview"
        from . import cv as cvmod

        res = cvmod.build_cv(profile, plan, config.user_path("cv_template").read_text(encoding="utf-8"), outdir, f"cv_{lang}", st)
        log = (outdir / f"cv_{lang}.log")
        return {"ok": res.ok, "message": res.message, "pages": res.pages, "engine": res.engine,
                "pdf": f"/api/preview/cv_{lang}.pdf" if res.ok else None,
                "log_tail": log.read_text(encoding="utf-8", errors="replace")[-1800:] if (not res.ok and log.exists()) else ""}

    @app.get("/api/preview/{name}")
    def preview_file(name: str) -> FileResponse:
        if name not in ("cv_en.pdf", "cv_fr.pdf"):
            raise HTTPException(404)
        path = config.DATA_DIR / "preview" / name
        if not path.exists():
            raise HTTPException(404)
        return FileResponse(path, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{name}"'})

    # ------------------------------------------------------------------ job intake
    def _fit_for(job) -> list:
        try:
            return [i.model_dump() for i in fit.check_fit(job, prof.load_profile())]
        except Exception:
            return []


    @app.post("/api/job/fetch")
    def job_fetch(body: FetchBody) -> dict:
        try:
            job = fetch.fetch_job(body.url)
        except fetch.FetchError as exc:
            raise HTTPException(422, {"message": str(exc), "blocked": exc.blocked, "platform": exc.platform})
        return {"job": job.model_dump(), "fit": _fit_for(job)}


    @app.post("/api/job/ingest")
    def job_ingest(body: IngestBody) -> dict:
        try:
            if body.source == "paste":
                job = fetch.job_from_text(body.text, url=body.url, title=body.title, company=body.company)
            else:
                job = fetch.job_from_ingest(body.model_dump())
                if body.company and not job.company:
                    job.company = body.company
        except fetch.FetchError as exc:
            raise HTTPException(422, {"message": str(exc), "blocked": False, "platform": exc.platform})
        return {"job": job.model_dump(), "fit": _fit_for(job)}


    @app.post("/api/job/fit")
    def job_fit(job: dict) -> list:
        """Re-run the rule-based fit check after the user edits the job text (instant, no LLM)."""
        from .models import Job

        return _fit_for(Job.model_validate(job))

    # ------------------------------------------------------------------ generation tasks
    def _preflight() -> None:
        try:
            prof.load_profile()
        except prof.ConfigFileError as exc:
            raise HTTPException(422, {"message": "Your profile has errors: " + "; ".join(exc.errors[:3]), "where": "profile"})
        s = llm.get_llm().status()
        if not s.get("ok"):
            raise HTTPException(422, {"message": s.get("error") or "The language model is not reachable.", "where": "setup"})
        if not s.get("model_installed"):
            raise HTTPException(422, {"message": f"Model “{s.get('model')}” is not installed. Run:  ollama pull {s.get('model')}",
                                      "where": "setup"})

    @app.post("/api/application/generate")
    def generate_job(req: pipeline.JobRequest) -> dict:
        _preflight()
        req.job.description = req.job.description[:20000]
        t = tasks.submit("job", lambda task: pipeline.run_job(req, task))
        return {"task_id": t.id}

    @app.post("/api/spontaneous/generate")
    def generate_spontaneous(req: pipeline.SpontaneousRequest) -> dict:
        if not req.company.strip():
            raise HTTPException(422, {"message": "Please enter the company name.", "where": "form"})
        _preflight()
        t = tasks.submit("spontaneous", lambda task: pipeline.run_spontaneous(req, task))
        return {"task_id": t.id}

    # ------------------------------------------------------------------ tailor CV (deterministic, no LLM: synchronous)
    def _tailor_guard(call):
        try:
            return call()
        except prof.ConfigFileError as exc:
            raise HTTPException(422, {"message": "Your profile has errors: " + "; ".join(exc.errors[:3]), "where": "profile"})
        except fetch.FetchError as exc:
            raise HTTPException(422, {"message": str(exc), "where": "form"})

    @app.post("/api/tailor/analyze")
    def tailor_analyze(req: pipeline.TailorRequest) -> dict:
        return _tailor_guard(lambda: pipeline.tailor_analyze(req))

    @app.post("/api/tailor/build")
    def tailor_build(req: pipeline.TailorRequest) -> dict:
        return _state_payload(_tailor_guard(lambda: pipeline.tailor_cv(req)))

    @app.get("/api/tasks/{task_id}")
    def task_get(task_id: str) -> dict:
        t = tasks.get(task_id)
        if not t:
            raise HTTPException(404, "unknown task")
        return t.to_dict(tasks.position(t))

    @app.post("/api/tasks/{task_id}/cancel")
    def task_cancel(task_id: str) -> dict:
        return {"cancelled": tasks.cancel(task_id)}

    # ------------------------------------------------------------------ applications
    @app.get("/api/applications")
    def applications() -> dict:
        return {"items": store.list_apps(), "statuses": store.STATUSES}

    @app.get("/api/applications.csv")
    def applications_csv() -> Response:
        return Response(store.export_csv(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="applications.csv"'})

    def _state_payload(s: ApplicationState) -> dict:
        data = s.model_dump(mode="json")
        d = store.app_dir(s.id)
        data["file_info"] = [{"name": n, "label": lbl, "size": (d / n).stat().st_size if (d / n).exists() else 0}
                             for n, lbl in s.files.items() if (d / n).exists()]
        if s.kind == "spontaneous" and s.doc:
            data["mailto"] = letter.mailto_link(s.contact.get("email", ""), s.doc.subject, letter.body_text(s.doc))
            data["body_text"] = letter.body_text(s.doc)
        elif s.doc:
            data["body_text"] = letter.body_text(s.doc)
        try:
            p = prof.load_profile()
            data["cv_items"] = {
                "projects": [{"id": x.id, "name": x.name, "bullets": [{"id": b.id, "text": letter.localized(b.text, s.lang)} for b in x.bullets]}
                             for x in p.projects],
                "experience": [{"id": x.id, "name": f"{x.org} – {letter.localized(x.role, s.lang)}",
                                "bullets": [{"id": b.id, "text": letter.localized(b.text, s.lang)} for b in x.bullets]} for x in p.experience],
            }
            bp = prof.load_blueprint(pipeline.bp_kind(s), s.target.get("blueprint", ""))
            data["specs"] = {sp.id: {"max_words": sp.max_words, "kind": sp.kind} for sp in bp.paragraphs}
        except Exception:
            data["cv_items"], data["specs"] = {"projects": [], "experience": []}, {}
        return data

    @app.get("/api/application/{app_id}")
    def application_get(app_id: str) -> dict:
        try:
            return _state_payload(store.load_state(app_id))
        except KeyError:
            raise HTTPException(404, "unknown application")

    @app.put("/api/application/{app_id}/edits")
    def application_edits(app_id: str, edits: pipeline.DocEdits) -> dict:
        try:
            s = pipeline.apply_edits(app_id, edits)
        except KeyError:
            raise HTTPException(404, "unknown application")
        return _state_payload(s)

    @app.post("/api/application/{app_id}/rebuild")
    def application_rebuild(app_id: str) -> dict:
        try:
            return _state_payload(pipeline.rebuild_documents(app_id))
        except KeyError:
            raise HTTPException(404, "unknown application")


    @app.post("/api/application/{app_id}/regenerate")
    def application_regenerate(app_id: str, body: RegenBody) -> dict:
        try:
            store.load_state(app_id)
        except KeyError:
            raise HTTPException(404, "unknown application")
        _preflight()
        t = tasks.submit("regenerate", lambda task: pipeline.regenerate(app_id, body.paragraph_ids, body.extra, task, body.model))
        return {"task_id": t.id}


    @app.put("/api/application/{app_id}/status")
    def application_status(app_id: str, body: StatusBody) -> dict:
        try:
            s = store.set_status(app_id, body.status, note=body.note, follow_up_on=body.follow_up_on, sent_on=body.sent_on)
        except KeyError:
            raise HTTPException(404, "unknown application")
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return store.summary(s)

    @app.delete("/api/application/{app_id}")
    def application_delete(app_id: str) -> dict:
        try:
            store.delete_app(app_id)
        except KeyError:
            raise HTTPException(404)
        return {"ok": True}

    _MEDIA = {"pdf": "application/pdf", "tex": "text/plain; charset=utf-8", "txt": "text/plain; charset=utf-8",
              "eml": "message/rfc822", "log": "text/plain; charset=utf-8", "json": "application/json"}

    @app.get("/api/download/{app_id}/{filename}")
    def download(app_id: str, filename: str, inline: int = 0) -> FileResponse:
        try:
            path = store.safe_file(app_id, filename)
            s = store.load_state(app_id)
        except (KeyError, FileNotFoundError):
            raise HTTPException(404, "file not found")
        stem, ext = filename.rsplit(".", 1)
        try:
            who = slugify(prof.load_profile().identity.name, 30)
        except Exception:
            who = "candidate"
        kind = {"cv": "cv", "letter": "cover-letter", "email": "email"}.get(stem, stem)
        nice = f"{who}_{kind}_{slugify(s.company, 24)}.{ext}"
        disp = "inline" if inline else "attachment"
        return FileResponse(path, media_type=_MEDIA.get(ext, "application/octet-stream"),
                            headers={"Content-Disposition": f'{disp}; filename="{nice}"', "Cache-Control": "no-store"})

    # ------------------------------------------------------------------ UI
    @app.get("/", include_in_schema=False)
    def index() -> Response:
        html = (config.STATIC_DIR / "index.html").read_text(encoding="utf-8").replace("__VERSION__", __version__)
        return Response(html, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})

    app.mount("/static", NoCacheStatic(directory=str(config.STATIC_DIR)), name="static")
    return app


app = None  # created lazily by __main__ / tests (create_app()) so import has no side effects
