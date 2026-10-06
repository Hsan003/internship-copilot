"""Tailored CV: plan (selection/order only - nothing is ever invented) -> LaTeX context -> PDF with page-fit loop."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import config, latex
from .models import CVPlan, Profile
from .textutil import localized

LABELS = {
    "en": {"education": "Education", "experience": "Experience", "projects": "Projects", "skills": "Skills",
           "more": "Additional information", "languages": "Languages", "certifications": "Certifications",
           "interests": "Interests", "subject": "Subject:", "keywords": "Other keywords"},
    "fr": {"education": "Formation", "experience": "Expérience", "projects": "Projets", "skills": "Compétences",
           "more": "Informations complémentaires", "languages": "Langues", "certifications": "Certifications",
           "interests": "Centres d'intérêt", "subject": "Objet :", "keywords": "Autres mots-clés"},
}


def display_url(u: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", (u or "").strip()).rstrip("/")


def contact_line(profile: Profile) -> latex.RawTeX:
    i = profile.identity
    parts: list = []
    if i.email:
        parts.append(latex.RawTeX(rf"\href{{mailto:{latex.url_escape(i.email)}}}{{{latex.tex_escape(i.email)}}}"))
    if i.phone:
        parts.append(i.phone)
    if i.location:
        parts.append(i.location)
    for _, url in i.links.items():
        if url:
            parts.append(latex.href(url, display_url(url)))
    return latex.join_raw(parts, r" \textbar\ ")


def build_context(profile: Profile, plan: CVPlan, lang: str) -> dict:
    exps = {x.id: x for x in profile.experience}
    projs = {p.id: p for p in profile.projects}

    def bullets(item) -> list[str]:
        ids = plan.bullet_ids.get(item.id)
        by_id = {b.id: b for b in item.bullets}
        chosen = [by_id[i] for i in ids if i in by_id] if ids is not None else list(item.bullets)
        return [localized(b.text, lang) for b in chosen]

    skills = list(plan.skills or [{"category": localized(g.category, lang), "items": g.items} for g in profile.skills])
    if plan.extra_keywords:
        listed = {i.lower() for s in skills for i in s["items"]}
        extra = [k for k in plan.extra_keywords if k.lower() not in listed]
        if extra:
            skills.append({"category": LABELS.get(lang, LABELS["en"])["keywords"], "items": extra})
    return {
        "lang": lang,
        "L": LABELS.get(lang, LABELS["en"]),
        "name": profile.identity.name,
        "headline": plan.headline,
        "contact_line": contact_line(profile),
        "education": [
            {"school": e.school, "degree": localized(e.degree, lang), "period": e.period, "location": e.location,
             "details": [localized(d, lang) for d in e.details]}
            for e in profile.education
        ],
        "experience": [
            {"org": x.org, "role": localized(x.role, lang), "period": x.period, "location": x.location, "bullets": bullets(x)}
            for x in (exps[i] for i in plan.experience_ids if i in exps)
        ],
        "projects": [
            {"name": p.name, "tagline": localized(p.tagline, lang), "stack": ", ".join(p.stack), "period": p.period,
             "link": p.link, "link_text": display_url(p.link), "bullets": bullets(p)}
            for p in (projs[i] for i in plan.project_ids if i in projs)
        ],
        "skills": [{"category": s["category"], "items": ", ".join(s["items"])} for s in skills],
        "languages": [{"name": localized(l.name, lang), "level": localized(l.level, lang)} for l in profile.languages],
        "certifications": [localized(c, lang) for c in profile.certifications],
        "interests": [localized(i, lang) for i in profile.interests],
    }


def render_cv(profile: Profile, plan: CVPlan, template_text: str, babel: Optional[str] = None) -> str:
    ctx = build_context(profile, plan, plan.lang)
    ctx["babel"] = babel or ("french" if plan.lang == "fr" else "english")
    return latex.render(template_text, ctx)


@dataclass
class CVBuild:
    ok: bool
    plan: CVPlan
    tex: str = ""
    pdf: Optional[Path] = None
    pages: Optional[int] = None
    message: str = ""
    trimmed: list[str] = field(default_factory=list)
    engine: str = ""


def _trim_once(profile: Profile, plan: CVPlan) -> Optional[str]:
    """Remove the least relevant content. Returns a description of what was removed, or None if nothing is left to trim."""
    names = {p.id: p.name for p in profile.projects} | {x.id: f"{x.org}" for x in profile.experience}
    # 1) last (= least relevant) bullet of the lowest-scoring item that still has more than one bullet
    items = [i for i in [*plan.project_ids, *plan.experience_ids] if len(plan.bullet_ids.get(i, [])) > 1]
    if items:
        victim = min(items, key=lambda i: (plan.scores.get(i, 0.0), -len(plan.bullet_ids[i])))
        plan.bullet_ids[victim].pop()
        return f"1 bullet from “{names.get(victim, victim)}”"
    # 2) drop the lowest-scoring project when more than one remains
    if len(plan.project_ids) > 1:
        victim = min(plan.project_ids, key=lambda i: plan.scores.get(i, 0.0))
        plan.project_ids.remove(victim)
        return f"project “{names.get(victim, victim)}”"
    return None


def build_cv(profile: Profile, plan: CVPlan, template_text: str, outdir: Path, name: str = "cv",
             settings: Optional[config.Settings] = None) -> CVBuild:
    st = settings or config.load_settings()
    plan = plan.model_copy(deep=True)
    trimmed: list[str] = []
    last = None
    for attempt in range(40):
        try:
            tex = render_cv(profile, plan, template_text)
        except latex.TemplateError as exc:
            return CVBuild(False, plan, message=str(exc), trimmed=trimmed)
        res = latex.build_pdf(lambda babel: render_cv(profile, plan, template_text, babel), outdir, name, plan.lang,
                              st.latex.engine, st.latex.timeout_s)
        last = res
        if not res.ok:
            return CVBuild(False, plan, tex=tex, message=res.message, trimmed=trimmed, engine=res.engine)
        if res.pages is None or res.pages <= st.latex.max_cv_pages:
            msg = res.message
            if trimmed:
                shown = ", ".join(dict.fromkeys(trimmed))
                msg = (msg + " " if msg else "") + f"Trimmed to fit {st.latex.max_cv_pages} page(s): removed {len(trimmed)} least-relevant item(s) ({shown})."
            return CVBuild(True, plan, tex=tex, pdf=res.pdf, pages=res.pages, message=msg, trimmed=trimmed, engine=res.engine)
        step = 1 + attempt // 4  # trim faster when the CV is far too long (each attempt is one compile)
        removed_any = False
        for _ in range(step):
            removed = _trim_once(profile, plan)
            if removed is None:
                break
            trimmed.append(removed)
            removed_any = True
        if not removed_any:
            break
    msg = f"The CV is {last.pages} pages (target {st.latex.max_cv_pages}); nothing more could be trimmed automatically."
    if trimmed:
        msg += f" Removed {len(trimmed)} item(s)."
    tex = render_cv(profile, plan, template_text)
    return CVBuild(True, plan, tex=tex, pdf=last.pdf, pages=last.pages, message=msg, trimmed=trimmed, engine=last.engine)
