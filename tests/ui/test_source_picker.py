"""Exercise the production picker, sources, values, failures and cleanup."""

from threading import Event, get_ident

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QFileDialog

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core import (
    CancellationToken,
    IteratorSource,
    SignalDescriptor,
    SignalSample,
    SignalSchema,
    SourceCapabilities,
    SourceEvent,
    SourceEventKind,
    SourceState,
)
from lbr_dashboard.source_controller import SourceController, SourceOption


@pytest.fixture
def picker(qtbot):
    window = create_main_window()
    # This fixture owns async close; pytest-qt deletes registered windows immediately.
    window.show()
    owner = window.source_owner
    assert isinstance(owner, SourceController)
    yield window, owner
    owner.disconnect()
    qtbot.waitUntil(lambda: owner.source is None and owner._preparing is None)
    window.close()
    window.deleteLater()


def wait_done(qtbot, owner):
    qtbot.waitUntil(
        lambda: (
            owner.source is not None
            and owner._preparing is None
            and not owner._releasing
            and owner.source.state is SourceState.ENDED
            and owner.source.events.stats.buffered == 0
        )
    )
    assert owner.session is not None
    return owner.session.snapshot()


def test_picker_csv_simulator_switch_and_original_values(picker, qtbot, tmp_path, monkeypatch):
    window, owner = picker
    path = tmp_path / "known.csv"
    path.write_text(
        "timestamp_ms,lab.temp,other,note\n0,0,7,ready\n10,,8,waiting\n20,5,9,end\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(path), ""))
    qtbot.mouseClick(window.source_picker.connect_button, Qt.MouseButton.LeftButton)
    snapshot = wait_done(qtbot, owner)
    old_source = owner.source
    assert [r.sample.value for r in snapshot.series["csv.column.1"]] == [0, None, 5]
    assert [r.sample.timestamp_ns for r in snapshot.series["csv.column.1"]] == [0, 10**7, 2 * 10**7]
    assert str(path) in window.source_picker.status.text()
    assert "ended" in window.source_picker.status.text()
    assert "unsupported" in window.source_picker.capabilities.text()
    assert "USB/serial is not available" in window.source_picker.controls.text()
    assert not window.source_picker.seek_button.isEnabled()
    assert not window.source_picker.rate_button.isEnabled()
    assert "does not support" in window.source_picker.seek_button.toolTip()
    plot = window.right_panel.plot_widget.getPlotItem()
    assert plot.listDataItems(), "Source samples never reached the plot"
    assert window.right_panel.table.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    xs, ys = plot.listDataItems()[0].getData()
    assert list(xs) == [0, 0.01, 0.02]
    assert ys[0] == 0 and ys[2] == 5
    assert str(ys[1]) == "nan"
    window.left_panel.list_widget.setCurrentRow(2)
    assert not plot.listDataItems()
    assert window.right_panel.table.item(0, 1).text() == "ready"
    for family, signal in (("process", "process.pressure"), ("motion", "motion.speed")):
        window.source_picker.choices.setCurrentIndex(window.source_picker.choices.findData(family))
        qtbot.mouseClick(window.source_picker.connect_button, Qt.MouseButton.LeftButton)
        snapshot = wait_done(qtbot, owner)
        assert old_source.state is SourceState.CLOSED
        assert snapshot.series[signal]
        old_source = owner.source
    original = snapshot
    qtbot.mouseClick(window.source_picker.retry_button, Qt.MouseButton.LeftButton)
    replay = wait_done(qtbot, owner)
    assert [r.sample for r in replay.series[signal]] == [r.sample for r in original.series[signal]]
    qtbot.mouseClick(window.source_picker.disconnect_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: owner.source is None)
    assert not window.source_picker.disconnect_button.isEnabled()
    assert window.right_panel.table.rowCount() > 0


def fake_option(*, mode="normal", release=None, cleaned=None, threads=None):
    descriptor = SignalDescriptor(
        "arbitrary.id",
        "Pressure",
        ("custom",),
        "numeric",
        "fake-source",
        canonical_unit="kPa",
        timestamp_domain="relative",
    )

    def prepare(token):
        if mode == "opening":
            while not token.is_cancelled:
                token.wait(0.001)
            token.raise_if_cancelled()

        def records(cancel):
            if threads is not None:
                threads.append(get_ident())
            try:
                yield SourceEvent(
                    SourceEventKind.RECORD,
                    "fake-source",
                    signal_id=descriptor.identifier,
                    sample=SignalSample(
                        2**60 + 1,
                        0,
                        source_id="fake-source",
                        unit="kPa",
                        timestamp_domain="relative",
                    ),
                )
                if mode == "error":
                    raise OSError("reader disconnected")
                if mode == "blocked":
                    assert release is not None
                    release.wait(3)
                if mode == "live":
                    cancel.wait(3)
                    cancel.raise_if_cancelled()
            finally:
                if cleaned is not None:
                    cleaned.set()

        return lambda: IteratorSource(
            "fake-source", SignalSchema(1, (descriptor,)), records, is_live=mode == "live"
        )

    return SourceOption("test-" + mode, "Test " + mode, prepare)


def test_fake_failure_is_visible_retry_and_registered_choice(picker, qtbot):
    window, owner = picker
    cleaned, threads = Event(), []
    option = fake_option(mode="error", cleaned=cleaned, threads=threads)
    owner.register(option)
    with pytest.raises(ValueError, match="already registered"):
        owner.register(option)
    window.source_picker.choices.setCurrentIndex(window.source_picker.choices.findData(option.key))
    qtbot.mouseClick(window.source_picker.connect_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: "reader disconnected" in window.source_picker.status.text())
    qtbot.waitUntil(lambda: owner.source.state is SourceState.FAILED)
    owner.poll()
    assert "Retry or choose" in window.source_picker.status.text()
    assert cleaned.is_set()
    assert threads == [threads[0]] and threads[0] != get_ident()
    assert owner.session.snapshot().series["arbitrary.id"][0].sample.timestamp_ns == 2**60 + 1
    qtbot.mouseClick(window.source_picker.retry_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: len(threads) == 2)


def test_csv_failure_preserves_previous_session_and_retry(picker, qtbot, tmp_path):
    window, owner = picker
    owner.choose("process")
    snapshot = wait_done(qtbot, owner)
    old_source = owner.source
    path = tmp_path / "missing.csv"
    owner.open_csv(path)
    qtbot.waitUntil(lambda: "Could not open source" in window.source_picker.status.text())
    assert owner.source is old_source
    assert owner.session.snapshot() == snapshot
    path.write_text("timestamp_ms,new\n0,42\n", encoding="utf-8")
    qtbot.mouseClick(window.source_picker.retry_button, Qt.MouseButton.LeftButton)
    assert wait_done(qtbot, owner).series["csv.column.1"][0].sample.value == 42


def test_cancel_dialog_and_slow_preparation(picker, qtbot, monkeypatch):
    window, owner = picker
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: ("", ""))
    owner.choose("csv")
    assert owner._preparing is None
    owner.request(fake_option(mode="opening"))
    assert not window.source_picker.connect_button.isEnabled()
    assert window.source_picker.disconnect_button.isEnabled()
    # Busy requests cannot overwrite the pending result.
    owner.choose("process")
    qtbot.mouseClick(window.source_picker.disconnect_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: owner._preparing is None)
    assert owner.source is None
    assert window.source_picker.connect_button.isEnabled()


