"""Paths, user-editable files and application settings."""
from __future__ import annotations

import os
import re
import shutil
import threading
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

PKG_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PKG_DIR.parent
DEFAULTS_DIR = PKG_DIR / "defaults"
STATIC_DIR = PKG_DIR / "static"
DATA_DIR = Path(os.environ.get("COPILOT_DATA", PROJECT_DIR / "data")).resolve()
APPS_DIR = DATA_DIR / "applications"

# --------------------------------------------------------------------------- YAML (strings stay strings)
class _StrLoader(yaml.SafeLoader):
    """YAML loader that keeps scalars as text.

    Hand-written YAML is full of traps: ``period: 2025`` becomes an int, ``phone: +33612345678`` loses its plus sign,
    ``think: off`` becomes False. Keeping every plain scalar as a string lets pydantic convert to the *declared* type
    (int/float/bool/date) only where a field really needs it.
    """


_DROP = {"tag:yaml.org,2002:int", "tag:yaml.org,2002:float", "tag:yaml.org,2002:bool", "tag:yaml.org,2002:timestamp"}
_StrLoader.yaml_implicit_resolvers = {
    ch: [(tag, rx) for tag, rx in resolvers if tag not in _DROP]
    for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def safe_load_yaml(text: str) -> Any:
    return yaml.load(text, Loader=_StrLoader)  # noqa: S506 - SafeLoader subclass


# key -> (path inside DATA_DIR, editor syntax, default file inside DEFAULTS_DIR, human label)
EDITABLE: dict[str, tuple[str, str, str, str]] = {
    "profile": ("profile.yaml", "yaml", "profile.example.yaml", "Master profile (your facts)"),
    "letter_blueprint": ("blueprints/letter.yaml", "yaml", "blueprints/letter.yaml", "Cover-letter blueprint"),
    "email_blueprint": ("blueprints/email.yaml", "yaml", "blueprints/email.yaml", "Spontaneous e-mail blueprint"),
    "cv_template": ("templates/cv.tex.j2", "latex", "templates/cv.tex.j2", "CV template (LaTeX)"),
    "letter_template": ("templates/letter.tex.j2", "latex", "templates/letter.tex.j2", "Letter template (LaTeX)"),
    "config": ("config.yaml", "yaml", "config.yaml", "Settings"),
}


# --------------------------------------------------------------------------- settings
class LLMSettings(BaseModel):
    backend: str = "ollama"  # ollama | fake (demo mode, no model needed)
    base_url: str = "http://localhost:11434"
    model: str = "gemma4:e4b"
    num_ctx: int = 6144
    temperature_extract: float = 0.1
    temperature_write: float = 0.6
    keep_alive: str = "30m"
    timeout_s: int = 900
    think: str = "off"  # off | model-default  (thinking is slow and not needed here)


class LatexSettings(BaseModel):
    engine: str = "auto"  # auto | pdflatex | xelatex | lualatex | tectonic
    timeout_s: int = 120
    max_cv_pages: int = 1


class CVSettings(BaseModel):
    max_projects: int = 3
    max_bullets_per_item: int = 3
    max_experience_items: int = 4


class FetchSettings(BaseModel):
    timeout_s: int = 20
    use_browser: str = "auto"  # auto | never | always  (needs: pip install playwright && playwright install chromium)
    max_job_chars: int = 9000
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )


class TrackerSettings(BaseModel):
    follow_up_days: int = 10


class Settings(BaseModel):
    llm: LLMSettings = Field(default_factory=LLMSettings)
    latex: LatexSettings = Field(default_factory=LatexSettings)
    cv: CVSettings = Field(default_factory=CVSettings)
    fetch: FetchSettings = Field(default_factory=FetchSettings)
    tracker: TrackerSettings = Field(default_factory=TrackerSettings)


_lock = threading.Lock()
_settings: Settings | None = None
config_error: str = ""  # non-empty when config.yaml is broken (defaults are used instead)


_BP_FILE = re.compile(r"^(letter|email)[A-Za-z0-9_\-]*\.yaml$")


def is_blueprint_file(name: str) -> bool:
    return bool(_BP_FILE.match(name or ""))


