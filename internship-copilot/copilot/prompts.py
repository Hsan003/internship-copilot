"""All prompt text in one place (tune freely).

Design notes for small local models:
- instructions in English (most reliable), output language stated explicitly,
- stable content first (job + facts), task-specific instruction last -> Ollama reuses the cached prompt prefix
  between the paragraph calls, which saves a lot of time on CPU,
- one short task per call, plain text output for prose, JSON-schema-constrained output for extraction.
"""
from __future__ import annotations

from typing import Optional

from . import config
from .models import Blueprint, CompanyFact, Evidence, Job, JobAnalysis, ParagraphSpec, Profile
from .retrieve import education_line, evidence_text, languages_line
from .textutil import LANG_NAMES, detect_language, localized, norm_ws

DOMAIN_ENUM = ["devops", "sre", "cloud", "backend", "frontend", "fullstack", "ai_ml", "data", "security", "mobile", "embedded", "other"]

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "role_title": {"type": "string"},
        "company_name": {"type": "string"},
        "mission": {"type": "string"},
        "must_have": {"type": "array", "items": {"type": "string"}},
        "nice_to_have": {"type": "array", "items": {"type": "string"}},
        "domains": {"type": "array", "items": {"type": "string", "enum": DOMAIN_ENUM}},
    },
    "required": ["role_title", "company_name", "mission", "must_have", "nice_to_have", "domains"],
}

KEYWORDS_SCHEMA = {
    "type": "object",
    "properties": {
        "keywords": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "importance": {"type": "string", "enum": ["must", "nice"]},
                    "category": {"type": "string", "enum": ["tech", "method", "soft", "language", "other"]},
                    "quote": {"type": "string"},
                },
                "required": ["term", "importance", "category", "quote"],
            },
        }
    },
    "required": ["keywords"],
}

RANK_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "score": {"type": "number"},
                    "bullets": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "score", "bullets"],
            },
        }
    },
    "required": ["items"],
}


def keywords_prompts(title: str, text: str) -> tuple[str, str]:
    system = (
        "You are an ATS / recruiter keyword extractor for internship postings written in French, English or German. "
        "Reply with a single JSON object only. Only list keywords that really appear in the posting; never invent any."
    )
    user = f"""Job title: {title or '(unknown)'}

JOB POSTING:
\"\"\"
{text}
\"\"\"

List EVERY keyword a recruiter or ATS would screen a CV for (up to 40): technologies, tools, frameworks, languages, cloud
platforms, methods and practices (e.g. CI/CD, agile, TDD), certifications, spoken languages, domains and key soft skills.
For each keyword return:
- term: the keyword as written in the posting (max 4 words, original spelling)
- importance: "must" if the posting requires it, "nice" if it is optional or a plus
- category: tech | method | soft | language | other
- quote: a short verbatim excerpt (max 12 words) of the posting that contains the term"""
    return system, user


def rank_prompts(title: str, text: str, items_text: str) -> tuple[str, str]:
    system = (
        "You help a student choose which of THEIR OWN projects and experiences to show on a one-page CV for a given job. "
        "Reply with a single JSON object only. Use only the ids listed below; never invent ids or content."
    )
    user = f"""Job title: {title or '(unknown)'}

JOB POSTING:
\"\"\"
{text}
\"\"\"

CANDIDATE ITEMS (id, then its bullets with their ids):
{items_text}

For EVERY item return: id, score (0-10: how relevant it is for this job) and bullets (the ids of its bullets, most relevant
to the job first, irrelevant ones last)."""
    return system, user


FACTS_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"fact": {"type": "string"}, "quote": {"type": "string"}},
                "required": ["fact", "quote"],
            },
        }
    },
    "required": ["facts"],
}


def analysis_prompts(job: Job) -> tuple[str, str]:
    system = (
        "You extract structured data from internship job postings written in French, English or German. "
        "Reply with a single JSON object only. Copy information from the posting; never invent anything. "
        "Keep every string short (max 25 words)."
    )
    user = f"""Page title: {job.title or '(unknown)'}
Company (from the page, may be empty): {job.company or '(unknown)'}

JOB POSTING:
\"\"\"
{job.description}
\"\"\"

Return JSON with these keys:
- role_title: the exact job title
- company_name: the hiring company ("" if the posting does not say)
- mission: 1-2 sentences saying what the intern will do, in the posting's language
- must_have: up to 6 required skills, technologies or qualifications (short phrases)
- nice_to_have: up to 4 optional skills or technologies
- domains: 1 to 3 values among {', '.join(DOMAIN_ENUM)}"""
    return system, user


def facts_prompts(company: str, source_text: str, lang: str) -> tuple[str, str]:
    system = (
        "You extract verifiable facts about a company from a text. "
        "Reply with a single JSON object only. Never use outside knowledge: only what the text says."
    )
    user = f"""COMPANY: {company or '(unknown)'}

TEXT:
\"\"\"
{source_text}
\"\"\"

List up to 2 concrete facts about the COMPANY itself (what it builds or sells, who its customers are, its scale, its technology, its mission).
Each item has:
- fact: the fact as one short sentence in {LANG_NAMES.get(lang, 'English')}
- quote: the exact words copied from the TEXT that support it (max 20 words, copied verbatim)
Skip anything about the job, the candidate profile, benefits or generic HR boilerplate.
If the text says nothing concrete about the company, return {{"facts": []}}."""
    return system, user


