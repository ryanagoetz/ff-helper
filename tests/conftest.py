from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Keep tests from touching the real ~/.ff-helper directory or reading its knobs."""
    monkeypatch.setenv("FF_HELPER_HOME", str(tmp_path / "state"))
    # A developer's own FF_MC_ROLLOUTS must not silently switch the suite to Monte
    # Carlo; tests that want the simulator set Assistant.mc_rollouts explicitly.
    monkeypatch.delenv("FF_MC_ROLLOUTS", raising=False)
    yield
    os.environ.pop("FF_HELPER_HOME", None)


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def fixture():
    return load_fixture
