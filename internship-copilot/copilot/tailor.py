"""Tailor CV: paste a job description -> keyword report + a CV plan carrying the recruiter's keywords.

Two modes:
  * deterministic (no model): keywords come from the built-in technology vocabulary (``techvocab``);
  * AI-assisted (local Ollama model): the model extracts ALL keywords (methods, soft skills, tools missing from the
    vocabulary ...) and ranks your projects / bullets for the posting.

Honesty rules, identical in both modes: the model only *proposes*. A keyword is proposed for the CV only when the text of
your profile contains it (``covered``); a keyword the model found in the posting but your profile lacks is a ``gap`` and is
added only if the user explicitly ticks it. Model output is validated: keywords must literally occur in the posting, and
ranking ids must exist in the profile.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

from . import cv, prompts, retrieve, techvocab
from . import profile as prof
from .config import CVSettings
from .models import CVPlan, Profile
from .textutil import dedupe, norm_match, one_line

MAX_AI_KEYWORDS = 40


# --------------------------------------------------------------------------- helpers
def _flatten(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for k, v in value.items():
            if k != "contact_line":
                yield from _flatten(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _flatten(v)
    elif isinstance(value, str):
        yield str(value)


def cv_text(profile: Profile, plan: CVPlan, lang: str) -> str:
    ctx = cv.build_context(profile, plan, lang)
    return "\n".join(_flatten({k: v for k, v in ctx.items() if k not in ("L", "lang", "babel")}))


def cv_terms(profile: Profile, plan: CVPlan, lang: str) -> set[str]:
    """Canonical tech terms that are visible on the CV produced by ``plan``."""
    return set(techvocab.find_terms(cv_text(profile, plan, lang)))


def _contains(norm_blob: str, term: str) -> bool:
    """Whole-word occurrence of ``term`` in an already-normalised blob ('java' is not in 'javascript')."""
    n = norm_match(term)
    return bool(n) and f" {n} " in f" {norm_blob} "


def keyword_match(weights: dict[str, float], present: Iterable[str]) -> int:
    """Percentage (0-100) of the job's keyword weight that appears on the CV."""
    present = set(present)
    total = sum(weights.values())
    if total <= 0:
        return 0
    return round(100 * sum(w for t, w in weights.items() if t in present) / total)


def apply_keywords(plan: CVPlan, keywords: Iterable[str]) -> CVPlan:
    seen: set[str] = set()
    out: list[str] = []
    for k in keywords:
        k = one_line(str(k))
        if k and k.lower() not in seen:
            seen.add(k.lower())
            out.append(k)
    plan.extra_keywords = out
    return plan


# --------------------------------------------------------------------------- keyword table
def _vocab_keywords(text: str, title: str, jt: retrieve.JobTerms) -> list[dict]:
    counts = techvocab.find_terms(f"{title}\n{text}")
    return [{"key": t, "display": techvocab.display(t), "group": techvocab.group_of(t), "count": int(counts.get(t, 0)),
             "weight": round(w, 2), "source": "vocab"} for t, w in jt.weights.items()]


def extract_keywords_ai(llm, title: str, text: str, *, cancel=None, on_progress=None) -> list[dict]:
    """Model-proposed keywords, filtered so that every one really occurs in the posting."""
    system, user = prompts.keywords_prompts(title, text[:9000])

    def validate(d: Any) -> dict:
        if not isinstance(d, dict) or not isinstance(d.get("keywords"), list):
            raise ValueError("expected {'keywords': [...]}")
        return d

    raw = llm.chat_json(system, user, schema=prompts.KEYWORDS_SCHEMA, validate=validate, task="tailor_keywords",
                        ctx={"text": text, "title": title}, temperature=0.1, max_tokens=900, cancel=cancel, on_progress=on_progress)
    blob = norm_match(f"{title}\n{text}")
    out: list[dict] = []
    seen: set[str] = set()
    for k in raw["keywords"]:
        if not isinstance(k, dict):
            continue
        term = one_line(str(k.get("term") or ""))
        if not term or len(term) > 40 or len(term.split()) > 4 or not _contains(blob, term):
            continue  # not in the posting -> hallucination, dropped
        found = techvocab.find_terms(term)
        key = next(iter(found)) if found else "kw:" + norm_match(term)
        if key in seen:
            continue
        seen.add(key)
        must = k.get("importance") == "must"
        out.append({"key": key, "display": techvocab.display(key) if found else term, "group": techvocab.group_of(key) if found else str(k.get("category") or "other"),
                    "count": max(1, blob.count(norm_match(term))), "weight": 3.0 if must else 1.5, "source": "ai"})
        if len(out) >= MAX_AI_KEYWORDS:
            break
    return out


