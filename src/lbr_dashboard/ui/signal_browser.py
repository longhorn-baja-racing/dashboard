"""Descriptor-driven Qt tree with stable identities across filtering and updates."""

from __future__ import annotations

import json
from collections.abc import Mapping

from PySide6.QtCore import QMimeData, QModelIndex, QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPalette, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QLineEdit,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ..core import SignalDescriptor, SignalSchema

ID_ROLE = int(Qt.ItemDataRole.UserRole) + 1
SEARCH_ROLE = ID_ROLE + 1
SOURCE_ROLE = ID_ROLE + 2
MIME_TYPE = "application/x-lbr-signal-ids+json"


class SignalTreeModel(QStandardItemModel):
    def mimeTypes(self) -> list[str]:
        return [MIME_TYPE]

    def mimeData(self, indexes) -> QMimeData:
        identities = sorted(
            {
                (index.data(ID_ROLE), index.data(SOURCE_ROLE))
                for index in indexes
                if index.isValid()
                and index.column() == 0
                and index.data(ID_ROLE)
                and self.flags(index) & Qt.ItemFlag.ItemIsDragEnabled
            }
        )
        data = QMimeData()
        if identities:
            data.setData(
                MIME_TYPE,
                json.dumps(
                    {
                        "version": 1,
                        "signals": [
                            {"signal_id": identifier, "source_id": source}
                            for identifier, source in identities
                        ],
                    }
                ).encode("utf-8"),
            )
        return data

    def supportedDragActions(self) -> Qt.DropAction:
        return Qt.DropAction.CopyAction


