"""Reading, validating and saving the user-editable files (profile, blueprints, LaTeX templates, settings)."""
from __future__ import annotations

import re
import shutil

from pydantic import ValidationError

from . import config, cv, latex, letter, retrieve
from . import profile as prof
from .models import DocDraft, ParagraphDraft, Profile
from .textutil import localized, unknown_placeholders

_SAL_KEYS = {"full_name": "", "first_name": "", "last_name": ""}


def info(key: str) -> dict:
    _, syntax, _, label = config.key_info(key)
    return {"key": key, "label": label, "syntax": syntax}


def read(key: str) -> dict:
    config.ensure_data_dir()
    path = config.user_path(key)
    text = path.read_text(encoding="utf-8")
    try:
        default = config.default_path(key).read_text(encoding="utf-8")
    except FileNotFoundError:  # a form you created yourself has no shipped default
        default = None
    return {**info(key), "text": text, "is_default": text == default, "has_default": default is not None, "path": str(path)}


def _sample_profile() -> Profile:
    try:
        return prof.load_profile()
    except Exception:
        return prof.parse_profile(config.default_path("profile").read_text(encoding="utf-8"))


def _check_blueprint(bp, kind: str) -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    if bp.kind != kind:
        errors.append(f"`kind` must be “{kind}” in this file (found “{bp.kind}”).")
    profile = _sample_profile()
    for lang in ("en", "fr"):
        ph = letter.placeholders(profile, lang, company="X", role="X", role_focus="X", contact_name="X")
        ph.update(_SAL_KEYS)
        texts = [("subject", localized(bp.subject, lang)), ("closing", localized(bp.closing, lang))]
        texts += [(f"signature line {i + 1}", l) for i, l in enumerate(bp.signature_lines)]
        texts += [(f"paragraph “{p.id}”", localized(p.text, lang)) for p in bp.paragraphs if p.kind == "fixed"]
        for lang_sal in bp.salutations.get(lang, {}).values():
            texts.append(("salutation", lang_sal))
        for where, t in texts:
            for name in unknown_placeholders(t, ph):
                msg = f"Unknown placeholder {{{name}}} in {where} ({lang})."
                if msg not in warnings:
                    warnings.append(msg)
    for p in bp.paragraphs:
        if p.kind == "generated" and not (localized(p.goal, "en") or localized(p.goal, "fr")):
            warnings.append(f"Paragraph “{p.id}” is generated but has no `goal`: the model will have little to go on.")
        if p.kind == "fixed" and not (localized(p.text, "en") or localized(p.text, "fr")):
            errors.append(f"Paragraph “{p.id}” is fixed but has no `text`.")
    return errors, warnings


def _trial_render(key: str, text: str) -> list[str]:
    """Render the template with the current profile so typos in variable names are caught at save time."""
    profile = _sample_profile()
    errors: list[str] = []
    for lang in ("en", "fr"):
        try:
            if key == "cv_template":
                jt = retrieve.JobTerms()
                plan = retrieve.plan_cv(profile, jt, lang, config.load_settings().cv)
                latex.render(text, {**cv.build_context(profile, plan, lang), "babel": "english"})
            else:
                doc = DocDraft(kind="letter", subject="Subject", salutation="Hello,",
                               paragraphs=[ParagraphDraft(id="p", text="Text")], closing="Regards,", signature=["Name"],
                               recipient=["Company"], place_date="City, 1 January 2027")
                latex.render(text, letter.letter_context(profile, doc, lang, "english"))
        except latex.TemplateError as exc:
            errors.append(str(exc))
            break
    return errors


