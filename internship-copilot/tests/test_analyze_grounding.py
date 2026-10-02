import json

import pytest

from copilot import analyze, grounding, letter, prompts
from copilot import profile as prof
from copilot.llm import BaseLLM, LLMError, extract_json
from copilot.models import Job
from tests.conftest import FIXTURES

POSTING = (FIXTURES.parent.parent / "copilot" / "defaults" / "samples" / "job_fr.txt").read_text(encoding="utf-8")


class Scripted(BaseLLM):
    """Returns queued answers (strings); records the prompts it was given."""
    name, model = "scripted", "scripted"

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def chat(self, system, user, *, task="", schema=None, history=None, **kw):
        self.calls.append({"task": task, "system": system, "user": user, "history": history})
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a if isinstance(a, str) else json.dumps(a)


# ---------------------------------------------------------------------------- json extraction
def test_extract_json_tolerates_fences_think_and_trailing_commas():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('<think>hmm</think>{"a": [1,2,],}') == {"a": [1, 2]}
    assert extract_json('Sure! Here it is: {"a": "b"} Hope it helps') == {"a": "b"}
    with pytest.raises(LLMError):
        extract_json("no json here")


def test_chat_json_retries_once_with_the_error():
    llm = Scripted("not json", {"x": 1})
    out = llm.chat_json("s", "u", schema={}, validate=lambda d: d)
    assert out == {"x": 1} and len(llm.calls) == 2 and "invalid" in llm.calls[1]["history"][1]["content"]
    with pytest.raises(LLMError):
        Scripted("bad", "still bad").chat_json("s", "u", schema={}, validate=lambda d: d)


# ---------------------------------------------------------------------------- analysis
def job():
    return Job(title="Stage de fin d'études – Ingénieur DevOps / SRE (H/F)", company="Nimbus Logistics", description=POSTING, language="fr")


def test_analyze_job_normalises_and_enriches():
    llm = Scripted({"role_title": "Ingénieur DevOps / SRE (H/F)", "company_name": "Totally Invented Corp",
                    "mission": "Industrialiser les déploiements.", "must_have": ["Linux", "linux", "Docker", ""], "nice_to_have": ["AWS"],
                    "domains": ["devops", "devops", "nonsense", "sre"]})
    a = analyze.analyze_job(llm, job())
    assert a.role_title == "Ingénieur DevOps / SRE"  # gender tag removed
    assert a.company_name == "Nimbus Logistics"  # hallucinated company name replaced by the page's value
    assert a.must_have == ["Linux", "Docker"] and a.domains == ["devops", "sre"]
    assert "kubernetes" in a.terms and "Kubernetes" in a.stack


def test_analyze_job_falls_back_to_inferred_domains():
    a = analyze.analyze_job(Scripted({"role_title": "", "company_name": "", "mission": "", "must_have": [], "nice_to_have": [], "domains": ["other"]}), job())
    assert a.domains and a.domains[0] in ("devops", "sre", "cloud") and a.role_title.startswith("Stage")


# ---------------------------------------------------------------------------- verified facts
def test_verify_quote_is_strict_but_tolerant():
    src = "Nimbus Logistics édite une plateforme d'optimisation de tournées de livraison utilisée par plus de 200 retailers en Europe."
    assert analyze.verify_quote("plateforme d'optimisation de tournées de livraison utilisée par plus de 200 retailers", src)
    assert analyze.verify_quote("PLATEFORME d’optimisation de tournees de livraison, utilisée par plus de 200 retailers", src)  # case/accents/punctuation
    assert analyze.verify_quote("plateforme d'optimisation de tournées de livraison utilisée par plus de 200 grands retailers", src)  # one slipped word
    assert not analyze.verify_quote("plateforme d'optimisation de tournées de livraison utilisée par plus de 900 retailers en Europe", src)  # changed figure
    assert not analyze.verify_quote("la plus grande plateforme de livraison par drones au monde", src)
    assert not analyze.verify_quote("short", src)


def test_numbers_in_normalises_thousands():
    assert analyze.numbers_in("1 200 pages, 1,200 pages, 3.5 ms, 2027") == {"1200", "3.5", "2027"}


