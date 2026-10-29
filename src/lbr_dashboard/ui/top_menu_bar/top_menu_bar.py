"""Application menu bar."""

from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import QMenuBar


class TopMenuBar(QMenuBar):
    """File menu for the dashboard shell."""

    def __init__(self, main_window) -> None:
        super().__init__(main_window)
        file_menu = self.addMenu("&File")

        open_action = QAction("&Open", self)
        open_action.triggered.connect(main_window.open_csv)
        file_menu.addAction(open_action)
        self.recent_menu = file_menu.addMenu("Recent CSV files")

        file_menu.addSeparator()
        exit_action = QAction("E&xit", self)
        exit_action.triggered.connect(main_window.close)
        file_menu.addAction(exit_action)
        theme_menu = self.addMenu("&Preferences").addMenu("Theme")
        group = QActionGroup(self)
        group.setExclusive(True)
        self.theme_actions = {}
        self._window = main_window
        for theme in ("system", "light", "dark"):
            action = QAction(theme.title(), self)
            action.setCheckable(True)
            action.triggered.connect(
                lambda checked=False, theme=theme: main_window.theme_requested.emit(theme)
            )
            group.addAction(action)
            theme_menu.addAction(action)
            self.theme_actions[theme] = action

    def set_preference_choices(self, theme: str, recent_paths: tuple[str, ...]) -> None:
        self.theme_actions[theme].setChecked(True)
        self.recent_menu.clear()
        if not recent_paths:
            self.recent_menu.addAction("No recent files").setEnabled(False)
        for index, path in enumerate(recent_paths):
            action = QAction(f"{index + 1}. {path.replace('&', '&&')}", self.recent_menu)
            action.triggered.connect(
                lambda checked=False, path=path: self._window.csv_requested.emit(path)
            )
            self.recent_menu.addAction(action)
        self.recent_menu.addSeparator()
        clear_action = self.recent_menu.addAction("Clear recent files")
        clear_action.triggered.connect(self._window.clear_recent_requested.emit)
