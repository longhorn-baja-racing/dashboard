"""Small validated local preferences with recoverable atomic JSON persistence."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .signals import _freeze_mapping, _thaw_json, _validate_identifier

MAX_BYTES = 65536
MAX_RECENT = 10


class PreferencesError(ValueError):
    """Actionable storage/validation error; no partial active-state update."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 8192 or "\0" in value:
        raise PreferencesError(
            "invalid_field", f"{name} must be nonempty text without nulls (max 8192 characters)"
        )
    return value


def _bounded(value: object) -> None:
    remaining = 4096
    active: set[int] = set()

    def visit(item: object, depth: int) -> None:
        nonlocal remaining
        remaining -= 1
        if depth > 16 or remaining < 0:
            raise PreferencesError("too_complex", "preferences exceed depth or item limits")
        if isinstance(item, str) and len(item) > 8192:
            raise PreferencesError("too_complex", "preference text exceeds 8192 characters")
        if isinstance(item, (Mapping, list, tuple)):
            if id(item) in active:
                raise PreferencesError("invalid_json", "preferences contain a cycle")
            active.add(id(item))
            try:
                if isinstance(item, Mapping):
                    for key, child in item.items():
                        if not isinstance(key, str) or len(key) > 128:
                            raise PreferencesError(
                                "invalid_json",
                                "preference keys must be strings of at most 128 characters",
                            )
                        visit(child, depth + 1)
                else:
                    for child in item:
                        visit(child, depth + 1)
            finally:
                active.remove(id(item))

    visit(value, 0)


def _extra(value: Mapping[str, Any], reserved: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PreferencesError("invalid_field", "extensions must be an object")
    _bounded(value)
    if set(value) & reserved:
        raise PreferencesError("invalid_field", "extension fields cannot replace known preferences")
    try:
        return _freeze_mapping(value, "preference extensions")
    except ValueError as error:
        raise PreferencesError("invalid_json", str(error)) from error


@dataclass(frozen=True, slots=True)
class RecentSource:
    """Machine-local source location; never a portable signal/workspace binding."""

    kind: str
    location: str
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            _validate_identifier(self.kind, "source kind")
        except ValueError as error:
            raise PreferencesError("invalid_field", str(error)) from error
        _text(self.location, "source location")
        object.__setattr__(self, "extra", _extra(self.extra, {"kind", "location"}))

    def to_dict(self) -> dict[str, Any]:
        return {**_thaw_json(self.extra), "kind": self.kind, "location": self.location}

    @classmethod
    def from_dict(cls, value: object) -> RecentSource:
        if not isinstance(value, Mapping) or "kind" not in value or "location" not in value:
            raise PreferencesError("invalid_field", "recent source requires kind and location")
        return cls(
            value["kind"],
            value["location"],
            {key: item for key, item in value.items() if key not in {"kind", "location"}},
        )


@dataclass(frozen=True, slots=True)
class Preferences:
    """Immutable local app/source defaults; no layout or telemetry data fields."""

    theme: str = "system"
    recent_sources: tuple[RecentSource, ...] = ()
    default_source: str = "csv"
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.theme, str) or self.theme not in {"system", "light", "dark"}:
            raise PreferencesError("invalid_field", "theme must be system, light or dark")
        try:
            _validate_identifier(self.default_source, "default_source")
        except ValueError as error:
            raise PreferencesError("invalid_field", str(error)) from error
        if not isinstance(self.recent_sources, (tuple, list)):
            raise PreferencesError("invalid_field", "recent_sources must be a list")
        recent = tuple(self.recent_sources)
        if len(recent) > MAX_RECENT or any(not isinstance(item, RecentSource) for item in recent):
            raise PreferencesError(
                "invalid_field", "recent_sources must contain at most ten RecentSource values"
            )
        if len({(item.kind, item.location) for item in recent}) != len(recent):
            raise PreferencesError("invalid_field", "recent sources must not contain duplicates")
        object.__setattr__(self, "recent_sources", recent)
        object.__setattr__(
            self,
            "extra",
            _extra(self.extra, {"schema_version", "theme", "recent_sources", "default_source"}),
        )

    def remember(self, source: RecentSource) -> Preferences:
        if not isinstance(source, RecentSource):
            raise PreferencesError("invalid_field", "source must be RecentSource")
        recent = (source,) + tuple(
            item
            for item in self.recent_sources
            if (item.kind, item.location) != (source.kind, source.location)
        )
        return replace(self, recent_sources=recent[:MAX_RECENT], default_source=source.kind)

    def to_dict(self) -> dict[str, Any]:
        return {
            **_thaw_json(self.extra),
            "schema_version": 1,
            "theme": self.theme,
            "recent_sources": [item.to_dict() for item in self.recent_sources],
            "default_source": self.default_source,
        }

    def to_bytes(self) -> bytes:
        data = self.to_dict()
        _bounded(data)
        try:
            result = (
                json.dumps(data, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
                + "\n"
            ).encode("utf-8")
        except (ValueError, UnicodeError) as error:
            raise PreferencesError(
                "invalid_json", "preferences cannot be encoded as UTF-8 JSON"
            ) from error
        if len(result) > MAX_BYTES:
            raise PreferencesError("too_large", "preferences exceed 64 KiB")
        return result

    @classmethod
    def from_bytes(cls, data: bytes) -> Preferences:
        if not isinstance(data, bytes):
            raise PreferencesError("invalid_json", "preferences must be UTF-8 bytes")
        if len(data) > MAX_BYTES:
            raise PreferencesError("too_large", "preferences exceed 64 KiB")

        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise PreferencesError("invalid_json", f"duplicate preference key: {key}")
                result[key] = value
            return result

        try:
            value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs)
        except (ValueError, UnicodeError, RecursionError) as error:
            raise PreferencesError(
                "invalid_json", "preferences contain invalid UTF-8 JSON"
            ) from error
        _bounded(value)
        if not isinstance(value, dict):
            raise PreferencesError("invalid_field", "preferences must be an object")
        version = value.get("schema_version")
        if isinstance(version, bool) or not isinstance(version, int) or version not in {0, 1}:
            raise PreferencesError(
                "unsupported_version", "supported preference versions are 0 and 1"
            )
        if version == 0:
            # Explicit minimal legacy form; never infer another document as preferences.
            files = value.get("recent_files", [])
            if not isinstance(files, list):
                raise PreferencesError("invalid_field", "version 0 recent_files must be a list")
            recent: list[RecentSource] = []
            for path in files:
                item = RecentSource("csv", path)
                if item not in recent:
                    recent.append(item)
            if len(recent) > MAX_RECENT:
                raise PreferencesError("invalid_field", "version 0 has too many recent files")
            extra = {
                key: item
                for key, item in value.items()
                if key not in {"schema_version", "theme", "recent_files"}
            }
            return cls(value.get("theme", "system"), tuple(recent), "csv", extra)
        recent_values = value.get("recent_sources", [])
        if not isinstance(recent_values, list):
            raise PreferencesError("invalid_field", "recent_sources must be a list")
        return cls(
            value.get("theme", "system"),
            tuple(RecentSource.from_dict(item) for item in recent_values),
            value.get("default_source", "csv"),
            {
                key: item
                for key, item in value.items()
                if key not in {"schema_version", "theme", "recent_sources", "default_source"}
            },
        )