def test_live_disconnect_and_close_release_worker(picker, qtbot):
    window, owner = picker
    cleaned = Event()
    owner.request(fake_option(mode="live", cleaned=cleaned))
    qtbot.waitUntil(lambda: owner.source is not None and owner.source.state is SourceState.RUNNING)
    source = owner.source
    window.close()
    qtbot.waitUntil(lambda: not window.isVisible())
    assert cleaned.is_set() and source.join(0) and source.state is SourceState.CLOSED
    assert not owner.timer.isActive()


def test_slow_cleanup_does_not_freeze_or_claim_released(picker, qtbot, monkeypatch):
    window, owner = picker
    release, cleaned = Event(), Event()
    owner.request(fake_option(mode="blocked", release=release, cleaned=cleaned))
    qtbot.waitUntil(lambda: owner.source is not None and owner.source.state is SourceState.RUNNING)
    source = owner.source
    try:
        owner.choose("motion")
        qtbot.waitUntil(lambda: owner._releasing)
        assert owner.source is source and not cleaned.is_set()
        assert not window.source_picker.connect_button.isEnabled()
        owner._release_started -= 3
        owner.poll()
        assert "Cleanup is still pending" in window.source_picker.status.text()
        assert window.isVisible()
    finally:
        release.set()
    snapshot = wait_done(qtbot, owner)
    assert "motion.speed" in snapshot.series
    assert cleaned.is_set() and source.state is SourceState.CLOSED


