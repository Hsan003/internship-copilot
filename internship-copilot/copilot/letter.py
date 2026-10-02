"""Compose a cover letter / spontaneous e-mail from a blueprint, then render it to LaTeX/PDF and e-mail formats."""
from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path
from typing import Callable, Optional

from . import config, grounding, latex, prompts, techvocab
from . import profile as prof
from .analyze import numbers_in
from .cv import LABELS, display_url
from .llm import BaseLLM
from .models import (Blueprint, CompanyFact, DocDraft, Evidence, Flag, Job, JobAnalysis, ParagraphDraft, ParagraphSpec,
                     Profile)
from .textutil import (clean_paragraph, date_long, duration_text, localized, month_year, one_line, safe_format,
                       split_name, truncate_words, unknown_placeholders, word_count)

_DEFAULT_LABEL = {"en": "end-of-studies internship", "fr": "stage de fin d'études"}


# --------------------------------------------------------------------------- placeholders
def placeholders(profile: Profile, lang: str, *, company: str = "", role: str = "", role_focus: str = "",
                 contact_name: str = "") -> dict[str, str]:
    av, ident = profile.availability, profile.identity
    edu = profile.education[0] if profile.education else None
    title = localized(ident.title, lang)
    school = edu.school if edu else ""
    links = {k: display_url(v) for k, v in ident.links.items() if v}
    label = localized(av.internship_label, lang) or _DEFAULT_LABEL.get(lang, _DEFAULT_LABEL["en"])
    article = ("un " if lang == "fr" else ("an " if label[:1].lower() in "aeiou" else "a ")) + label
    ph = {
        "name": ident.name, "email": ident.email, "phone": ident.phone, "location": ident.location,
        "title": title, "school": school, "degree": localized(edu.degree, lang) if edu else "",
        "linkedin": links.get("linkedin", ""), "github": links.get("github", ""), "website": links.get("website", "") or links.get("portfolio", ""),
        "links_line": " · ".join(links.values()),
        "programme_short": " · ".join(x for x in (title, school) if x),
        "internship_label": label, "internship_label_a": article,
        "duration_text": duration_text(av.min_months, av.max_months, lang),
        "start_month_year": month_year(av.start_date, lang),
        "start_date_text": date_long(av.start_date, lang),
        "programme": localized(av.programme, lang),
        "availability_note": localized(av.note, lang),
        "company": company or ("votre entreprise" if lang == "fr" else "your company"),
        "role_title": role or ("stage" if lang == "fr" else "internship"),
        "role_focus": role_focus,
        "contact_name": contact_name,
    }
    return ph


def salutation(bp: Blueprint, lang: str, contact_name: str = "", style: str = "auto") -> str:
    table = bp.salutations.get(lang) or bp.salutations.get("en") or {}
    default = table.get("default") or ("Madame, Monsieur," if lang == "fr" else "Dear Hiring Team,")
    name = one_line(contact_name)
    if not name:
        return default
    first, last = split_name(name)
    key = {"ms": "named_ms", "mr": "named_mr", "first": "first_name"}.get(style, "named_neutral")
    tpl = table.get(key) or table.get("named_neutral") or default
    if not last and key in ("named_ms", "named_mr"):
        tpl = table.get("named_neutral") or default
    return safe_format(tpl, {"full_name": name, "first_name": first, "last_name": last or first})


# --------------------------------------------------------------------------- inputs bundle
@dataclass
class WriteInput:
    profile: Profile
    bp: Blueprint
    lang: str
    company: str = ""
    role: str = ""
    role_focus: str = ""
    job: Optional[Job] = None
    analysis: Optional[JobAnalysis] = None
    facts: Optional[list[CompanyFact]] = None
    evidence: Optional[list[Evidence]] = None
    note: str = ""
    contact_name: str = ""
    contact_role: str = ""
    greeting_style: str = "auto"
    domains_hint: str = ""
    job_terms: Optional[set[str]] = None  # canonical terms of the job (or targeted domains)


