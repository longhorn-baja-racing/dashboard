"""Visible import paths, failed-load transactionality and worker lifecycle."""

from threading import Event

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QFileDialog

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core.log import LogSession
from lbr_dashboard.import_controller import classify, validate


@pytest.fixture
def window(qtbot):
    widget = create_main_window()
    qtbot.addWidget(widget)
    widget.show()
    yield widget
    assert widget.import_controller.close()
    widget.close()


def series(window):
    plot = window.right_panel.plot_widget.getPlotItem()
    return tuple(tuple(data.tolist()) for data in plot.listDataItems()[0].getData())


def test_fresh_launch_and_demo_buttons_have_real_values(window, qtbot):
    assert window.import_state == "empty"
    assert window.open_button.isVisible() and window.demo_button.isVisible()
    assert not window.cancel_button.isVisible()
    assert "No sensor is connected" in window.source_label.text()
    qtbot.mouseClick(window.demo_button, Qt.MouseButton.LeftButton)
    assert window.import_state == "ready"
    assert series(window) == ((0, 15, 100), (20, 21, 22))
    assert "Fictional" in window.source_label.text()
    window.left_panel.list_widget.setCurrentRow(2)
    assert series(window) == ((0, 45, 100), (101.2, 103.4, 0))
    assert window.left_panel.list_widget.count() == 3
    assert [window.left_panel.list_widget.item(i).text() for i in range(3)] == [
        "timestamp_ms",
        "lab.temperature",
        "bench.pressure",
    ]
    assert window.right_panel.table.item(0, 3).text() == "ready"


def test_hidden_text_column_does_not_shift_table_selection(window, qtbot, tmp_path):
    path = tmp_path / "middle-text.csv"
    path.write_text("note,timestamp_ms,missing,arbitrary.pressure\nready,0,,0\ndone,20,,7\n")
    window.import_controller.start(path)
    qtbot.waitUntil(lambda: window.import_state == "ready")
    assert window.left_panel.list_widget.count() == 2
    assert series(window) == ((0, 20), (0, 7))
    assert {item.column() for item in window.right_panel.table.selectedItems()} == {3}
    assert window.right_panel.table.item(0, 0).text() == "ready"
    window.left_panel.list_widget.setCurrentRow(0)
    assert {item.column() for item in window.right_panel.table.selectedItems()} == {1}


def test_text_only_file_keeps_table_and_explains_empty_plot_selector(window, qtbot, tmp_path):
    path = tmp_path / "text.csv"
    path.write_text("note\nready\ndone\n")
    window.import_controller.start(path)
    qtbot.waitUntil(lambda: window.import_state == "ready")
    assert window.left_panel.list_widget.count() == 0
    assert window.right_panel.table.item(1, 0).text() == "done"
    plot = window.right_panel.plot_widget.getPlotItem()
    assert not plot.listDataItems() and "No numeric readings" in plot.titleLabel.text


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"", "no data rows"),
        (b"timestamp_ms,value\n", "no data rows"),
        (b"timestamp_ms,value\n0,1,extra\n", "row 2"),
        (b"timestamp_ms,value\n0\n1,2\n", "row 2"),
        (b"timestamp_ms,value\nNaN,1\n", "non-finite time"),
        (b"timestamp_ms,value\nbad,1\n", "invalid"),
        (b"timestamp_ms,value\n,1\n", "missing"),
        (b"timestamp_ms,value\n0,\xff\n", "UTF-8"),
        (b'timestamp_ms,value\n0,"unclosed\n', "quoting"),
        (b"timestamp_ms,same,same\n0,1,2\n", "Duplicate"),
        (b"timestamp_ms,\n0,1\n", "no name"),
    ],
)
def test_invalid_import_is_actionable_and_preserves_previous_data(
    window, qtbot, tmp_path, data, reason
):
    window.demo_button.click()
    original = series(window)
    path = tmp_path / "bad.csv"
    path.write_bytes(data)
    failures = []
    window.import_failed.connect(lambda *args: failures.append(args))
    window.import_controller.start(path)
    assert window.import_state == "loading" and window.progress.isVisible()
    qtbot.waitUntil(lambda: window.import_state == "error")
    assert reason in window.import_message.text()
    assert series(window) == original and window.right_panel.table.rowCount() == 4
    assert window._log_store.numeric_column("lab.temperature") == (20, 21, None, 22)
    assert window.error_details.isVisible() and window.copy_button.isVisible()
    assert len(failures) == 1 and failures[0][1]
    assert path.read_bytes() == data
    window.details_button.click()
    assert not window.error_details.isVisible()
    window.details_button.click()
    assert window.error_details.isVisible()
    window.demo_button.click()
    assert window.import_state == "ready" and not window.error_details.isVisible()