@dataclass(frozen=True, slots=True)
class PreferencesLoad:
    preferences: Preferences
    origin: str
    warning: str | None = None


def default_preferences_path() -> Path:
    """Use platform-local config storage, never the checkout/current directory."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        configured = os.environ.get("XDG_CONFIG_HOME")
        base = (
            Path(configured)
            if configured and Path(configured).is_absolute()
            else Path.home() / ".config"
        )
    return base / "LonghornBaja" / "dashboard" / "preferences.json"


class PreferencesStore:
    """Owner-thread local settings; failed load/save keeps the current object intact."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_preferences_path()
        self.backup_path = self.path.with_name(self.path.name + ".bak")
        self.current = Preferences()

    def _read(self, path: Path) -> tuple[bytes, Preferences]:
        with path.open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
        return data, Preferences.from_bytes(data)

    def reload(self) -> PreferencesLoad:
        failures = []
        missing = True
        for origin, path in (("primary", self.path), ("backup", self.backup_path)):
            try:
                _, preferences = self._read(path)
            except FileNotFoundError:
                continue
            except (OSError, PreferencesError) as error:
                missing = False
                failures.append(f"{path.name}: {error}")
                continue
            self.current = preferences
            warning = (
                "Recovered preferences from backup. " + "; ".join(failures)
                if origin == "backup"
                else None
            )
            return PreferencesLoad(preferences, origin, warning)
        warning = "; ".join(failures) if failures else None
        return PreferencesLoad(self.current, "defaults" if missing else "unchanged", warning)

    def _atomic_write(self, path: Path, data: bytes) -> None:
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=".preferences-", suffix=".tmp", dir=path.parent, delete=False
            ) as stream:
                temporary = stream.name
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink()
                except OSError:
                    pass

    def save(self, preferences: Preferences) -> None:
        if not isinstance(preferences, Preferences):
            raise PreferencesError("invalid_field", "save requires Preferences")
        data = preferences.to_bytes()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                old_data, _ = self._read(self.path)
            except PreferencesError as error:
                if error.code == "unsupported_version":
                    raise PreferencesError(
                        "unsupported_version",
                        "Refusing to overwrite an unsupported preferences version",
                    ) from error
                old_data = None
            except FileNotFoundError:
                old_data = None
            # A corrupt primary must never replace a valid recovery copy.
            if old_data is not None:
                self._atomic_write(self.backup_path, old_data)
            self._atomic_write(self.path, data)
        except OSError as error:
            raise PreferencesError(
                "save_failed", f"Could not save local preferences: {error}"
            ) from error
        self.current = preferences