def test_extract_company_facts_drops_unverifiable_and_number_inventing_facts():
    sources = [("job posting", POSTING)]
    llm = Scripted({"facts": [
        {"fact": "Nimbus édite une plateforme d'optimisation de tournées utilisée par plus de 200 retailers.", "quote": "plateforme d'optimisation de tournées de livraison utilisée par plus de 200 retailers en Europe"},
        {"fact": "Nimbus a levé 50 millions d'euros.", "quote": "a levé 50 millions d'euros auprès d'investisseurs"},  # quote not in the source
        {"fact": "Nimbus sert plus de 900 retailers.", "quote": "utilisée par plus de 200 retailers en Europe"},  # number not in quote
    ]})
    facts = analyze.extract_company_facts(llm, "Nimbus Logistics", sources, "fr")
    assert len(facts) == 1 and facts[0].source == "job posting" and "200" in facts[0].fact


def test_extract_company_facts_never_raises_on_model_garbage():
    assert analyze.extract_company_facts(Scripted("garbage"), "X", [("job posting", POSTING)], "fr") == []
    assert analyze.extract_company_facts(Scripted(LLMError("boom")), "X", [("job posting", POSTING)], "fr") == []
    assert analyze.extract_company_facts(Scripted(), "X", [("job posting", "too short")], "fr") == []


# ---------------------------------------------------------------------------- grounding
def gctx(profile, **kw):
    return letter._grounding_ctx(letter.WriteInput(profile=profile, bp=prof.load_blueprint("letter"), lang=kw.pop("lang", "en"), job=kw.pop("job", None),
                                                   company="Nimbus", role="DevOps Intern", **kw), prof.load_blueprint("letter").paragraphs[2])


def codes(flags):
    return {f.code for f in flags}


def test_grounding_flags_invented_tools_numbers_and_units(profile):
    gc = gctx(profile)
    ok = "At Acme Tech I containerised 4 legacy services with Docker; build time dropped from 14 to 6 minutes."
    assert grounding.check_paragraph(ok, gc) == []
    bad = "I deployed everything with Jenkins and Ansible and cut build time from 14 minutes to six hours, serving 5000 users."
    c = codes(grounding.check_paragraph(bad, gc))
    assert {"tech_unverified", "unit_mismatch", "number_unverified"} <= c


def test_grounding_job_terms_are_info_not_warning(profile):
    gc = gctx(profile, job_terms={"jenkins"}) if False else gctx(profile)
    gc.job_terms = {"jenkins"}
    flags = grounding.check_paragraph("I would enjoy working with Jenkins pipelines at your company.", gc)
    assert [f.level for f in flags if f.code.startswith("tech")] == ["info"]
    gc.exempt_terms = {"jenkins"}
    assert not [f for f in grounding.check_paragraph("I would enjoy working with Jenkins pipelines at your company.", gc) if f.code.startswith("tech")]


def test_grounding_language_avoid_placeholders_example_leak_length(profile):
    gc = gctx(profile)
    assert "language" in codes(grounding.check_paragraph("Je souhaite rejoindre votre équipe parce que votre plateforme est très intéressante pour moi.", gc))
    assert "avoid_phrase" in codes(grounding.check_paragraph("I am a passionate student who wants to learn from your experienced engineers and teams.", gc))
    assert "placeholder" in codes(grounding.check_paragraph("I would like to join [Company Name] as a backend intern next year.", gc))
    assert "example_leak" in codes(grounding.check_paragraph("What I like about Orbit Labs is that releases became routine for the whole team there.", gc))
    assert "too_long" in codes(grounding.check_paragraph("word " * 200, gc)) and "too_short" in codes(grounding.check_paragraph("Hi there.", gc))


def test_quantities():
    q = grounding.quantities("de 14 à 6 minutes, 99,9 % de disponibilité, quatre jours, 1 200 pages")
    assert {("14", "minute"), ("6", "minute"), ("99.9", "percent"), ("4", "day")} <= q


# ---------------------------------------------------------------------------- writing with retry
def wi(profile, **kw):
    return letter.WriteInput(profile=profile, bp=prof.load_blueprint("letter"), lang=kw.pop("lang", "en"), company="Nimbus", role="DevOps Intern",
                             evidence=[], **kw)


