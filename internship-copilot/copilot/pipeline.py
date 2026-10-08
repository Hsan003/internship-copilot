"""End-to-end flows: job link -> letter + CV;  spontaneous application -> e-mail + CV;  edits / regeneration / rebuilds."""
from __future__ import annotations

import re
from typing import Dict, List, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from . import analyze, config, cv, fetch, fit, grounding, letter, retrieve, store, tailor, techvocab
from . import profile as prof
from .llm import get_llm
from .models import ApplicationState, CVPlan, Job
from .research import fetch_company_text
from .tasks import Task
from .textutil import one_line


# --------------------------------------------------------------------------- requests
class JobRequest(BaseModel):
    job: Job
    lang: str = "auto"  # auto | en | fr
    note: str = ""
    contact_name: str = ""
    contact_role: str = ""
    contact_email: str = ""
    greeting_style: str = "auto"
    company_url: str = ""
    model: str = ""
    with_cv: bool = True
    blueprint: str = ""  # form file in data/blueprints (default: letter.yaml)
    extras: Dict[str, str] = Field(default_factory=dict)


class SpontaneousRequest(BaseModel):
    company: str
    company_url: str = ""
    contact_name: str = ""
    contact_role: str = ""
    contact_email: str = ""
    domains: List[str] = Field(default_factory=list)
    role_focus: str = ""
    note: str = ""
    lang: str = "auto"
    greeting_style: str = "auto"
    model: str = ""
    with_cv: bool = True
    blueprint: str = ""  # form file in data/blueprints (default: email.yaml)
    extras: Dict[str, str] = Field(default_factory=dict)


class TailorRequest(BaseModel):
    text: str  # the pasted job description
    title: str = ""
    company: str = ""
    lang: str = "auto"  # auto | en | fr
    keywords: Optional[List[str]] = None  # None = the profile-backed keywords proposed by the analysis
    use_ai: bool = False  # extract keywords and rank your work with the local model (analysis only)
    model: str = ""
    plan: Optional[CVPlan] = None  # the plan returned by the analysis (AI ranking is not recomputed at build time)


class DocEdits(BaseModel):
    paragraphs: Dict[str, str] = Field(default_factory=dict)
    subject: Optional[str] = None
    salutation: Optional[str] = None
    closing: Optional[str] = None
    cv_plan: Optional[CVPlan] = None


# --------------------------------------------------------------------------- helpers
_INTERN_LEAD = re.compile(r"^(stage(?:\s+de\s+fin\s+d['’]études)?|pfe|internship|intern|praktikum|stagiaire|werkstudent)\s*[-–:|/]\s*", re.IGNORECASE)
_INTERN_TAIL = re.compile(r"(?:\s+|\s*[-–:|/(]\s*)(stage(?:\s+de\s+fin\s+d['’]études)?|pfe|internship|intern|praktikum|praktikant(?:in)?|stagiaire)\)?\s*$", re.IGNORECASE)


def subject_role(role: str) -> str:
    """'Stage - Ingénieur DevOps (H/F)' -> 'Ingénieur DevOps' (the subject already says 'internship')."""
    r = analyze.clean_role_title(role)
    for _ in range(2):
        r2 = _INTERN_TAIL.sub("", _INTERN_LEAD.sub("", r)).strip(" -–:|/")
        if r2 and r2.lower() != r.lower():
            r = r2
    return r


def _tld(url_or_email: str) -> str:
    v = (url_or_email or "").strip().lower()
    if "@" in v:
        host = v.split("@")[-1]
    else:
        host = urlparse(v if "://" in v else "https://" + v).netloc if v else ""
    return host.rsplit(".", 1)[-1] if "." in host else ""


