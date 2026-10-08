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


# ---------------------------------------------------------------------------- AI mode (scripted model, no Ollama)
import json
import time

from copilot.llm import BaseLLM, LLMError

AI_POSTING = JOB + "\nWe value Chess-like strategic thinking and stakeholder management. Python and Docker daily.\n"


class ScriptedLLM(BaseLLM):
    name, model = "scripted", "scripted"

    def __init__(self, keywords=None, items=None, fail=False):
        self.keywords, self.items, self.fail = keywords or [], items or [], fail

    def chat(self, system, user, *, task="", **kw):
        if self.fail:
            raise LLMError("boom")
        return json.dumps({"keywords": self.keywords} if task == "tailor_keywords" else {"items": self.items})

    def status(self):
        return {"ok": True}


def _kw(term, imp="must", cat="soft"):
    return {"term": term, "importance": imp, "category": cat, "quote": term}


def test_ai_keywords_must_occur_in_posting_and_are_grounded_in_profile(profile):
    llm = ScriptedLLM(keywords=[_kw("Chess"), _kw("stakeholder management"), _kw("Quantum computing"), _kw("Docker", cat="tech")])
    res = tailor.analyze(profile, AI_POSTING, "DevOps Intern", "en", config.load_settings().cv, llm=llm)
    by = {i["display"].lower(): i for i in res["items"]}
    assert "quantum computing" not in by                       # not in the posting -> hallucination dropped
    assert by["chess"]["status"] == "covered"                    # not in the vocabulary, but your profile says it
    assert by["stakeholder management"]["status"] == "gap"       # asked, but you never wrote it -> never auto-added
    assert by["chess"]["in_cv"] is True and "Chess" not in res["default_keywords"]   # already on the CV (interests)
    assert "stakeholder management" not in res["default_keywords"] and "stakeholder management" in res["gaps"]
    assert res["ai"] is True


def test_ai_ranking_ignores_unknown_ids_and_reorders(profile):
    cvs = config.load_settings().cv
    base = tailor.analyze(profile, AI_POSTING, "", "en", cvs)["plan"]
    last = base.project_ids[-1]
    llm = ScriptedLLM(items=[{"id": last, "score": 10, "bullets": ["nope", profile.projects[0].bullets[0].id]},
                             {"id": "does-not-exist", "score": 10, "bullets": []}])
    res = tailor.analyze(profile, AI_POSTING, "", "en", cvs, llm=llm)
    assert res["plan"].project_ids[0] == last
    assert all(b != "nope" for bs in res["plan"].bullet_ids.values() for b in bs)


def test_ai_failure_falls_back_to_vocabulary(profile):
    res = tailor.analyze(profile, AI_POSTING, "", "en", config.load_settings().cv, llm=ScriptedLLM(fail=True))
    assert res["ai"] is False and res["items"] and len(res["ai_notes"]) == 2


def test_sanitize_plan_drops_foreign_ids(profile):
    plan = tailor.analyze(profile, AI_POSTING, "", "en", config.load_settings().cv)["plan"]
    plan.project_ids += ["x-evil"]
    plan.bullet_ids["x-evil"] = ["b"]
    plan.skills = [{"category": "Hacks", "items": ["Invented skill"]}]
    clean = tailor.sanitize_plan(profile, plan)
    assert "x-evil" not in clean.project_ids and "x-evil" not in clean.bullet_ids and clean.skills == []


def test_api_ai_analysis_runs_as_task(client):
    r = client.post("/api/tailor/analyze", json={"text": AI_POSTING, "title": "DevOps Intern", "use_ai": True})
    assert r.status_code == 200, r.text
    tid = r.json()["task_id"]
    for _ in range(100):
        t = client.get(f"/api/tasks/{tid}").json()
        if t["status"] in ("done", "error", "cancelled"):
            break
        time.sleep(0.1)
    assert t["status"] == "done", t
    assert t["result"]["ai"] is True and t["result"]["plan"]["project_ids"]
    assert client.post("/api/tailor/analyze", json={"text": "short", "use_ai": True}).status_code == 422


def test_api_build_uses_plan_from_analysis_and_sanitizes(client, monkeypatch):
    monkeypatch.setattr(latex, "find_engine", lambda *a, **k: None)
    rep = client.post("/api/tailor/analyze", json={"text": AI_POSTING}).json()
    plan = rep["plan"]
    plan["project_ids"].append("x-evil")
    r = client.post("/api/tailor/build", json={"text": AI_POSTING, "keywords": ["Chess"], "plan": plan})
    assert r.status_code == 200, r.text
    cp = r.json()["cv_plan"]
    assert "x-evil" not in cp["project_ids"] and cp["extra_keywords"] == ["Chess"]
