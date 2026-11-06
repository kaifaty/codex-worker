"""Shared board test settings."""

import pytest


@pytest.fixture(autouse=True)
def no_inherited_data_home(monkeypatch):
    # TEAMSTER_HOME takes precedence over the temporary CODEX_TASK_HOME the tests set.
    monkeypatch.delenv("TEAMSTER_HOME", raising=False)