def resolve_language(requested: str, job_lang: str, company_url: str = "", email: str = "", has_posting: bool = True) -> tuple[str, list[str]]:
    """-> (lang, messages). Letters are written in French or English only."""
    if requested in ("en", "fr"):
        return requested, []
    if has_posting and job_lang in ("fr", "en"):
        return job_lang, []
    if has_posting and job_lang == "de":
        return "en", ["The posting is in German. Letters are written in French or English only, so this one is in English — "
                      "check whether the role really requires German."]
    if "fr" in (_tld(company_url), _tld(email)):
        return "fr", ["Language: French (the company's address ends in .fr). Change it in the options if you prefer English."]
    what = "the posting" if has_posting else "the company"
    return "en", [f"Could not tell which language {what} uses: the text is in English (switch to French in the options if needed)."]


def _template(key: str) -> str:
    return config.user_path(key).read_text(encoding="utf-8")


def _contact(req) -> dict:
    return {k: v for k, v in {"name": one_line(req.contact_name), "role": one_line(req.contact_role),
                              "email": one_line(req.contact_email), "style": req.greeting_style}.items() if v}


def write_input_from_state(state: ApplicationState, profile, bp) -> letter.WriteInput:
    terms = set(state.analysis.terms) if state.analysis else set(state.target.get("terms", []))
    return letter.WriteInput(
        profile=profile, bp=bp, lang=state.lang, company=state.company, role=subject_role(state.role),
        role_focus=state.target.get("role_focus", ""), job=state.job, analysis=state.analysis, facts=state.facts,
        evidence=state.evidence, note=state.note, contact_name=state.contact.get("name", ""),
        contact_role=state.contact.get("role", ""), greeting_style=state.contact.get("style", "auto"),
        domains_hint=state.target.get("domains_hint", ""), job_terms=terms)


def bp_kind(state: ApplicationState) -> str:
    """Which blueprint family a state uses (CV-only states have no document, so 'letter' is just a harmless default)."""
    return "email" if state.kind == "spontaneous" else "letter"


def _filebase(profile, state: ApplicationState) -> str:
    from .textutil import slugify

    return f"{slugify(profile.identity.name, 30)}_{slugify(state.company, 24)}"


def _build_outputs(profile, state: ApplicationState, *, with_cv: bool, with_letter: bool = True) -> list[str]:
    """(Re)build PDFs/.tex/.txt/.eml for a state. Returns human messages."""
    st = config.load_settings()
    d = store.app_dir(state.id)
    msgs: list[str] = []
    if with_cv and state.cv_plan:
        res = cv.build_cv(profile, state.cv_plan, _template("cv_template"), d, "cv", st)
        state.cv_plan = res.plan
        if res.message:
            msgs.append("CV: " + res.message)
        if res.ok:
            state.files["cv.pdf"] = "CV (PDF)"
            state.files["cv.tex"] = "CV (LaTeX source)"
        else:
            state.files.pop("cv.pdf", None)
            if res.tex:
                state.files["cv.tex"] = "CV (LaTeX source)"
    if with_letter and state.doc:
        if state.kind == "job":
            lb = letter.build_letter(profile, state.doc, state.lang, _template("letter_template"), d, "letter", st)
            if lb.message:
                msgs.append("Letter: " + lb.message)
            if lb.tex:
                state.files["letter.tex"] = "Cover letter (LaTeX source)"
            if lb.ok:
                state.files["letter.pdf"] = "Cover letter (PDF)"
            else:
                state.files.pop("letter.pdf", None)
        else:
            (d / "email.txt").write_text(letter.body_text(state.doc, include_subject=True), encoding="utf-8")
            state.files["email.txt"] = "E-mail text"
            cvpdf = d / "cv.pdf" if "cv.pdf" in state.files else None
            sender = f"{profile.identity.name} <{profile.identity.email}>" if profile.identity.email else ""
            (d / "email.eml").write_bytes(letter.build_eml(state.doc, sender, state.contact.get("email", ""), cvpdf))
            state.files["email.eml"] = "E-mail draft (.eml, opens in your mail app)"
    return msgs


def _refresh_flags(state: ApplicationState, profile, bp) -> None:
    wi = write_input_from_state(state, profile, bp)
    if not state.doc:
        return
    specs = {p.id: p for p in bp.paragraphs}
    for p in state.doc.paragraphs:
        if p.kind == "generated" and p.id in specs:
            p.flags = grounding.check_paragraph(p.text, letter._grounding_ctx(wi, specs[p.id]))


