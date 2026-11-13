"""Cyclic garbage collection must only run on the GUI thread."""

import gc
import threading

from PySide6.QtWidgets import QApplication

from lbr_dashboard import app as app_module
from lbr_dashboard.app import GuiThreadGarbageCollector, install_garbage_collector


class _Cycle:
    def __init__(self) -> None:
        self.me = self


def test_install_disables_automatic_collection_once_per_application(qapp) -> None:
    collector = install_garbage_collector(qapp)
    assert not gc.isenabled()
    assert collector.parent() is qapp
    assert collector.timer.isActive()
    assert install_garbage_collector(qapp) is collector


def test_worker_thread_allocations_do_not_trigger_collection(qapp) -> None:
    install_garbage_collector(qapp)
    collecting_threads: list[int] = []

    def record(phase: str, info: dict) -> None:
        if phase == "start":
            collecting_threads.append(threading.get_ident())

    def allocate() -> None:
        # Far more container allocations than the generation 0 threshold.
        for _ in range(gc.get_threshold()[0] * 20):
            _Cycle()

    gc.callbacks.append(record)
    try:
        worker = threading.Thread(target=allocate)
        worker.start()
        worker.join()
    finally:
        gc.callbacks.remove(record)
    assert collecting_threads == []


def test_collect_runs_on_gui_thread_once_threshold_is_passed(qapp) -> None:
    collector = install_garbage_collector(qapp)
    gc.collect()
    assert collector.collect() == 0
    for _ in range(gc.get_threshold()[0] + 1):
        _Cycle()
    assert collector.collect() > 0


def test_stop_restores_automatic_collection_when_it_was_enabled(qapp) -> None:
    gc.enable()
    collector = GuiThreadGarbageCollector()
    try:
        assert not gc.isenabled()
        collector.stop()
        assert gc.isenabled()
        assert not collector.timer.isActive()
    finally:
        gc.disable()
        collector.deleteLater()


def test_main_installs_the_collector_before_building_the_window(qapp, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(app_module.signal, "signal", lambda *args: None)
    monkeypatch.setattr(
        app_module, "install_garbage_collector", lambda application: calls.append("collector")
    )

    class _Window:
        def show(self) -> None:
            calls.append("show")

    def create(*, preferences_store) -> _Window:
        assert preferences_store is not None
        calls.append("window")
        return _Window()

    monkeypatch.setattr(app_module, "create_main_window", create)
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    assert app_module.main([]) == 0
    assert calls == ["collector", "window", "show"]
