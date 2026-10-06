"""Tailor CV: paste a job description -> keyword report + a CV plan carrying the recruiter's keywords.  (deterministic, no LLM)

Honesty rule: a keyword is added to the CV only if the profile backs it (``prof.profile_vocab``). Job terms the profile
does not mention are reported as *gaps*; they are added only when the user explicitly ticks them.
"""
from __future__ import annotations

from typing import Any, Iterable

from . import cv, retrieve, techvocab
from . import profile as prof
from .config import CVSettings
from .models import CVPlan, Profile


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


def cv_terms(profile: Profile, plan: CVPlan, lang: str) -> set[str]:
    """Canonical tech terms that are visible on the CV produced by ``plan``."""
    ctx = cv.build_context(profile, plan, lang)
    ctx = {k: v for k, v in ctx.items() if k not in ("L", "lang", "babel")}
    return set(techvocab.find_terms("\n".join(_flatten(ctx))))


def keyword_match(weights: dict[str, float], present: set[str]) -> int:
    """Percentage (0-100) of the job's keyword weight that appears on the CV."""
    total = sum(weights.values())
    if total <= 0:
        return 0
    return round(100 * sum(w for t, w in weights.items() if t in present) / total)


def apply_keywords(plan: CVPlan, keywords: Iterable[str]) -> CVPlan:
    seen: set[str] = set()
    out: list[str] = []
    for k in keywords:
        k = " ".join(str(k).split())
        if k and k.lower() not in seen:
            seen.add(k.lower())
            out.append(k)
    plan.extra_keywords = out
    return plan


def analyze(profile: Profile, text: str, title: str, lang: str, cv_settings: CVSettings) -> dict:
    """-> {plan, items, default_keywords, gaps, score_before, score_after, domains}"""
    jt = retrieve.build_job_terms(f"{title}\n{text}", title=title)
    counts = techvocab.find_terms(f"{title}\n{text}")
    plan = retrieve.plan_cv(profile, jt, lang, cv_settings)
    vocab = prof.profile_vocab(profile)
    base_present = cv_terms(profile, plan, lang)

    items = []
    for t, w in sorted(jt.weights.items(), key=lambda kv: -kv[1]):
        items.append({
            "key": t, "display": techvocab.display(t), "group": techvocab.group_of(t), "count": int(counts.get(t, 0)),
            "weight": round(w, 2), "status": "covered" if t in vocab else "gap", "in_cv": t in base_present,
        })
    default_keywords = [i["display"] for i in items if i["status"] == "covered" and not i["in_cv"]]
    gaps = [i["display"] for i in items if i["status"] == "gap"]

    with_default = apply_keywords(plan.model_copy(deep=True), default_keywords)
    return {
        "plan": plan,
        "items": items,
        "default_keywords": default_keywords,
        "gaps": gaps,
        "score_before": keyword_match(jt.weights, base_present),
        "score_after": keyword_match(jt.weights, cv_terms(profile, with_default, lang)),
        "domains": jt.domains,
    }