def _merge(vocab_kw: list[dict], ai_kw: list[dict]) -> list[dict]:
    by = {k["key"]: dict(k) for k in vocab_kw}
    for k in ai_kw:
        if k["key"] in by:
            by[k["key"]]["weight"] = round(max(by[k["key"]]["weight"], k["weight"]) + 0.25 * min(by[k["key"]]["count"], 3), 2)
            by[k["key"]]["source"] = "both"
        else:
            by[k["key"]] = k
    return sorted(by.values(), key=lambda k: -k["weight"])


# --------------------------------------------------------------------------- AI ranking of the CV content
def _items_text(profile: Profile, lang: str) -> str:
    from .textutil import localized

    lines = []
    for p in profile.projects:
        lines.append(f"[{p.id}] PROJECT {p.name} | {localized(p.tagline, lang)} | tools: {', '.join(p.stack)}")
        lines += [f"    ({b.id}) {localized(b.text, lang)[:170]}" for b in p.bullets]
    for x in profile.experience:
        lines.append(f"[{x.id}] EXPERIENCE {localized(x.role, lang)} @ {x.org}")
        lines += [f"    ({b.id}) {localized(b.text, lang)[:170]}" for b in x.bullets]
    return "\n".join(lines)


def rank_with_ai(llm, profile: Profile, plan: CVPlan, title: str, text: str, lang: str, cv_settings: CVSettings,
                 *, cancel=None, on_progress=None) -> tuple[CVPlan, bool]:
    """Re-order ``plan`` with the model's relevance scores. Unknown ids are ignored; returns (plan, used)."""
    system, user = prompts.rank_prompts(title, text[:6000], _items_text(profile, lang))

    def validate(d: Any) -> dict:
        if not isinstance(d, dict) or not isinstance(d.get("items"), list):
            raise ValueError("expected {'items': [...]}")
        return d

    raw = llm.chat_json(system, user, schema=prompts.RANK_SCHEMA, validate=validate, task="tailor_rank", ctx={"text": text, "title": title},
                        temperature=0.1, max_tokens=1100, cancel=cancel, on_progress=on_progress)
    items = {p.id: p for p in profile.projects} | {x.id: x for x in profile.experience}
    llm_score: dict[str, float] = {}
    llm_bullets: dict[str, list[str]] = {}
    for it in raw["items"]:
        if not isinstance(it, dict) or it.get("id") not in items:
            continue
        try:
            llm_score[it["id"]] = max(0.0, min(10.0, float(it.get("score"))))
        except (TypeError, ValueError):
            continue
        valid = {b.id for b in items[it["id"]].bullets}
        llm_bullets[it["id"]] = [b for b in dedupe(str(x) for x in (it.get("bullets") or [])) if b in valid]
    if not llm_score:
        return plan, False

    det = plan.scores
    top = max(det.values(), default=0.0) or 1.0
    # model opinion first; the deterministic score (normalised to 0-1) only breaks ties and covers items the model skipped
    plan.scores = {i: round(llm_score.get(i, 0.0) + det.get(i, 0.0) / top, 3) for i in items}
    projects = sorted((p.id for p in profile.projects), key=lambda i: -plan.scores[i])
    plan.project_ids = projects[: cv_settings.max_projects]
    for iid in [*plan.project_ids, *plan.experience_ids]:
        item = items[iid]
        order = llm_bullets.get(iid, [])
        rest = [b.id for b in item.bullets if b.id not in order]
        plan.bullet_ids[iid] = (order + rest)[: cv_settings.max_bullets_per_item]
    plan.notes = [f"Ranked with the AI model (relevance 0–10): " + ", ".join(
        f"{items[i].name if hasattr(items[i], 'name') else items[i].org} {llm_score[i]:g}" for i in sorted(llm_score, key=lambda i: -llm_score[i])[:4]) + "."]
    return plan, True