def gender_rule(profile: Profile, lang: str) -> str:
    if lang != "fr":
        return ""
    g = (profile.identity.gender or "").strip().lower()
    if g in ("f", "female", "femme", "feminine", "féminin"):
        return "The candidate is a woman: use feminine agreement in French (e.g. « étudiante »)."
    if g in ("m", "male", "homme", "masculine", "masculin"):
        return "The candidate is a man: use masculine agreement in French (e.g. « étudiant »)."
    return "The candidate's gender is unknown: avoid gendered words (write « en dernière année d'école d'ingénieurs » rather than « étudiant(e) »)."


def writer_system(bp: Blueprint, profile: Profile, lang: str, doc_kind: str) -> str:
    avoid = bp.avoid.get(lang, [])
    what = "a cover letter" if doc_kind == "letter" else "a short spontaneous application e-mail"
    parts = [
        f"You write ONE paragraph of {what} for a student applying for an internship.",
        "Rules:",
        f"- Write in {LANG_NAMES.get(lang, 'English')}. First person, as the candidate.",
    ]
    gr = gender_rule(profile, lang)
    if gr:
        parts.append(f"- {gr}")
    tone = localized(bp.tone, lang)
    if tone:
        parts.append(f"- Tone: {tone}")
    parts += [
        "- Use ONLY facts from CANDIDATE FACTS, COMPANY FACTS and the JOB section. Never invent employers, projects, tools, numbers, news, quotes or values.",
        "- Say you used or built something with a technology ONLY if that technology appears in CANDIDATE FACTS. You may mention the job's technologies as things you want to work with.",
        "- Be specific: name the project or experience, the tools and the result. No filler.",
    ]
    if avoid:
        parts.append("- Never use these words or phrases: " + "; ".join(f"“{a}”" for a in avoid) + ".")
    parts.append("- Output the paragraph text only: no greeting, no sign-off, no title, no bullet points, no markdown, no placeholders such as [Company].")
    return "\n".join(parts)


def candidate_block(profile: Profile, evidence: list[Evidence], lang: str) -> str:
    ident = profile.identity
    skills = ", ".join(dict.fromkeys(i for g in profile.skills for i in g.items))
    lines = [
        f"Candidate: {ident.name}, {localized(ident.title, lang)}.",
        f"Education: {education_line(profile, lang)}",
    ]
    ll = languages_line(profile, lang)
    if ll:
        lines.append(f"Languages: {ll}")
    if skills:
        lines.append(f"Skills: {skills}")
    lines.append("Most relevant work (best match first):")
    lines.append(evidence_text(evidence) or "(none)")
    return "\n".join(lines)


def job_block(job: Optional[Job], an: Optional[JobAnalysis], role: str, company: str, domains_hint: str = "") -> str:
    lines = [f"Title: {role or '(none)'}", f"Company: {company or '(unknown)'}"]
    if an:
        if an.mission:
            lines.append(f"Mission: {an.mission}")
        if an.must_have:
            lines.append("Key requirements: " + "; ".join(an.must_have))
        if an.stack:
            lines.append("Tech named in the posting: " + ", ".join(an.stack))
    if domains_hint:
        lines.append(f"Domain targeted by the candidate: {domains_hint}")
    return "\n".join(lines)


def context_block(*, job_part: str, facts: list[CompanyFact], candidate_part: str, note: str) -> str:
    facts_txt = "\n".join(f"- {f.fact}" for f in facts) if facts else "(none: do not make up anything about the company)"
    note_txt = note.strip() or "(none)"
    return (
        f"JOB\n{job_part}\n\nCOMPANY FACTS (verified in the sources)\n{facts_txt}\n\n"
        f"CANDIDATE FACTS\n{candidate_part}\n\nCANDIDATE NOTE (from the candidate; use it if relevant)\n{note_txt}"
    )


def load_style_examples(lang: str, max_chars: int = 1400) -> str:
    """Your own past letters (data/examples/*.txt) in the letter's language: the model imitates the voice, never the facts."""
    folder = config.DATA_DIR / "examples"
    if not folder.is_dir():
        return ""
    chosen: list[str] = []
    total = 0
    for f in sorted(folder.glob("*.txt")):
        if f.name.lower() == "readme.txt":
            continue
        try:
            text = norm_ws(f.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if len(text) < 200 or detect_language(text) not in (lang, "unknown"):
            continue
        room = max_chars - total
        if room < 300:
            break
        if len(text) > room:  # keep whole paragraphs where possible
            cut = text[:room].rsplit("\n\n", 1)[0] if "\n\n" in text[:room] else text[:room].rsplit(" ", 1)[0]
            text = cut.rstrip() + " […]"
        chosen.append(text)
        total += len(text)
    return "\n\n---\n\n".join(chosen)


def paragraph_task(spec: ParagraphSpec, lang: str, already: list[str], max_words: int, extra: str = "") -> str:
    label = localized(spec.label, lang) or spec.id
    goal = localized(spec.goal, lang)
    example = localized(spec.example, lang)
    prev = "\n".join(f"{i}) {t}" for i, t in enumerate(already, 1)) or "(none yet)"
    parts = [f"ALREADY WRITTEN PARAGRAPHS (do not repeat their content)\n{prev}", f"TASK\nWrite the “{label}” paragraph in {LANG_NAMES.get(lang, 'English')}, at most {max_words} words."]
    if goal:
        parts.append(goal)
    if extra.strip():
        parts.append(f"EXTRA INSTRUCTION FROM THE CANDIDATE: {extra.strip()}")
    voice = load_style_examples(lang)
    if voice:
        parts.insert(1, "VOICE REFERENCE (earlier letter(s) written by the candidate: imitate the voice and sentence rhythm only; "
                        f"never reuse their facts, employers, tools or numbers):\n{voice}")
    if example:
        parts.append(f"STYLE EXAMPLE (imitate the style and length only; do NOT reuse its facts, names or numbers):\n{example}")
    return "\n\n".join(parts)
