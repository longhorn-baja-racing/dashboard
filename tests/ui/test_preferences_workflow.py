"""Real menu/storage/restart flows with isolated local preference files."""

from typing import cast

import pytest
from PySide6.QtGui import QPalette

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core import Preferences, PreferencesError, PreferencesStore, RecentSource
from lbr_dashboard.preferences_controller import PreferencesController
from lbr_dashboard.ui.top_menu_bar.top_menu_bar import TopMenuBar


def window(qtbot, path):
    store = PreferencesStore(path)
    view = create_main_window(preferences_store=store)
    qtbot.addWidget(view)
    view.show()
    return view, store, cast(PreferencesController, view.preferences_controller)


def menu(view):
    return cast(TopMenuBar, view.menuBar())


def test_theme_and_recent_paths_survive_window_restart_and_menu_reopens_file(
    qtbot, tmp_path
) -> None:
    prefs = tmp_path / "preferences.json"
    csv = tmp_path / "temp & pressure.csv"
    csv.write_text(
        "timestamp_ms,unrelated.temperature,custom.pressure\n0,20,0\n10,21,8\n", encoding="utf-8"
    )
    view, store, controller = window(qtbot, prefs)
    menu(view).theme_actions["dark"].trigger()
    assert controller.current.theme == store.current.theme == "dark"
    assert view.palette().color(QPalette.ColorRole.Window).lightness() < 100
    view.load_csv(csv)
    assert controller.current.recent_sources == (RecentSource("csv", str(csv.resolve())),)
    assert "&&" in menu(view).recent_menu.actions()[0].text()
    view.left_panel.list_widget.setCurrentRow(2)
    plot = view.right_panel.plot_widget.getPlotItem()
    assert plot is not None
    before = plot.listDataItems()[0].getData()
    menu(view).theme_actions["light"].trigger()
    after = plot.listDataItems()[0].getData()
    assert list(before[0]) == list(after[0]) and list(before[1]) == list(after[1]) == [0, 8]
    assert view.palette().color(QPalette.ColorRole.Window).lightness() > 200
    assert view.left_panel.list_widget.palette().color(QPalette.ColorRole.Base).lightness() > 200
    assert view.right_panel.table.palette().color(QPalette.ColorRole.Base).lightness() > 200
    assert view.menuBar().palette().color(QPalette.ColorRole.WindowText).lightness() < 100
    view.close()
    restarted, new_store, new_controller = window(qtbot, prefs)
    assert new_controller.current == new_store.current
    assert menu(restarted).theme_actions["light"].isChecked()
    assert restarted.right_panel.table.rowCount() == 0  # no automatic source opening
    menu(restarted).recent_menu.actions()[0].trigger()
    assert restarted.right_panel.table.rowCount() == 2
    menu(restarted).recent_menu.actions()[-1].trigger()
    assert new_controller.current.recent_sources == ()
    assert PreferencesStore(prefs).reload().preferences.recent_sources == ()
    menu(restarted).theme_actions["system"].trigger()
    assert new_controller.current.theme == "system"


def test_theme_save_failure_keeps_active_theme_palette_file_and_recent_state(
    qtbot, tmp_path, monkeypatch
) -> None:
    prefs = tmp_path / "preferences.json"
    store = PreferencesStore(prefs)
    store.save(Preferences("light"))
    view, store, controller = window(qtbot, prefs)
    before_file = prefs.read_bytes()
    before_palette = view.palette()

    def fail(candidate):
        raise PreferencesError("save_failed", "Injected save failure")

    monkeypatch.setattr(store, "save", fail)
    menu(view).theme_actions["dark"].trigger()
    assert controller.current.theme == "light" and store.current.theme == "light"
    assert view.palette() == before_palette and prefs.read_bytes() == before_file
    assert menu(view).theme_actions["light"].isChecked()
    assert "Injected save failure" in view.statusBar().currentMessage()
    csv = tmp_path / "good.csv"
    csv.write_text("timestamp_ms,sensor.x\n0,1\n")
    view.load_csv(csv)
    assert (
        view.right_panel.table.rowCount() == 1
    )  # preference save failure is separate from load success
    assert controller.current.recent_sources == ()
    assert "Injected save failure" in view.statusBar().currentMessage()


def test_failed_source_load_does_not_change_recent_preferences(qtbot, tmp_path) -> None:
    view, store, controller = window(qtbot, tmp_path / "preferences.json")
    csv = tmp_path / "good.csv"
    csv.write_text("timestamp_ms,meter.x\n0,0\n")
    view.load_csv(csv)
    before = store.current
    data = store.path.read_bytes()
    for path in (tmp_path / "missing.csv", tmp_path / "bad.csv"):
        if path.name == "bad.csv":
            path.write_bytes(b"header\n\xff")
        with pytest.raises((FileNotFoundError, UnicodeDecodeError)):
            view.load_csv(path)
        view.csv_requested.emit(str(path))
        assert controller.current is before and store.path.read_bytes() == data
        assert "Could not load" in view.statusBar().currentMessage()


def test_corrupt_preferences_recover_visibly_and_invalid_input_never_opens_sources(
    qtbot, tmp_path
) -> None:
    path = tmp_path / "preferences.json"
    store = PreferencesStore(path)
    old = Preferences("dark", (RecentSource("csv", str(tmp_path / "never-opened.csv")),))
    store.save(old)
    store.save(Preferences("light"))
    path.write_bytes(b"bad primary")
    view, _, controller = window(qtbot, path)
    assert controller.current == old
    assert "Recovered preferences" in view.statusBar().currentMessage()
    assert view.right_panel.table.rowCount() == 0
    menu(view).theme_actions["light"].trigger()
    assert Preferences.from_bytes(store.backup_path.read_bytes()) == old
    assert Preferences.from_bytes(path.read_bytes()).theme == "light"


def test_missing_recent_file_is_reported_without_removing_its_entry(qtbot, tmp_path) -> None:
    path = tmp_path / "preferences.json"
    store = PreferencesStore(path)
    entry = RecentSource("csv", str(tmp_path / "missing.csv"))
    store.save(Preferences(recent_sources=(entry,)))
    view, _, controller = window(qtbot, path)
    menu(view).recent_menu.actions()[0].trigger()
    assert "Could not load" in view.statusBar().currentMessage()
    assert controller.current.recent_sources == (entry,)


def test_memory_only_composition_does_not_write_user_preferences(
    qtbot, monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    view = create_main_window()
    qtbot.addWidget(view)
    menu(view).theme_actions["dark"].trigger()
    assert not list(tmp_path.rglob("preferences.json"))
