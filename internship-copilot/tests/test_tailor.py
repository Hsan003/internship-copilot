import pytest
from fastapi.testclient import TestClient

from copilot import config, cv, latex, server, store, tailor
from copilot import profile as prof

needs_latex = pytest.mark.skipif(latex.find_engine() is None, reason="no LaTeX engine installed")
JOB = (config.DEFAULTS_DIR / "samples" / "job_en.txt").read_text(encoding="utf-8")
# one skill the sample profile has (Kubernetes) and one it certainly lacks (COBOL is not in the vocabulary, so use Rust)
JOB_TEXT = JOB + "\nYou know Kubernetes, Terraform, Rust and Elixir. Rust is a plus. Rust. Rust.\n"


@pytest.fixture()
def client():
    server._status_cache["v"] = None
    return TestClient(server.create_app(), base_url="http://localhost")


def test_analyze_separates_covered_and_gaps(profile):
    res = tailor.analyze(profile, JOB_TEXT, "DevOps Intern", "en", config.load_settings().cv)
    by = {i["key"]: i for i in res["items"]}
    assert by["kubernetes"]["status"] == "covered"
    assert by["rust"]["status"] == "gap"
    assert "Rust" in res["gaps"] and "Rust" not in res["default_keywords"]
    assert 0 <= res["score_before"] <= res["score_after"] <= 100


def test_default_keywords_are_never_gaps(profile):
    res = tailor.analyze(profile, JOB_TEXT, "", "en", config.load_settings().cv)
    vocab = prof.profile_vocab(profile)
    from copilot import techvocab

    for k in res["default_keywords"]:
        assert set(techvocab.find_terms(k)) <= vocab


def test_extra_keywords_reach_the_cv_context_once(profile):
    res = tailor.analyze(profile, JOB_TEXT, "", "fr", config.load_settings().cv)
    plan = tailor.apply_keywords(res["plan"], ["Zig", "zig", " Nim "])
    ctx = cv.build_context(profile, plan, "fr")
    last = ctx["skills"][-1]
    assert last["category"] == "Autres mots-clés" and last["items"] == "Zig, Nim"
    plan.extra_keywords = []
    assert all(s["category"] != "Autres mots-clés" for s in cv.build_context(profile, plan, "fr")["skills"])


def test_keyword_match_bounds():
    assert tailor.keyword_match({}, set()) == 0
    assert tailor.keyword_match({"a": 1.0, "b": 1.0}, {"a"}) == 50


def test_api_analyze_and_short_text(client):
    r = client.post("/api/tailor/analyze", json={"text": JOB_TEXT, "title": "DevOps Intern"})
    assert r.status_code == 200
    body = r.json()
    assert body["lang"] == "en" and body["items"] and "gaps" in body
    assert client.post("/api/tailor/analyze", json={"text": "too short"}).status_code == 422


@needs_latex
def test_api_build_creates_cv_only_application(client):
    r = client.post("/api/tailor/build", json={"text": JOB_TEXT, "title": "DevOps Intern", "company": "Acme", "keywords": ["Terraform"]})
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["kind"] == "cv" and a["doc"] is None and a["cv_plan"]["extra_keywords"] == ["Terraform"]
    assert any(f["name"] == "cv.pdf" for f in a["file_info"])
    assert store.load_state(a["id"]).kind == "cv"
    assert any(x["kind"] == "cv" for x in client.get("/api/applications").json()["items"])
    # editing the CV of a cv-only application works (no blueprint / document involved)
    assert client.put(f"/api/application/{a['id']}/edits", json={"cv_plan": a["cv_plan"]}).status_code == 200


def test_api_build_without_latex_still_stores_tex_state(client, monkeypatch):
    monkeypatch.setattr(latex, "find_engine", lambda *a, **k: None)
    r = client.post("/api/tailor/build", json={"text": JOB_TEXT, "company": "Acme"})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "cv"
