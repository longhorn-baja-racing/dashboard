"""Runtime acceptance checks for issue #17, independent of the widget layer."""

import subprocess
import sys
from pathlib import Path
from threading import Event

import pytest

from lbr_dashboard.core import (
    BackgroundService,
    FactoryError,
    Registry,
    ServiceEvent,
    ServiceFailure,
)


def test_core_imports_with_all_qt_imports_blocked() -> None:
    source_root = Path(__file__).parents[2] / "src"
    script = """
import importlib.abc
import importlib
import pkgutil
import sys

class NoQt(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'PySide6', 'PyQt6', 'PyQt5', 'pyqtgraph'}:
            raise AssertionError('Core attempted framework import: ' + fullname)

sys.meta_path.insert(0, NoQt())
sys.path.insert(0, sys.argv[1])
import lbr_dashboard.core
for module in pkgutil.walk_packages(lbr_dashboard.core.__path__, lbr_dashboard.core.__name__ + '.'):
    importlib.import_module(module.name)
frameworks = {'PySide6', 'PyQt6', 'PyQt5', 'pyqtgraph'}
assert not any(name.split('.')[0] in frameworks for name in sys.modules)
print('All core modules imported without Qt')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(source_root)], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "All core modules imported without Qt" in result.stdout


def test_worker_cancel_join_timeout_cleanup_and_owner_events() -> None:
    started, release, cleaned = Event(), Event(), Event()
    payload = {
        "signal_id": "custom.signal/999",
        "units": "furlongs/min",
        "timestamp": -12.345,
        "values": [1, 2],
    }

    def work(token, emit):
        try:
            emit(ServiceEvent("sample", payload))
            started.set()
            assert release.wait(timeout=2)
            token.raise_if_cancelled()
        finally:
            cleaned.set()

    service = BackgroundService(work)
    service.start()
    try:
        assert started.wait(timeout=1)
        assert not service.join(timeout=0)
        service.cancel()
        release.set()
        assert service.join(timeout=1)
        assert cleaned.is_set() and service.is_finished
        assert service.failure is None
        payload["values"].append(3)  # type: ignore[union-attr]
        events = service.drain_events()
        assert len(events) == 1
        assert events[0].payload == {
            "signal_id": "custom.signal/999",
            "units": "furlongs/min",
            "timestamp": -12.345,
            "values": (1, 2),
        }
        assert service.drain_events() == ()
        with pytest.raises(RuntimeError, match="already been started"):
            service.start()
    finally:
        service.cancel()
        release.set()
        assert service.join(timeout=2)


def test_worker_failure_runs_cleanup_and_reaches_owner() -> None:
    cleaned = Event()

    def work(token, emit):
        try:
            emit(ServiceEvent("ready", {"source": "custom"}))
            raise OSError("source disconnected")
        finally:
            cleaned.set()

    service = BackgroundService(work)
    service.start()
    assert service.join(timeout=1)
    assert cleaned.is_set() and service.is_finished
    assert service.failure == ServiceFailure("OSError", "source disconnected")
    assert service.drain_events() == (ServiceEvent("ready", {"source": "custom"}),)


def test_registries_are_independent_and_preserve_factory_arguments_and_causes() -> None:
    first = Registry[object]("first")
    second = Registry[object]("second")
    first.register("custom", lambda value, *, unit: (value, unit))
    second.register("custom", lambda: "independent")
    assert first.create("custom", 17.5, unit="custom-unit") == (17.5, "custom-unit")
    assert second.create("custom") == "independent"
    original = ValueError("provider configuration invalid")

    def broken():
        raise original

    first.register("broken", broken)
    with pytest.raises(FactoryError) as raised:
        first.create("broken")
    assert raised.value.__cause__ is original
    assert raised.value.cause is original
    assert raised.value.identifier == "broken"
    assert raised.value.registry_name == "first"
