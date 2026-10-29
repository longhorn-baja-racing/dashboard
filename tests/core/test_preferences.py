"""Local JSON validation, recovery and interrupted-save acceptance checks."""

import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any, cast

import pytest

from lbr_dashboard.core import Preferences, PreferencesError, PreferencesStore, RecentSource
from lbr_dashboard.core.preferences import MAX_BYTES, default_preferences_path


def encoded(**fields):
    return json.dumps({"schema_version": 1, **fields}).encode()


def test_preferences_and_recent_source_extensions_round_trip_exactly_and_are_immutable() -> None:
    raw: Any = {"future": {" k ": [1, 2]}, "": True}
    recent = RecentSource("custom-adapter", "C:/測定/α & β.csv", {" k ": {"raw": [True, 0]}})
    prefs = Preferences("dark", (recent,), "custom-adapter", raw)
    raw["future"][" k "].append(3)
    assert Preferences.from_bytes(prefs.to_bytes()) == prefs
    assert prefs.extra["future"] == {" k ": (1, 2)}
    with pytest.raises(TypeError):
        cast(Any, prefs.extra)["future"][" k "][0] = 5
    with pytest.raises(FrozenInstanceError):
        cast(Any, prefs).theme = "light"
    copy = prefs.to_dict()
    copy["future"][" k "].append(99)
    assert Preferences.from_bytes(prefs.to_bytes()) == prefs


def test_remember_moves_to_front_keeps_distinct_source_kinds_and_caps_at_ten() -> None:
    prefs = Preferences()
    for i in range(12):
        prefs = prefs.remember(RecentSource("csv", f"C:/data/{i}.csv"))
    assert len(prefs.recent_sources) == 10 and prefs.recent_sources[-1].location == "C:/data/2.csv"
    item = prefs.recent_sources[4]
    prefs = prefs.remember(item)
    assert prefs.recent_sources[0] == item and len(prefs.recent_sources) == 10
    prefs = prefs.remember(RecentSource("serial", item.location))
    assert prefs.default_source == "serial"
    assert prefs.recent_sources[1] == item
    with pytest.raises(PreferencesError):
        prefs.remember(cast(Any, None))