def test_write_paragraph_retries_once_on_hard_problem_and_keeps_the_better_text(profile):
    bp = prof.load_blueprint("letter")
    spec = bp.paragraphs[2]  # fit
    bad = "I am a passionate engineer. I built things with Jenkins for 5000 users and loved it a lot during my time at school."
    good = "At Acme Tech I containerised four services with Docker and wrote GitHub Actions pipelines that made the builds faster and more reliable."
    llm = Scripted(bad, good)
    p = letter.write_paragraph(llm, wi(profile), spec, [])
    assert p.text == good and len(llm.calls) == 2
    corr = llm.calls[1]["history"][-1]["content"]
    assert "Jenkins" in corr and "passionate" in corr  # the correction names the exact problems
    assert not [f for f in p.flags if f.level in ("warn", "error")]


def test_write_paragraph_truncates_and_rechecks_flags(profile):
    spec = prof.load_blueprint("letter").paragraphs[0]  # intro, 60 words
    long = ("I would like to join Nimbus as a DevOps intern because the platform is interesting. " * 3 +
            "Later I used Docker and Kubernetes a lot. " * 10)
    p = letter.write_paragraph(Scripted(long, long), wi(profile), spec, [])
    assert len(p.text.split()) <= 60 and any(f.code == "shortened" for f in p.flags)


def test_write_paragraph_propagates_llm_errors(profile):
    with pytest.raises(LLMError):
        letter.write_paragraph(Scripted(LLMError("down", kind="unreachable")), wi(profile), prof.load_blueprint("letter").paragraphs[0], [])


def test_prompts_contain_guardrails(profile):
    bp = prof.load_blueprint("letter")
    sys_en = prompts.writer_system(bp, profile, "en", "letter")
    assert "Never invent" in sys_en and "passionate" in sys_en and "Write in English" in sys_en
    assert "avoid gendered words" in prompts.writer_system(bp, profile, "fr", "letter")
    profile.identity.gender = "f"
    assert "feminine" in prompts.writer_system(bp, profile, "fr", "letter")


# ---------------------------------------------------------------------------- your own past letters as voice reference
EN_LETTER = ("Dear Hiring Team,\n\nI am writing because your team builds the kind of infrastructure I want to learn from. "
             "Last summer I wrote the deployment scripts for a small startup and I enjoyed every incident review.\n\n"
             "I like short sentences and concrete examples, and I prefer to explain what I did rather than what I am. " * 2 + "\n\nBest regards,\nSam")


def test_style_examples_are_loaded_by_language(data_dir):
    ex = data_dir / "examples"
    (ex / "my_en.txt").write_text(EN_LETTER, encoding="utf-8")
    (ex / "too_short.txt").write_text("tiny", encoding="utf-8")
    assert "short sentences and concrete examples" in prompts.load_style_examples("en")
    assert prompts.load_style_examples("fr") == ""  # an English letter is not a voice reference for a French one
    assert "README" not in prompts.load_style_examples("en")
    spec = prof.load_blueprint("letter").paragraphs[0]
    assert "VOICE REFERENCE" in prompts.paragraph_task(spec, "en", [], 60)
    (ex / "my_en.txt").unlink()
    assert "VOICE REFERENCE" not in prompts.paragraph_task(spec, "en", [], 60)


def test_style_examples_are_capped(data_dir):
    (data_dir / "examples" / "long.txt").write_text(("A paragraph of my letter with some words in it.\n\n" * 200), encoding="utf-8")
    assert len(prompts.load_style_examples("en", max_chars=1000)) <= 1100


# ---------------------------------------------------------------------------- regressions from a REAL (tiny) model run
def email_ctx(profile, **kw):
    bp = prof.load_blueprint("email")
    wi_ = letter.WriteInput(profile=profile, bp=bp, lang="en", company="Orbit Labs", role="", note="I read your post about local LLMs on edge devices.", **kw)
    return letter._grounding_ctx(wi_, bp.paragraphs[0]), wi_, bp


