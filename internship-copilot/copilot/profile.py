"""Loading + validating the editable YAML files (profile, blueprints)."""
from __future__ import annotations

from typing import Any

import yaml
from pydantic import ValidationError

from . import config, techvocab
from .models import Blueprint, Profile
from .textutil import localized


class ConfigFileError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _fmt_errors(err: ValidationError) -> list[str]:
    out = []
    for e in err.errors():
        loc = " → ".join(str(x) for x in e["loc"])
        out.append(f"{loc}: {e['msg']}")
    return out


def parse_yaml(text: str) -> Any:
    try:
        return config.safe_load_yaml(text) or {}
    except yaml.YAMLError as exc:  # pragma: no cover - message formatting only
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        raise ConfigFileError([f"YAML syntax error{where}: {getattr(exc, 'problem', exc)}"]) from exc


def parse_profile(text: str) -> Profile:
    raw = parse_yaml(text)
    if not isinstance(raw, dict):
        raise ConfigFileError(["The profile must be a YAML mapping (key: value)."])
    try:
        return Profile.model_validate(raw).finalize()
    except ValidationError as exc:
        raise ConfigFileError(_fmt_errors(exc)) from exc


def parse_blueprint(text: str) -> Blueprint:
    raw = parse_yaml(text)
    if not isinstance(raw, dict):
        raise ConfigFileError(["The blueprint must be a YAML mapping (key: value)."])
    try:
        bp = Blueprint.model_validate(raw)
    except ValidationError as exc:
        raise ConfigFileError(_fmt_errors(exc)) from exc
    ids = [p.id for p in bp.paragraphs]
    if len(ids) != len(set(ids)):
        raise ConfigFileError(["Paragraph ids must be unique."])
    return bp


def load_profile() -> Profile:
    return parse_profile(config.user_path("profile").read_text(encoding="utf-8"))


def load_blueprint(kind: str, file: str | None = None) -> Blueprint:
    """The main form of a kind, or a specific form file from data/blueprints (e.g. ``letter-short.yaml``)."""
    if file and file not in (f"{kind}.yaml", "") and config.is_blueprint_file(file) and file.startswith(kind) \
            and (config.DATA_DIR / "blueprints" / file).exists():
        return parse_blueprint((config.DATA_DIR / "blueprints" / file).read_text(encoding="utf-8"))
    key = "letter_blueprint" if kind == "letter" else "email_blueprint"
    return parse_blueprint(config.user_path(key).read_text(encoding="utf-8"))


def list_blueprints(kind: str) -> list[dict]:
    """All forms of a kind with their paragraphs (invalid files are listed with their errors)."""
    out = []
    for name in config.blueprint_files(kind):
        try:
            bp = parse_blueprint((config.DATA_DIR / "blueprints" / name).read_text(encoding="utf-8"))
            out.append({"file": name, "name": bp.name, "paragraphs": [
                {"id": p.id, "kind": p.kind, "label": localized(p.label, "en") or p.id, "max_words": p.max_words} for p in bp.paragraphs]})
        except ConfigFileError as exc:
            out.append({"file": name, "name": f"{name} (invalid)", "paragraphs": [], "errors": exc.errors})
    return out


# ------------------------------------------------------------------ derived helpers
def profile_text_blobs(profile: Profile) -> list[str]:
    """Every free-text string in the profile (used for vocabulary / grounding checks)."""
    blobs: list[str] = []
    ident = profile.identity
    blobs += [ident.name, localized(ident.title, "en"), localized(ident.title, "fr")]
    for e in profile.education:
        blobs += [e.school, localized(e.degree, "en"), localized(e.degree, "fr")]
        blobs += [localized(d, "en") for d in e.details] + [localized(d, "fr") for d in e.details]
    for x in profile.experience:
        blobs += [x.org, localized(x.role, "en"), localized(x.role, "fr"), *x.tags]
        for b in x.bullets:
            blobs += [localized(b.text, "en"), localized(b.text, "fr"), *b.tags]
    for p in profile.projects:
        blobs += [p.name, localized(p.tagline, "en"), localized(p.tagline, "fr"), *p.stack, *p.tags]
        for b in p.bullets:
            blobs += [localized(b.text, "en"), localized(b.text, "fr"), *b.tags]
    for g in profile.skills:
        blobs += [localized(g.category, "en"), *g.items]
    for c in profile.certifications:
        blobs += [localized(c, "en"), localized(c, "fr")]
    for i in profile.interests:
        blobs += [localized(i, "en"), localized(i, "fr")]
    for l in profile.languages:
        blobs += [localized(l.name, "en"), localized(l.level, "en")]
    return [b for b in blobs if b]


def profile_vocab(profile: Profile) -> set[str]:
    """Canonical tech terms the candidate can legitimately claim."""
    vocab: set[str] = set()
    text = "\n".join(profile_text_blobs(profile))
    vocab.update(techvocab.find_terms(text).keys())
    return vocab


def profile_numbers(profile: Profile) -> set[str]:
    import re

    nums = set()
    for m in re.finditer(r"\d+(?:[.,]\d+)?", "\n".join(profile_text_blobs(profile))):
        nums.add(m.group(0).replace(",", "."))
    return nums


def is_example(profile: Profile) -> bool:
    return bool(profile.example)