def validate(key: str, text: str) -> tuple[list[str], list[str]]:
    """-> (errors, warnings). Errors block saving."""
    errors: list[str] = []
    warnings: list[str] = []
    try:
        if key == "profile":
            p = prof.parse_profile(text)
            if p.example:
                warnings.append("This is still the sample profile (`example: true`). Replace it with your own data, then remove that line.")
            if not p.projects and not p.experience:
                warnings.append("No projects or experience yet: letters will be generic.")
            for proj in p.projects:
                if not proj.bullets:
                    warnings.append(f"Project “{proj.name}” has no bullets (concrete results make better letters).")
        elif key == "letter_blueprint" or (key.startswith("bp:letter")):
            errors, warnings = _check_blueprint(prof.parse_blueprint(text), "letter")
        elif key == "email_blueprint" or (key.startswith("bp:email")):
            errors, warnings = _check_blueprint(prof.parse_blueprint(text), "email")
        elif key in ("cv_template", "letter_template"):
            latex.validate_template(text)
            if "\\documentclass" not in text:
                errors.append("A LaTeX template must contain \\documentclass.")
            elif "\\begin{document}" not in text:
                errors.append("A LaTeX template must contain \\begin{document}.")
            else:
                errors += _trial_render(key, text)
        elif key == "config":
            raw = config.safe_load_yaml(text) or {}
            if not isinstance(raw, dict):
                errors.append("The settings file must be a YAML mapping.")
            else:
                try:
                    config.Settings.model_validate(raw)
                except ValidationError as exc:
                    errors += [f"{' → '.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()]
        else:
            errors.append("Unknown file.")
    except prof.ConfigFileError as exc:
        errors += exc.errors
    except latex.TemplateError as exc:
        errors.append(str(exc))
    except Exception as exc:  # YAML syntax errors raised outside ConfigFileError
        errors.append(f"{type(exc).__name__}: {exc}")
    return errors, warnings


def write(key: str, text: str) -> tuple[bool, list[str], list[str]]:
    errors, warnings = validate(key, text)
    if errors:
        return False, errors, warnings
    path = config.user_path(key)
    if path.exists():
        shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    tmp.replace(path)
    if key == "config":
        config.reload_settings()
        from . import llm

        llm.reset_cache()
    return True, [], warnings


def duplicate_blueprint(source_file: str, new_name: str) -> str:
    """Copy a form under a new name (``letter-<slug>.yaml``); returns the new file name."""
    from .textutil import slugify

    if not config.is_blueprint_file(source_file) or not (config.DATA_DIR / "blueprints" / source_file).exists():
        raise ValueError("Unknown form.")
    kind = "letter" if source_file.startswith("letter") else "email"
    label = " ".join((new_name or "").split())[:60]
    if not label:
        raise ValueError("Please give the new form a name.")
    base = f"{kind}-{slugify(label, 30)}"
    name, i = f"{base}.yaml", 2
    while (config.DATA_DIR / "blueprints" / name).exists():
        name, i = f"{base}-{i}.yaml", i + 1
    text = (config.DATA_DIR / "blueprints" / source_file).read_text(encoding="utf-8")
    safe = label.replace('"', "'")
    text, n = re.subn(r"(?m)^name:.*$", f'name: "{safe}"', text, count=1)
    if not n:
        text = f'name: "{safe}"\n' + text
    (config.DATA_DIR / "blueprints" / name).write_text(text, encoding="utf-8")
    return name


def reset(key: str) -> None:
    path = config.user_path(key)
    if path.exists():
        shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
    shutil.copyfile(config.default_path(key), path)
    if key == "config":
        config.reload_settings()


def set_llm_model(name: str) -> None:
    """Change llm.model in config.yaml while preserving comments."""
    name = name.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:/\-]{1,80}", name):
        raise ValueError("Invalid model name.")
    path = config.user_path("config")
    lines = path.read_text(encoding="utf-8").splitlines()
    in_llm, done = False, False
    for i, ln in enumerate(lines):
        if re.match(r"^llm:\s*(#.*)?$", ln):
            in_llm = True
            continue
        if in_llm and re.match(r"^\S", ln):  # next top-level key
            break
        if in_llm and re.match(r"^\s+model:\s", ln):
            comment = re.search(r"\s+#.*$", ln)
            lines[i] = re.sub(r"model:\s.*$", f"model: {name}", ln) if not comment else f"  model: {name}{comment.group(0)}"
            done = True
            break
    if not done:
        lines += ["", "llm:", f"  model: {name}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    config.reload_settings()
    from . import llm

    llm.reset_cache()