def test_style_example_facts_leaking_into_another_company_are_caught(profile):
    gc, *_ = email_ctx(profile)
    leak = "I have been following Orbit Labs' real-time route optimisation and built similar local LLMs for course assistants."
    flags = grounding.check_paragraph(leak, gc)
    assert any(f.code == "example_leak" and "route optimisation" in f.message for f in flags)
    ok = "Your post about running local LLMs on edge devices caught my eye: I built a small course assistant with Ollama and pgvector."
    assert not [f for f in grounding.check_paragraph(ok, gc) if f.code == "example_leak"]


def test_repeated_sentences_detected_and_removed():
    first = "I built a CI/CD pipeline with GitHub Actions and Docker for a team project."
    second = "I built a CI/CD pipeline with GitHub Actions and Docker for a team project! I also wrote Prometheus alert rules for five incidents."
    assert grounding.repeated_sentences(second, [first])
    assert grounding.drop_repeats(second, [first]) == "I also wrote Prometheus alert rules for five incidents."
    assert grounding.drop_repeats(first, [first]) == first  # never empties a paragraph
    assert grounding.repeated_sentences("A totally different sentence about something else entirely.", [first]) == []


def test_write_paragraph_removes_a_repeated_sentence(profile):
    bp = prof.load_blueprint("letter")
    prev = "At Acme Tech I containerised four services with Docker and wrote GitHub Actions pipelines."
    new = prev + " I also built Grafana dashboards for an API used by three hundred colleagues, which made incidents easier to follow."
    p = letter.write_paragraph(Scripted(new, new), wi(profile), bp.paragraphs[2], [prev])
    assert p.text.startswith("I also built Grafana") and any(f.code == "dedup" for f in p.flags)


def test_article_placeholder_for_internship_label(profile):
    en = letter.placeholders(profile, "en")
    assert en["internship_label_a"] == "an end-of-studies internship"
    fr = letter.placeholders(profile, "fr")
    assert fr["internship_label_a"] == "un stage de fin d'études"
    profile.availability.internship_label = {"en": "summer internship"}
    assert letter.placeholders(profile, "en")["internship_label_a"] == "a summer internship"
    bp = prof.load_blueprint("email")
    wi_ = letter.WriteInput(profile=profile, bp=bp, lang="en", company="X")
    assert "available for a summer internship of 4 to 6 months" in letter.fixed_paragraph(wi_, bp.paragraphs[2]).text


def test_personal_note_must_be_used_by_the_opening_paragraph(profile):
    gc, wi_, bp = email_ctx(profile)
    gc.use_note = True
    generic = "As a final-year student I built a small course assistant with Ollama and pgvector, and I would like to join your team."
    flags = grounding.check_paragraph(generic, gc)
    assert any(f.code == "note_ignored" for f in flags)
    uses = "Your post about running local LLMs on edge devices resonated with me: I built a small course assistant with Ollama and pgvector."
    assert not [f for f in grounding.check_paragraph(uses, gc) if f.code == "note_ignored"]
    gc.lang = "fr"  # a French letter may translate the English note: not checkable, so never flagged
    assert not [f for f in grounding.check_paragraph("Étudiant en dernière année, j'ai construit un assistant de cours avec Ollama et pgvector.", gc) if f.code == "note_ignored"]


def test_note_triggers_a_rewrite_only_for_the_first_generated_paragraph(profile):
    bp = prof.load_blueprint("email")
    wi_ = letter.WriteInput(profile=profile, bp=bp, lang="en", company="Orbit Labs", note="I read your post about running local LLMs on edge devices.", evidence=[])
    generic = "As a final-year student I am looking for a hands-on role where I can apply my backend and infrastructure skills."
    uses = "Your post about running local LLMs on edge devices stood out to me, and it matches what I want to build as a final-year student."
    llm = Scripted(generic, uses)
    p = letter.write_paragraph(llm, wi_, bp.paragraphs[0], [], first=True)
    assert p.text == uses and len(llm.calls) == 2 and "note" in llm.calls[1]["history"][-1]["content"].lower()
    llm2 = Scripted(generic)  # second paragraph: the note is not required again
    p2 = letter.write_paragraph(llm2, wi_, bp.paragraphs[1], [uses], first=False)
    assert len(llm2.calls) == 1 and not any(f.code == "note_ignored" for f in p2.flags)
