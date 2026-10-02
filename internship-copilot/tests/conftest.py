import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["COPILOT_LLM"] = "fake"
os.environ["COPILOT_ALLOW_PRIVATE_URLS"] = "1"  # tests serve fixtures from 127.0.0.1

import pytest

from copilot import config

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    """Every test gets its own empty data folder seeded with the default files."""
    d = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", d)
    monkeypatch.setattr(config, "APPS_DIR", d / "applications")
    monkeypatch.setenv("COPILOT_LLM", "fake")
    config.ensure_data_dir()
    config.reload_settings()
    yield d
    config.reload_settings()


@pytest.fixture()
def profile():
    from copilot import profile as prof

    return prof.load_profile()


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")
