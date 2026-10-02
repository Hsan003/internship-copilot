import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from copilot import config, fetch
from copilot.fetch import FetchError
from tests.conftest import fixture_text

ROUTES = {
    "/jsonld": (200, "jsonld_job.html"), "/plain": (200, "plain_job.html"), "/blocked": (403, "blocked.html"),
    "/blocked200": (200, "blocked.html"), "/authwall": (200, "authwall.html"), "/missing": (404, "plain_job.html"),
    "/thin": (200, None),
}


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        status, name = ROUTES.get(self.path.split("?")[0], (404, None))
        body = fixture_text(name) if name else "<html><body><p>tiny</p></body></html>"
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def base():
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def cfg():
    s = config.FetchSettings()
    s.use_browser = "never"
    return s


def test_jsonld_extraction_and_mapping(base):
    job = fetch.fetch_job(base + "/jsonld", cfg())
    assert job.source == "jsonld" and job.title == "Backend Intern (m/f/d)" and job.company == "Acme Cloud"
    assert job.location == "Berlin, DE" and job.valid_through == "2026-12-31" and "INTERN" in job.employment_type
    assert job.language == "en" and "Operate services on Kubernetes" in job.description and "<li>" not in job.description
    assert job.company_url == "https://acme.example"


def test_plain_page_uses_main_content_and_title_heuristics(base):
    job = fetch.fetch_job(base + "/plain", cfg())
    assert job.source == "html" and job.language == "fr"
    assert "pipelines de données" in job.description and "Se connecter" not in job.description
    assert job.title.startswith("Stage Ingénieur Data") and job.company == "DataFlow"


def test_bot_wall_raises_blocked_with_helpful_message(base):
    for path in ("/blocked", "/blocked200", "/authwall"):
        with pytest.raises(FetchError) as e:
            fetch.fetch_job(base + path, cfg())
        assert e.value.blocked and "bookmarklet" in str(e.value)


def test_404_and_thin_pages(base):
    with pytest.raises(FetchError) as e:
        fetch.fetch_job(base + "/missing", cfg())
    assert "404" in str(e.value) and not e.value.blocked
    with pytest.raises(FetchError) as e:
        fetch.fetch_job(base + "/thin", cfg())
    assert "JavaScript" in str(e.value)


def test_private_addresses_are_refused(base, monkeypatch):
    monkeypatch.delenv("COPILOT_ALLOW_PRIVATE_URLS")
    with pytest.raises(FetchError) as e:
        fetch.fetch_job(base + "/jsonld", cfg())
    assert "private network" in str(e.value)


def test_unreachable_host_message():
    with pytest.raises(FetchError) as e:
        fetch.fetch_job("http://127.0.0.1:9/x", cfg())
    assert "Could not reach" in str(e.value)


def test_normalize_url():
    assert fetch.normalize_url("example.com/job") == "https://example.com/job"
    assert fetch.normalize_url("https://www.linkedin.com/jobs/collections/recommended/?currentJobId=3912345678") == "https://www.linkedin.com/jobs/view/3912345678/"
    assert fetch.normalize_url("https://fr.indeed.com/jobs?q=stage&vjk=abc123") == "https://fr.indeed.com/viewjob?jk=abc123"
    with pytest.raises(FetchError):
        fetch.normalize_url("   ")


@pytest.mark.parametrize("title,url,role,company", [
    ("Nimbus Logistics hiring Stage DevOps in Paris, Île-de-France | LinkedIn", "", "Stage DevOps", "Nimbus Logistics"),
    ("Stage DevOps (H/F) - Nimbus - Paris | Indeed.com", "", "Stage DevOps (H/F)", "Nimbus"),
    ("Backend Intern", "https://jobs.lever.co/acme-cloud/1234", "Backend Intern", "Acme Cloud"),
    ("", "https://boards.greenhouse.io/some-company/jobs/5", "", "Some Company"),
    ("Applied Science Intern - Datadog Careers", "", "Applied Science Intern", "Datadog"),
])
def test_guess_title_company(title, url, role, company):
    assert fetch.guess_title_company(title, url) == (role, company)


def test_platform_detection():
    assert fetch.platform_of("https://www.linkedin.com/jobs/view/1") == "LinkedIn"
    assert fetch.platform_of("https://www.welcometothejungle.com/fr/companies/x/jobs/y") == "Welcome to the Jungle"
    assert fetch.platform_of("https://acme.jobs.personio.de/job/1") == "Personio"
    assert fetch.platform_of("https://example.org/") == ""


LONG = "Stage DevOps. Nous recherchons un étudiant pour rejoindre notre équipe plateforme et travailler avec Docker et Kubernetes. " * 4


def test_job_from_text_and_paste():
    j = fetch.job_from_text("Stage DevOps chez Nimbus\n" + LONG, company="Nimbus", cfg=cfg())
    assert j.source == "pasted" and j.title == "Stage DevOps chez Nimbus" and j.company == "Nimbus" and j.language == "fr"
    with pytest.raises(FetchError):
        fetch.job_from_text("too short", cfg=cfg())


def test_bookmarklet_payload_prefers_jsonld_then_selection_then_text():
    ld = json.loads(fixture_text("jsonld_job.html").split('type="application/ld+json">')[1].split("</script>")[0])["@graph"][1]
    j = fetch.job_from_ingest({"url": "https://x.example/j", "jsonld": ld, "text": "ignored"}, cfg())
    assert j.source == "bookmarklet" and j.company == "Acme Cloud"
    j = fetch.job_from_ingest({"url": "https://x.example/j", "title": "Stage Dev - Foo - Paris | LinkedIn", "selection": LONG, "text": "short"}, cfg())
    assert j.description.startswith("Stage DevOps") and j.title == "Stage Dev" and j.company == "Foo"
    j = fetch.job_from_ingest({"url": "", "text": "Menu\n" + LONG}, cfg())
    assert j.source == "bookmarklet" and "Docker" in j.description


def test_bookmarklet_fragment_roundtrip_matches_app_decoder():
    """The page decodes #ingest=<url-safe base64 of UTF-8 JSON>; make sure non-ASCII survives the encoding the JS uses."""
    payload = {"url": "https://x", "title": "Stage Ingénieur – Données", "text": "é à ü ß – « »"}
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    b64 = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    back = json.loads(base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-8"))
    assert back == payload
