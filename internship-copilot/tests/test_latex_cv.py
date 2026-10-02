import copy

import pytest

from copilot import config, cv, editor, latex, retrieve
from copilot.models import Bullet

needs_latex = pytest.mark.skipif(latex.find_engine() is None, reason="no LaTeX engine installed")


# ---------------------------------------------------------------------------- escaping & templates
def test_tex_escape_specials_unicode_and_junk():
    assert latex.tex_escape("100% & $5 #1 a_b {x} ~ ^ \\") == r"100\% \& \$5 \#1 a\_b \{x\} \textasciitilde{} \textasciicircum{} \textbackslash{}"
    assert latex.tex_escape("a → b — c ‘d’ €5 …") == r"a $\rightarrow$ b — c ‘d’ \texteuro{}5 \ldots{}"
    assert latex.tex_escape("ok 😀 日本 \x00 end").split() == ["ok", "end"]
    assert latex.tex_escape(None) == ""


def test_render_autoescapes_and_raw_filter():
    out = latex.render(r"\VAR{ a }|\VAR{ b|raw }|\VAR{ c.items }", {"a": "R&D 50%", "b": r"\textbf{x}", "c": {"items": "k"}})
    assert out == r"R\&D 50\%|\textbf{x}|k"


def test_render_errors_are_friendly():
    with pytest.raises(latex.TemplateError, match="unknown variable"):
        latex.render(r"\VAR{ nope }", {})
    with pytest.raises(latex.TemplateError, match="syntax"):
        latex.render(r"\BLOCK{ if }", {})


def test_raw_blocks_protect_documentation_comments():
    assert latex.render("\\BLOCK{ raw }\n%% \\VAR{ x } stays\n\\BLOCK{ endraw }\nA", {}).lstrip().startswith("%% \\VAR{ x } stays")


@needs_latex
def test_compile_ok_error_and_page_count(tmp_path):
    doc = lambda body: "\\documentclass{article}\\begin{document}" + body + "\\end{document}"
    r = latex.compile_tex(doc("Hello"), tmp_path, "a")
    assert r.ok and r.pages == 1 and r.pdf.exists()
    r = latex.compile_tex(doc("One\\newpage Two"), tmp_path, "b")
    assert r.ok and r.pages == 2
    r = latex.compile_tex(doc("\\undefinedmacro"), tmp_path, "c")
    assert not r.ok and "Undefined control sequence" in r.message and r.error == "failed"
    assert not (tmp_path / "c.pdf").exists() and not (tmp_path / "a.aux").exists()


def test_compile_without_engine(tmp_path, monkeypatch):
    monkeypatch.setattr(latex, "find_engine", lambda pref="auto": None)
    r = latex.compile_tex("x", tmp_path, "a")
    assert not r.ok and r.error == "no_engine" and (tmp_path / "a.tex").exists()


# ---------------------------------------------------------------------------- retrieval
def test_retrieval_prefers_relevant_projects(profile):
    devops = retrieve.build_job_terms("Kubernetes Terraform Argo CD Prometheus Grafana SRE", title="DevOps")
    ai = retrieve.build_job_terms("LLM RAG pgvector LangChain Ollama PyTorch machine learning", title="AI intern")
    best = lambda jt: max((r for r in retrieve.rank_items(profile, jt) if r.kind == "project"), key=lambda r: r.score).item.name
    assert best(devops) == "Homelab Kubernetes platform"
    assert best(ai) == "Course-assistant RAG chatbot"
    ev = retrieve.pick_evidence(profile, ai, "en", k=3)
    assert ev[0].title == "Course-assistant RAG chatbot" and len(ev) == 3 and any(e.kind == "experience" for e in ev)


def test_cv_plan_selects_and_orders_without_inventing(profile):
    jt = retrieve.build_job_terms("Kubernetes Terraform Prometheus Grafana", title="SRE")
    plan = retrieve.plan_cv(profile, jt, "fr", config.load_settings().cv)
    assert plan.project_ids[0] == "proj-1" and set(plan.project_ids) <= {p.id for p in profile.projects}
    all_bullets = {b.id for x in [*profile.projects, *profile.experience] for b in x.bullets}
    assert all(set(ids) <= all_bullets for ids in plan.bullet_ids.values())
    assert plan.skills[0]["category"] in ("Cloud & DevOps", "Observabilité")  # most relevant skill group first, localised
    assert "Disponible dès février 2027" in plan.headline


