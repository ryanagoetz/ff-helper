from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Keep tests from touching the real ~/.ff-helper directory."""
    monkeypatch.setenv("FF_HELPER_HOME", str(tmp_path / "state"))
    yield
    os.environ.pop("FF_HELPER_HOME", None)


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def fixture():
    return load_fixture
