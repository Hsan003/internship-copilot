from datetime import date

import pytest

from copilot import fit, techvocab
from copilot.models import Job
from copilot.textutil import (clean_job_text, clean_paragraph, date_long, detect_language, duration_text, html_to_text,
                              localized, norm_match, safe_format, truncate_words, unknown_placeholders, word_count)


# ----------------------------------------------------------------------------- textutil
def test_detect_language():
    assert detect_language("We are looking for a motivated student to join our platform team and help us build reliable services for customers.") == "en"
    assert detect_language("Nous recherchons un étudiant motivé pour rejoindre notre équipe plateforme et nous aider à construire des services fiables pour nos clients.") == "fr"
    assert detect_language("Wir suchen einen motivierten Studenten, der unser Plattform-Team unterstützt und zuverlässige Dienste für unsere Kunden entwickelt.") == "de"
    assert detect_language("ok") == "unknown"


def test_safe_format_leaves_unknown_placeholders():
    assert safe_format("Hello {name}, {unknown}!", {"name": "Sam"}) == "Hello Sam, {unknown}!"
    assert unknown_placeholders("{a} {b} {a}", {"a": 1}) == ["b"]


def test_truncate_words_at_sentence_boundary():
    text = "First sentence is here. Second sentence follows right after. Third one is the long extra tail we cut."
    out = truncate_words(text, 9)
    assert out == "First sentence is here. Second sentence follows right after."[:len(out)] or out.endswith(".")
    assert word_count(out) <= 9 and out.endswith(".")
    assert truncate_words("short text", 10) == "short text"


def test_dates_and_durations():
    assert date_long(date(2027, 2, 1), "fr") == "1er février 2027"
    assert date_long(date(2027, 2, 15), "en") == "15 February 2027"
    assert duration_text(4, 6, "fr") == "4 à 6 mois" and duration_text(6, 6, "en") == "6 months"


def test_localized_fallbacks():
    assert localized("x", "fr") == "x"
    assert localized({"en": "a", "fr": "b"}, "fr") == "b"
    assert localized({"en": "a"}, "fr") == "a"
    assert localized(None, "fr") == ""


def test_norm_match_ignores_accents_case_punctuation():
    assert norm_match("L’Équipe  « Plateforme »!") == norm_match("l'equipe plateforme")


def test_html_to_text_keeps_lists_and_drops_scripts():
    html = "<div><script>evil()</script><h2>Missions</h2><ul><li>Docker</li><li>Terraform</li></ul><p>Fin<br>ligne</p></div>"
    t = html_to_text(html)
    assert "evil" not in t and "- Docker" in t and "- Terraform" in t and "Fin\nligne" in t


def test_clean_job_text_cuts_similar_jobs_and_noise():
    body = "About the job\n" + "We build things with Python. " * 30 + "\nShow more\nSet alert for similar jobs\nOther job 1\nOther job 2"
    out = clean_job_text("Skip to main content\nAcme · Paris\n" + body)
    assert "Other job" not in out and "Skip to main content" not in out and "Show more" not in out
    assert out.startswith("Acme") and "About the job" in out


def test_clean_paragraph_strips_greeting_markdown_and_quotes():
    raw = "Dear Hiring Team,\n\n**I** would love to join.\n\nBest regards,\nAlex"
    assert clean_paragraph(raw) == "I would love to join."
    assert clean_paragraph('"Quoted text here."') == "Quoted text here."
    assert clean_paragraph("<think>hidden</think>Visible") == "Visible"


# ----------------------------------------------------------------------------- techvocab
def terms(text):
    return set(techvocab.find_terms(text))


def test_vocab_basic_and_aliases():
    t = terms("Docker, Kubernetes (k8s), Terraform, GitLab CI, intégration continue, Prometheus/Grafana")
    assert {"docker", "kubernetes", "terraform", "gitlab ci", "ci/cd", "prometheus", "grafana"} <= t


@pytest.mark.parametrize("text,absent", [
    ("Vue d'ensemble du projet", "vue"),
    ("Spring 2027 internship", "spring"),
    ("We react quickly to incidents", "react"),
    ("The rest of the team", "rest"),
    ("You will go to meetings", "go"),
    ("Contact Claude Martin", "llm"),
])
def test_vocab_ambiguous_words_not_matched(text, absent):
    assert absent not in terms(text)


@pytest.mark.parametrize("text,present", [
    ("Python, Go, Rust", "go"), ("Python, R, SQL", "r"), ("C/C++ embedded", "c"), ("Go developer", "go"),
    ("Vue.js or React", "vue"), ("Spring Boot", "spring"), ("RAG with LLMs", "rag"), ("Künstliche Intelligenz", "ai"),
])
def test_vocab_ambiguous_words_matched_in_context(text, present):
    assert present in terms(text)