class SignalBrowser(QWidget):
    signal_selected = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search name, ID, path, units or source")
        self.search.setAccessibleName("Search signals")
        self.search.setClearButtonEnabled(True)
        self.tree = QTreeView()
        palette = self.tree.palette()
        for group in (
            QPalette.ColorGroup.Active,
            QPalette.ColorGroup.Inactive,
            QPalette.ColorGroup.Disabled,
        ):
            for role, color in (
                (QPalette.ColorRole.Base, "#2e2e2e"),
                (QPalette.ColorRole.Text, "#f2f2f2"),
                (QPalette.ColorRole.Highlight, "#174a73"),
                (QPalette.ColorRole.HighlightedText, "#ffffff"),
            ):
                palette.setColor(group, role, QColor(color))
        self.tree.setPalette(palette)
        self.tree.setStyleSheet(
            "QTreeView { background-color: #2e2e2e; color: #f2f2f2; "
            "selection-background-color: #174a73; selection-color: #ffffff; }"
            "QTreeView::item:selected { background-color: #174a73; color: #ffffff; }"
            "QHeaderView::section { background-color: #333333; color: #f2f2f2; }"
        )
        self.tree.setAccessibleName("Telemetry signal hierarchy")
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setDragEnabled(True)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setUniformRowHeights(True)
        self.model = SignalTreeModel()
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterRole(SEARCH_ROLE)
        self.proxy.setFilterKeyColumn(0)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.proxy.setRecursiveFilteringEnabled(True)
        self.proxy.setAutoAcceptChildRows(True)
        self.tree.setModel(self.proxy)
        self.summary = QLabel("No signals")
        self.details = QLabel("Select a signal to inspect its metadata.")
        self.details.setWordWrap(True)
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.summary.setWordWrap(True)
        layout = QVBoxLayout(self)
        for widget in (self.search, self.summary, self.tree, self.details):
            layout.addWidget(widget)
        self._schema: SignalSchema | None = None
        self._descriptors: dict[str, SignalDescriptor] = {}
        self._items: dict[str, list[QStandardItem]] = {}
        self._groups: dict[tuple[str, ...], QStandardItem] = {}
        self._availability: dict[str, tuple[bool | None, str]] = {}
        self.selected_id: str | None = None
        self.search.textChanged.connect(self._filter)
        self.tree.selectionModel().currentChanged.connect(self._current)

    def refresh(
        self,
        schema: SignalSchema,
        availability: Mapping[str, tuple[bool | None, str]],
    ) -> None:
        """Update availability in place; rebuild only when descriptor metadata changes."""
        if schema != self._schema:
            expanded = {
                path
                for path, item in self._groups.items()
                if self.tree.isExpanded(self.proxy.mapFromSource(item.index()))
            }
            self.tree.selectionModel().blockSignals(True)
            self._schema = schema
            self._descriptors = {d.identifier: d for d in schema.signals}
            self._items.clear()
            self._groups.clear()
            self.model.clear()
            self.model.setHorizontalHeaderLabels(["Signal / ID", "Unit", "Availability"])
            for descriptor in sorted(schema.signals, key=lambda d: (d.path, d.identifier)):
                parent = self.model.invisibleRootItem()
                for depth, segment in enumerate(descriptor.path, 1):
                    path = descriptor.path[:depth]
                    if path not in self._groups:
                        group = QStandardItem(segment)
                        group.setEditable(False)
                        group.setForeground(QBrush(QColor("#f2f2f2")))
                        group.setDragEnabled(False)
                        group.setData(" / ".join(path), SEARCH_ROLE)
                        parent.appendRow(group)
                        self._groups[path] = group
                    parent = self._groups[path]
                row = [
                    QStandardItem(f"{descriptor.display_name} — {descriptor.identifier}"),
                    QStandardItem(descriptor.canonical_unit or "unspecified"),
                    QStandardItem(),
                ]
                for item in row:
                    item.setEditable(False)
                    item.setDropEnabled(False)
                    item.setData(descriptor.identifier, ID_ROLE)
                    item.setData(descriptor.source_id, SOURCE_ROLE)
                row[0].setData(
                    " ".join(
                        (
                            descriptor.display_name,
                            descriptor.identifier,
                            "/".join(descriptor.path),
                            " / ".join(descriptor.path),
                            descriptor.canonical_unit or "",
                            descriptor.source_id,
                            descriptor.device_id or "",
                            descriptor.description,
                        )
                    ),
                    SEARCH_ROLE,
                )
                parent.appendRow(row)
                self._items[descriptor.identifier] = row
            self._availability.clear()
            if self.selected_id not in self._descriptors:
                had_selection = self.selected_id is not None
                self.selected_id = None
                if had_selection:
                    self.signal_selected.emit("")
            for path, item in self._groups.items():
                if path in expanded or len(path) == 1:
                    self.tree.setExpanded(self.proxy.mapFromSource(item.index()), True)
            self.tree.selectionModel().blockSignals(False)
            self.tree.setColumnWidth(0, 260)
        for identifier, descriptor in self._descriptors.items():
            available, reason = availability.get(identifier, (None, "No readings yet"))
            if not descriptor.availability.available:
                available, reason = False, descriptor.availability.reason or "Not available"
            state = (available, reason)
            if self._availability.get(identifier) == state:
                continue
            self._availability[identifier] = state
            row = self._items[identifier]
            row[2].setText(
                ("Available" if available else "Unavailable")
                if available is not None
                else "No readings yet"
            )
            for item in row:
                item.setToolTip(reason)
                item.setForeground(QBrush(QColor("#b8b8b8" if available is False else "#f2f2f2")))
                item.setDragEnabled(available is True)
        self._restore_selection()

    def select_id(self, identifier: str | None, *, emit: bool = False) -> None:
        self.selected_id = identifier if identifier in self._items else None
        self._restore_selection()
        if emit:
            self.signal_selected.emit(self.selected_id or "")

    def _filter(self, text: str) -> None:
        # Literal matching: user-entered brackets or regex syntax are ordinary text.
        self.tree.selectionModel().blockSignals(True)
        self.proxy.setFilterFixedString(text.strip())
        if text.strip():
            self.tree.expandAll()
        self._restore_selection()

    def _current(self, current: QModelIndex, previous: QModelIndex) -> None:
        identifier = current.data(ID_ROLE) if current.isValid() else None
        if identifier:
            self.selected_id = identifier
            self._details()
            self.signal_selected.emit(identifier)

    def _restore_selection(self) -> None:
        selection = self.tree.selectionModel()
        selection.blockSignals(True)
        item = self._items.get(self.selected_id or "")
        index = self.proxy.mapFromSource(item[0].index()) if item else QModelIndex()
        self.tree.setCurrentIndex(index)
        if index.isValid():
            parent = index.parent()
            while parent.isValid():
                self.tree.setExpanded(parent, True)
                parent = parent.parent()
        selection.blockSignals(False)
        visible = sum(
            self.proxy.mapFromSource(row[0].index()).isValid() for row in self._items.values()
        )
        suffix = " — selected signal hidden by filter" if item and not index.isValid() else ""
        self.summary.setText(f"{visible} of {len(self._items)} signals{suffix}")
        self._details()

    def _details(self) -> None:
        descriptor = self._descriptors.get(self.selected_id or "")
        if descriptor is None:
            self.details.setText("Select a signal to inspect its metadata.")
            return
        available, reason = self._availability.get(descriptor.identifier, (None, "No readings yet"))
        state = (
            "Available" if available else "Unavailable" if available is False else "No readings yet"
        )
        self.details.setText(
            f"{descriptor.display_name}\nID: {descriptor.identifier}\n"
            f"Path: {' / '.join(descriptor.path)}\nSource: {descriptor.source_id}\n"
            f"Unit: {descriptor.canonical_unit or 'unspecified'} | Type: {descriptor.data_type}\n"
            f"Device: {descriptor.device_id or 'unspecified'}\n"
            f"{state}: {reason}"
        )
