"""Fact-checking of generated paragraphs against what is actually known (profile, job, verified company facts).

Hard problems trigger one automatic rewrite; whatever remains is shown to the user as a flag next to the paragraph.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from . import techvocab
from .analyze import numbers_in
from .models import Flag
from .textutil import _STOP, detect_language, norm_match, split_sentences, strip_accents, word_count

RETRY_CODES = {"placeholder", "language", "avoid_phrase", "too_long", "too_short", "example_leak", "tech_unverified",
               "number_unverified", "unit_mismatch", "repeats_previous", "note_ignored"}

_PLACEHOLDER = re.compile(r"\[[^\]\n]{2,40}\]|\{[a-zA-Z_ ]{2,30}\}|<[A-Za-zÀ-ÿ ]{2,30}>|\bX{2,}\b|\[\.\.\.\]|\bLorem ipsum\b", re.IGNORECASE)
_CAP_NAME = re.compile(r"(?<=[a-zà-ÿ,;:] )([A-ZÀ-Ý][a-zà-ÿ]{3,})")


@dataclass
class GroundingContext:
    lang: str
    max_words: int
    profile_vocab: set[str] = field(default_factory=set)
    profile_numbers: set[str] = field(default_factory=set)
    job_terms: set[str] = field(default_factory=set)
    job_numbers: set[str] = field(default_factory=set)
    extra_numbers: set[str] = field(default_factory=set)  # from verified facts and the user's own note
    avoid: list[str] = field(default_factory=list)
    known_text: str = ""  # job + profile + facts + note (used to see whether a name came from the style example)
    example_text: str = ""
    min_words: int = 8
    exempt_terms: set[str] = field(default_factory=set)  # terms naming the target role / company (not claims)
    earlier: list = field(default_factory=list)  # paragraphs already written (for repetition checks)
    note: str = ""  # the user's personal note; checked only for the paragraph that must use it
    use_note: bool = False
    source_quantities: set = field(default_factory=set)  # {(number, unit_category)} present in the sources


# --------------------------------------------------------------------------- quantities: "6 minutes" is not "6 hours"
_UNIT_WORDS = {
    "minute": "min mins minute minutes",
    "hour": "h hr hrs heure heures hour hours stunde stunden",
    "second": "s sec secs seconde secondes second seconds sekunde sekunden",
    "day": "jour jours day days tag tage tagen",
    "week": "semaine semaines week weeks woche wochen",
    "month": "mois month months monat monate monaten",
    "year": "an ans annee annees year years jahr jahre jahren",
    "percent": "% pour_cent percent prozent",
    "ms": "ms milliseconde millisecondes millisecond milliseconds millisekunden",
}
_UNIT_OF = {w: cat for cat, words in _UNIT_WORDS.items() for w in words.split()}
_WORD_NUM = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "fifty": 50, "hundred": 100,
    "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "sept": 7, "huit": 8, "neuf": 9, "dix": 10, "onze": 11,
    "douze": 12, "quinze": 15, "vingt": 20, "trente": 30, "cinquante": 50, "cent": 100,
    "ein": 1, "eine": 1, "zwei": 2, "drei": 3, "vier": 4, "funf": 5, "fuenf": 5, "sechs": 6, "sieben": 7, "acht": 8, "neun": 9,
    "zehn": 10, "elf": 11, "zwolf": 12, "zwoelf": 12,
}
_NUMTOK = r"(?:\d+(?:\.\d+)?|" + "|".join(sorted(_WORD_NUM, key=len, reverse=True)) + r")"
_UNITTOK = r"(%|[a-z_]+)"
_RANGE_Q = re.compile(rf"(?<![a-z0-9.]){_NUMTOK}\s*(?:a|to|et|and|bis|ou|or|-)\s*({_NUMTOK})\s*{_UNITTOK}")
_SINGLE_Q = re.compile(rf"(?<![a-z0-9.])({_NUMTOK})\s*{_UNITTOK}")


def _num(tok: str) -> str:
    return str(_WORD_NUM[tok]) if tok in _WORD_NUM else str(float(tok)).rstrip("0").rstrip(".")


def quantities(text: str) -> set[tuple[str, str]]:
    """{(number, unit_category)} for time units and percentages, in EN/FR/DE, digits or spelled-out numbers."""
    t = strip_accents((text or "").lower()).replace("\u202f", " ").replace("\u00a0", " ")
    t = re.sub(r"(?<=\d)[ ,.](?=\d{3}(?!\d))", "", t)
    t = re.sub(r"(?<=\d),(?=\d)", ".", t)
    t = t.replace("pour cent", "%").replace("per cent", "%")
    out: set[tuple[str, str]] = set()
    for m in _RANGE_Q.finditer(t):
        unit = _UNIT_OF.get(m.group(2))
        if unit:
            lo = re.match(rf"\s*({_NUMTOK})", t[m.start():])
            out.add((_num(m.group(1)), unit))
            if lo:
                out.add((_num(lo.group(1)), unit))
    for m in _SINGLE_Q.finditer(t):
        unit = _UNIT_OF.get(m.group(2))
        if unit:
            out.add((_num(m.group(1)), unit))
    return out


# --------------------------------------------------------------------------- phrase-level helpers
_STOPWORDS = set().union(*_STOP.values()) | {"have", "been", "with", "that", "this", "from", "your", "very", "also", "which", "their", "will", "would"}


_NUMBER_WORDS = set(
    "zero one two three four five six seven eight nine ten eleven twelve fifteen twenty thirty fifty hundred "
    "deux trois quatre cinq sept huit neuf dix onze douze quinze vingt trente cinquante cent "
    "zwei drei vier funf fuenf sechs sieben acht neun zehn elf zwolf zwoelf".split())


def _content_tokens(text: str) -> list[str]:
    return [t for t in norm_match(text).split() if len(t) > 3 and t not in _STOPWORDS and t not in _NUMBER_WORDS and not t.isdigit()]


def content_bigrams(text: str) -> set[str]:
    toks = _content_tokens(text)
    return {f"{a} {b}" for a, b in zip(toks, toks[1:])}


def _sig(sentence: str) -> frozenset:
    return frozenset(_content_tokens(sentence))


def repeated_sentences(text: str, earlier: list[str], threshold: float = 0.75) -> list[str]:
    """Sentences of ``text`` that (nearly) repeat a sentence of an earlier paragraph."""
    prev = [_sig(x) for e in earlier for x in split_sentences(e)]
    out = []
    for sent in split_sentences(text):
        sig = _sig(sent)
        if len(sig) < 3:
            continue
        if any(p and len(sig & p) / len(sig | p) >= threshold for p in prev):
            out.append(sent)
    return out


def drop_repeats(text: str, earlier: list[str]) -> str:
    """Remove repeated sentences (keeps the text unchanged if everything would be removed)."""
    dups = set(repeated_sentences(text, earlier))
    if not dups:
        return text
    kept = [s for s in split_sentences(text) if s not in dups]
    return " ".join(kept) if kept else text


def _years(n: str) -> bool:
    return n.isdigit() and 1900 <= int(n) <= 2100


def check_paragraph(text: str, gc: GroundingContext) -> list[Flag]:
    flags: list[Flag] = []
    wc = word_count(text)
    if wc < gc.min_words:
        flags.append(Flag(level="warn", code="too_short", message=f"Only {wc} words: the model produced almost nothing."))
    if wc > gc.max_words * 1.4:
        flags.append(Flag(level="info", code="too_long", message=f"{wc} words (target ≤ {gc.max_words})."))
    if _PLACEHOLDER.search(text):
        flags.append(Flag(level="error", code="placeholder", message=f"Contains a placeholder: “{_PLACEHOLDER.search(text).group(0)}”."))

    lang = detect_language(text, min_tokens=14)
    if lang in ("en", "fr", "de") and lang != gc.lang:
        flags.append(Flag(level="warn", code="language", message=f"Looks {lang.upper()} but the letter language is {gc.lang.upper()}."))

    nt = " " + norm_match(text) + " "
    for phrase in gc.avoid:
        p = norm_match(phrase)
        if p and f" {p} " in nt:
            flags.append(Flag(level="warn", code="avoid_phrase", message=f"Uses a phrase you asked to avoid: “{phrase}”."))

    found = set(techvocab.find_terms(text))
    missing = sorted(found - gc.profile_vocab - gc.exempt_terms)
    job_only = [techvocab.display(t) for t in missing if t in gc.job_terms]
    unverified = [techvocab.display(t) for t in missing if t not in gc.job_terms]
    if job_only:
        flags.append(Flag(level="info", code="tech_job_only",
                          message=f"{', '.join(job_only)}: named in the posting or target, but not in your profile. Keep it only if you can honestly claim it."))
    if unverified:
        flags.append(Flag(level="warn", code="tech_unverified",
                          message=f"Mentions {', '.join(unverified)}: neither in your profile nor in the job posting."))

    allowed = gc.profile_numbers | gc.job_numbers | gc.extra_numbers
    bad = sorted((n for n in numbers_in(text) if n not in allowed and not _years(n)), key=lambda x: float(x))
    if bad:
        flags.append(Flag(level="warn", code="number_unverified",
                          message="Numbers not found in your profile or the sources: " + ", ".join(bad) + "."))

    known_q = gc.source_quantities
    if known_q:
        known_nums = {n for n, _ in known_q}
        for n, cat in sorted(quantities(text) - known_q):
            if n in known_nums:
                have = sorted({c for m, c in known_q if m == n})
                flags.append(Flag(level="warn", code="unit_mismatch",
                                  message=f"“{n} {cat}”: your sources have {n} but as {', '.join(have)}. Check the figure."))

    if gc.example_text:
        known_words = set(norm_match(gc.known_text).split())
        text_words = set(norm_match(text).split())
        leaked = []
        for m in _CAP_NAME.finditer(gc.example_text):
            w = norm_match(m.group(1))
            if w and w not in known_words and w in text_words and m.group(1) not in leaked:
                leaked.append(m.group(1))
        # content phrases shared with the style example that appear nowhere in the real sources: its *facts* were reused
        known_content = set(_content_tokens(gc.known_text))
        shared = sorted(b for b in (content_bigrams(text) & content_bigrams(gc.example_text)) - content_bigrams(gc.known_text)
                        if any(w not in known_content for w in b.split()))  # a shared phrase made only of words your sources use is fine
        if leaked or len(shared) >= 2:
            what = ", ".join(leaked + [f"“{b}”" for b in shared[:3]])
            flags.append(Flag(level="warn", code="example_leak", message="Copied from the style example (not from your sources): " + what + "."))

    if gc.use_note and gc.note.strip():
        note_lang = detect_language(gc.note, min_tokens=5)
        translated = note_lang in ("en", "fr", "de") and note_lang != gc.lang  # a translation shares few words: cannot check
        if not translated:
            note_words = set(_content_tokens(gc.note))
            reflected = (note_words & set(_content_tokens(text))) or (set(techvocab.find_terms(gc.note)) & found)
            if note_words and not reflected:
                flags.append(Flag(level="warn", code="note_ignored", message="Your personal note does not appear in this paragraph: “" + gc.note.strip()[:80] + "”"))

    dups = repeated_sentences(text, gc.earlier)
    if dups:
        flags.append(Flag(level="warn", code="repeats_previous", message="Repeats an earlier paragraph: “" + dups[0][:70] + "…”"))
    return flags


def retry_instructions(flags: list[Flag]) -> list[str]:
    """Turn blocking flags into concrete instructions for the rewrite."""
    out: list[str] = []
    for f in flags:
        if f.code not in RETRY_CODES:
            continue
        if f.code == "placeholder":
            out.append("Remove every placeholder or bracketed text; write the real content.")
        elif f.code == "language":
            out.append("Write the whole paragraph in the required language.")
        elif f.code == "avoid_phrase":
            out.append(f.message.replace("Uses a phrase you asked to avoid", "Do not use the phrase") + " Rephrase.")
        elif f.code == "too_long":
            out.append("Shorten it: respect the maximum word count.")
        elif f.code == "too_short":
            out.append("Write a real paragraph that follows the task.")
        elif f.code == "example_leak":
            out.append(f.message + " Do not reuse the style example's names, facts or phrases: write about THIS company and THIS candidate only.")
        elif f.code == "note_ignored":
            out.append("Build this paragraph on the candidate's note (rephrase it in your own words, in the required language): " + f.message.split("“", 1)[-1].rstrip("”"))
        elif f.code == "repeats_previous":
            out.append("Do not repeat what the earlier paragraphs already say; add something new.")
        elif f.code == "tech_unverified":
            out.append(f.message + " Remove them.")
        elif f.code == "unit_mismatch":
            out.append(f.message + " Use exactly the figures and units written in the candidate facts.")
        elif f.code == "number_unverified":
            out.append(f.message + " Remove them or use only numbers that appear in the candidate facts.")
    return out
