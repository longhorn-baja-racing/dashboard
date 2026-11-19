"""Shared test setup."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_log_directory(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Keep any application log written during a test out of the real user folders."""

    directory = tmp_path_factory.mktemp("logs")
    monkeypatch.setenv("LBR_DASHBOARD_LOG_DIR", str(directory))
    monkeypatch.delenv("LBR_DASHBOARD_LOG_LEVEL", raising=False)
    return directory