def _grounding_ctx(wi: WriteInput, spec: ParagraphSpec) -> grounding.GroundingContext:
    job_text = f"{wi.job.title}\n{wi.job.description}" if wi.job else ""
    fact_text = " ".join(f.fact + " " + f.quote for f in (wi.facts or []))
    blobs = "\n".join(prof.profile_text_blobs(wi.profile))
    return grounding.GroundingContext(
        lang=wi.lang, max_words=spec.max_words,
        profile_vocab=prof.profile_vocab(wi.profile), profile_numbers=prof.profile_numbers(wi.profile),
        job_terms=set(wi.job_terms or (wi.analysis.terms if wi.analysis else {})),
        job_numbers=numbers_in(job_text), extra_numbers=numbers_in(fact_text + " " + wi.note),
        avoid=wi.bp.avoid.get(wi.lang, []),
        known_text=" ".join([job_text, blobs, fact_text, wi.note, wi.company, wi.role]),
        example_text=localized(spec.example, wi.lang), note=wi.note,
        exempt_terms=set(techvocab.find_terms(f"{wi.role} {wi.role_focus} {wi.company} {wi.domains_hint}")),
        source_quantities=grounding.quantities(" ".join([job_text, blobs, fact_text, wi.note])),
    )


# --------------------------------------------------------------------------- writing
def _ctx_for_fake(wi: WriteInput, spec: ParagraphSpec) -> dict:
    return {"lang": wi.lang, "pid": spec.id, "company": wi.company, "role": wi.role or wi.role_focus,
            "facts": [{"fact": f.fact} for f in (wi.facts or [])],
            "evidence": [{"title": e.title, "stack": e.stack, "bullets": e.bullets} for e in (wi.evidence or [])],
            "note": wi.note}


def write_paragraph(llm: BaseLLM, wi: WriteInput, spec: ParagraphSpec, already: list[str], *, extra: str = "",
                    cancel=None, on_progress=None, temperature: Optional[float] = None, first: bool = False) -> ParagraphDraft:
    st = config.load_settings()
    system = prompts.writer_system(wi.bp, wi.profile, wi.lang, wi.bp.kind)
    context = prompts.context_block(
        job_part=prompts.job_block(wi.job, wi.analysis, wi.role, wi.company, wi.domains_hint),
        facts=wi.facts or [],
        candidate_part=prompts.candidate_block(wi.profile, wi.evidence or [], wi.lang),
        note=wi.note,
    )
    task = prompts.paragraph_task(spec, wi.lang, already, spec.max_words, extra)
    user = context + "\n\n" + task
    temp = st.llm.temperature_write if temperature is None else temperature
    max_tokens = int(spec.max_words * 2.4) + 40
    gc = _grounding_ctx(wi, spec)
    gc.earlier = list(already)
    gc.note, gc.use_note = wi.note, first and bool(wi.note.strip())  # the opening paragraph must build on the note

    def ask(history=None) -> str:
        raw = llm.chat(system, user, task=f"paragraph:{spec.id}", ctx=_ctx_for_fake(wi, spec), temperature=temp,
                       max_tokens=max_tokens, history=history, cancel=cancel, on_progress=on_progress)
        return clean_paragraph(raw)

    text = ask()
    flags = grounding.check_paragraph(text, gc)
    fixes = grounding.retry_instructions(flags)
    if fixes:  # one automatic rewrite
        correction = "Rewrite the paragraph fixing these problems:\n- " + "\n- ".join(fixes) + "\nOutput only the corrected paragraph."
        text2 = ask([{"role": "assistant", "content": text}, {"role": "user", "content": correction}])
        flags2 = grounding.check_paragraph(text2, gc)
        if len(grounding.retry_instructions(flags2)) <= len(fixes) and word_count(text2) >= gc.min_words:
            text, flags = text2, flags2
    deduped = grounding.drop_repeats(text, already)
    if deduped != text and word_count(deduped) >= gc.min_words:
        text = deduped
        flags = grounding.check_paragraph(text, gc)
        flags.append(Flag(level="info", code="dedup", message="A sentence that repeated an earlier paragraph was removed."))
    if word_count(text) > spec.max_words:
        cut = truncate_words(text, spec.max_words)
        if cut != text:
            text = cut
            # re-check the text that will actually be used (the cut may have removed what was flagged)
            flags = [f for f in grounding.check_paragraph(text, gc) if f.code != "too_long"]
            flags.append(Flag(level="info", code="shortened", message=f"Shortened to ≤ {spec.max_words} words at a sentence boundary."))
    return ParagraphDraft(id=spec.id, kind="generated", label=localized(spec.label, wi.lang) or spec.id, text=text,
                          max_words=spec.max_words, flags=flags)


