"""Application-owned preference commits, local recovery and presentation wiring."""

from dataclasses import replace
from pathlib import Path

from .core.preferences import Preferences, PreferencesError, PreferencesStore, RecentSource
from .ui.main_window import MainWindow
from .ui.theme import apply_theme

#: Machine-local preference holding the absolute path of the last saved or
#: opened workspace file, reopened at startup.
LAST_WORKSPACE = "last_workspace"


class PreferencesController:
    def __init__(self, window: MainWindow, store: PreferencesStore | None = None) -> None:
        self.window, self.store = window, store
        self.current = Preferences()
        warning = None
        if store is not None:
            loaded = store.reload()
            self.current, warning = loaded.preferences, loaded.warning
        window.theme_requested.connect(self.set_theme)
        window.clear_recent_requested.connect(self.clear_recent)
        window.csv_loaded.connect(self.remember_csv)
        self._display()
        if warning:
            window.statusBar().showMessage(warning)
        manager = window.workspace_manager
        manager.path_changed.connect(self.remember_workspace)
        last = self.current.extra.get(LAST_WORKSPACE)
        if store is not None and isinstance(last, str) and last:
            manager.restore_last(last)

    def _display(self) -> None:
        self.window.set_preference_choices(
            self.current.theme,
            tuple(item.location for item in self.current.recent_sources if item.kind == "csv"),
        )
        apply_theme(self.window, self.current.theme)

    def _commit(self, candidate: Preferences) -> bool:
        try:
            if self.store is not None:
                self.store.save(candidate)
        except PreferencesError as error:
            self.window.statusBar().showMessage(str(error))
            self.window.set_preference_choices(
                self.current.theme,
                tuple(item.location for item in self.current.recent_sources if item.kind == "csv"),
            )
            return False
        self.current = candidate
        self._display()
        return True

    def set_theme(self, theme: str) -> None:
        self._commit(replace(self.current, theme=theme))

    def remember_csv(self, path: str) -> None:
        self._commit(self.current.remember(RecentSource("csv", str(Path(path).resolve()))))

    def clear_recent(self) -> None:
        self._commit(replace(self.current, recent_sources=()))

    def remember_workspace(self, path: object) -> None:
        """Store the current workspace file locally; None forgets it."""

        extra = dict(self.current.extra)
        if isinstance(path, str):
            extra[LAST_WORKSPACE] = str(Path(path).resolve())
        else:
            extra.pop(LAST_WORKSPACE, None)
        if extra == dict(self.current.extra):
            return
        candidate = replace(self.current, extra=extra)
        try:
            if self.store is not None:
                self.store.save(candidate)
        except PreferencesError as error:
            self.window.statusBar().showMessage(str(error))
            return
        self.current = candidate