def test_real_open_button_bom_quotes_missing_zero_and_copy(window, qtbot, tmp_path, monkeypatch):
    path = tmp_path / "arbitrary.csv"
    content = (
        '\ufefftimestamp_ms,"arbitrary,pressure",state\n0,0,"ready, ok"\n10,,pause\n35,9,stop\n'
    )
    path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(path), "CSV"))
    qtbot.mouseClick(window.open_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: window.import_state == "ready")
    assert series(window) == ((0, 35), (0, 9))
    assert window.right_panel.table.item(1, 1).text() == ""
    assert window.right_panel.table.item(0, 2).text() == "ready, ok"
    assert path.read_text(encoding="utf-8") == content
    window.import_controller.start(tmp_path / "missing.csv")
    qtbot.waitUntil(lambda: window.import_state == "error")
    qtbot.mouseClick(window.copy_button, Qt.MouseButton.LeftButton)
    assert "not found" in QApplication.clipboard().text()
    assert "Recovery:" in QApplication.clipboard().text()
    before = window.import_state
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: ("", ""))
    window.open_button.click()
    assert window.import_state == before


def test_missing_timestamp_and_text_are_explicit_not_fabricated(window, qtbot, tmp_path):
    path = tmp_path / "no-time.csv"
    path.write_text("unrelated.level,note\n1,hello\n2,world\n")
    window.import_controller.start(path)
    qtbot.waitUntil(lambda: window.import_state == "ready")
    assert "sample index" in window.import_message.text()
    assert "numeric values only" in window.import_message.text()
    assert series(window) == ((0, 1), (1, 2))
    assert "No live sensor" in window.source_label.text()
    path2 = tmp_path / "wrong.bin"
    path2.write_text("a\n1\n")
    window.import_controller.start(path2)
    qtbot.waitUntil(lambda: window.import_state == "error")
    assert "Unsupported file" in window.import_message.text()


def test_cancel_during_load_close_cleanup_and_no_late_commit(window, qtbot, tmp_path):
    started = Event()

    class SlowImporter:
        def import_session(self, path, *, cancelled, progress):
            started.set()
            progress(250)
            wait = Event()
            while not cancelled():
                wait.wait(0.001)
            raise InterruptedError()

    window.import_controller.importer = SlowImporter()
    window.demo_button.click()
    before = series(window)
    window.import_controller.start(tmp_path / "slow.csv")
    qtbot.waitUntil(started.is_set)
    assert not window.open_button.isEnabled()
    beats = []
    QTimer.singleShot(0, lambda: beats.append(True))
    qtbot.waitUntil(lambda: bool(beats))
    window.import_controller.start(tmp_path / "ignored.csv")
    window.try_demo()
    qtbot.mouseClick(window.cancel_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: window.import_state == "cancelled")
    assert series(window) == before
    assert window.import_controller.worker is None and not window.import_controller.timer.isActive()
    window.import_controller.start(tmp_path / "close.csv")
    worker = window.import_controller.worker
    assert worker is not None
    window.close()
    assert not worker.is_alive() and not window.import_controller.timer.isActive()


def test_close_refuses_to_abandon_a_reader(window, monkeypatch):
    original = window.import_controller.close
    monkeypatch.setattr(window.import_controller, "close", lambda: False)
    assert not window.close()
    assert window.isVisible() and "Waiting" in window.import_message.text()
    monkeypatch.setattr(window.import_controller, "close", original)


def test_cancel_completed_worker_before_poll_never_commits(window, qtbot, tmp_path):
    window.demo_button.click()
    before = series(window)
    path = tmp_path / "ready.csv"
    path.write_text("timestamp_ms,value\n0,99\n")
    window.import_controller.start(path)
    worker = window.import_controller.worker
    assert worker is not None
    worker.join(1)
    assert not worker.is_alive()
    window.import_controller.cancel()
    window.import_controller.poll()
    assert window.import_state == "cancelled" and series(window) == before
    window.import_controller.poll()


@pytest.mark.parametrize("error", [PermissionError(), OSError(), RuntimeError()])
def test_error_classification_always_gives_recovery(error):
    failure = classify(error)
    assert failure.reason and failure.recovery


def test_validation_timestamp_order_is_not_rewritten():
    session = LogSession.from_rows(
        ("timestamp_ms", "anything"), (("10", "1"), ("-5", "2"), ("10", "3"))
    )
    assert validate(session) == ""
    assert session.numeric_column("timestamp_ms") == (10, -5, 10)
