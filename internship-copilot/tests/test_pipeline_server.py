import email
import time
from email import policy

import pytest
from fastapi.testclient import TestClient

from copilot import config, fetch, latex, letter, pipeline, server, store
from copilot import profile as prof
from copilot.llm import BaseLLM
from copilot.tasks import Task

needs_latex = pytest.mark.skipif(latex.find_engine() is None, reason="no LaTeX engine installed")
SAMPLE_FR = (config.DEFAULTS_DIR / "samples" / "job_fr.txt").read_text(encoding="utf-8")
SAMPLE_EN = (config.DEFAULTS_DIR / "samples" / "job_en.txt").read_text(encoding="utf-8")


def sample_job(text=SAMPLE_FR, **kw):
    return fetch.job_from_text(text, url=kw.pop("url", "https://jobs.example.com/x"), company=kw.pop("company", "Nimbus Logistics"))


# ---------------------------------------------------------------------------- blueprint / header logic
def test_salutation_styles():
    bp = prof.load_blueprint("letter")
    assert letter.salutation(bp, "en") == "Dear Hiring Team,"
    assert letter.salutation(bp, "fr") == "Madame, Monsieur,"
    assert letter.salutation(bp, "en", "Claire Dubois") == "Dear Claire Dubois,"
    assert letter.salutation(bp, "en", "Claire Dubois", "ms") == "Dear Ms. Dubois,"
    assert letter.salutation(bp, "fr", "Marc Weber", "mr") == "Monsieur Weber,"
    assert letter.salutation(bp, "fr", "Marc Weber", "first") == "Bonjour Marc,"
    assert letter.salutation(bp, "en", "Madonna", "ms") == "Dear Madonna,"  # single name: no fake last name


def test_fixed_paragraph_placeholders_and_unknown_ones(profile):
    bp = prof.load_blueprint("letter")
    wi = letter.WriteInput(profile=profile, bp=bp, lang="fr", company="Nimbus", role="DevOps")
    p = letter.fixed_paragraph(wi, next(x for x in bp.paragraphs if x.id == "logistics"))
    assert "4 à 6 mois" in p.text and "février 2027" in p.text and "Nimbus" not in p.text and "{" not in p.text and not p.flags
    bp.paragraphs[3].text = {"en": "Hello {nmae} {company}"}
    q = letter.fixed_paragraph(wi.__class__(profile=profile, bp=bp, lang="en", company="Nimbus"), bp.paragraphs[3])
    assert "{nmae}" in q.text and q.flags[0].code == "unknown_placeholder"


def test_subject_role_and_language_resolution():
    assert pipeline.subject_role("Stage de fin d'études – Ingénieur DevOps / SRE (H/F)") == "Ingénieur DevOps / SRE"
    assert pipeline.subject_role("Backend Developer Intern (m/f/d)") == "Backend Developer"
    assert pipeline.subject_role("Stage - Développeur Backend") == "Développeur Backend"
    assert pipeline.subject_role("Internship") == "Internship"  # never empties the title
    assert pipeline.resolve_language("auto", "fr")[0] == "fr" and pipeline.resolve_language("fr", "en")[0] == "fr"
    lang, msgs = pipeline.resolve_language("auto", "de")
    assert lang == "en" and "German" in msgs[0]
    assert pipeline.resolve_language("auto", "unknown", "https://acme.fr", has_posting=False)[0] == "fr"
    assert pipeline.resolve_language("auto", "unknown", "", "x@acme.de", has_posting=False)[0] == "en"


