"""Shared test setup."""

from __future__ import annotations

import gc
from collections.abc import Iterator
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


@pytest.fixture(scope="session", autouse=True)
def main_thread_garbage_collection() -> Iterator[None]:
    """Run cyclic garbage collection only on the main thread, as the app does.

    Background source workers allocate while earlier tests may have left Qt
    objects in reference cycles. If a worker triggers an automatic collection,
    those objects are destroyed off the GUI thread, which can deadlock. Tests
    therefore collect between tests on the main thread instead (the UI tests
    additionally run the application's GUI-thread collector).
    """

    was_enabled = gc.isenabled()
    gc.disable()
    yield
    gc.collect()
    if was_enabled:
        gc.enable()


@pytest.fixture(autouse=True)
def collect_garbage_after_each_test() -> Iterator[None]:
    """Collect each generation that passed its threshold, on the main thread."""

    yield
    for generation, (count, threshold) in enumerate(
        zip(gc.get_count(), gc.get_threshold(), strict=True)
    ):
        if threshold <= 0 or count <= threshold:
            break
        gc.collect(generation)
