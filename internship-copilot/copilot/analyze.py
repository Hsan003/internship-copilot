"""Job analysis (LLM, schema-constrained) + deterministic enrichment + verified company facts."""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Callable, Optional

from . import prompts, techvocab
from .llm import BaseLLM
from .models import CompanyFact, Job, JobAnalysis
from .textutil import dedupe, norm_match, one_line, truncate_words

_ROLE_NOISE = re.compile(r"\s*[\(\[]\s*(h\s*/\s*f|f\s*/\s*h|m\s*/\s*[wf]\s*/\s*[dx]|w\s*/\s*m\s*/\s*d|m\s*/\s*f\s*/\s*d|all genders|gn)\s*[\)\]]\s*", re.IGNORECASE)


def clean_role_title(t: str) -> str:
    t = _ROLE_NOISE.sub(" ", t or "")
    t = re.sub(r"\s*[-–|]\s*(h\s*/\s*f|f\s*/\s*h|m\s*/\s*w\s*/\s*d)\s*$", "", t, flags=re.IGNORECASE)
    return one_line(t).strip(" -–|:")


def _short_list(xs, n: int, maxlen: int = 90) -> list[str]:
    out = []
    for x in dedupe(one_line(str(i)) for i in (xs or []) if i):
        out.append(x[:maxlen].rstrip())
    return out[:n]


def analyze_job(llm: BaseLLM, job: Job, *, cancel=None, on_progress: Optional[Callable[[int], None]] = None,
                temperature: float = 0.1) -> JobAnalysis:
    system, user = prompts.analysis_prompts(job)

    def validate(d: dict) -> dict:
        if not isinstance(d, dict):
            raise ValueError("expected an object")
        return d

    raw = llm.chat_json(system, user, schema=prompts.ANALYSIS_SCHEMA, validate=validate, task="analysis",
                        ctx={"text": job.description, "title": job.title, "company": job.company},
                        temperature=temperature, max_tokens=450, cancel=cancel, on_progress=on_progress)
    text_all = f"{job.title}\n{job.description}"
    counts = techvocab.find_terms(text_all)
    domains = [d for d in dedupe(raw.get("domains") or []) if d in prompts.DOMAIN_ENUM]
    inferred = techvocab.infer_domains(counts)
    domains = [d for d in domains if d != "other"] or inferred or ["other"]
    company = one_line(str(raw.get("company_name") or "")) or job.company
    # a company name the model "found" that is nowhere in the posting is a hallucination: prefer the page's value
    if company and job.company and norm_match(company) not in norm_match(text_all) and norm_match(company) != norm_match(job.company):
        company = job.company
    role = clean_role_title(str(raw.get("role_title") or "")) or clean_role_title(job.title)
    return JobAnalysis(
        role_title=role,
        company_name=company,
        mission=truncate_words(one_line(str(raw.get("mission") or "")), 45),
        must_have=_short_list(raw.get("must_have"), 6),
        nice_to_have=_short_list(raw.get("nice_to_have"), 4),
        domains=domains[:3],
        terms=dict(counts.most_common(30)),
        stack=[techvocab.display(t) for t, _ in counts.most_common(8)],
    )


# --------------------------------------------------------------------------- verified company facts
def verify_quote(quote: str, source: str, min_ratio: float = 0.9) -> bool:
    """True if ``quote`` really comes from ``source``.

    Accent/case/punctuation-insensitive. A verbatim (normalised) occurrence always passes. Otherwise the quote must be
    covered at >= 90% by long matching blocks of the source (tolerates a slipped word) AND every number in it must be
    present in the source (a changed figure such as 200 -> 900 never passes).
    """
    q, s = norm_match(quote), norm_match(source)
    if len(q) < 20:
        return False
    if q in s:
        return True
    if numbers_in(quote) - numbers_in(source):
        return False
    sm = SequenceMatcher(None, s, q, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size >= 4]
    return bool(blocks) and max(b.size for b in blocks) >= 25 and sum(b.size for b in blocks) >= min_ratio * len(q)


_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def numbers_in(text: str) -> set[str]:
    t = re.sub(r"(?<=\d)[ \u00a0\u202f,.](?=\d{3}(?!\d))", "", text or "")  # 1 200 / 1,200 / 1.200 -> 1200
    return {n.replace(",", ".") for n in _NUM.findall(t)}


def extract_company_facts(llm: BaseLLM, company: str, sources: list[tuple[str, str]], lang: str, *, cancel=None,
                          on_progress=None) -> list[CompanyFact]:
    """sources: [(label, text)] e.g. ("job posting", ...), ("https://acme.com/about", ...).

    Every fact must carry a verbatim quote that is found in one of the sources (else it is dropped) and must not
    contain numbers absent from its quote: the writer only ever sees verified facts.
    """
    sources = [(l, t) for l, t in sources if t and len(t.strip()) > 150]
    if not sources:
        return []
    combined = "\n\n".join(f"[Source: {l}]\n{t.strip()}" for l, t in sources)[:7000]
    system, user = prompts.facts_prompts(company, combined, lang)

    def validate(d: dict) -> list:
        facts = d.get("facts") if isinstance(d, dict) else None
        if not isinstance(facts, list):
            raise ValueError("'facts' must be a list")
        return facts

    try:
        raw = llm.chat_json(system, user, schema=prompts.FACTS_SCHEMA, validate=validate, task="facts", ctx={"text": combined},
                            temperature=0.1, max_tokens=300, cancel=cancel, on_progress=on_progress, retries=0)
    except Exception as exc:  # facts are a bonus: never fail the whole run because of them
        if exc.__class__.__name__ == "Cancelled":
            raise
        return []
    out: list[CompanyFact] = []
    for item in raw[:5]:
        if not isinstance(item, dict):
            continue
        fact, quote = one_line(str(item.get("fact") or "")), one_line(str(item.get("quote") or ""))
        if not fact or not quote:
            continue
        src = next((label for label, text in sources if verify_quote(quote, text)), None)
        if not src:
            continue
        if numbers_in(fact) - numbers_in(quote):
            continue  # a number in the paraphrase that the quote does not contain
        if any(f.fact.lower() == fact.lower() for f in out):
            continue
        out.append(CompanyFact(fact=fact[:240], quote=quote[:240], source=src))
        if len(out) >= 3:
            break
    return out
