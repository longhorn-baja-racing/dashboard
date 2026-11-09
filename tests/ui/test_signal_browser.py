"""Real model/view filtering, stable ID selection and drag payload assertions."""

import json
from dataclasses import replace

import pytest
from PySide6.QtCore import QPersistentModelIndex, Qt

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core import SignalAvailability, SignalDescriptor, SignalSchema
from lbr_dashboard.source_controller import SourceController
from lbr_dashboard.ui.signal_browser import ID_ROLE, MIME_TYPE, SignalBrowser


def descriptors():
    return (
        SignalDescriptor(
            "lab.temp",
            "Reading",
            ("lab", "thermal"),
            "numeric",
            "Source-A",
            canonical_unit="°C",
            device_id="Device-X",
            timestamp_domain="relative",
        ),
        SignalDescriptor(
            "bench.pressure",
            "Reading",
            ("bench", "pneumatic"),
            "numeric",
            "Source-B",
            canonical_unit="kPa",
            timestamp_domain="relative",
        ),
        SignalDescriptor(
            "aux.offline",
            "Offline",
            ("aux",),
            "numeric",
            "Source-C",
            availability=SignalAvailability(False, "NOT_FITTED"),
            timestamp_domain="relative",
        ),
    )


@pytest.fixture
def browser(qtbot):
    view = SignalBrowser()
    qtbot.addWidget(view)
    view.refresh(
        SignalSchema(1, descriptors()),
        {"lab.temp": (True, "Measured"), "bench.pressure": (False, "Disconnected")},
    )
    view.show()
    return view


def visible(browser):
    return {
        identifier
        for identifier, row in browser._items.items()
        if browser.proxy.mapFromSource(row[0].index()).isValid()
    }


@pytest.mark.parametrize(
    "query, expected",
    [
        ("Reading", {"lab.temp", "bench.pressure"}),
        ("LAB.TEMP", {"lab.temp"}),
        ("lab/thermal", {"lab.temp"}),
        ("pneumatic", {"bench.pressure"}),
        ("°c", {"lab.temp"}),
        ("kPa", {"bench.pressure"}),
        ("source-b", {"bench.pressure"}),
        ("Device-X", {"lab.temp"}),
        ("aux", {"aux.offline"}),
        ("[.*]", set()),
        ("nonsense", set()),
        (" ", {"lab.temp", "bench.pressure", "aux.offline"}),
    ],
)
def test_literal_search_by_descriptor_fields(browser, query, expected):
    browser.search.setText(query)
    assert visible(browser) == expected, "Search did not return the expected signal identities"
    assert browser.summary.text().startswith(f"{len(expected)} of 3 signals")


def test_duplicate_names_keep_distinct_identity_details_and_drag_payload(browser):
    selected = []
    browser.signal_selected.connect(selected.append)
    for identifier in ("lab.temp", "bench.pressure"):
        index = browser.proxy.mapFromSource(browser._items[identifier][0].index())
        browser.tree.setCurrentIndex(index)
        assert browser.selected_id == identifier
        assert f"ID: {identifier}" in browser.details.text()
    assert selected == ["lab.temp", "bench.pressure"]
    first = browser._items["lab.temp"][0].index()
    data = browser.model.mimeData([first, first.siblingAtColumn(1), first])
    assert json.loads(bytes(data.data(MIME_TYPE))) == {
        "version": 1,
        "signals": [{"signal_id": "lab.temp", "source_id": "Source-A"}],
    }
    assert browser.model.mimeTypes() == [MIME_TYPE]
    assert browser.model.supportedDragActions() == Qt.DropAction.CopyAction
    assert not browser.model.mimeData([browser._groups[("lab",)].index()]).hasFormat(MIME_TYPE)
    assert not browser.model.mimeData([browser._items["bench.pressure"][0].index()]).hasFormat(
        MIME_TYPE
    )


def test_unavailable_unknown_and_zero_are_distinct(browser):
    browser.select_id("aux.offline")
    assert "Unavailable: NOT_FITTED" in browser.details.text()
    assert browser._items["aux.offline"][2].text() == "Unavailable"
    browser.refresh(browser._schema, {"lab.temp": (True, "Zero is measured")})
    assert browser._items["lab.temp"][2].text() == "Available"
    assert browser._items["bench.pressure"][2].text() == "No readings yet"
    index = browser._items["aux.offline"][0].index()
    assert not browser.model.flags(index) & Qt.ItemFlag.ItemIsDragEnabled
    assert browser.model.flags(index) & Qt.ItemFlag.ItemIsSelectable
    assert not browser.model.flags(index) & Qt.ItemFlag.ItemIsEditable


