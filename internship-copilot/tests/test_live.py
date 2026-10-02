"""Opt-in smoke tests against REAL public job pages (they depend on the network and on live data).
Run with:  COPILOT_LIVE=1 pytest tests/test_live.py -v
Useful after LinkedIn / Lever / Greenhouse change their markup."""
import os
import re

import httpx
import pytest

from copilot import config, fetch

pytestmark = pytest.mark.skipif(os.environ.get("COPILOT_LIVE") != "1", reason="set COPILOT_LIVE=1 to run live network tests")
UA = config.FetchSettings().user_agent


def cfg():
    c = config.FetchSettings()
    c.use_browser = "never"
    return c


def test_linkedin_public_guest_posting():
    r = httpx.get("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search",
                  params={"keywords": "stage devops", "location": "Paris, France"}, headers={"User-Agent": UA}, timeout=20)
    urls = re.findall(r'href="(https://[a-z.]*linkedin.com/jobs/view/[^"?]+)', r.text)
    if not urls:
        pytest.skip("LinkedIn returned no guest results from this network")
    job = fetch.fetch_job(urls[0], cfg())
    assert job.title and job.company and len(job.description) > 300 and job.platform == "LinkedIn"


def test_lever_posting():
    jobs = httpx.get("https://api.lever.co/v0/postings/palantir?mode=json", timeout=20).json()
    job = fetch.fetch_job(jobs[0]["hostedUrl"], cfg())
    assert job.source == "jsonld" and job.platform == "Lever" and len(job.description) > 300


def test_greenhouse_hosted_posting():
    jobs = httpx.get("https://boards-api.greenhouse.io/v1/boards/cloudflare/jobs", timeout=20).json()["jobs"]
    job = fetch.fetch_job(jobs[0]["absolute_url"], cfg())
    assert job.title and len(job.description) > 300


def test_blocked_sites_give_actionable_message():
    with pytest.raises(fetch.FetchError) as e:
        fetch.fetch_job("https://fr.indeed.com/jobs?q=stage+devops&l=Paris", cfg())
    assert e.value.blocked and "bookmarklet" in str(e.value)
