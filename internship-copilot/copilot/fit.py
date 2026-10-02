"""Rule-based fit check between a job posting and your availability (no LLM -> instant and predictable).

Understands French, English and German postings: duration, start date, internship vs apprenticeship,
language requirements, work-authorisation hints, expiry. Every finding is a *hint* shown as a chip in the UI.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional

from .models import FitItem, Job, Profile
from .textutil import localized, month_year

# --------------------------------------------------------------------------- numbers / months
_NUMWORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12,
    "un": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "sept": 7, "huit": 8, "neuf": 9, "dix": 10,
    "onze": 11, "douze": 12,
    "ein": 1, "eine": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5, "sechs": 6, "sieben": 7, "acht": 8,
    "neun": 9, "zehn": 10, "elf": 11, "zwölf": 12,
}
_NUM = r"(\d{1,2}|" + "|".join(sorted(_NUMWORDS, key=len, reverse=True)) + r")"
_UNIT = r"(?:months?|mois|monate[n]?)"
_RANGE = re.compile(rf"{_NUM}\s*(?:-|–|—|to|à|bis|et|and|/|or|ou|oder)\s*{_NUM}\s*[- ]?{_UNIT}", re.IGNORECASE)
_BETWEEN = re.compile(rf"(?:between|entre|zwischen)\s+{_NUM}\s+(?:and|et|und)\s+{_NUM}\s*{_UNIT}", re.IGNORECASE)
_MIN = re.compile(rf"(?:minimum(?: of| de)?|min\.?|at least|au moins|mindestens|a minimum of)\s*{_NUM}\s*[- ]?{_UNIT}", re.IGNORECASE)
_MAX = re.compile(rf"(?:up to|maximum(?: of| de)?|max\.?|jusqu['’]à|bis zu|maximal|no more than)\s*{_NUM}\s*[- ]?{_UNIT}", re.IGNORECASE)
_EXACT = re.compile(rf"(?<![\w/-]){_NUM}[\s-]*{_UNIT}", re.IGNORECASE)
_NOT_DURATION_AFTER = re.compile(r"^\s*(ago|d['’]exp|of exp|experience|erfahrung|d['’]anciennet|de pratique|minimum d['’]exp)", re.IGNORECASE)
_NOT_DURATION_BEFORE = re.compile(r"(il y a|vor|depuis|since|posted|publié|veröffentlicht)\s*$", re.IGNORECASE)


def _n(tok: str) -> int:
    tok = tok.lower()
    return int(tok) if tok.isdigit() else _NUMWORDS[tok]


def find_durations(text: str) -> list[tuple[str, int, int, str]]:
    """[(kind, lo, hi, snippet)] kind in range|exact|min|max."""
    out: list[tuple[str, int, int, str]] = []
    taken: list[tuple[int, int]] = []

    def free(span: tuple[int, int]) -> bool:
        return not any(span[0] < b and a < span[1] for a, b in taken)

    def ok_context(m: re.Match) -> bool:
        return not _NOT_DURATION_AFTER.match(text[m.end(): m.end() + 30]) and not _NOT_DURATION_BEFORE.search(text[max(0, m.start() - 14): m.start()])

    for kind, rx in (("range", _BETWEEN), ("range", _RANGE), ("min", _MIN), ("max", _MAX), ("exact", _EXACT)):
        for m in rx.finditer(text):
            if not free(m.span()) or not ok_context(m):
                continue
            try:
                if kind == "range":
                    lo, hi = sorted((_n(m.group(1)), _n(m.group(2))))
                else:
                    lo = hi = _n(m.group(1))
            except KeyError:
                continue
            if not 1 <= lo <= 24 or hi > 36:
                continue
            taken.append(m.span())
            out.append((kind, lo, hi, m.group(0).strip()))
    return out[:4]


_MONTHS = {
    "january": 1, "janvier": 1, "januar": 1, "jan": 1, "february": 2, "février": 2, "fevrier": 2, "februar": 2, "feb": 2,
    "march": 3, "mars": 3, "märz": 3, "maerz": 3, "april": 4, "avril": 4, "may": 5, "mai": 5, "june": 6, "juin": 6,
    "juni": 6, "july": 7, "juillet": 7, "juli": 7, "august": 8, "août": 8, "aout": 8, "september": 9, "septembre": 9,
    "sept": 9, "sep": 9, "october": 10, "octobre": 10, "oktober": 10, "oct": 10, "november": 11, "novembre": 11,
    "nov": 11, "december": 12, "décembre": 12, "decembre": 12, "dezember": 12, "dec": 12, "dez": 12,
}
_MONTH_RX = "|".join(sorted(_MONTHS, key=len, reverse=True))
_MONTH_YEAR = re.compile(rf"(?<![\w])({_MONTH_RX})\.?\s*(?:de |of |,)?\s*(20\d\d)(?![\w])", re.IGNORECASE)
_NUM_DATE = re.compile(r"(?<![\w/])(?:(\d{1,2})[/.])?(0?[1-9]|1[0-2])[/.](20\d\d)(?![\w/])")
_MONTH_ONLY_AFTER_KW = re.compile(
    rf"(?:start(?:ing)?|begin(?:ning)?|from|as of|à partir d[eu']|a partir d[eu']|dès|des|début|debut|ab|beginn|starttermin)\s*(?:en |in |im |on |le |de |d[’'])?\s*({_MONTH_RX})(?![\w])",
    re.IGNORECASE)
_START_KW = re.compile(
    r"start|begin|from|as of|commenc|début|debut|démarrage|a partir|à partir|dès|ab |beginn|eintritt|starttermin|prise de poste|disponib|available",
    re.IGNORECASE)
_NOT_START_KW = re.compile(
    r"posted|publié|publiée|veröffentlicht|deadline|avant le|before|until|jusqu|bis zum|candidature|apply by|bewerbungsfrist|closing|expires?",
    re.IGNORECASE)
_ASAP = re.compile(
    r"\basap\b|as soon as possible|dès que possible|des que possible|immédiat|immediate start|start immediately|"
    r"ab sofort|so bald wie möglich|so schnell wie möglich|dès maintenant|poste à pourvoir immédiatement",
    re.IGNORECASE)


def find_start_dates(text: str, ref: date) -> list[tuple[date, str, bool]]:
    """[(date, snippet, near_start_keyword)]"""
    found: list[tuple[date, str, bool]] = []
    for m in _MONTH_YEAR.finditer(text):
        month, year = _MONTHS[m.group(1).lower()], int(m.group(2))
        ctx = text[max(0, m.start() - 55): m.start()]
        found.append((date(year, month, 1), m.group(0), bool(_START_KW.search(ctx)) and not _NOT_START_KW.search(ctx[-35:])))
    for m in _NUM_DATE.finditer(text):
        month, year = int(m.group(2)), int(m.group(3))
        ctx = text[max(0, m.start() - 55): m.start()]
        found.append((date(year, month, int(m.group(1)) if m.group(1) and 1 <= int(m.group(1)) <= 28 else 1), m.group(0),
                      bool(_START_KW.search(ctx)) and not _NOT_START_KW.search(ctx[-35:])))
    for m in _MONTH_ONLY_AFTER_KW.finditer(text):
        month = _MONTHS[m.group(1).lower()]
        # no year given: take the year that lands closest to the candidate's own start date
        cands = [date(ref.year + d, month, 1) for d in (-1, 0, 1)]
        best = min(cands, key=lambda d: abs((d - ref).days))
        if not any(abs((best - f[0]).days) < 5 for f in found):
            found.append((best, m.group(0), True))
    return found


# --------------------------------------------------------------------------- languages
_LANG_NAMES = {
    "de": r"german|deutsch|allemand|deutschkenntnisse|deutsche",
    "fr": r"french|français|francais|französisch|franzoesisch|franz[öo]sisch|francophone",
    "en": r"english|anglais|englisch|englischkenntnisse",
}
_LEVEL_WORDS = {
    6: r"native|natif|maternelle|muttersprach\w*|bilingual|bilingue|c2|mother tongue",
    5: r"fluent(?:ly)?|business[- ]fluent|courant|fließend|fliessend|verhandlungssicher|c1|advanced|avancé|avance|excellent\w*|"
       r"proficien\w+|sehr gute\w*|très bon niveau|tres bon niveau|ausgezeichnete\w*",
    4: r"professional|professionnel\w*|opérationnel\w*|operationnel\w*|good|strong|bon niveau|bonne maîtrise|bonne maitrise|"
       r"gute\w*|solid\w*|b2|working proficiency|upper[- ]intermediate",
    3: r"intermediate|intermédiaire|intermediaire|b1|grundkenntnisse|basic knowledge",
}
_LEVEL_ANY = "|".join(f"(?:{v})" for v in _LEVEL_WORDS.values())


def level_rank(level: str) -> int:
    t = (level or "").lower()
    for rank, rx in sorted(_LEVEL_WORDS.items(), reverse=True):
        if re.search(rf"(?<![\w]){rx}(?![\w])", t):
            return rank
    if re.search(r"\ba2\b|beginner|débutant|debutant|notions|basic|grundlagen", t):
        return 2
    if re.search(r"\ba1\b", t):
        return 1
    return 3 if t else 0


def candidate_levels(profile: Profile) -> dict[str, int]:
    out: dict[str, int] = {}
    for l in profile.languages:
        name = localized(l.name, "en").lower() + " " + localized(l.name, "fr").lower()
        for code, rx in _LANG_NAMES.items():
            if re.search(rx, name):
                out[code] = max(out.get(code, 0), level_rank(localized(l.level, "en") + " " + localized(l.level, "fr")))
    return out


def candidate_level_names(profile: Profile) -> dict[str, str]:
    out: dict[str, str] = {}
    for l in profile.languages:
        name = (localized(l.name, "en") + " " + localized(l.name, "fr")).lower()
        for code, rx in _LANG_NAMES.items():
            if re.search(rx, name) and code not in out:
                out[code] = localized(l.level, "en")
    return out


def required_languages(text: str) -> dict[str, int]:
    """{'de': 5, ...} - the highest level word attached to each language name in the posting."""
    req: dict[str, int] = {}
    for code, names in _LANG_NAMES.items():
        for m in re.finditer(rf"(?:{names})[^.\n;]{{0,45}}?({_LEVEL_ANY})(?![\w])", text, re.IGNORECASE):
            req[code] = max(req.get(code, 0), level_rank(m.group(1)))
        for m in re.finditer(rf"(?<![\w])({_LEVEL_ANY})[^.\n;]{{0,28}}?(?:{names})", text, re.IGNORECASE):
            req[code] = max(req.get(code, 0), level_rank(m.group(1)))
    return req


_LANG_LABEL = {"de": "German", "fr": "French", "en": "English"}

# --------------------------------------------------------------------------- misc patterns
_INTERN = re.compile(
    r"(?<![\w])(stage|stagiaire|internship|intern|praktikum|praktikant(?:in)?|pfe|pfa|abschlussarbeit|masterarbeit|"
    r"bachelorarbeit|thesis|stage de fin d['’]études|end[- ]of[- ]studies)(?![\w])", re.IGNORECASE)
_NOT_INTERN = re.compile(
    r"(?<![\w])(alternance|alternant(?:e)?|apprentissage|apprenti(?:e)?|apprenticeship|apprentice|ausbildung|"
    r"duales studium|dual study|werkstudent(?:in)?|working student|contrat de professionnalisation)(?![\w])", re.IGNORECASE)
_SENIOR_TITLE = re.compile(
    r"(?<![\w])(senior|sr\.?|lead|principal|staff|manager|director|head of|vp|chief|architect|expert|confirmé|confirmée|"
    r"expérimenté|expérimentée|responsable|leiter|leiterin|teamleiter)(?![\w])", re.IGNORECASE)
_PERMANENT = re.compile(r"(?<![\w])(cdi|permanent contract|unbefristet|full[- ]time permanent)(?![\w])", re.IGNORECASE)
_WORKAUTH = re.compile(
    r"right to work|work permit|authori[sz]ed to work|eligible to work|work authori[sz]ation|autorisation de travail|"
    r"permis de travail|titre de séjour|eu citizen|citoyen(?:ne)? (?:de l['’])?(?:ue|européen)|resident(?:e)? (?:de l['’])?(?:ue|eu)|"
    r"arbeitserlaubnis|arbeitsgenehmigung|aufenthaltstitel|eu-bürger|eu citizenship|visa sponsorship|no sponsorship",
    re.IGNORECASE)
_FR_PLACES = (
    "france paris lyon marseille toulouse nantes lille bordeaux nice grenoble rennes strasbourg montpellier "
    "sophia antipolis villeneuve-d'ascq aix-en-provence clermont-ferrand dijon rouen brest metz nancy "
    "boulogne-billancourt issy-les-moulineaux levallois courbevoie la défense puteaux saint-denis"
).split()
_DE_PLACES = (
    "germany deutschland allemagne berlin münchen munich hamburg frankfurt köln cologne stuttgart düsseldorf "
    "dresden leipzig karlsruhe nürnberg nuremberg hannover aachen heidelberg darmstadt bremen dortmund essen "
    "potsdam mannheim freiburg bonn münster ulm walldorf"
).split()
_REMOTE = re.compile(r"remote|télétravail|teletravail|home ?office|full remote|100% remote|hybrid|hybride", re.IGNORECASE)


def _has_place(text: str, places: list[str]) -> Optional[str]:
    low = text.lower()
    for p in places:
        if re.search(rf"(?<![\w]){re.escape(p)}(?![\w])", low):
            return p.title()
    return None


# --------------------------------------------------------------------------- main
def check_fit(job: Job, profile: Profile, today: Optional[date] = None) -> list[FitItem]:
    today = today or date.today()
    av = profile.availability
    text = f"{job.title}\n{job.description}"
    items: list[FitItem] = []

    # --- contract type ------------------------------------------------------------------
    title_intern = bool(_INTERN.search(job.title))
    intern_hits = _INTERN.findall(text)
    # one stray mention deep in a long text ("we hire interns every summer") is not evidence: require the title,
    # the structured employment type, two mentions, or a mention in the opening lines
    has_intern = title_intern or "INTERN" in (job.employment_type or "").upper() or len(intern_hits) >= 2 or bool(_INTERN.search(text[:400]))
    not_intern = _NOT_INTERN.search(text)
    senior = _SENIOR_TITLE.search(job.title)
    if title_intern or (has_intern and not not_intern and not senior):
        items.append(FitItem(key="type", status="ok", label="Internship", detail="The posting is an internship (stage / Praktikum)."))
    elif senior and not title_intern:
        items.append(FitItem(key="type", status="bad", label="Looks like a senior role",
                             detail=f"The title contains “{senior.group(0)}” and does not say internship. Check before applying."))
    elif not_intern and not has_intern:
        items.append(FitItem(key="type", status="bad", label="Not an internship?",
                             detail=f"Mentions “{not_intern.group(0)}” (apprenticeship / work-study) and no internship wording."))
    elif not_intern and has_intern:
        items.append(FitItem(key="type", status="warn", label="Mixed contract wording",
                             detail=f"Mentions both internship and “{not_intern.group(0)}”. Check the contract type."))
    elif _PERMANENT.search(text):
        items.append(FitItem(key="type", status="warn", label="Looks permanent",
                             detail="Mentions a permanent contract (CDI / unbefristet) and no internship wording."))
    elif intern_hits:
        items.append(FitItem(key="type", status="warn", label="Internship mentioned only in passing",
                             detail="The word internship appears once, not in the title. Check that this is an internship posting."))
    else:
        items.append(FitItem(key="type", status="unknown", label="Contract type unclear", detail="No internship / apprenticeship wording found."))

    # --- duration -------------------------------------------------------------------------
    want = f"{av.min_months}–{av.max_months} months"
    durs = find_durations(text)
    if not durs:
        items.append(FitItem(key="duration", status="unknown", label="Duration not stated", detail=f"You want {want}."))
    else:
        verdicts = []
        for kind, lo, hi, snip in durs:
            if kind == "range":
                good = lo <= av.max_months and hi >= av.min_months
            elif kind == "exact":
                good = av.min_months <= lo <= av.max_months
            elif kind == "min":
                good = lo <= av.max_months
            else:  # max
                good = lo >= av.min_months
            verdicts.append((good, snip))
        shown = "; ".join(sorted({s for _, s in verdicts}))[:90]
        if any(g for g, _ in verdicts):
            items.append(FitItem(key="duration", status="ok", label=f"Duration OK ({shown})", detail=f"Compatible with your {want}."))
        else:
            items.append(FitItem(key="duration", status="warn", label=f"Duration: {shown}", detail=f"You are looking for {want}."))

    # --- start date -----------------------------------------------------------------------
    starts = [(d, s) for d, s, near in find_start_dates(text, av.start_date) if near]
    mine = month_year(av.start_date, "en")
    if starts:
        d, snip = min(starts, key=lambda x: abs((x[0] - av.start_date).days))
        diff = (d - av.start_date).days
        if abs(diff) <= av.start_flex_days:
            items.append(FitItem(key="start", status="ok", label=f"Start {month_year(d, 'en')}", detail=f"Close to your availability ({mine})."))
        elif diff < 0:
            items.append(FitItem(key="start", status="warn", label=f"Start {month_year(d, 'en')} (earlier)", detail=f"Posting starts ~{abs(diff) // 30} month(s) before you are available ({mine})."))
        else:
            items.append(FitItem(key="start", status="warn", label=f"Start {month_year(d, 'en')} (later)", detail=f"Posting starts ~{diff // 30} month(s) after your availability ({mine}). Could be flexible - ask."))
    elif _ASAP.search(text):
        items.append(FitItem(key="start", status="warn", label="Immediate start wanted", detail=f"The posting asks for an immediate start; you are available from {mine}. Mention it clearly."))
    else:
        items.append(FitItem(key="start", status="unknown", label="Start date not stated", detail=f"You are available from {mine}."))

    # --- language -------------------------------------------------------------------------
    mine_lv = candidate_levels(profile)
    req = required_languages(text)
    problems: list[str] = []
    worst = "ok"
    for code, need in req.items():
        have = mine_lv.get(code, 0)
        if have == 0 and need >= 4:
            problems.append(f"{_LANG_LABEL[code]} required, not in your profile")
            worst = "bad"
        elif have and have < need:
            problems.append(f"{_LANG_LABEL[code]} level asked is higher than yours")
            worst = "warn" if worst != "bad" else worst
    if job.language == "de" and mine_lv.get("de", 0) < 4:
        problems.append("posting is written in German")
        worst = "warn" if worst != "bad" else worst
    if problems:
        items.append(FitItem(key="language", status=worst, label="Language: " + "; ".join(problems)[:80], detail="; ".join(problems)))
    else:
        names = candidate_level_names(profile)
        asked = ", ".join(f"{_LANG_LABEL[c]} (you: {names.get(c, '?')})" for c in req)
        items.append(FitItem(key="language", status="ok" if req else "unknown",
                             label=f"Language requirement met: {asked}" if req else "No language requirement stated",
                             detail=f"The posting is written in {job.language.upper()}." if job.language in ("fr", "en", "de") else ""))

    # --- location -------------------------------------------------------------------------
    loc_text = f"{job.location} {job.description[:800]}"
    fr, de = _has_place(loc_text, _FR_PLACES), _has_place(loc_text, _DE_PLACES)
    remote = bool(_REMOTE.search(loc_text))
    targets = {c.lower() for c in av.target_countries}
    shown_loc = job.location or fr or de or ""
    where = fr and "France" or de and "Germany" or ""
    if where and (where.lower() in targets or {"france": "france", "germany": "germany"}.get(where.lower(), "") in targets):
        items.append(FitItem(key="location", status="ok", label=f"{shown_loc or where}" + (" · remote option" if remote else ""),
                             detail=f"In one of your target countries ({where})."))
    elif shown_loc:
        items.append(FitItem(key="location", status="info", label=shown_loc + (" · remote option" if remote else ""),
                             detail="Check this is where you want to work (target: " + ", ".join(av.target_countries) + ")."))
    else:
        items.append(FitItem(key="location", status="unknown", label="Location not detected", detail=""))

    # --- work authorisation hint ----------------------------------------------------------
    m = _WORKAUTH.search(text)
    if m:
        items.append(FitItem(key="work_auth", status="info", label="Work-authorisation wording",
                             detail=f"Mentions “{m.group(0)}”. Make sure your situation (school agreement / visa / residence) fits, and say so in your note if relevant."))

    # --- freshness ------------------------------------------------------------------------
    def parse_d(s: str) -> Optional[date]:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except ValueError:
            return None

    vt = parse_d(job.valid_through)
    if vt and vt < today:
        items.append(FitItem(key="freshness", status="bad", label=f"Expired {vt.isoformat()}", detail="The posting's validity date has passed."))
    elif vt and vt <= today + timedelta(days=7):
        items.append(FitItem(key="freshness", status="warn", label=f"Closes {vt.isoformat()}", detail="Closing within a week."))
    else:
        dp = parse_d(job.date_posted)
        if dp and (today - dp).days > 75:
            items.append(FitItem(key="freshness", status="warn", label=f"Posted {(today - dp).days} days ago", detail="Old postings are often already filled."))
    return items