# --------------------------------------------------------------------------- flow 1: job link
def run_job(req: JobRequest, task: Task) -> dict:
    st = config.load_settings()
    profile = prof.load_profile()
    bp = prof.load_blueprint("letter", req.blueprint)
    llm = get_llm(req.model or None)
    job = req.job.model_copy(deep=True)
    job.description = job.description.strip()
    lang, notes = resolve_language(req.lang, job.language, req.company_url or job.company_url)
    messages = list(notes)
    if profile.example:
        messages.append("Your profile is still the SAMPLE profile: replace it in “Profile & templates” before sending anything.")

    task.step("analyze", "Reading the job posting")
    analysis = analyze.analyze_job(llm, job, cancel=task.cancel, on_progress=task.tokens, temperature=st.llm.temperature_extract)
    company = one_line(job.company) or analysis.company_name
    role = one_line(job.title) and analyze.clean_role_title(job.title) or analysis.role_title
    if analysis.role_title and (not role or len(role) > 90):
        role = analysis.role_title
    analysis.company_name = company

    state = ApplicationState(id=store.new_id(company, subject_role(role)), kind="job", lang=lang, company=company, role=role,
                             url=job.url, contact=_contact(req), note=req.note, job=job, analysis=analysis, model=llm.model,
                             messages=messages, target={"extras": req.extras, "blueprint": req.blueprint})
    store.save_state(state)

    task.step("facts", "Looking for verifiable facts about the company")
    sources = [("job posting", job.description)]
    cu = req.company_url or job.company_url
    if cu:
        pages = fetch_company_text(cu, st.fetch)
        sources += pages
        if not pages:
            messages.append(f"Could not read any text from {cu}: company facts come from the posting only.")
    state.facts = analyze.extract_company_facts(llm, company, sources, lang, cancel=task.cancel, on_progress=task.tokens)
    if state.facts:
        analysis.company_description = state.facts[0].fact
    else:
        messages.append("No verifiable company fact was found, so the “Why this company” paragraph stays general. "
                        "Add a one-line note or a company website to make it specific.")
    state.job = job

    task.step("match", "Checking fit and picking your most relevant work")
    state.fit = fit.check_fit(job, profile)
    jt = retrieve.build_job_terms(f"{job.title}\n{job.description}", analysis.must_have, analysis.nice_to_have, job.title, analysis.domains)
    state.evidence = retrieve.pick_evidence(profile, jt, lang)
    if not state.evidence:
        messages.append("Your profile has no projects or experience yet: the letter can only be generic.")
    store.save_state(state)

    wi = write_input_from_state(state, profile, bp)
    state.doc = letter.compose(llm, wi, extras=req.extras, cancel=task.cancel, progress=lambda k, l: task.step(k, l),
                               on_tokens=task.tokens)
    store.save_state(state)

    if req.with_cv:
        task.step("cv", "Tailoring the CV")
        state.cv_plan = retrieve.plan_cv(profile, jt, lang, st.cv)
    task.step("build", "Compiling PDFs")
    messages += _build_outputs(profile, state, with_cv=req.with_cv)
    state.messages = messages
    store.save_state(state)
    return {"application_id": state.id}