def fixed_paragraph(wi: WriteInput, spec: ParagraphSpec) -> ParagraphDraft:
    ph = placeholders(wi.profile, wi.lang, company=wi.company, role=wi.role, role_focus=wi.role_focus, contact_name=wi.contact_name)
    raw = localized(spec.text, wi.lang)
    text = one_line(safe_format(raw, ph))
    flags = [Flag(level="warn", code="unknown_placeholder", message=f"Unknown placeholder {{{n}}} (check the blueprint).")
             for n in unknown_placeholders(raw, ph)]
    return ParagraphDraft(id=spec.id, kind="fixed", label=localized(spec.label, wi.lang) or spec.id, text=text,
                          max_words=spec.max_words, flags=flags)


def header_parts(wi: WriteInput, today: Optional[date] = None) -> dict:
    today = today or date.today()
    ph = placeholders(wi.profile, wi.lang, company=wi.company, role=wi.role, role_focus=wi.role_focus, contact_name=wi.contact_name)
    subj_tpl = localized(wi.bp.subject, wi.lang)
    if wi.bp.kind == "email" and not wi.role_focus and wi.bp.subject_no_role:
        subj_tpl = localized(wi.bp.subject_no_role, wi.lang)
    subject = one_line(safe_format(subj_tpl, ph))
    city = wi.profile.identity.location.split(",")[0].strip()
    when = date_long(today, wi.lang)
    place_date = (f"{city}, le {when}" if wi.lang == "fr" else f"{city}, {when}") if city else when
    recipient: list[str] = []
    if wi.bp.kind == "letter":
        if wi.contact_name:
            recipient.append(wi.contact_name + (f", {wi.contact_role}" if wi.contact_role else ""))
        if wi.company:
            recipient.append(wi.company)
    sig = [one_line(safe_format(l, ph)) for l in wi.bp.signature_lines]
    sig = [s for s in sig if s.strip(" ·|-")]
    return {
        "subject": subject,
        "salutation": salutation(wi.bp, wi.lang, wi.contact_name, wi.greeting_style),
        "closing": one_line(safe_format(localized(wi.bp.closing, wi.lang), ph)),
        "signature": sig, "recipient": recipient, "place_date": place_date,
    }


def compose(llm: BaseLLM, wi: WriteInput, *, extras: Optional[dict[str, str]] = None, existing: Optional[DocDraft] = None,
            only: Optional[set[str]] = None, cancel=None, progress: Optional[Callable[[str, str], None]] = None,
            on_tokens: Optional[Callable[[int], None]] = None, today: Optional[date] = None) -> DocDraft:
    """Write all paragraphs (or only ``only`` when regenerating) in blueprint order."""
    extras = extras or {}
    old = {p.id: p for p in (existing.paragraphs if existing else [])}
    paras: list[ParagraphDraft] = []
    first_generated = next((sp.id for sp in wi.bp.paragraphs if sp.kind == "generated"), None)
    for spec in wi.bp.paragraphs:
        if only is not None and spec.id not in only and spec.id in old:
            paras.append(old[spec.id])
            continue
        if spec.kind == "fixed":
            paras.append(fixed_paragraph(wi, spec))
            continue
        label = localized(spec.label, wi.lang) or spec.id
        if progress:
            progress(f"write:{spec.id}", f"Writing “{label}”")
        paras.append(write_paragraph(llm, wi, spec, [p.text for p in paras if p.text], extra=extras.get(spec.id, ""),
                                     cancel=cancel, on_progress=on_tokens, first=(spec.id == first_generated)))
    hp = header_parts(wi, today)
    return DocDraft(kind=wi.bp.kind, paragraphs=paras, **hp)


