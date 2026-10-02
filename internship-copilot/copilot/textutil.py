"""Small, dependency-light text helpers (normalisation, language detection, dates, HTML)."""
from __future__ import annotations

import html as _html
import re
import unicodedata
from datetime import date
from typing import Any, Iterable, Mapping

# --------------------------------------------------------------------------- basics
_WS = re.compile(r"[ \t\u00a0\u202f\u2009]+")


def norm_ws(s: str) -> str:
    """Collapse runs of spaces/tabs and trim every line; keep single newlines."""
    lines = [_WS.sub(" ", ln).strip() for ln in (s or "").replace("\r", "").split("\n")]
    out: list[str] = []
    blank = 0
    for ln in lines:
        if ln:
            blank = 0
            out.append(ln)
        else:
            blank += 1
            if blank == 1 and out:
                out.append("")
    return "\n".join(out).strip()


def one_line(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm_match(s: str) -> str:
    """Aggressive normalisation used to verify that a quote really occurs in a source text."""
    s = strip_accents(s or "").lower()
    s = s.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    s = re.sub(r"[^a-z0-9%+#]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def word_count(s: str) -> int:
    return len(re.findall(r"\b[\w'’\-]+\b", s or "", flags=re.UNICODE))


_SENT_SPLIT = re.compile(r"(?<=[.!?…])[\"”»)]?\s+(?=[\"“«(]?[A-ZÀ-ÖØ-Þ0-9])")


def split_sentences(text: str) -> list[str]:
    text = one_line(text)
    return [p.strip() for p in _SENT_SPLIT.split(text) if p.strip()]


def truncate_words(text: str, max_words: int) -> str:
    """Cut to at most ``max_words`` at a sentence boundary when possible."""
    text = one_line(text)
    if word_count(text) <= max_words:
        return text
    kept: list[str] = []
    total = 0
    for sent in split_sentences(text):
        n = word_count(sent)
        if total + n > max_words and kept:
            break
        kept.append(sent)
        total += n
        if total >= max_words:
            break
    out = " ".join(kept)
    if word_count(out) > max_words:  # a single huge sentence
        out = " ".join(out.split()[:max_words]).rstrip(",;:") + "…"
    return out


def slugify(s: str, maxlen: int = 40) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return (s[:maxlen].strip("-")) or "x"


def localized(value: Any, lang: str, fallback: str = "en") -> str:
    """Resolve ``str | {lang: str}`` to a plain string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in (lang, fallback, "en", "fr"):
            if key in value and value[key]:
                return str(value[key])
        for v in value.values():
            if v:
                return str(v)
    return str(value)


# --------------------------------------------------------------------------- templating
_PH = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def safe_format(template: str, mapping: Mapping[str, Any]) -> str:
    """``str.format`` that leaves unknown {placeholders} untouched instead of raising."""
    return _PH.sub(lambda m: str(mapping[m.group(1)]) if m.group(1) in mapping else m.group(0), template or "")


def unknown_placeholders(template: str, mapping: Mapping[str, Any]) -> list[str]:
    return sorted({n for n in _PH.findall(template or "") if n not in mapping})


# --------------------------------------------------------------------------- language detection
_STOP = {
    "en": set(
        "the and of to is are for with that this we our you your will be from at by or it not can my i about "
        "who which their they been would have has was were these those into more also as such than other "
        "us what when where how all any each if but so do does".split()
    ),
    "fr": set(
        "le la les des du de un une et est sont pour avec dans que qui nous vous votre notre nos vos au aux "
        "sur par ce cette ces en je mon ma mes ses leur leurs pas plus ou où être avoir sera serait très aussi "
        "comme mais donc afin chez entre vers sans sous son sa été ont fait faire elle il ils elles te tu".split()
    ),
    "de": set(
        "der die das und ist sind für mit von zu den dem ein eine einer einen nicht auf wir sie ihr ihre bei "
        "als auch oder werden wird im zum zur über nach aus um durch sich uns unser unsere haben hat sowie "
        "bitte dich dir wenn wie noch nur sehr aber ihren einem eines dass können kann".split()
    ),
}
_TOK = re.compile(r"[a-zàâäçéèêëîïôöûùüÿœß']+", re.IGNORECASE)


def language_scores(text: str) -> dict[str, float]:
    toks = [t.lower() for t in _TOK.findall(text or "")]
    if not toks:
        return {k: 0.0 for k in _STOP}
    return {k: sum(1 for t in toks if t in sw) / len(toks) for k, sw in _STOP.items()}


def detect_language(text: str, min_tokens: int = 12) -> str:
    """Return 'fr', 'en', 'de' or 'unknown' (stop-word heuristic: enough for FR/EN/DE)."""
    toks = _TOK.findall(text or "")
    if len(toks) < min_tokens:
        return "unknown"
    sc = language_scores(text)
    best, second = sorted(sc.items(), key=lambda kv: kv[1], reverse=True)[:2]
    if best[1] < 0.07 or best[1] - second[1] < 0.015:
        return "unknown"
    return best[0]


LANG_NAMES = {"en": "English", "fr": "French (français)", "de": "German (Deutsch)"}


# --------------------------------------------------------------------------- dates
MONTHS = {
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September",
           "October", "November", "December"],
    "fr": ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre",
           "octobre", "novembre", "décembre"],
}


def month_year(d: date, lang: str) -> str:
    return f"{MONTHS.get(lang, MONTHS['en'])[d.month - 1]} {d.year}"


def date_long(d: date, lang: str) -> str:
    if lang == "fr":
        day = "1er" if d.day == 1 else str(d.day)
        return f"{day} {MONTHS['fr'][d.month - 1]} {d.year}"
    return f"{d.day} {MONTHS['en'][d.month - 1]} {d.year}"


def duration_text(min_m: int, max_m: int, lang: str) -> str:
    if min_m == max_m:
        return f"{min_m} mois" if lang == "fr" else f"{min_m} months"
    return f"{min_m} à {max_m} mois" if lang == "fr" else f"{min_m} to {max_m} months"


# --------------------------------------------------------------------------- names
def split_name(full: str) -> tuple[str, str]:
    parts = [p for p in re.split(r"\s+", (full or "").strip()) if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


# --------------------------------------------------------------------------- HTML -> text
def html_to_text(html: str) -> str:
    """Readable text from an HTML fragment/page (keeps list bullets and paragraph breaks)."""
    from bs4 import BeautifulSoup  # local import: keeps module import cheap

    if not html:
        return ""
    if "&lt;" in html and "<" not in html:  # double-escaped markup (common in JSON-LD)
        html = _html.unescape(html)
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "template", "svg", "iframe"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        li.insert_before("\n- ")
        li.append("\n")
    for tag in soup.find_all(["p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol",
                              "tr", "table", "header", "footer", "blockquote"]):
        tag.insert_before("\n")
        tag.append("\n")
    text = _html.unescape(soup.get_text())
    return norm_ws(text)


# --------------------------------------------------------------------------- job-text cleaning
_START_MARKERS = [
    r"about the job", r"à propos de l[’']offre", r"a propos de l[’']offre", r"description du poste",
    r"job description", r"stellenbeschreibung", r"your mission", r"vos missions", r"mission[s]? du stage",
    r"the role", r"le poste", r"ihre aufgaben", r"what you['’]ll do", r"missions?\s*:",
]
_END_MARKERS = [
    r"set alert for similar jobs", r"similar jobs", r"offres similaires", r"ähnliche (jobs|stellen)",
    r"show more jobs", r"people also viewed", r"report this job", r"signaler cette offre",
    r"more jobs like this", r"jobs you may be interested in", r"offres d['’]emploi similaires",
    r"voir plus d['’]offres", r"related jobs", r"other jobs at", r"autres offres",
]
_NOISE_LINES = re.compile(
    r"^(skip to (main )?content|accept (all )?cookies?|accepter( les cookies)?|tout accepter|reject all|"
    r"sign in|se connecter|join now|s['’]inscrire|log in|connexion|menu|share|partager|save|enregistrer|"
    r"apply( now)?|postuler|easy apply|candidature simplifiée|show (more|less)|voir (plus|moins)|"
    r"anmelden|bewerben|teilen|cookie.*)$",
    re.IGNORECASE,
)


def clean_job_text(text: str, max_chars: int = 9000) -> str:
    text = norm_ws("\n".join(ln for ln in norm_ws(text).split("\n") if not _NOISE_LINES.match(ln.strip())))
    low = text.lower()
    # cut at "similar jobs"-style markers (keep what precedes)
    cut = len(text)
    for pat in _END_MARKERS:
        m = re.search(pat, low)
        if m and m.start() > 300:
            cut = min(cut, m.start())
    text = text[:cut]
    low = text.lower()
    # start at the description heading if it appears early enough
    best = None
    for pat in _START_MARKERS:
        m = re.search(pat, low)
        if m and m.start() < max(600, int(len(text) * 0.5)):
            best = m.start() if best is None else min(best, m.start())
    header = ""
    if best is not None and best > 0:
        header = " | ".join(one_line(x) for x in text[:best].split("\n") if one_line(x))[:300]
        text = text[best:]
    lines = [ln for ln in text.split("\n") if not _NOISE_LINES.match(ln.strip())]
    text = norm_ws("\n".join(lines))
    if header:
        text = f"{header}\n\n{text}"
    if len(text) > max_chars:
        text = text[:max_chars].rsplit("\n", 1)[0].rstrip() + "\n[…]"
    return text


# --------------------------------------------------------------------------- LLM output hygiene
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_SIGNOFF = re.compile(
    r"^(cordialement|bien cordialement|sincèrement|respectueusement|dans l['’]attente|best regards|kind regards|"
    r"sincerely|yours sincerely|regards|thank you for your (time|consideration)|je vous prie|veuillez agréer)",
    re.IGNORECASE,
)
_GREETING = re.compile(r"^(madame|monsieur|bonjour|dear|hello|hi |sehr geehrte|guten tag)\b", re.IGNORECASE)


def clean_paragraph(text: str) -> str:
    """Normalise a model-written paragraph into plain single-paragraph prose."""
    t = _THINK.sub("", text or "")
    t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", t.strip())
    lines = [ln.strip() for ln in t.split("\n")]
    lines = [ln for ln in lines if ln]
    # drop greetings / sign-offs / labels the model sometimes adds
    cleaned: list[str] = []
    for i, ln in enumerate(lines):
        if _GREETING.match(ln) and len(ln) < 60:
            continue
        if _SIGNOFF.match(ln) and len(ln) < 60:
            if sum(len(x.split()) for x in lines[i + 1:]) <= 12:  # what follows is just a name / signature block
                break
            continue
        ln = re.sub(r"^(paragraph|paragraphe|absatz)\s*[:\-–]\s*", "", ln, flags=re.IGNORECASE)
        ln = re.sub(r"^[-*•]\s+", "", ln)
        cleaned.append(ln)
    t = " ".join(cleaned)
    t = re.sub(r"\*\*|__|`", "", t)
    t = re.sub(r"^\s*#+\s*", "", t)
    t = t.strip()
    if len(t) > 2 and ((t[0] == '"' and t[-1] == '"') or (t[0] == "«" and t[-1] == "»") or (t[0] == "“" and t[-1] == "”")):
        t = t[1:-1].strip()
    t = re.sub(r"\s*\((?:\d+\s*(?:words|mots))\)\s*$", "", t, flags=re.IGNORECASE)
    return one_line(t)


def dedupe(seq: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in seq:
        k = x.strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(x.strip())
    return out