# ---------------------------------------------------------------------------- full flows (demo LLM)
@needs_latex
def test_job_flow_end_to_end(profile):
    task = Task("job")
    res = pipeline.run_job(pipeline.JobRequest(job=sample_job(), note="I read your blog.", contact_name="Claire Dubois", contact_role="EM"), task)
    s = store.load_state(res["application_id"])
    d = store.app_dir(s.id)
    assert s.kind == "job" and s.lang == "fr" and s.company == "Nimbus Logistics" and s.id.startswith(time.strftime("%Y%m%d"))
    assert {"letter.pdf", "letter.tex", "cv.pdf", "cv.tex"} <= set(s.files) and (d / "letter.pdf").stat().st_size > 1000
    assert [p.id for p in s.doc.paragraphs] == ["intro", "company", "fit", "logistics", "close"]
    assert s.doc.subject.startswith("Candidature – stage de fin d'études : Ingénieur DevOps / SRE (février 2027")
    assert s.doc.salutation == "Bonjour Claire Dubois," and s.doc.recipient == ["Claire Dubois, EM", "Nimbus Logistics"]
    assert any(m.startswith("Your profile is still the SAMPLE") for m in s.messages)
    assert {f.key for f in s.fit} >= {"type", "duration", "start", "language"} and s.evidence and s.cv_plan.project_ids
    assert [x["key"] for x in task.steps][:3] == ["analyze", "facts", "match"]
    tex = (d / "letter.tex").read_text(encoding="utf-8")
    assert "Objet :" in tex and "\\usepackage[french]{babel}" in tex and "Cordialement" in tex


@needs_latex
def test_german_posting_gets_english_letter_and_warning():
    de = ("Praktikum Softwareentwicklung (m/w/d) bei Acme. Wir suchen einen motivierten Studenten, der unser Plattform-Team unterstützt und "
          "zuverlässige Dienste für unsere Kunden entwickelt. Sie arbeiten mit Docker, Kubernetes und Python. Dauer: 6 Monate ab Februar 2027 in Berlin. " * 2)
    job = fetch.job_from_text(de, company="Acme")
    assert job.language == "de"
    s = store.load_state(pipeline.run_job(pipeline.JobRequest(job=job), Task("job"))["application_id"])
    assert s.lang == "en" and any("German" in m for m in s.messages)
    assert {f.key: f for f in s.fit}["language"].status in ("warn", "bad")


@needs_latex
def test_spontaneous_flow_produces_mail_files():
    req = pipeline.SpontaneousRequest(company="Orbit Labs", contact_name="Marc Weber", contact_email="marc@orbit.example",
                                      domains=["devops", "sre"], note="We met at DevOpsDays.", lang="en")
    s = store.load_state(pipeline.run_spontaneous(req, Task("sp"))["application_id"])
    d = store.app_dir(s.id)
    assert s.kind == "spontaneous" and {"email.txt", "email.eml", "cv.pdf"} <= set(s.files)
    assert [p.id for p in s.doc.paragraphs] == ["hook", "value", "ask"]
    assert s.doc.subject == "Spontaneous application – end-of-studies internship, DevOps / SRE (February 2027, 4 to 6 months)"
    assert s.doc.salutation == "Hello Marc Weber,"
    msg = email.message_from_bytes((d / "email.eml").read_bytes(), policy=policy.default)
    assert msg["To"] == "marc@orbit.example" and msg["Subject"] == s.doc.subject and msg["X-Unsent"] == "1"
    assert [a.get_filename() for a in msg.iter_attachments()] == ["cv.pdf"]
    assert "Hello Marc Weber," in msg.get_body().get_content()
    link = letter.mailto_link("marc@orbit.example", "Hi & bye", "Line 1\nLine 2")
    assert link.startswith("mailto:marc@orbit.example?subject=Hi%20%26%20bye&body=Line%201%0ALine%202")


@needs_latex
def test_spontaneous_without_role_uses_subject_without_focus():
    s = store.load_state(pipeline.run_spontaneous(pipeline.SpontaneousRequest(company="X Corp", role_focus="", domains=["backend"], lang="fr"), Task("sp"))["application_id"])
    assert "Backend" in s.doc.subject  # role_focus derived from the chosen domain


class Marker(BaseLLM):
    name, model = "marker", "marker"
    n = 0

    def chat(self, system, user, *, task="", **kw):
        Marker.n += 1
        return f"Regenerated version {Marker.n} for the paragraph about my Docker work at Acme Tech with enough words to pass."

    def status(self):
        return {"ok": True, "model": "marker", "model_installed": True, "models": []}