def test_domain_terms_for_spontaneous(profile):
    jt = retrieve.domain_job_terms(["ai_ml"])
    assert jt.domains == ["ai_ml"] and "pytorch" in jt.weights
    assert max(retrieve.rank_items(profile, jt), key=lambda r: r.score).id == "proj-2"


# ---------------------------------------------------------------------------- CV build
def cv_template():
    return config.user_path("cv_template").read_text(encoding="utf-8")


@needs_latex
@pytest.mark.parametrize("lang", ["en", "fr"])
def test_cv_builds_on_one_page(profile, tmp_path, lang):
    plan = retrieve.plan_cv(profile, retrieve.build_job_terms("Kubernetes DevOps"), lang, config.load_settings().cv)
    b = cv.build_cv(profile, plan, cv_template(), tmp_path, "cv")
    assert b.ok and b.pages == 1 and b.pdf.exists() and not b.trimmed
    tex = b.tex
    assert "Alex Morgan" in tex and ("Formation" in tex if lang == "fr" else "Education" in tex)


@needs_latex
def test_cv_survives_hostile_characters(profile, tmp_path):
    p = copy.deepcopy(profile)
    p.identity.name = "Zoë O’Brien & Søn_#1"
    p.projects[0].name = "R&D 100% {tool} ~ _x^2 \\ 😀 日本"
    p.projects[0].bullets[0] = Bullet(id="proj-1-b1", text="Cut costs by 50% & latency $ 10_ms #fast {x} → done")
    plan = retrieve.plan_cv(p, retrieve.JobTerms(), "en", config.load_settings().cv)
    b = cv.build_cv(p, plan, cv_template(), tmp_path, "cv")
    assert b.ok, b.message


@needs_latex
def test_cv_trims_least_relevant_content_to_fit_one_page(profile, tmp_path):
    p = copy.deepcopy(profile)
    for proj in p.projects:  # make the CV far too long
        proj.bullets = [Bullet(id=f"{proj.id}-x{i}", text="A long sentence about work that was done in this project with many concrete details. " * 2) for i in range(9)]
    for x in p.experience:
        x.bullets = [Bullet(id=f"{x.id}-x{i}", text="Experience bullet with several details about what was achieved at the company. " * 2) for i in range(9)]
    st = config.load_settings()
    st.cv.max_bullets_per_item = 9
    jt = retrieve.build_job_terms("Kubernetes Terraform DevOps")
    plan = retrieve.plan_cv(p, jt, "en", st.cv)
    b = cv.build_cv(p, plan, cv_template(), tmp_path, "cv", st)
    assert b.ok and b.pages == 1 and b.trimmed, b.message
    assert "Trimmed to fit" in b.message
    assert b.plan.project_ids[0] == plan.project_ids[0]  # the most relevant project is never the one sacrificed first


def test_cv_template_typo_is_caught_at_save_time():
    text = cv_template().replace("\\VAR{p.name}", "\\VAR{p.nmae}")
    errors, _ = editor.validate("cv_template", text)
    assert errors and "nmae" in errors[0]
    errors, _ = editor.validate("cv_template", cv_template())
    assert errors == []
    errors, _ = editor.validate("cv_template", "\\documentclass{article}")
    assert "begin{document}" in errors[0]


@needs_latex
def test_custom_style_files_in_assets_are_found(tmp_path, data_dir):
    (data_dir / "templates" / "assets" / "mymacros.sty").write_text(
        "\\ProvidesPackage{mymacros}\\newcommand{\\resumeItem}[1]{\\item #1}\\newcommand{\\mybrand}{Brand-42}", encoding="utf-8")
    tex = "\\documentclass{article}\\usepackage{mymacros}\\begin{document}\\mybrand\\end{document}"
    r = latex.compile_tex(tex, tmp_path, "a")
    assert r.ok, r.message