# --------------------------------------------------------------------------- rendering
def sender_block(profile: Profile) -> latex.RawTeX:
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


def letter_context(profile: Profile, doc: DocDraft, lang: str, babel: Optional[str] = None) -> dict:
    return {
        "babel": babel or ("french" if lang == "fr" else "english"),
        "lang": lang,
        "L": LABELS.get(lang, LABELS["en"]),
        "sender_name": profile.identity.name,
        "sender_block": sender_block(profile),
        "recipient_block": latex.tex_lines(doc.recipient),
        "place_date": doc.place_date,
        "subject": doc.subject,
        "salutation": doc.salutation,
        "paragraphs": [p.text for p in doc.paragraphs if p.text.strip()],
        "closing": doc.closing,
        "signature_block": latex.tex_lines(doc.signature or [profile.identity.name]),
    }


def render_letter(profile: Profile, doc: DocDraft, lang: str, template_text: str, babel: Optional[str] = None) -> str:
    return latex.render(template_text, letter_context(profile, doc, lang, babel))


@dataclass
class LetterBuild:
    ok: bool
    tex: str = ""
    pdf: Optional[Path] = None
    pages: Optional[int] = None
    message: str = ""


def build_letter(profile: Profile, doc: DocDraft, lang: str, template_text: str, outdir: Path, name: str = "letter",
                 settings: Optional[config.Settings] = None) -> LetterBuild:
    st = settings or config.load_settings()
    try:
        tex = render_letter(profile, doc, lang, template_text)
    except latex.TemplateError as exc:
        return LetterBuild(False, message=str(exc))
    res = latex.build_pdf(lambda babel: render_letter(profile, doc, lang, template_text, babel), outdir, name, lang,
                          st.latex.engine, st.latex.timeout_s)
    if not res.ok:
        return LetterBuild(False, tex=tex, message=res.message)
    msg = res.message
    if res.pages and res.pages > 1:
        msg = (msg + " " if msg else "") + f"The letter is {res.pages} pages: shorten a paragraph or reduce max_words in the blueprint."
    return LetterBuild(True, tex=tex, pdf=res.pdf, pages=res.pages, message=msg)


# --------------------------------------------------------------------------- plain-text / e-mail outputs
def body_text(doc: DocDraft, include_subject: bool = False) -> str:
    parts = [doc.salutation, *[p.text for p in doc.paragraphs if p.text.strip()], doc.closing]
    body = "\n\n".join(parts) + "\n" + "\n".join(doc.signature)
    return (f"Subject: {doc.subject}\n\n" if include_subject else "") + body.strip() + "\n"


def mailto_link(to: str, subject: str, body: str) -> str:
    q = urllib.parse.urlencode({"subject": subject, "body": body}, quote_via=urllib.parse.quote)
    return f"mailto:{urllib.parse.quote(to or '', safe='@,')}?{q}"


def build_eml(doc: DocDraft, sender: str, to: str = "", attachment: Optional[Path] = None) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = doc.subject
    if sender:
        msg["From"] = sender
    msg["To"] = to or ""
    msg["Date"] = formatdate(localtime=True)
    msg["X-Unsent"] = "1"  # opens as an editable draft in Outlook / Thunderbird
    msg.set_content(body_text(doc))
    if attachment and attachment.exists():
        msg.add_attachment(attachment.read_bytes(), maintype="application", subtype="pdf", filename=attachment.name)
    return bytes(msg)
