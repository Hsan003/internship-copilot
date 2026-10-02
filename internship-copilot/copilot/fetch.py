"""Job link -> structured Job.

Strategy (cheapest / most reliable first):
  1. schema.org ``JobPosting`` JSON-LD embedded in the page (Google-for-Jobs data: most boards and ATS have it)
  2. main-content extraction (trafilatura) + <title>/OpenGraph hints
  3. optional headless browser (Playwright) for JavaScript-rendered pages
  4. when a site blocks automated requests (LinkedIn / Indeed / Glassdoor / WTTJ often do): a clear message so
     the user can paste the text or use the browser bookmarklet instead - we never try to evade bot protection.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup

from . import config, netguard
from .models import Job
from .textutil import clean_job_text, detect_language, html_to_text, norm_ws, one_line


class FetchError(Exception):
    def __init__(self, message: str, *, blocked: bool = False, platform: str = "", status: int = 0):
        super().__init__(message)
        self.blocked = blocked
        self.platform = platform
        self.status = status


PLATFORMS = [
    ("linkedin.com", "LinkedIn"), ("indeed.", "Indeed"), ("glassdoor.", "Glassdoor"),
    ("welcometothejungle.com", "Welcome to the Jungle"), ("jobteaser.com", "JobTeaser"),
    ("hellowork.com", "HelloWork"), ("apec.fr", "APEC"), ("francetravail.fr", "France Travail"),
    ("pole-emploi.fr", "France Travail"), ("stepstone.", "StepStone"), ("xing.com", "XING"),
    ("join.com", "Join"), ("personio.", "Personio"), ("greenhouse.io", "Greenhouse"), ("lever.co", "Lever"),
    ("workable.com", "Workable"), ("smartrecruiters.com", "SmartRecruiters"), ("ashbyhq.com", "Ashby"),
    ("myworkdayjobs.com", "Workday"), ("recruitee.com", "Recruitee"), ("teamtailor.com", "Teamtailor"),
    ("bamboohr.com", "BambooHR"), ("successfactors", "SuccessFactors"), ("breezy.hr", "Breezy"),
    ("jobvite.com", "Jobvite"), ("icims.com", "iCIMS"), ("monster.", "Monster"), ("meinestadt.de", "meinestadt.de"),
    ("honeypot.io", "Honeypot"), ("choisirleservicepublic.gouv.fr", "Choisir le service public"),
]

BLOCK_PATTERNS = re.compile(
    r"just a moment\.\.\.|cf-browser-verification|attention required|access denied|verify you are (a )?human|"
    r"captcha|unusual traffic|enable javascript and cookies|are you a robot|px-captcha|"
    r"checking your browser|request blocked|incapsula|datadome",
    re.IGNORECASE,
)


def platform_of(url: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    for needle, name in PLATFORMS:
        if needle in host:
            return name
    return ""


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise FetchError("Please enter a job link.")
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    p = urlparse(url)
    q = parse_qs(p.query)
    host = p.netloc.lower()
    if "linkedin.com" in host and "currentJobId" in q:
        return f"https://www.linkedin.com/jobs/view/{q['currentJobId'][0]}/"
    if "indeed." in host and (q.get("jk") or q.get("vjk")) and "/viewjob" not in p.path:
        return f"{p.scheme}://{p.netloc}/viewjob?jk={(q.get('jk') or q.get('vjk'))[0]}"
    return url


# --------------------------------------------------------------------------- JSON-LD
def _walk(obj: Any):
    if isinstance(obj, list):
        for x in obj:
            yield from _walk(x)
    elif isinstance(obj, dict):
        yield obj
        for k in ("@graph", "mainEntity", "itemListElement", "item"):
            if k in obj:
                yield from _walk(obj[k])


def extract_jobposting_jsonld(html: str) -> Optional[dict]:
    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup.find_all("script", type=lambda t: bool(t) and "ld+json" in t.lower()):
        raw = tag.string or tag.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        data = None
        for candidate in (raw, re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", raw)):
            try:
                data = json.loads(candidate)
                break
            except json.JSONDecodeError:
                continue
        if data is None:
            continue
        for obj in _walk(data):
            t = obj.get("@type")
            types = t if isinstance(t, list) else [t]
            if any(isinstance(x, str) and x.lower() == "jobposting" for x in types):
                return obj
    return None


def _first(x: Any) -> Any:
    return x[0] if isinstance(x, list) and x else x


def _s(x: Any) -> str:
    if isinstance(x, dict):
        return str(x.get("name") or x.get("@value") or "")
    return str(x or "")


def job_from_jsonld(ld: dict, url: str = "") -> Job:
    org = _first(ld.get("hiringOrganization")) or {}
    company = _s(org) if isinstance(org, dict) else str(org)
    company_url = ""
    if isinstance(org, dict):
        company_url = str(org.get("sameAs") or org.get("url") or "")
    loc_parts: list[str] = []
    for loc in ld.get("jobLocation") if isinstance(ld.get("jobLocation"), list) else [ld.get("jobLocation")]:
        if not isinstance(loc, dict):
            continue
        addr = loc.get("address") or {}
        if isinstance(addr, str):
            loc_parts.append(addr)
            continue
        bits = [_s(addr.get("addressLocality")), _s(addr.get("addressRegion")), _s(addr.get("addressCountry"))]
        txt = ", ".join(b for b in bits if b)
        if txt:
            loc_parts.append(txt)
    location = " / ".join(dict.fromkeys(loc_parts))
    if str(ld.get("jobLocationType", "")).upper() == "TELECOMMUTE":
        location = (location + " (remote)").strip()
    desc_html = str(ld.get("description") or "")
    extras = []
    for key, label in (("responsibilities", "Responsibilities"), ("qualifications", "Qualifications"),
                       ("skills", "Skills"), ("experienceRequirements", "Experience"), ("educationRequirements", "Education")):
        v = ld.get(key)
        if v:
            extras.append(f"{label}: " + (", ".join(map(_s, v)) if isinstance(v, list) else html_to_text(_s(v))))
    description = html_to_text(desc_html)
    if extras:
        description += "\n\n" + "\n".join(extras)
    et = ld.get("employmentType")
    et = ", ".join(map(str, et)) if isinstance(et, list) else str(et or "")
    return Job(
        url=url, source="jsonld", platform=platform_of(url),
        title=one_line(_s(ld.get("title") or ld.get("name"))), company=one_line(company), location=location,
        description=description, employment_type=et,
        date_posted=str(ld.get("datePosted") or "")[:10], valid_through=str(ld.get("validThrough") or "")[:10],
        company_url=company_url,
    )


# --------------------------------------------------------------------------- title heuristics
_SITE_WORDS = re.compile(
    r"\b(linkedin|indeed(\.com)?|glassdoor|welcome to the jungle|jobteaser|hellowork|apec|stepstone|xing|"
    r"france travail|join\.com|personio|greenhouse|lever|workable|smartrecruiters)\b", re.IGNORECASE)


_COMPANY_SUFFIX = re.compile(r"\s+(careers?|jobs?|recrutement|recruiting|karriere|emplois?|talent|hiring)$", re.IGNORECASE)


def guess_title_company(page_title: str, url: str = "", site_name: str = "") -> tuple[str, str]:
    """Best-effort (role, company) from a page <title>. The UI always lets the user correct it."""
    role, company = _guess_title_company(page_title, url, site_name)
    return role, _COMPANY_SUFFIX.sub("", company).strip()


def _guess_title_company(page_title: str, url: str = "", site_name: str = "") -> tuple[str, str]:
    t = one_line(page_title)
    if not t:
        return "", company_from_url(url)
    m = re.match(r"^(?P<company>.+?)\s+hiring\s+(?P<role>.+?)(?:\s+in\s+.+?)?(?:\s*[|·-]\s*(?:LinkedIn|Glassdoor).*)?$", t, re.IGNORECASE)
    if m:
        return m.group("role").strip(), m.group("company").strip()
    t = _SITE_WORDS.sub("", t)
    parts = [p.strip(" -|–—·") for p in re.split(r"\s+[-|–—·]\s+", t) if p.strip(" -|–—·")]
    parts = [p for p in parts if not re.fullmatch(r"(h/f|f/h|m/w/d|\(?[mfwd/]+\)?)", p, re.IGNORECASE)]
    if len(parts) >= 2:
        return parts[0], parts[1]
    if parts:
        return parts[0], company_from_url(url)
    return "", company_from_url(url)


def company_from_url(url: str) -> str:
    p = urlparse(url or "")
    host, path = (p.netloc or "").lower(), [x for x in (p.path or "").split("/") if x]
    slug = ""
    if host.startswith(("jobs.lever.co", "boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.ashbyhq.com",
                        "apply.workable.com", "careers.smartrecruiters.com", "jobs.eu.lever.co", "boards.eu.greenhouse.io")) and path:
        slug = path[0]
    else:
        for suffix in (".recruitee.com", ".teamtailor.com", ".jobs.personio.de", ".jobs.personio.com", ".bamboohr.com", ".breezy.hr", ".join.com"):
            if host.endswith(suffix):
                slug = host[: -len(suffix)]
                break
    if "welcometothejungle.com" in host and "companies" in path:
        i = path.index("companies")
        slug = path[i + 1] if len(path) > i + 1 else ""
    return slug.replace("-", " ").replace("_", " ").title() if slug else ""


# --------------------------------------------------------------------------- HTML fallback
def _meta(soup: BeautifulSoup, *names: str) -> str:
    for n in names:
        tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
        if tag and tag.get("content"):
            return one_line(tag["content"])
    return ""


def job_from_html(html: str, url: str = "") -> Job:
    soup = BeautifulSoup(html or "", "lxml")
    page_title = _meta(soup, "og:title", "twitter:title")
    if not page_title and soup.title:
        page_title = one_line(soup.title.get_text())
    site = _meta(soup, "og:site_name")
    text = ""
    try:
        import trafilatura

        text = trafilatura.extract(html, include_comments=False, include_tables=False, favor_recall=True, output_format="txt") or ""
    except Exception:  # trafilatura missing or failed -> plain fallback below
        text = ""
    if len(text) < 300:
        text = html_to_text(html)
    title, company = guess_title_company(page_title, url, site)
    desc = _meta(soup, "og:description", "description")
    if len(text) < 300 and desc:
        text = desc + "\n\n" + text
    return Job(url=url, source="html", platform=platform_of(url), title=title, company=company or _COMPANY_SUFFIX.sub("", site).strip(),
               description=text)


# --------------------------------------------------------------------------- network
def _looks_blocked(status: int, html: str, has_jobposting: bool) -> bool:
    if has_jobposting:
        return False
    if status in (401, 403, 407, 429, 451, 999):
        return True
    head = (html or "")[:6000]
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", head, re.IGNORECASE | re.DOTALL)
    if m:
        title = one_line(m.group(1)).lower()
    if BLOCK_PATTERNS.search(head[:4000]) and len(html or "") < 60000:
        return True
    if re.search(r"^(sign up|sign in|log in|login|se connecter|anmelden)\b", title) and "linkedin" in title:
        return True
    return False


def _http_get(url: str, cfg: config.FetchSettings) -> tuple[int, str, str]:
    headers = {
        "User-Agent": cfg.user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8,de;q=0.6",
    }
    def guard(request: httpx.Request) -> None:  # runs for the first request and for every redirect hop
        try:
            netguard.check_url(str(request.url))
        except netguard.BlockedAddress as exc:
            raise FetchError(str(exc)) from exc

    try:
        with httpx.Client(follow_redirects=True, timeout=cfg.timeout_s, headers=headers,
                          event_hooks={"request": [guard]}) as c:
            r = c.get(url)
        return r.status_code, r.text, str(r.url)
    except httpx.TimeoutException as exc:
        raise FetchError("The site took too long to respond.") from exc
    except httpx.HTTPError as exc:
        raise FetchError(f"Could not reach the page ({type(exc).__name__}). Check the link and your connection.") from exc


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401

        return True
    except Exception:
        return False


def _browser_get(url: str, cfg: config.FetchSettings) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise FetchError("Playwright is not installed (pip install playwright && playwright install chromium).") from exc
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=cfg.user_agent, locale="fr-FR")
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=cfg.timeout_s * 1000)
            page.wait_for_timeout(2500)
            html = page.content()
            browser.close()
            return html
    except Exception as exc:
        raise FetchError(f"The headless browser could not load the page: {exc}") from exc


def blocked_message(platform: str, status: int) -> str:
    who = platform or "This site"
    code = f" (HTTP {status})" if status else ""
    return (f"{who} refuses automated requests{code}. Nothing is wrong with your link - use the browser bookmarklet "
            f"(opens this app with the page text) or paste the job description below.")


def fetch_job(url: str, cfg: Optional[config.FetchSettings] = None) -> Job:
    cfg = cfg or config.load_settings().fetch
    url = normalize_url(url)
    platform = platform_of(url)
    status, html, final_url = _http_get(url, cfg)
    ld = extract_jobposting_jsonld(html)
    blocked = _looks_blocked(status, html, ld is not None)

    need_browser = (blocked or (ld is None and len(html_to_text(html)) < 400)) and cfg.use_browser != "never"
    if cfg.use_browser == "always":
        need_browser = True
    if need_browser and playwright_available():
        try:
            html2 = _browser_get(url, cfg)
            ld2 = extract_jobposting_jsonld(html2)
            if ld2 is not None or not _looks_blocked(200, html2, False):
                html, ld, blocked = html2, ld2, False
        except FetchError:
            pass

    if blocked:
        raise FetchError(blocked_message(platform, status), blocked=True, platform=platform, status=status)
    if status >= 400:
        raise FetchError(f"The page returned HTTP {status}. The posting may have been removed.", status=status, platform=platform)

    job = job_from_jsonld(ld, final_url) if ld else job_from_html(html, final_url)
    job.url = url
    if not job.platform:
        job.platform = platform
    return finish_job(job, cfg)


def finish_job(job: Job, cfg: Optional[config.FetchSettings] = None) -> Job:
    cfg = cfg or config.load_settings().fetch
    job.description = clean_job_text(job.description, cfg.max_job_chars)
    if not job.company:
        job.company = company_from_url(job.url)
    job.language = detect_language(job.description + " " + job.title)
    if len(job.description) < 200:
        raise FetchError(
            "The page loaded but no job description could be extracted (it is probably rendered by JavaScript). "
            "Use the bookmarklet or paste the text.", platform=job.platform)
    return job


# --------------------------------------------------------------------------- pasted / bookmarklet input
def job_from_text(text: str, *, url: str = "", title: str = "", company: str = "", page_title: str = "",
                  cfg: Optional[config.FetchSettings] = None) -> Job:
    cfg = cfg or config.load_settings().fetch
    text = norm_ws(text)
    if len(text) < 80:
        raise FetchError("That text is too short to be a job description.")
    g_title, g_company = guess_title_company(page_title, url) if page_title else ("", "")
    first_line = next((ln for ln in text.split("\n") if ln.strip()), "")
    job = Job(
        url=url, source="pasted", platform=platform_of(url),
        title=title or g_title or (first_line if len(first_line) < 110 else ""),
        company=company or g_company or company_from_url(url),
        description=clean_job_text(text, cfg.max_job_chars),
    )
    job.language = detect_language(job.description + " " + job.title)
    return job


def job_from_ingest(payload: dict, cfg: Optional[config.FetchSettings] = None) -> Job:
    """Bookmarklet payload: {url, title, text, selection, jsonld}."""
    cfg = cfg or config.load_settings().fetch
    url = str(payload.get("url") or "")
    ld = payload.get("jsonld")
    if isinstance(ld, dict) and ld:
        job = job_from_jsonld(ld, url)
        job.source = "bookmarklet"
        try:
            return finish_job(job, cfg)
        except FetchError:
            pass
    selection = str(payload.get("selection") or "")
    text = selection if len(selection) > 200 else str(payload.get("text") or "")
    job = job_from_text(text, url=url, page_title=str(payload.get("title") or ""), cfg=cfg)
    job.source = "bookmarklet"
    return job
