"""Which of YOUR projects / experiences / skills are relevant to a job?  (deterministic, no LLM)

Score = weighted overlap of detected technologies  +  small TF-IDF cosine on the remaining words
        +  bonus for matching the job's domain (devops, ai_ml, backend ...).
The result feeds (1) the facts shown to the model and (2) the tailored CV selection.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable

from . import techvocab
from .config import CVSettings
from .models import CVPlan, Evidence, Experience, Profile, Project
from .textutil import _STOP, duration_text, localized, month_year, strip_accents

_TOKEN = re.compile(r"[a-z0-9+#.]{3,}")
_STOPWORDS = set().union(*_STOP.values()) | {"will", "work", "team", "using", "such", "stage", "internship", "poste", "mission"}


@dataclass
class JobTerms:
    weights: dict[str, float] = field(default_factory=dict)  # canonical term -> weight
    domains: list[str] = field(default_factory=list)
    tokens: Counter = field(default_factory=Counter)


def _tokens(text: str) -> Counter:
    return Counter(t for t in _TOKEN.findall(strip_accents(text.lower())) if t not in _STOPWORDS and not t.isdigit())


def build_job_terms(text: str, must_have: Iterable[str] = (), nice_to_have: Iterable[str] = (), title: str = "",
                    domains: Iterable[str] = ()) -> JobTerms:
    counts = techvocab.find_terms(text)
    weights: dict[str, float] = {t: 1.0 + min(n, 3) * 0.25 for t, n in counts.items()}
    for t in techvocab.find_terms(" ; ".join(must_have)):
        weights[t] = weights.get(t, 1.0) + 1.5
    for t in techvocab.find_terms(" ; ".join(nice_to_have)):
        weights[t] = weights.get(t, 1.0) + 0.4
    for t in techvocab.find_terms(title):
        weights[t] = weights.get(t, 1.0) + 1.0
    doms = list(dict.fromkeys(list(domains) + techvocab.infer_domains(counts)))
    return JobTerms(weights=weights, domains=[d for d in doms if d != "other"][:3], tokens=_tokens(text))


def domain_job_terms(domains: Iterable[str]) -> JobTerms:
    """For spontaneous applications: no posting, only the domains you target."""
    weights: dict[str, float] = {}
    doms = [d for d in domains if d in techvocab.DOMAIN_GROUPS and d != "other"]
    for d in doms:
        for t in techvocab.domain_terms(d):
            weights[t] = max(weights.get(t, 0), 1.0)
    return JobTerms(weights=weights, domains=doms)


# --------------------------------------------------------------------------- item helpers
def _both(value, *langs: str) -> str:
    return " ".join(dict.fromkeys(localized(value, l) for l in langs if localized(value, l)))


def project_blob(p: Project) -> str:
    parts = [p.name, _both(p.tagline, "en", "fr"), " ".join(p.stack), " ".join(p.tags)]
    parts += [_both(b.text, "en", "fr") + " " + " ".join(b.tags) for b in p.bullets]
    return "\n".join(x for x in parts if x)


def experience_blob(x: Experience) -> str:
    parts = [x.org, _both(x.role, "en", "fr"), " ".join(x.tags)]
    parts += [_both(b.text, "en", "fr") + " " + " ".join(b.tags) for b in x.bullets]
    return "\n".join(x for x in parts if x)


def _item_terms(blob: str, tags: Iterable[str]) -> set[str]:
    terms = set(techvocab.find_terms(blob))
    for t in tags:
        found = techvocab.find_terms(t)
        terms.update(found.keys() if found else {t.strip().lower()})
    return terms


@dataclass
class Ranked:
    kind: str
    id: str
    score: float
    matched: list[str]
    item: object


def _score_terms(terms: set[str], jt: JobTerms) -> tuple[float, list[str]]:
    hit = {t: jt.weights[t] for t in terms if t in jt.weights}
    score = sum(hit.values())
    if jt.domains:
        dom = set().union(*(techvocab.domain_terms(d) for d in jt.domains))
        score += min(2.0, 0.4 * len(terms & dom))
    matched = [techvocab.display(t) for t, _ in sorted(hit.items(), key=lambda kv: -kv[1])]
    return score, matched


def _cosine(job_tokens: Counter, item_tokens: Counter, idf: dict[str, float]) -> float:
    if not job_tokens or not item_tokens:
        return 0.0
    a = {t: (1 + math.log(c)) * idf.get(t, 1.0) for t, c in job_tokens.items() if t in item_tokens}
    if not a:
        return 0.0
    num = sum(a[t] * (1 + math.log(item_tokens[t])) * idf.get(t, 1.0) for t in a)
    da = math.sqrt(sum(((1 + math.log(c)) * idf.get(t, 1.0)) ** 2 for t, c in job_tokens.items()))
    db = math.sqrt(sum(((1 + math.log(c)) * idf.get(t, 1.0)) ** 2 for t, c in item_tokens.items()))
    return num / (da * db) if da and db else 0.0


def rank_items(profile: Profile, jt: JobTerms) -> list[Ranked]:
    items: list[tuple[str, str, str, list[str], object]] = []
    for p in profile.projects:
        items.append(("project", p.id, project_blob(p), p.tags + p.stack, p))
    for x in profile.experience:
        items.append(("experience", x.id, experience_blob(x), x.tags, x))
    toks = [_tokens(b) for _, _, b, _, _ in items]
    df: Counter = Counter()
    for t in toks:
        df.update(set(t))
    n = max(1, len(items))
    idf = {t: math.log((1 + n) / (1 + c)) + 1 for t, c in df.items()}
    out: list[Ranked] = []
    for (kind, iid, blob, tags, obj), tk in zip(items, toks):
        terms = _item_terms(blob, tags)
        s, matched = _score_terms(terms, jt)
        s += 3.0 * _cosine(jt.tokens, tk, idf)
        out.append(Ranked(kind, iid, round(s, 3), matched, obj))
    return out


def rank_bullets(bullets, jt: JobTerms, lang: str) -> list[tuple[float, object]]:
    scored = []
    for i, b in enumerate(bullets):
        terms = _item_terms(_both(b.text, "en", "fr"), b.tags)
        s, _ = _score_terms(terms, jt)
        scored.append((s - i * 0.001, b))  # stable: earlier bullets win ties
    return sorted(scored, key=lambda x: -x[0])


# --------------------------------------------------------------------------- evidence for the writer
def pick_evidence(profile: Profile, jt: JobTerms, lang: str, k: int = 3, bullets_per_item: int = 2) -> list[Evidence]:
    """The k most relevant items, always including the best work experience (a real internship is evidence for any job)."""
    ranked = sorted(rank_items(profile, jt), key=lambda r: -r.score)
    chosen = ranked[:k]
    best_exp = next((r for r in ranked if r.kind == "experience"), None)
    if best_exp and best_exp not in chosen and k >= 2:
        chosen = chosen[: k - 1] + [best_exp]
    chosen = sorted(chosen, key=lambda r: -r.score)
    out: list[Evidence] = []
    for r in chosen:
        o = r.item
        top = [b for _, b in rank_bullets(o.bullets, jt, lang)[:bullets_per_item]]
        if r.kind == "project":
            out.append(Evidence(id=o.id, kind="project", title=o.name, subtitle=localized(o.tagline, lang), period=o.period,
                                stack=list(o.stack), bullets=[localized(b.text, lang) for b in top], score=r.score, matched=r.matched))
        else:
            out.append(Evidence(id=o.id, kind="experience", title=f"{localized(o.role, lang)} @ {o.org}", subtitle="", period=o.period,
                                stack=[], bullets=[localized(b.text, lang) for b in top], score=r.score, matched=r.matched))
    return out


def evidence_text(evs: list[Evidence]) -> str:
    lines = []
    for e in evs:
        head = f"- [{e.kind}] {e.title}" + (f" ({e.period})" if e.period else "")
        if e.subtitle:
            head += f" — {e.subtitle}"
        if e.stack:
            head += f" | tools: {', '.join(e.stack)}"
        lines.append(head)
        lines += [f"    * {b}" for b in e.bullets]
    return "\n".join(lines)


def education_line(profile: Profile, lang: str) -> str:
    if not profile.education:
        return ""
    e = profile.education[0]
    return f"{localized(e.degree, lang)} — {e.school} ({e.period})".strip()


def languages_line(profile: Profile, lang: str) -> str:
    return ", ".join(f"{localized(l.name, lang)} ({localized(l.level, lang)})" for l in profile.languages)


# --------------------------------------------------------------------------- CV plan
def make_headline(profile: Profile, domains: list[str], lang: str) -> str:
    av = profile.availability
    title = localized(profile.identity.title, lang)
    labels = [techvocab.DOMAIN_SHORT[d][lang] for d in domains[:2] if d in techvocab.DOMAIN_SHORT]
    avail = (f"Disponible dès {month_year(av.start_date, 'fr')} · {duration_text(av.min_months, av.max_months, 'fr')}"
             if lang == "fr" else f"Available from {month_year(av.start_date, 'en')} · {duration_text(av.min_months, av.max_months, 'en')}")
    head = " · ".join(x for x in [title, " / ".join(labels)] if x)
    return f"{head} — {avail}" if head else avail


def plan_cv(profile: Profile, jt: JobTerms, lang: str, cv: CVSettings) -> CVPlan:
    ranked = rank_items(profile, jt)
    proj = sorted([r for r in ranked if r.kind == "project"], key=lambda r: -r.score)
    scores = {r.id: r.score for r in ranked}
    matched = {r.id: r.matched for r in ranked}
    notes: list[str] = []

    project_ids = [r.id for r in proj[: cv.max_projects]]
    for r in proj[: cv.max_projects]:
        if r.matched:
            notes.append(f"Project “{r.item.name}” kept (matches: {', '.join(r.matched[:5])}).")
    dropped = [r.item.name for r in proj[cv.max_projects:]]
    if dropped:
        notes.append("Left out (less relevant here): " + ", ".join(dropped) + ".")

    experience_ids = [x.id for x in profile.experience][: cv.max_experience_items]
    bullet_ids: dict[str, list[str]] = {}
    for item in [*[p for p in profile.projects if p.id in project_ids], *[x for x in profile.experience if x.id in experience_ids]]:
        order = rank_bullets(item.bullets, jt, lang)
        bullet_ids[item.id] = [b.id for _, b in order[: cv.max_bullets_per_item]]
    # project order = relevance order (already sorted); experience order = as in the profile (chronological)
    skills = []
    for g in profile.skills:
        its = list(g.items)
        hit = [i for i in its if _item_terms(i, []) & set(jt.weights)]
        rest = [i for i in its if i not in hit]
        skills.append({"category": localized(g.category, lang), "items": hit + rest, "_hits": len(hit)})
    skills.sort(key=lambda s: -s["_hits"])
    for s in skills:
        s.pop("_hits")
    return CVPlan(lang=lang, headline=make_headline(profile, jt.domains, lang), project_ids=project_ids,
                  experience_ids=experience_ids, bullet_ids=bullet_ids, skills=skills, scores=scores, matched=matched, notes=notes)