def test_selection_is_stable_when_filter_hides_it_and_availability_changes(browser):
    browser.select_id("lab.temp")
    signals = []
    browser.signal_selected.connect(signals.append)
    persistent = QPersistentModelIndex(browser._items["lab.temp"][0].index())
    browser.search.setText("Source-B")
    assert browser.selected_id == "lab.temp"
    assert not browser.tree.currentIndex().isValid()
    assert "hidden by filter" in browser.summary.text()
    browser.refresh(browser._schema, {"lab.temp": (False, "Sensor absent")})
    assert persistent.isValid()
    assert browser.selected_id == "lab.temp"
    assert "Sensor absent" in browser.details.text()
    browser.search.clear()
    assert browser.tree.currentIndex().data(ID_ROLE) == "lab.temp"
    assert signals == []


def test_catalog_changes_preserve_id_and_remove_missing_selection(browser):
    browser.select_id("lab.temp")
    added = SignalDescriptor("new.value", "New value", ("new", "group"), "numeric", "Source-D")
    changed = replace(descriptors()[0], display_name="New display name", canonical_unit="K")
    browser.refresh(SignalSchema(1, (changed, *descriptors()[1:], added)), {})
    assert browser.selected_id == "lab.temp"
    assert "New display name" in browser.details.text()
    assert "Unit: K" in browser.details.text()
    signals = []
    browser.signal_selected.connect(signals.append)
    browser.refresh(SignalSchema(1, (added,)), {})
    assert browser.selected_id is None and signals == [""]
    assert browser.tree.currentIndex().isValid() is False
    browser.select_id("missing", emit=True)
    assert signals == ["", ""]
    browser.refresh(SignalSchema(1, ()), {})
    assert browser.summary.text() == "0 of 0 signals"


def test_large_nested_catalog_filter_and_drag_from_proxy(qtbot):
    browser = SignalBrowser()
    qtbot.addWidget(browser)
    catalog = tuple(
        SignalDescriptor(
            f"arbitrary.signal_{i}",
            "Repeated",
            (f"family_{i // 100}", "nested", "signals"),
            "numeric",
            f"Source-{i % 7}",
            canonical_unit="m/s",
        )
        for i in range(3000)
    )
    browser.refresh(SignalSchema(1, catalog), {"arbitrary.signal_2345": (True, "Measured")})
    browser.search.setText("arbitrary.signal_2345")
    assert visible(browser) == {"arbitrary.signal_2345"}
    index = browser.proxy.mapFromSource(browser._items["arbitrary.signal_2345"][0].index())
    browser.tree.setCurrentIndex(index)
    assert browser.selected_id == "arbitrary.signal_2345"
    payload = browser.proxy.mimeData([index])
    assert json.loads(bytes(payload.data(MIME_TYPE).data()))["signals"][0] == {
        "signal_id": "arbitrary.signal_2345",
        "source_id": "Source-0",
    }


def test_dashboard_browser_selection_changes_real_curve_and_preserves_filter(qtbot):
    window = create_main_window()
    # Own asynchronous cleanup rather than pytest-qt's immediate deletion.
    owner = window.source_owner
    assert isinstance(owner, SourceController)
    window.show()
    try:
        owner.choose("process")
        qtbot.waitUntil(lambda: owner._finished_rendered)
        browser = window.left_panel.signal_browser
        assert browser.isVisible() and not window.left_panel.list_widget.isVisible()
        browser.search.setText("temperature")
        assert visible(browser) == {"process.temperature"}
        index = browser.proxy.mapFromSource(browser._items["process.temperature"][0].index())
        browser.tree.setCurrentIndex(index)
        plot = window.right_panel.plot_widget.getPlotItem()
        assert plot is not None and owner.session is not None
        xs, ys = plot.listDataItems()[0].getData()
        snapshot = owner.session.snapshot()
        expected = snapshot.series["process.temperature"]
        assert list(xs) == [0, 1, 2, 3]
        assert list(ys) == [record.sample.value for record in expected]
        owner._render()
        assert browser.selected_id == "process.temperature"
        assert browser.search.text() == "temperature"
        owner.retry()
        qtbot.waitUntil(lambda: owner._preparing is None and owner._finished_rendered)
        assert browser.selected_id == "process.temperature"
        assert browser.search.text() == "temperature"
    finally:
        owner.disconnect()
        qtbot.waitUntil(lambda: owner.source is None and owner._preparing is None)
        window.close()
        window.deleteLater()


def test_broken_search_binding_fails_normal_search_case(browser):
    browser.proxy.setFilterRole(ID_ROLE)
    with pytest.raises(AssertionError, match="Search did not return"):
        test_literal_search_by_descriptor_fields(browser, "lab/thermal", {"lab.temp"})
