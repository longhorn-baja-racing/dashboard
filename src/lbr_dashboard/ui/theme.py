"""Window-scoped theme presentation; persistence belongs to the application."""

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QWidget

from .main_window import MainWindow


def apply_theme(window: MainWindow, theme: str) -> None:
    palette = QApplication.style().standardPalette()
    if theme in {"light", "dark"}:
        dark = theme == "dark"
        background, base, text = (
            ("#303030", "#202020", "#eeeeee") if dark else ("#f0f0f0", "#ffffff", "#202020")
        )
        for role, color in (
            (QPalette.ColorRole.Window, background),
            (QPalette.ColorRole.Base, base),
            (QPalette.ColorRole.AlternateBase, background),
            (QPalette.ColorRole.Text, text),
            (QPalette.ColorRole.WindowText, text),
            (QPalette.ColorRole.Button, background),
            (QPalette.ColorRole.ButtonText, text),
            (QPalette.ColorRole.Highlight, "#1976d2"),
            (QPalette.ColorRole.HighlightedText, "#ffffff"),
            (QPalette.ColorRole.ToolTipBase, base),
            (QPalette.ColorRole.ToolTipText, text),
        ):
            palette.setColor(role, QColor(color))
    window.setStyleSheet("")
    window.left_panel.setStyleSheet("")
    window.left_panel.list_widget.setStyleSheet("")
    for widget in (window, *window.findChildren(QWidget)):
        widget.setPalette(palette)
    background = palette.color(QPalette.ColorRole.Window).name()
    base = palette.color(QPalette.ColorRole.Base).name()
    text = palette.color(QPalette.ColorRole.WindowText).name()
    window.setStyleSheet(
        "QMainWindow, QMenuBar, QMenu, QToolBar, QToolButton, QPushButton, "
        "QDockWidget, QStatusBar "
        f"{{ background-color: {background}; color: {text}; }}"
        f"QListWidget, QTableWidget, QPlainTextEdit "
        f"{{ background-color: {base}; color: {text}; }}"
        f"QHeaderView, QHeaderView::section, QTableCornerButton::section "
        f"{{ background-color: {background}; color: {text}; }}"
        "QTableWidget::item:selected, QListWidget::item:selected "
        "{ background-color: #1976d2; color: #ffffff; }"
    )
    window.left_panel.setAutoFillBackground(True)
    window.right_panel.setAutoFillBackground(True)
    window.right_panel.set_theme_colors(
        palette.color(QPalette.ColorRole.Base).name(), palette.color(QPalette.ColorRole.Text).name()
    )