# --------------------------------------------------------------------------- flow 2: spontaneous
def run_spontaneous(req: SpontaneousRequest, task: Task) -> dict:
    st = config.load_settings()
    profile = prof.load_profile()
    bp = prof.load_blueprint("email", req.blueprint)
    llm = get_llm(req.model or None)
    company = one_line(req.company)
    lang, notes = resolve_language(req.lang, "unknown", req.company_url, req.contact_email, has_posting=False)
    messages = list(notes)
    if profile.example:
        messages.append("Your profile is still the SAMPLE profile: replace it in “Profile & templates” before sending anything.")

    domains = [d for d in req.domains if d in techvocab.DOMAIN_GROUPS and d != "other"]
    focus_terms = techvocab.find_terms(req.role_focus)
    role_focus = one_line(req.role_focus) or " / ".join(techvocab.DOMAIN_SHORT[d][lang] for d in domains[:2] if d in techvocab.DOMAIN_SHORT)
    jt = retrieve.domain_job_terms(domains)
    for t, n in focus_terms.items():
        jt.weights[t] = jt.weights.get(t, 0) + 1.5
    state = ApplicationState(
        id=store.new_id(company, role_focus or "spontaneous"), kind="spontaneous", lang=lang, company=company,
        role=role_focus or "Spontaneous application", url=req.company_url, contact=_contact(req), note=req.note,
        model=llm.model, messages=messages,
        target={"domains": domains, "role_focus": role_focus, "company_url": req.company_url, "extras": req.extras, "blueprint": req.blueprint,
                "terms": sorted(set(jt.weights)), "domains_hint": ", ".join(techvocab.DOMAIN_LABELS[d]["en"] for d in domains)})

    task.step("facts", "Reading the company website" if req.company_url else "Preparing")
    sources: list[tuple[str, str]] = []
    if req.company_url:
        sources = fetch_company_text(req.company_url, st.fetch)
        if not sources:
            messages.append(f"Could not read any text from {req.company_url}.")
    if sources:
        task.step("facts2", "Extracting verifiable facts")
        state.facts = analyze.extract_company_facts(llm, company, sources, lang, cancel=task.cancel, on_progress=task.tokens)
    if not state.facts and not req.note.strip():
        messages.append("No company fact and no personal note: the opening paragraph will be generic. A one-line note "
                        "(“I read your post about …”) makes a big difference.")

    task.step("match", "Picking your most relevant work")
    state.evidence = retrieve.pick_evidence(profile, jt, lang, k=2, bullets_per_item=2)
    store.save_state(state)

    wi = write_input_from_state(state, profile, bp)
    state.doc = letter.compose(llm, wi, extras=req.extras, cancel=task.cancel, progress=lambda k, l: task.step(k, l),
                               on_tokens=task.tokens)
    if req.with_cv:
        task.step("cv", "Tailoring the CV")
        state.cv_plan = retrieve.plan_cv(profile, jt, lang, st.cv)
    task.step("build", "Compiling the CV and e-mail files")
    messages += _build_outputs(profile, state, with_cv=req.with_cv)
    state.messages = messages
    store.save_state(state)
    return {"application_id": state.id}


# --------------------------------------------------------------------------- flow 3: tailor the CV only
def _tailor_job(req: TailorRequest):
    job = fetch.job_from_text(req.text[:20000], title=req.title, company=req.company)  # FetchError if too short
    lang, notes = resolve_language(req.lang, job.language)
    return job, lang, notes


def tailor_analyze(req: TailorRequest, task: Optional[Task] = None) -> dict:
    """Keyword report for a pasted description. Instant without the model; ``use_ai`` runs as a background task."""
    profile = prof.load_profile()
    job, lang, notes = _tailor_job(req)
    llm = get_llm(req.model or None) if req.use_ai else None
    extra = {"llm": llm, "cancel": task.cancel, "on_progress": task.tokens, "step": task.step} if (llm and task) else {"llm": llm}
    res = tailor.analyze(profile, job.description, job.title, lang, config.load_settings().cv, **extra)
    notes = notes + res["ai_notes"]
    if profile.example:
        notes.append("Your profile is still the SAMPLE profile: replace it in “Profile & templates” first.")
    return {"lang": lang, "notes": notes, "job": job.model_dump(), "items": res["items"],
            "default_keywords": res["default_keywords"], "gaps": res["gaps"], "domains": res["domains"],
            "score_before": res["score_before"], "score_after": res["score_after"], "ai": res["ai"],
            "plan": res["plan"].model_dump()}