def test_close_during_preparation_cancels_without_starting(picker, qtbot):
    window, owner = picker
    owner.request(fake_option(mode="opening"))
    window.close()
    qtbot.waitUntil(lambda: not window.isVisible())
    assert owner.source is None and owner._preparing is None


def test_preparation_cleanup_wait_is_visible_and_cannot_start_a_source(picker, qtbot):
    window, owner = picker
    entered, release = Event(), Event()

    def prepare(token):
        entered.set()
        release.wait(3)
        return fake_option().prepare(token)

    try:
        owner.request(SourceOption("slow", "Slow preparation", prepare))
        qtbot.waitUntil(entered.is_set)
        owner.disconnect()
        assert owner._preparation_cancel_started is not None
        owner._preparation_cancel_started -= 3
        owner.poll()
        assert "Preparation cleanup is still pending" in window.source_picker.status.text()
        assert not window.source_picker.connect_button.isEnabled()
        assert owner.source is None
    finally:
        release.set()
    qtbot.waitUntil(lambda: owner._preparing is None)
    assert owner.source is None


def test_start_and_ingest_errors_are_reported(picker, qtbot, monkeypatch):
    window, owner = picker

    def broken_factory():
        raise ValueError("factory failed")

    owner.request(SourceOption("broken", "Broken", lambda token: broken_factory))
    qtbot.waitUntil(lambda: "factory failed" in window.source_picker.status.text())
    qtbot.waitUntil(lambda: window.source_picker.connect_button.isEnabled())
    assert owner.source is None
    owner.choose("process")
    wait_done(qtbot, owner)

    def fail(*args, **kwargs):
        raise ValueError("invalid batch")

    monkeypatch.setattr(owner.session, "ingest_source", fail)
    owner._finished_rendered = False
    owner.poll()
    assert "invalid batch" in window.source_picker.status.text()
    qtbot.waitUntil(lambda: owner.source is None)


def test_legacy_table_load_releases_active_source(picker, qtbot, tmp_path):
    window, owner = picker
    owner.choose("live")
    qtbot.waitUntil(lambda: owner.session is not None and owner._revision > 0)
    source = owner.source
    path = tmp_path / "legacy.csv"
    path.write_text("timestamp_ms,legacy\n0,123\n", encoding="utf-8")
    window.load_csv(path)
    qtbot.waitUntil(lambda: owner.source is None)
    assert source.state is SourceState.CLOSED
    assert owner.session is None
    assert window.right_panel.table.item(0, 1).text() == "123"


def test_broken_plot_binding_fails_real_picker_workflow(picker, qtbot, tmp_path, monkeypatch):
    _, owner = picker
    monkeypatch.setattr(owner, "_render", lambda: None)
    with pytest.raises(AssertionError, match="Source samples never reached the plot"):
        test_picker_csv_simulator_switch_and_original_values(picker, qtbot, tmp_path, monkeypatch)


def test_advertised_controls_are_enabled_and_invoked(picker, qtbot):
    window, owner = picker
    option = fake_option()
    factory = option.prepare(CancellationToken())
    source = factory()
    assert isinstance(source, IteratorSource)
    changes = []

    class ControlledSource(IteratorSource):
        @property
        def capabilities(self):
            return SourceCapabilities(False, True, True, 128)

        def seek(self, timestamp_ns):
            changes.append(("seek", timestamp_ns))

        def set_rate(self, rate_hz):
            changes.append(("rate", rate_hz))

    controlled = ControlledSource(source.source_id, source.schema, source._factory)
    owner.request(SourceOption("controls", "Controls", lambda token: lambda: controlled))
    wait_done(qtbot, owner)
    assert window.source_picker.seek_button.isEnabled()
    assert window.source_picker.rate_button.isEnabled()
    qtbot.mouseClick(window.source_picker.seek_button, Qt.MouseButton.LeftButton)
    qtbot.mouseClick(window.source_picker.rate_button, Qt.MouseButton.LeftButton)
    assert changes == [("seek", 2**60 + 1), ("rate", 2)]

    def fail(rate_hz):
        raise ValueError("rate rejected")

    controlled.set_rate = fail
    qtbot.mouseClick(window.source_picker.rate_button, Qt.MouseButton.LeftButton)
    assert "rate rejected" in window.source_picker.status.text()