def test_infer_domains():
    c = techvocab.find_terms("Kubernetes Terraform Argo CD Prometheus Grafana SRE incidents")
    assert techvocab.infer_domains(c)[0] in ("devops", "sre")


# ----------------------------------------------------------------------------- fit
def J(text, **kw):
    return Job(title=kw.pop("title", "Stage"), description=text, language=kw.pop("language", "fr"), **kw)


def by_key(items):
    return {i.key: i for i in items}


def test_fit_all_good(profile):
    f = by_key(fit.check_fit(J("Stage de fin d'études, durée : 6 mois, début février 2027 à Paris. Anglais courant."), profile, today=date(2026, 10, 1)))
    assert f["type"].status == "ok" and f["duration"].status == "ok" and f["start"].status == "ok" and f["location"].status == "ok"


@pytest.mark.parametrize("text,status", [
    ("Stage de 3 mois", "warn"), ("Internship of 12 months", "warn"), ("Stage de 4 à 6 mois", "ok"), ("Internship, 6 months", "ok"),
    ("Praktikum für 5 Monate", "ok"), ("stage de six mois", "ok"), ("Duration: between 4 and 6 months", "ok"),
    ("Minimum 8 months", "warn"), ("up to 3 months", "warn"),
])
def test_fit_duration(profile, text, status):
    assert by_key(fit.check_fit(J(text), profile))["duration"].status == status


def test_fit_duration_ignores_experience_and_ago(profile):
    f = by_key(fit.check_fit(J("Stage. 12 mois d'expérience requis. Publié il y a 2 mois."), profile))
    assert f["duration"].status == "unknown"


@pytest.mark.parametrize("text,status", [
    ("Début : février 2027", "ok"), ("Starting March 2027", "ok"), ("à partir de septembre 2027", "warn"),
    ("Start: ASAP", "warn"), ("Poste à pourvoir dès que possible", "warn"), ("Beginn: Februar 2027", "ok"),
    ("Candidature avant le 15 novembre 2026", "unknown"),
])
def test_fit_start(profile, text, status):
    assert by_key(fit.check_fit(J(text), profile))["start"].status == status


@pytest.mark.parametrize("text,status", [
    ("Alternance 12 mois", "bad"), ("Contrat d'apprentissage", "bad"), ("Werkstudent (m/w/d)", "bad"),
    ("Stage de fin d'études", "ok"), ("Poste en CDI", "warn"), ("Stage ou alternance", "warn"),
])
def test_fit_type(profile, text, status):
    assert by_key(fit.check_fit(J(text, title="Dev"), profile))["type"].status == status


def test_fit_language_requirements(profile):
    # sample profile: English C1, French C1, German A2
    assert by_key(fit.check_fit(J("Stage. Allemand courant exigé.", language="fr"), profile))["language"].status in ("warn", "bad")
    assert by_key(fit.check_fit(J("Internship. Fluent English required.", language="en"), profile))["language"].status == "ok"
    german_post = J("Praktikum. Sie sprechen fließend Deutsch und gutes Englisch. " * 3, language="de")
    assert by_key(fit.check_fit(german_post, profile))["language"].status in ("warn", "bad")


def test_fit_freshness(profile):
    today = date(2026, 10, 1)
    assert by_key(fit.check_fit(J("Stage", valid_through="2026-09-01"), profile, today))["freshness"].status == "bad"
    assert by_key(fit.check_fit(J("Stage", date_posted="2026-06-01"), profile, today))["freshness"].status == "warn"
    assert "freshness" not in by_key(fit.check_fit(J("Stage", date_posted="2026-09-20"), profile, today))


def test_fit_work_auth_hint(profile):
    assert "work_auth" in by_key(fit.check_fit(J("Stage. You must have the right to work in the EU."), profile))


# ---- regressions found on real postings (Cloudflare / GitLab / Datadog pages)
FILLER = " We build reliable infrastructure for the internet and care about quality." * 12


def test_fit_type_senior_title_beats_a_stray_intern_mention(profile):
    job = J("Engineering Manager role. " + FILLER + " Our interns present their work every summer.", title="Engineering Manager, Platform", language="en")
    t = by_key(fit.check_fit(job, profile))["type"]
    assert t.status == "bad" and "senior" in t.label.lower()
    assert by_key(fit.check_fit(J("Lead Internal Events Strategist. " + FILLER, title="Lead Internal Events Strategist"), profile))["type"].status == "bad"


def test_fit_type_single_mention_deep_in_text_is_only_a_warning(profile):
    job = J("Software Developer. " + FILLER + " Previous internship experience is valued.", title="Software Developer", language="en")
    assert by_key(fit.check_fit(job, profile))["type"].status == "warn"
    ok = J("Développeur backend. Type de contrat : stage de 6 mois." + FILLER, title="Développeur backend")
    assert by_key(fit.check_fit(ok, profile))["type"].status == "ok"