# --------------------------------------------------------------------------- the report
def analyze(profile: Profile, text: str, title: str, lang: str, cv_settings: CVSettings, *, llm=None,
            cancel=None, on_progress=None, step: Optional[Callable[[str, str], None]] = None) -> dict:
    """-> {plan, items, default_keywords, gaps, score_before, score_after, domains, ai, ai_notes}.  ``llm=None`` = deterministic."""
    step = step or (lambda k, l: None)
    jt = retrieve.build_job_terms(f"{title}\n{text}", title=title)
    plan = retrieve.plan_cv(profile, jt, lang, cv_settings)
    keywords = _vocab_keywords(text, title, jt)
    ai_notes: list[str] = []
    ai_used = False

    if llm is not None:
        from .llm import Cancelled

        try:
            step("keywords", "Extracting every keyword from the posting")
            keywords = _merge(keywords, extract_keywords_ai(llm, title, text, cancel=cancel, on_progress=on_progress))
            ai_used = True
        except Cancelled:
            raise
        except Exception as exc:
            ai_notes.append(f"The model could not extract keywords ({one_line(str(exc))[:140]}): the built-in vocabulary was used instead.")
        try:
            step("rank", "Ranking your projects and bullets for this job")
            plan, ranked = rank_with_ai(llm, profile, plan, title, text, lang, cv_settings, cancel=cancel, on_progress=on_progress)
            ai_used = ai_used or ranked
            if not ranked:
                ai_notes.append("The model gave no usable ranking: the built-in ranking was used.")
        except Cancelled:
            raise
        except Exception as exc:
            ai_notes.append(f"The model could not rank your work ({one_line(str(exc))[:140]}): the built-in ranking was used.")

    vocab = prof.profile_vocab(profile)
    profile_blob = norm_match("\n".join(prof.profile_text_blobs(profile)))
    base_text = cv_text(profile, plan, lang)
    base_terms, base_blob = set(techvocab.find_terms(base_text)), norm_match(base_text)

    def covered(k: dict) -> bool:
        return k["key"] in vocab or _contains(profile_blob, k["display"])

    def on_cv(k: dict, terms: set[str], blob: str) -> bool:
        return k["key"] in terms or _contains(blob, k["display"])

    items = [{**k, "status": "covered" if covered(k) else "gap", "in_cv": on_cv(k, base_terms, base_blob)} for k in keywords]
    default_keywords = [i["display"] for i in items if i["status"] == "covered" and not i["in_cv"]]
    gaps = [i["display"] for i in items if i["status"] == "gap"]

    with_default = apply_keywords(plan.model_copy(deep=True), default_keywords)
    after_text = cv_text(profile, with_default, lang)
    after_terms, after_blob = set(techvocab.find_terms(after_text)), norm_match(after_text)
    weights = {i["key"]: i["weight"] for i in items}
    return {
        "plan": plan, "items": items, "default_keywords": default_keywords, "gaps": gaps, "domains": jt.domains,
        "score_before": keyword_match(weights, (i["key"] for i in items if on_cv(i, base_terms, base_blob))),
        "score_after": keyword_match(weights, (i["key"] for i in items if on_cv(i, after_terms, after_blob))),
        "ai": ai_used, "ai_notes": ai_notes,
    }


def sanitize_plan(profile: Profile, plan: CVPlan) -> CVPlan:
    """A plan sent back by the browser may only reference things that exist in the profile."""
    proj = {p.id: p for p in profile.projects}
    exp = {x.id: x for x in profile.experience}
    plan.project_ids = [i for i in dedupe(plan.project_ids) if i in proj]
    plan.experience_ids = [i for i in dedupe(plan.experience_ids) if i in exp]
    items = proj | exp
    plan.bullet_ids = {i: [b for b in dedupe(bs) if b in {x.id for x in items[i].bullets}] for i, bs in plan.bullet_ids.items() if i in items}
    allowed = {s.lower() for g in profile.skills for s in g.items}
    skills = []
    for g in plan.skills:
        its = [s for s in g.get("items", []) if isinstance(s, str) and s.lower() in allowed]
        if its:
            skills.append({"category": str(g.get("category", "")), "items": its})
    plan.skills = skills
    return plan
