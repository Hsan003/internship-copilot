"""Best-effort company research for personalisation: the homepage + an 'about' page (nothing else, no search engines)."""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from . import config, netguard
from .textutil import html_to_text, norm_ws, one_line

ABOUT_PATHS = ["/about", "/about-us", "/company", "/a-propos", "/qui-sommes-nous", "/entreprise", "/notre-histoire",
               "/ueber-uns", "/unternehmen", "/en/about", "/fr/a-propos"]


def _clean_text(html: str) -> str:
    text = ""
    try:
        import trafilatura

        text = trafilatura.extract(html, include_comments=False, favor_recall=True, output_format="txt") or ""
    except Exception:
        text = ""
    if len(text) < 250:
        text = html_to_text(html)
    soup = BeautifulSoup(html or "", "lxml")
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    desc = one_line(meta["content"]) if meta and meta.get("content") else ""
    if desc and desc not in text:
        text = desc + "\n\n" + text
    return norm_ws(text)


def _get(client: httpx.Client, url: str) -> Optional[str]:
    try:
        r = client.get(url)
    except (httpx.HTTPError, netguard.BlockedAddress):
        return None
    if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"):
        return None
    return r.text


def fetch_company_text(url: str, cfg: Optional[config.FetchSettings] = None, max_pages: int = 2, per_page: int = 3000) -> list[tuple[str, str]]:
    """Returns [(source_url, text)] - may be empty. Never raises for network problems."""
    cfg = cfg or config.load_settings().fetch
    url = (url or "").strip()
    if not url:
        return []
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    p = urlparse(url)
    if not p.netloc:
        return []
    try:
        netguard.check_url(url)
    except netguard.BlockedAddress:
        return []
    root = f"{p.scheme}://{p.netloc}"
    headers = {"User-Agent": cfg.user_agent, "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8,de;q=0.6"}

    def guard(request: httpx.Request) -> None:
        netguard.check_url(str(request.url))

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    candidates = [url] + [root + path for path in ABOUT_PATHS]
    try:
        with httpx.Client(follow_redirects=True, timeout=min(cfg.timeout_s, 12), headers=headers,
                          event_hooks={"request": [guard]}) as client:
            for cand in candidates:
                if cand in seen or len(out) >= max_pages:
                    break
                seen.add(cand)
                html = _get(client, cand)
                if not html:
                    continue
                text = _clean_text(html)
                if len(text) >= 250:
                    out.append((cand, text[:per_page]))
    except Exception:
        return out
    return out
