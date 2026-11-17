"""Window-scoped theme presentation; persistence belongs to the application."""

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QWidget

from .main_window import MainWindow


def apply_theme(window: MainWindow, theme: str) -> None:
    palette = (
        QApplication.palette() if theme == "system" else QApplication.style().standardPalette()
    )
    if theme in {"light", "dark"}:
        dark = theme == "dark"
        background, base, text = (
            ("#303030", "#202020", "#eeeeee") if dark else ("#f0f0f0", "#ffffff", "#202020")
        )
        accent = "#58b9ff" if dark else "#14589c"
        disabled = "#b7bec6" if dark else "#626a72"
        for role, color in (
            (QPalette.ColorRole.Window, background),
            (QPalette.ColorRole.Base, base),
            (QPalette.ColorRole.AlternateBase, background),
            (QPalette.ColorRole.Text, text),
            (QPalette.ColorRole.WindowText, text),
            (QPalette.ColorRole.Button, background),
            (QPalette.ColorRole.ButtonText, text),
            (QPalette.ColorRole.Highlight, accent),
            (QPalette.ColorRole.HighlightedText, "#141414" if dark else "#ffffff"),
            (QPalette.ColorRole.ToolTipBase, base),
            (QPalette.ColorRole.ToolTipText, text),
        ):
            palette.setColor(role, QColor(color))
        for role in (
            QPalette.ColorRole.WindowText,
            QPalette.ColorRole.Text,
            QPalette.ColorRole.ButtonText,
        ):
            palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(disabled))
    window.setStyleSheet("")
    window.left_panel.setStyleSheet("")
    window.left_panel.list_widget.setStyleSheet("")
    window.right_panel.setStyleSheet("")
    window.right_panel.table.setStyleSheet("")
    for widget in (window, *window.findChildren(QWidget)):
        widget.setPalette(palette)
    background = palette.color(QPalette.ColorRole.Window).name()
    base = palette.color(QPalette.ColorRole.Base).name()
    text = palette.color(QPalette.ColorRole.WindowText).name()
    accent = palette.color(QPalette.ColorRole.Highlight).name()
    selected = palette.color(QPalette.ColorRole.HighlightedText).name()
    disabled = palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText).name()
    window.setStyleSheet(
        "QMainWindow, QMenuBar, QMenu, QToolBar, QToolButton, QPushButton, "
        "QDockWidget, QStatusBar "
        f"{{ background-color: {background}; color: {text}; }}"
        f"QListWidget, QTableWidget, QPlainTextEdit, QTreeView "
        f"{{ background-color: {base}; color: {text}; }}"
        f"QHeaderView, QHeaderView::section, QTableCornerButton::section "
        f"{{ background-color: {background}; color: {text}; }}"
        "QTableWidget::item:selected, QListWidget::item:selected, QTreeView::item:selected "
        f"{{ background-color: {accent}; color: {selected}; }}"
        "QPushButton:focus, QToolButton:focus, QComboBox:focus, QLineEdit:focus "
        f"{{ border: 2px solid {accent}; }}"
        "QPushButton:disabled, QToolButton:disabled, QComboBox:disabled "
        f"{{ color: {disabled}; }}"
    )
    window.left_panel.setAutoFillBackground(True)
    window.right_panel.setAutoFillBackground(True)
    window.left_panel.signal_browser.apply_palette(palette)
    plot_text = palette.color(QPalette.ColorRole.Text).name()
    window.right_panel.set_theme_colors(base, plot_text)
    window.workspace_docks.set_plot_colors(base, plot_text)