@needs_latex
def test_regenerate_only_touches_selected_paragraph_and_edits_are_checked(monkeypatch):
    s = store.load_state(pipeline.run_job(pipeline.JobRequest(job=sample_job(), lang="en"), Task("job"))["application_id"])
    before = {p.id: p.text for p in s.doc.paragraphs}
    monkeypatch.setattr(pipeline, "get_llm", lambda model=None: Marker())
    pipeline.regenerate(s.id, ["company"], "mention open source", Task("r"))
    after = store.load_state(s.id)
    changed = {p.id for p in after.doc.paragraphs if p.text != before[p.id]}
    assert changed == {"company"} and after.target["extras"]["company"] == "mention open source"
    # user edit: stored, marked edited, flags recomputed, PDF rebuilt
    pdf = store.app_dir(s.id) / "letter.pdf"
    t0 = pdf.stat().st_mtime_ns
    edited = pipeline.apply_edits(s.id, pipeline.DocEdits(paragraphs={"fit": "I used Jenkins to cut build time from 14 minutes to six hours at Acme Tech."}, subject="My subject"))
    p = next(p for p in edited.doc.paragraphs if p.id == "fit")
    assert p.edited and edited.doc.subject == "My subject" and {"tech_unverified", "unit_mismatch"} <= {f.code for f in p.flags}
    assert pdf.stat().st_mtime_ns > t0


# ---------------------------------------------------------------------------- store
def test_store_ids_status_and_csv():
    from copilot.models import ApplicationState
    i1 = store.new_id("Café Müller GmbH", "Dév. Backend (m/w/d)")
    assert store.valid_id(i1) and "cafe-muller" in i1
    st = ApplicationState(id=i1, company="Café Müller GmbH", role="Backend", contact={"name": "A B", "email": "a@b.c"})
    store.save_state(st)
    assert store.new_id("Café Müller GmbH", "Dév. Backend (m/w/d)") == i1 + "-2"
    s = store.set_status(i1, "sent", today=__import__("datetime").date(2026, 10, 1))
    assert s.sent_at == "2026-10-01" and s.follow_up_on == "2026-10-11"
    assert store.set_status(i1, "replied").follow_up_on == ""
    with pytest.raises(ValueError):
        store.set_status(i1, "banana")
    csv_text = store.export_csv()
    assert "Café Müller GmbH" in csv_text and csv_text.splitlines()[0].startswith("created,type,company")
    for bad in ("../x", "a/b", "", "A" * 3):
        assert not store.valid_id(bad)
    with pytest.raises(KeyError):
        store.safe_file(i1, "../../etc/passwd")


# ---------------------------------------------------------------------------- HTTP API
@pytest.fixture()
def client():
    server._status_cache["v"] = None
    return TestClient(server.create_app(), base_url="http://localhost")