def blueprint_files(kind: str) -> list[str]:
    """Blueprint ("form") files of a kind in the data folder; the main one first."""
    folder = DATA_DIR / "blueprints"
    names = sorted(f.name for f in folder.glob(f"{kind}*.yaml") if is_blueprint_file(f.name)) if folder.is_dir() else []
    main = f"{kind}.yaml"
    return ([main] if main in names else []) + [n for n in names if n != main]


def valid_key(key: str) -> bool:
    return key in EDITABLE or (key.startswith("bp:") and is_blueprint_file(key[3:]))


def key_info(key: str) -> tuple[str, str, str, str]:
    """(relative path, syntax, default relative path or '', label) for fixed keys and dynamic ``bp:<file>`` keys."""
    if key in EDITABLE:
        rel, syntax, default, label = EDITABLE[key]
        return rel, syntax, default, label
    if key.startswith("bp:") and is_blueprint_file(key[3:]):
        name = key[3:]
        has_default = (DEFAULTS_DIR / "blueprints" / name).exists()
        kind = "letter" if name.startswith("letter") else "e-mail"
        return f"blueprints/{name}", "yaml", f"blueprints/{name}" if has_default else "", f"Form ({kind}): {name}"
    raise KeyError(key)


def user_path(key: str) -> Path:
    return DATA_DIR / key_info(key)[0]


def default_path(key: str) -> Path:
    rel = key_info(key)[2]
    if not rel:
        raise FileNotFoundError(f"{key} has no shipped default")
    return DEFAULTS_DIR / rel


def ensure_data_dir() -> None:
    """Create ./data and seed it with the default files (never overwrites)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    APPS_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "examples").mkdir(exist_ok=True)
    readme = DATA_DIR / "examples" / "README.txt"
    if not readme.exists():
        readme.write_text(
            "Drop 1-3 of your OWN best past cover letters here as .txt files.\n"
            "They are shown to the local model as style references (your voice), never copied verbatim.\n",
            encoding="utf-8",
        )
    assets = DATA_DIR / "templates" / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    note = assets / "README.txt"
    if not note.exists():
        note.write_text(
            "Put the extra files your own LaTeX CV needs here: custom .cls / .sty files, images, logos.\n"
            "This folder is added to LaTeX's search path for every build (no copying needed).\n"
            "Fonts used with XeLaTeX/LuaLaTeX (fontspec) should be installed on your system or referenced by absolute path.\n",
            encoding="utf-8",
        )
    for key in EDITABLE:
        target = user_path(key)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(default_path(key), target)
    for src in sorted((DEFAULTS_DIR / "blueprints").glob("*.yaml")):  # the shipped extra forms
        target = DATA_DIR / "blueprints" / src.name
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)


def _deep_update(base: dict, new: dict) -> dict:
    for k, v in (new or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v
    return base


def load_settings(force: bool = False) -> Settings:
    global _settings, config_error
    with _lock:
        if _settings is not None and not force:
            return _settings
        raw: dict[str, Any] = {}
        config_error = ""
        path = user_path("config")
        if path.exists():
            try:
                raw = safe_load_yaml(path.read_text(encoding="utf-8")) or {}
                if not isinstance(raw, dict):
                    raise ValueError("the file must be a YAML mapping")
            except (yaml.YAMLError, ValueError) as exc:
                raw, config_error = {}, f"config.yaml could not be read ({exc}); defaults are used."
        try:
            s = Settings.model_validate(_deep_update({}, raw))
        except ValidationError as exc:
            e = exc.errors()[0]
            config_error = f"config.yaml: {' → '.join(str(x) for x in e['loc'])}: {e['msg']}; defaults are used."
            s = Settings()
        # environment overrides (handy for demos / CI)
        if os.environ.get("COPILOT_LLM"):
            s.llm.backend = os.environ["COPILOT_LLM"]
        if os.environ.get("COPILOT_MODEL"):
            s.llm.model = os.environ["COPILOT_MODEL"]
        if os.environ.get("OLLAMA_HOST"):
            host = os.environ["OLLAMA_HOST"]
            s.llm.base_url = host if host.startswith("http") else f"http://{host}"
        _settings = s
        return s


def reload_settings() -> Settings:
    return load_settings(force=True)