def test_store_persists_across_actual_process_restart(tmp_path) -> None:
    path = tmp_path / "nested/preferences.json"
    expected = Preferences(
        "dark",
        (
            RecentSource("csv", "C:/unrelated/temperature.csv"),
            RecentSource("custom-source", "COM7"),
        ),
        "custom-source",
    )
    store = PreferencesStore(path)
    store.save(expected)
    script = """
import sys
from pathlib import Path
from lbr_dashboard.core import PreferencesStore
store=PreferencesStore(Path(sys.argv[1]))
result=store.reload()
assert result.origin=='primary'
assert result.preferences.theme=='dark'
assert result.preferences.recent_sources[0].location=='C:/unrelated/temperature.csv'
assert result.preferences.recent_sources[1].kind=='custom-source'
sys.stdout.buffer.write(result.preferences.to_bytes())
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")}
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)], env=env, capture_output=True, timeout=10
    )
    assert result.returncode == 0, result.stderr.decode()
    assert Preferences.from_bytes(result.stdout) == expected


def test_backup_recovery_does_not_promote_corrupt_primary_and_invalid_load_keeps_active_state(
    tmp_path,
) -> None:
    path = tmp_path / "preferences.json"
    store = PreferencesStore(path)
    old = Preferences("light")
    new = Preferences("dark")
    store.save(old)
    store.save(new)
    assert Preferences.from_bytes(store.backup_path.read_bytes()) == old
    path.write_bytes(b"{broken")
    recovered = PreferencesStore(path)
    result = recovered.reload()
    assert result.origin == "backup" and result.preferences == old and result.warning
    recovered.save(Preferences("system"))
    assert Preferences.from_bytes(recovered.backup_path.read_bytes()) == old
    assert recovered.reload().origin == "primary"
    path.write_bytes(b"bad")
    store.backup_path.write_bytes(b"bad too")
    before = recovered.current
    result = recovered.reload()
    assert result.origin == "unchanged" and result.preferences is before and result.warning


def test_missing_files_keep_defaults_and_missing_primary_can_recover_backup(tmp_path) -> None:
    store = PreferencesStore(tmp_path / "preferences.json")
    result = store.reload()
    assert (
        result.origin == "defaults"
        and result.preferences == Preferences()
        and result.warning is None
    )
    store.backup_path.write_bytes(Preferences("dark").to_bytes())
    assert store.reload().origin == "backup"


@pytest.mark.parametrize("stage", ["backup-replace", "primary-replace", "fsync", "write", "mkdir"])
def test_interrupted_save_preserves_previous_file_and_active_object(
    tmp_path, monkeypatch, stage
) -> None:
    from lbr_dashboard.core import preferences as module

    path = tmp_path / "preferences.json"
    store = PreferencesStore(path)
    store.save(Preferences("light"))
    before = path.read_bytes()
    active = store.current
    real_replace = module.os.replace

    def replace_file(source, target):
        if (stage == "backup-replace" and Path(target) == store.backup_path) or (
            stage == "primary-replace" and Path(target) == path
        ):
            raise OSError("injected replace interruption")
        return real_replace(source, target)

    def fail(*args, **kwargs):
        raise OSError("injected I/O interruption")

    monkeypatch.setattr(module.os, "replace", replace_file)
    if stage == "fsync":
        monkeypatch.setattr(module.os, "fsync", fail)
    if stage == "write":
        monkeypatch.setattr(module.tempfile, "NamedTemporaryFile", fail)
    if stage == "mkdir":
        monkeypatch.setattr(Path, "mkdir", fail)
    with pytest.raises(PreferencesError) as error:
        store.save(Preferences("dark"))
    assert error.value.code == "save_failed"
    assert path.read_bytes() == before and store.current is active
    assert not list(tmp_path.glob(".preferences-*.tmp"))


def test_future_version_is_never_overwritten_even_after_backup_recovery(tmp_path) -> None:
    store = PreferencesStore(tmp_path / "preferences.json")
    store.path.write_bytes(encoded(schema_version=99, theme="new-theme"))
    store.backup_path.write_bytes(Preferences("light").to_bytes())
    store.reload()
    before = store.path.read_bytes()
    active = store.current
    with pytest.raises(PreferencesError, match="unsupported"):
        store.save(Preferences("dark"))
    assert store.path.read_bytes() == before and store.current is active


def test_version_zero_migration_preserves_metadata_and_saves_current_version(
    tmp_path,
) -> None:
    legacy = json.dumps(
        {
            "schema_version": 0,
            "theme": "dark",
            "recent_files": ["a.csv", "b.csv", "a.csv"],
            "future": {" k ": [1, 2]},
        }
    ).encode()
    prefs = Preferences.from_bytes(legacy)
    assert prefs.recent_sources == (RecentSource("csv", "a.csv"), RecentSource("csv", "b.csv"))
    assert prefs.extra["future"] == {" k ": (1, 2)}
    assert Preferences.from_bytes(legacy).to_bytes() == prefs.to_bytes()
    store = PreferencesStore(tmp_path / "preferences.json")
    store.path.write_bytes(legacy)
    assert store.reload().preferences == prefs
    store.save(prefs)
    assert json.loads(store.path.read_bytes())["schema_version"] == 1
    assert store.backup_path.read_bytes() == legacy


@pytest.mark.parametrize(
    "fields",
    [
        {"schema_version": None},
        {"schema_version": True},
        {"schema_version": "1"},
        {"schema_version": 2},
        {"theme": "invalid"},
        {"theme": True},
        {"recent_sources": {}},
        {"recent_sources": [{}]},
        {"recent_sources": [{"kind": "BAD", "location": "x"}]},
        {"recent_sources": [{"kind": "csv", "location": ""}]},
        {"recent_sources": [{"kind": "csv", "location": "a"}] * 2},
        {"recent_sources": [{"kind": "csv", "location": str(i)} for i in range(11)]},
        {"default_source": " BAD "},
        {"default_source": None},
        {"unknown": float("nan")},
        {"unknown": float("inf")},
        {"schema_version": 0, "recent_files": "bad"},
        {"schema_version": 0, "recent_files": [None]},
        {"schema_version": 0, "recent_files": [str(i) for i in range(11)]},
        {"schema_version": 0, "default_source": "reserved-extra"},
    ],
)
def test_invalid_documents_do_not_partially_change_store(tmp_path, fields) -> None:
    store = PreferencesStore(tmp_path / "preferences.json")
    store.current = Preferences("dark")
    store.path.write_bytes(encoded(**fields))
    result = store.reload()
    assert result.preferences is store.current and store.current.theme == "dark"
    assert result.origin == "unchanged" and result.warning


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"{",
        b"[]",
        b"null",
        b"\xff",
        b'{"schema_version":1,"schema_version":0}',
        b'{"schema_version":1,"x":{"k":1,"k":2}}',
        b" " * (MAX_BYTES + 1),
    ],
    ids=[
        "empty",
        "truncated",
        "array",
        "null",
        "encoding",
        "duplicate-root",
        "duplicate-nested",
        "oversized",
    ],
)
def test_malformed_duplicate_and_oversized_json_is_rejected(data) -> None:
    with pytest.raises(PreferencesError):
        Preferences.from_bytes(data)


def test_depth_node_string_byte_and_unsupported_value_limits() -> None:
    nested: Any = 0
    for _ in range(18):
        nested = [nested]
    for extra in (
        {"x": nested},
        {"x": [0] * 4096},
        {"x": "x" * 8193},
        {"k" * 129: 1},
        {1: "bad"},
        {"x": object()},
        {"theme": "collision"},
    ):
        with pytest.raises(PreferencesError):
            Preferences(extra=cast(Any, extra))
    cycle: Any = {}
    cycle["self"] = cycle
    with pytest.raises(PreferencesError):
        Preferences(extra=cycle)
    large = Preferences(extra={str(i): "x" * 8000 for i in range(10)})
    with pytest.raises(PreferencesError, match="64 KiB"):
        large.to_bytes()
    invalid_unicode = Preferences(extra={"x": "\ud800"})
    with pytest.raises(PreferencesError, match="UTF-8"):
        invalid_unicode.to_bytes()
    with pytest.raises(PreferencesError):
        Preferences.from_bytes(cast(Any, "{}"))


def test_direct_invalid_constructor_fields() -> None:
    for action in (
        lambda: Preferences(recent_sources=cast(Any, None)),
        lambda: Preferences(recent_sources=cast(Any, (1,))),
        lambda: Preferences(extra=cast(Any, [])),
        lambda: RecentSource("csv", "bad\0path"),
        lambda: RecentSource("csv", "a" * 8193),
        lambda: RecentSource.from_dict([]),
    ):
        with pytest.raises(PreferencesError):
            action()


def test_invalid_save_does_not_touch_existing_data(tmp_path) -> None:
    store = PreferencesStore(tmp_path / "preferences.json")
    store.save(Preferences("dark"))
    before = store.path.read_bytes()
    with pytest.raises(PreferencesError):
        store.save(cast(Any, None))
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_platform_default_is_local_not_current_directory(monkeypatch, tmp_path, platform) -> None:
    from lbr_dashboard.core import preferences as module

    monkeypatch.setattr(module.sys, "platform", platform)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    expected = (
        tmp_path / "local"
        if platform == "win32"
        else tmp_path / "home/Library/Application Support"
        if platform == "darwin"
        else tmp_path / "config"
    )
    assert default_preferences_path() == expected / "LonghornBaja/dashboard/preferences.json"
    monkeypatch.delenv("LOCALAPPDATA")
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative")
    fallback = default_preferences_path()
    assert fallback.is_absolute() and fallback != Path.cwd() / "preferences.json"


def test_unreadable_primary_is_not_silently_replaced_without_backup(tmp_path, monkeypatch) -> None:
    store = PreferencesStore(tmp_path / "preferences.json")
    store.save(Preferences("light"))
    before = store.path.read_bytes()
    active = store.current

    def fail(path):
        raise PermissionError("Cannot read existing settings")

    monkeypatch.setattr(store, "_read", fail)
    with pytest.raises(PreferencesError, match="Cannot read"):
        store.save(Preferences("dark"))
    assert store.path.read_bytes() == before and store.current is active