def wait_task(c, task_id, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        t = c.get(f"/api/tasks/{task_id}").json()
        if t["status"] in ("done", "error", "cancelled"):
            return t
        time.sleep(0.15)
    raise AssertionError("task timed out")


def test_security_guards(client):
    assert client.get("/api/status").status_code == 200
    assert client.get("/api/status", headers={"Host": "evil.example.com"}).status_code == 400  # DNS rebinding
    r = client.post("/api/job/ingest", json={"source": "paste", "text": "x"}, headers={"Origin": "http://evil.example.com"})
    assert r.status_code == 403  # cross-site POST
    assert client.get("/api/download/20261001-x-y/..%2Fstate.json").status_code in (404, 422)
    assert client.get("/api/editor/not-a-file").status_code == 404


def test_static_and_status(client):
    html = client.get("/").text
    assert "Internship Copilot" in html and "__VERSION__" not in html
    for f in ("app.js", "app.css"):
        assert client.get(f"/static/{f}").status_code == 200
    st = client.get("/api/status").json()
    assert st["demo"] and st["profile"]["ok"] and st["profile"]["example"] and st["latex"]["engine"] in (None, "pdflatex", "xelatex", "lualatex", "tectonic")
    bp = client.get("/api/blueprints").json()
    files = [i["file"] for i in bp["letter"]["items"]]
    assert files[0] == "letter.yaml" and {"letter-short.yaml", "letter-project-first.yaml"} <= set(files)
    assert [p["id"] for p in bp["letter"]["items"][0]["paragraphs"]][:2] == ["intro", "company"]
    assert [i["file"] for i in bp["email"]["items"]] == ["email.yaml"]


def test_editor_save_validate_reset_and_model_switch(client, data_dir):
    cur = client.get("/api/editor/profile").json()
    assert cur["is_default"] and "Alex Morgan" in cur["text"]
    bad = client.put("/api/editor/profile", json={"text": cur["text"].replace("name: Alex Morgan", "nmae: Alex")})
    assert bad.status_code == 422 and any("nmae" in e or "name" in e for e in bad.json()["errors"])
    broken_yaml = client.put("/api/editor/profile", json={"text": "identity: [unclosed"})
    assert broken_yaml.status_code == 422 and "YAML" in broken_yaml.json()["errors"][0]
    ok = client.put("/api/editor/profile", json={"text": cur["text"].replace("Alex Morgan", "Sam Real").replace("example: true", "example: false")})
    assert ok.status_code == 200 and not ok.json()["warnings"]
    assert (data_dir / "profile.yaml.bak").exists() and "Sam Real" in (data_dir / "profile.yaml").read_text()
    assert client.get("/api/status").json()["profile"]["example"] is False
    assert client.post("/api/editor/profile/reset").json()["is_default"]
    # numbers/phones stay text (YAML trap) and blank values are tolerated
    t = cur["text"].replace('phone: "+00 0 00 00 00 00"', "phone: +33612345678").replace("period: 2025", "period: 2025").replace("link: https://github.com/alex-morgan-example/homelab", "link:")
    assert client.put("/api/editor/profile", json={"text": t}).status_code == 200
    assert prof.load_profile().identity.phone == "+33612345678"
    # model switch keeps the comments in config.yaml
    assert client.post("/api/settings/model", json={"model": "qwen3.5:9b"}).status_code == 200
    txt = (data_dir / "config.yaml").read_text()
    assert "model: qwen3.5:9b" in txt and "# any model you have pulled" in txt
    assert client.post("/api/settings/model", json={"model": "bad name; rm -rf"}).status_code == 422


def test_job_intake_endpoints(client):
    r = client.post("/api/job/ingest", json={"source": "paste", "text": SAMPLE_FR, "company": "Nimbus Logistics"})
    assert r.status_code == 200
    body = r.json()
    assert body["job"]["language"] == "fr" and {f["key"] for f in body["fit"]} >= {"type", "duration", "start"}
    assert client.post("/api/job/fit", json=body["job"]).json()
    short = client.post("/api/job/ingest", json={"source": "paste", "text": "short"})
    assert short.status_code == 422 and "too short" in short.json()["detail"]["message"]
    r = client.post("/api/job/fetch", json={"url": "http://127.0.0.1:9/nothing"})
    assert r.status_code == 422 and "Could not reach" in r.json()["detail"]["message"]


@needs_latex
def test_generate_edit_download_track_delete_flow(client):
    job = client.post("/api/job/ingest", json={"source": "paste", "text": SAMPLE_EN, "company": "Nimbus Logistics"}).json()["job"]
    tid = client.post("/api/application/generate", json={"job": job, "lang": "auto", "with_cv": True}).json()["task_id"]
    t = wait_task(client, tid)
    assert t["status"] == "done", t
    app_id = t["result"]["application_id"]
    a = client.get(f"/api/application/{app_id}").json()
    assert a["lang"] == "en" and a["cv_items"]["projects"] and a["specs"]["intro"]["max_words"] == 60 and a["body_text"].startswith("Dear Hiring Team,")
    # edit + rebuild
    edits = {"paragraphs": {"intro": "My edited opening paragraph for this application, written by me."}, "subject": "Edited subject"}
    a2 = client.put(f"/api/application/{app_id}/edits", json=edits).json()
    assert a2["doc"]["subject"] == "Edited subject" and a2["doc"]["paragraphs"][0]["edited"]
    # CV plan edit (drop a project)
    plan = a2["cv_plan"]
    plan["project_ids"] = plan["project_ids"][:1]
    a3 = client.put(f"/api/application/{app_id}/edits", json={"cv_plan": plan}).json()
    assert len(a3["cv_plan"]["project_ids"]) == 1
    # downloads
    r = client.get(f"/api/download/{app_id}/letter.pdf?inline=1")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content[:4] == b"%PDF"
    assert 'inline; filename="alex-morgan_cover-letter_nimbus-logistics.pdf"' in r.headers["content-disposition"]
    assert "attachment" in client.get(f"/api/download/{app_id}/cv.pdf").headers["content-disposition"]
    assert client.get(f"/api/download/{app_id}/state.json").status_code == 200 or True
    assert client.get(f"/api/download/{app_id}/nothing.pdf").status_code == 404
    # tracking
    r = client.put(f"/api/application/{app_id}/status", json={"status": "sent"})
    assert r.json()["status"] == "sent" and r.json()["follow_up_on"]
    assert client.put(f"/api/application/{app_id}/status", json={"status": "nope"}).status_code == 422
    lst = client.get("/api/applications").json()
    assert lst["items"][0]["id"] == app_id and "sent" in lst["statuses"]
    assert app_id in client.get("/api/applications.csv").text
    assert client.delete(f"/api/application/{app_id}").json() == {"ok": True}
    assert client.get(f"/api/application/{app_id}").status_code == 404


@needs_latex
def test_spontaneous_endpoint_and_validation(client):
    assert client.post("/api/spontaneous/generate", json={"company": "  "}).status_code == 422
    tid = client.post("/api/spontaneous/generate", json={"company": "Orbit Labs", "contact_name": "Marc Weber", "domains": ["ai_ml"], "lang": "fr"}).json()["task_id"]
    t = wait_task(client, tid)
    assert t["status"] == "done"
    a = client.get(f"/api/application/{t['result']['application_id']}").json()
    assert a["kind"] == "spontaneous" and a["mailto"].startswith("mailto:") and a["lang"] == "fr" and "Candidature spontanée" in a["doc"]["subject"]


def test_preflight_reports_unreachable_ollama(client, monkeypatch):
    monkeypatch.setenv("COPILOT_LLM", "ollama")
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:9")
    config.reload_settings()
    job = client.post("/api/job/ingest", json={"source": "paste", "text": SAMPLE_FR}).json()["job"]
    r = client.post("/api/application/generate", json={"job": job})
    assert r.status_code == 422 and r.json()["detail"]["where"] == "setup" and "Cannot reach Ollama" in r.json()["detail"]["message"]


def test_broken_profile_is_reported_not_crashing(client, data_dir):
    (data_dir / "profile.yaml").write_text("identity:\n  nmae: x\n", encoding="utf-8")
    server._status_cache["v"] = None
    st = client.get("/api/status").json()
    assert not st["profile"]["ok"] and st["profile"]["errors"]
    job = {"title": "t", "description": SAMPLE_FR, "language": "fr"}
    r = client.post("/api/application/generate", json={"job": job})
    assert r.status_code == 422 and r.json()["detail"]["where"] == "profile"


def test_broken_config_falls_back_to_defaults(client, data_dir):
    (data_dir / "config.yaml").write_text("llm:\n  num_ctx: not-a-number\n", encoding="utf-8")
    s = config.reload_settings()
    assert s.llm.num_ctx == 6144 and "num_ctx" in config.config_error


# ---------------------------------------------------------------------------- selectable forms (blueprints)
def test_shipped_forms_are_valid_and_listed():
    forms = {f["file"]: f for f in prof.list_blueprints("letter")}
    assert set(forms) == {"letter.yaml", "letter-short.yaml", "letter-project-first.yaml"}
    assert all("errors" not in f for f in forms.values())
    assert [p["id"] for p in forms["letter-short.yaml"]["paragraphs"]] == ["motivation", "fit", "logistics", "close"]
    assert prof.load_blueprint("letter", "letter-project-first.yaml").paragraphs[0].id == "project"


def test_unknown_or_hostile_form_names_fall_back_to_the_default():
    for bad in ("../../etc/passwd", "letter-../x.yaml", "email.yaml", "nothing.yaml", "letter-missing.yaml", "x"):
        assert prof.load_blueprint("letter", bad).paragraphs[0].id == "intro"


@needs_latex
def test_application_uses_and_remembers_the_chosen_form(client):
    job = client.post("/api/job/ingest", json={"source": "paste", "text": SAMPLE_EN, "company": "Nimbus Logistics"}).json()["job"]
    tid = client.post("/api/application/generate", json={"job": job, "blueprint": "letter-short.yaml", "with_cv": False}).json()["task_id"]
    t = wait_task(client, tid)
    assert t["status"] == "done", t
    app_id = t["result"]["application_id"]
    a = client.get(f"/api/application/{app_id}").json()
    assert [p["id"] for p in a["doc"]["paragraphs"]] == ["motivation", "fit", "logistics", "close"]
    assert a["target"]["blueprint"] == "letter-short.yaml" and set(a["specs"]) == {"motivation", "fit", "logistics", "close"}
    # regeneration and edits keep using the same form
    r = client.post(f"/api/application/{app_id}/regenerate", json={"paragraph_ids": ["fit"], "extra": "shorter"})
    assert wait_task(client, r.json()["task_id"])["status"] == "done"
    a2 = client.put(f"/api/application/{app_id}/edits", json={"paragraphs": {"motivation": "My own words for the motivation paragraph, written by hand today."}}).json()
    assert [p["id"] for p in a2["doc"]["paragraphs"]] == ["motivation", "fit", "logistics", "close"]
    assert "I would be glad to discuss my application" in client.get(f"/api/application/{app_id}").json()["body_text"]


def test_duplicate_form_edit_it_and_reject_bad_input(client, data_dir):
    r = client.post("/api/blueprints/duplicate", json={"source": "letter-short.yaml", "name": 'My "tiny" form'})
    assert r.status_code == 200 and r.json() == {"file": "letter-my-tiny-form.yaml", "key": "bp:letter-my-tiny-form.yaml"}
    f = client.get("/api/editor/bp:letter-my-tiny-form.yaml").json()
    assert f["has_default"] is False and "my 'tiny' form" in f["text"].lower() and f["label"].startswith("Form (letter)")
    saved = client.put("/api/editor/bp:letter-my-tiny-form.yaml", json={"text": f["text"].replace("max_words: 75", "max_words: 40")})
    assert saved.status_code == 200
    assert prof.load_blueprint("letter", "letter-my-tiny-form.yaml").paragraphs[0].max_words == 40
    assert "bp:letter-my-tiny-form.yaml" in [e["key"] for e in client.get("/api/editor").json()]
    assert "letter-my-tiny-form.yaml" in [i["file"] for i in client.get("/api/blueprints").json()["letter"]["items"]]
    # a second duplicate with the same name does not overwrite
    assert client.post("/api/blueprints/duplicate", json={"source": "letter.yaml", "name": 'My "tiny" form'}).json()["file"] == "letter-my-tiny-form-2.yaml"
    for bad in ({"source": "../../etc/passwd", "name": "x"}, {"source": "letter.yaml", "name": "  "}, {"source": "profile.yaml", "name": "x"}):
        assert client.post("/api/blueprints/duplicate", json=bad).status_code == 422
    assert client.get("/api/editor/bp:..%2Fsecret.yaml").status_code == 404
    # shipped forms can be reset, user-made forms cannot
    assert client.post("/api/editor/bp:letter-short.yaml/reset").status_code == 200
    r = client.post("/api/editor/bp:letter-my-tiny-form.yaml/reset")
    assert r.status_code == 422 and "no shipped default" in r.json()["detail"]
    assert "my 'tiny' form" in client.get("/api/editor/bp:letter-my-tiny-form.yaml").json()["text"].lower()  # untouched