def tailor_cv(req: TailorRequest) -> ApplicationState:
    """Build a CV tailored to the pasted description and store it as a 'cv' application (shows up in the tracker)."""
    st = config.load_settings()
    profile = prof.load_profile()
    job, lang, notes = _tailor_job(req)
    if req.plan is not None:  # the plan the user saw (possibly AI-ranked): only ids that exist in the profile survive
        plan = tailor.sanitize_plan(profile, req.plan.model_copy(deep=True))
        plan.lang = lang
        default_kw: List[str] = []
    else:
        res = tailor.analyze(profile, job.description, job.title, lang, st.cv)
        plan, default_kw = res["plan"], res["default_keywords"]
    tailor.apply_keywords(plan, default_kw if req.keywords is None else req.keywords)
    company = one_line(job.company) or "Tailored CV"
    role = analyze.clean_role_title(job.title) if job.title else "CV"
    state = ApplicationState(id=store.new_id(company, role), kind="cv", lang=lang, company=company, role=role, job=job,
                             cv_plan=plan, messages=list(notes))
    if profile.example:
        state.messages.append("Your profile is still the SAMPLE profile: replace it in “Profile & templates” before sending anything.")
    state.fit = fit.check_fit(job, profile)
    state.messages += _build_outputs(profile, state, with_cv=True, with_letter=False)
    store.save_state(state)
    return state


# --------------------------------------------------------------------------- regeneration / edits
def regenerate(app_id: str, paragraph_ids: List[str], extra: str, task: Task, model: str = "") -> dict:
    profile = prof.load_profile()
    state = store.load_state(app_id)
    bp = prof.load_blueprint(bp_kind(state), state.target.get("blueprint", ""))
    llm = get_llm(model or None)
    extras = dict(state.target.get("extras", {}))
    gen_ids = {p.id for p in bp.paragraphs if p.kind == "generated"}
    only = gen_ids if paragraph_ids in (["*"], []) else (set(paragraph_ids) & gen_ids)
    for pid in only:
        if extra.strip():
            extras[pid] = extra.strip()
    state.target["extras"] = extras
    wi = write_input_from_state(state, profile, bp)
    state.doc = letter.compose(llm, wi, extras=extras, existing=state.doc, only=only, cancel=task.cancel,
                               progress=lambda k, l: task.step(k, l), on_tokens=task.tokens)
    task.step("build", "Compiling")
    msgs = _build_outputs(profile, state, with_cv=False)
    state.messages = [m for m in state.messages if not m.startswith("Letter:")] + msgs
    store.save_state(state)
    return {"application_id": state.id}


def apply_edits(app_id: str, edits: DocEdits, rebuild: bool = True) -> ApplicationState:
    profile = prof.load_profile()
    state = store.load_state(app_id)
    bp = prof.load_blueprint(bp_kind(state), state.target.get("blueprint", ""))
    if state.doc:
        for p in state.doc.paragraphs:
            if p.id in edits.paragraphs and edits.paragraphs[p.id].strip() != p.text.strip():
                p.text = one_line(edits.paragraphs[p.id])
                p.edited = True
        if edits.subject is not None:
            state.doc.subject = one_line(edits.subject)
        if edits.salutation is not None:
            state.doc.salutation = one_line(edits.salutation)
        if edits.closing is not None:
            state.doc.closing = one_line(edits.closing)
    if edits.cv_plan is not None:
        state.cv_plan = edits.cv_plan
    _refresh_flags(state, profile, bp)
    if rebuild:
        msgs = _build_outputs(profile, state, with_cv=edits.cv_plan is not None)
        state.messages = [m for m in state.messages if not m.startswith(("Letter:", "CV:"))] + msgs
    store.save_state(state)
    return state


def rebuild_documents(app_id: str) -> ApplicationState:
    profile = prof.load_profile()
    state = store.load_state(app_id)
    msgs = _build_outputs(profile, state, with_cv=state.cv_plan is not None)
    state.messages = [m for m in state.messages if not m.startswith(("Letter:", "CV:"))] + msgs
    store.save_state(state)
    return state
