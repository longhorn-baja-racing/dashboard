"""Workspace files are written atomically and recover from a damaged primary."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lbr_dashboard.workspace import (
    SignalBinding,
    WidgetConfig,
    WidgetRef,
    Workspace,
    WorkspaceError,
    backup_path,
    codec,
    load_workspace,
    read_workspace,
    save_workspace,
    storage,
)


def make(name: str) -> Workspace:
    return Workspace(
        name=name,
        widgets=(WidgetConfig("values", "value_cards", bindings=(SignalBinding("speed"),)),),
        layout=WidgetRef("values"),
    )


def leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name.endswith(".tmp"))


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "layout.json"
    save_workspace(path, make("First"))
    assert path.read_bytes().isascii()
    result = load_workspace(path)
    assert result.workspace == make("First")
    assert result.source == path and not result.recovered and result.primary_error is None
    assert not backup_path(path).exists()
    assert leftovers(tmp_path) == []


def test_saving_over_a_valid_file_keeps_it_as_backup(tmp_path: Path) -> None:
    path = tmp_path / "layout.json"
    save_workspace(path, make("First"))
    save_workspace(path, make("Second"))
    assert read_workspace(path).name == "Second"
    assert read_workspace(backup_path(path)).name == "First"


def test_a_damaged_primary_never_replaces_a_good_backup(tmp_path: Path) -> None:
    path = tmp_path / "layout.json"
    save_workspace(path, make("First"))
    save_workspace(path, make("Second"))
    path.write_text('{"truncated": ')
    save_workspace(path, make("Third"))
    assert read_workspace(path).name == "Third"
    assert read_workspace(backup_path(path)).name == "First"


def test_interrupted_replace_preserves_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "layout.json"
    save_workspace(path, make("First"))
    original = path.read_bytes()
    real_replace = os.replace

    def fail_on_primary(source: str, target: str | Path) -> None:
        if Path(target) == path:
            raise OSError(28, "No space left on device")
        real_replace(source, target)

    monkeypatch.setattr(storage.os, "replace", fail_on_primary)
    with pytest.raises(WorkspaceError) as caught:
        save_workspace(path, make("Second"))
    assert caught.value.codes == {"io"}
    assert "No space left on device" in str(caught.value)
    assert path.read_bytes() == original
    assert leftovers(tmp_path) == []


def test_interruption_during_write_removes_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "layout.json"
    save_workspace(path, make("First"))
    original = path.read_bytes()

    def interrupted(descriptor: int) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(storage.os, "fsync", interrupted)
    with pytest.raises(KeyboardInterrupt):
        save_workspace(path, make("Second"))
    assert path.read_bytes() == original
    assert leftovers(tmp_path) == []


def test_temporary_file_already_gone_is_tolerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "layout.json"

    def vanish(source: str, target: str | Path) -> None:
        os.unlink(source)
        raise OSError(5, "I/O error")

    monkeypatch.setattr(storage.os, "replace", vanish)
    with pytest.raises(WorkspaceError):
        save_workspace(path, make("First"))
    assert not path.exists()


def test_invalid_workspace_is_never_written(tmp_path: Path) -> None:
    path = tmp_path / "layout.json"
    save_workspace(path, make("First"))
    original = path.read_bytes()
    with pytest.raises(WorkspaceError):
        save_workspace(path, make("C:\\Users\\driver"))
    assert path.read_bytes() == original


def test_unwritable_location_reports_io_issue(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError) as caught:
        save_workspace(tmp_path / "missing-dir" / "layout.json", make("First"))
    assert caught.value.codes == {"io"}


def test_corrupt_primary_recovers_from_backup(tmp_path: Path) -> None:
    path = tmp_path / "layout.json"
    save_workspace(path, make("First"))
    save_workspace(path, make("Second"))
    path.write_bytes(b"\x00garbage")
    result = load_workspace(path)
    assert result.recovered and result.source == backup_path(path)
    assert result.workspace.name == "First"
    assert result.primary_error is not None and result.primary_error.codes == {"json"}
    assert path.read_bytes() == b"\x00garbage"
    with pytest.raises(WorkspaceError):
        load_workspace(path, use_backup=False)


def test_missing_primary_without_backup_reports_io(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError) as caught:
        load_workspace(tmp_path / "absent.json")
    assert caught.value.codes == {"io"}


def test_when_both_files_are_bad_the_primary_error_is_raised(tmp_path: Path) -> None:
    path = tmp_path / "layout.json"
    path.write_text("[")
    backup_path(path).write_text("{}")
    with pytest.raises(WorkspaceError) as caught:
        load_workspace(path)
    assert caught.value.codes == {"json"}


def test_oversized_file_is_refused_without_reading_it_all(tmp_path: Path) -> None:
    path = tmp_path / "huge.json"
    path.write_bytes(b" " * (codec.MAX_DOCUMENT_BYTES * 2))
    with pytest.raises(WorkspaceError) as caught:
        read_workspace(path)
    assert caught.value.codes == {"too_large"}


def test_a_directory_is_not_a_workspace(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError) as caught:
        read_workspace(tmp_path)
    assert caught.value.codes == {"io"}
