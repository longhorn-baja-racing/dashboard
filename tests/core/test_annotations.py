"""Session annotations must round-trip exactly, save atomically, and never touch the log."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from lbr_dashboard.core import annotations as module
from lbr_dashboard.core.annotations import (
    FORMAT_NAME,
    FORMAT_VERSION,
    Annotation,
    AnnotationFileError,
    AnnotationSet,
    LogFingerprint,
    LogStatus,
    fingerprint,
    load_annotations,
    new_annotation,
    save_annotations,
    sidecar_path,
)

LOG_BYTES = b"time_ns,front_left_rpm,gps_lat\r\n0,900,30.28\r\n1000000,950,30.29\r\n"
# Built from code points so this source file stays ASCII: e-acute, degree sign,
# two CJK characters, and a character outside the Basic Multilingual Plane.
NON_ASCII_TEXT = (
    f"Caf{chr(0xE9)} stop: temp 85 {chr(0xB0)}C, {chr(0x6E29)}{chr(0x5EA6)} ok {chr(0x1F697)}"
)


def _write_log(directory: Path, name: str = "run 1.csv", data: bytes = LOG_BYTES) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(data)
    return path


def _sample_set(log_path: Path) -> AnnotationSet:
    return AnnotationSet(
        fingerprint(log_path),
        (
            Annotation("b" * 32, 1_000_000, NON_ASCII_TEXT, "driver", "front_left_rpm"),
            Annotation("a" * 32, -5, "Before first sample", "", ""),
            Annotation("c" * 32, 2**63 - 1, "Line one\nLine two", "pit", "gps/lat"),
        ),
    )


def _temp_files(directory: Path) -> list[Path]:
    return [path for path in directory.iterdir() if path.name.endswith(".tmp")]


def _document(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "log": {"name": "run.csv", "size": 3, "sha256": "0" * 64},
        "annotations": [
            {"id": "one", "time_ns": 10, "category": "", "source_id": "rpm", "text": "note"}
        ],
    }
    document.update(overrides)
    return document


def _annotation_document(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": "one",
        "time_ns": 10,
        "category": "",
        "source_id": "",
        "text": "note",
    }
    entry.update(overrides)
    return _document(annotations=[entry])


# Schema --------------------------------------------------------------------


def test_new_annotation_generates_unique_hex_ids() -> None:
    first = new_annotation(5, "Shift", category="driver", source_id="gear")
    second = new_annotation(5, "Shift")
    assert first.annotation_id != second.annotation_id
    assert len(first.annotation_id) == 32
    assert int(first.annotation_id, 16) >= 0
    assert (first.time_ns, first.text, first.category, first.source_id) == (
        5,
        "Shift",
        "driver",
        "gear",
    )
    assert (second.category, second.source_id) == ("", "")


def test_annotation_is_immutable() -> None:
    annotation = new_annotation(1, "note")
    with pytest.raises(FrozenInstanceError):
        annotation.text = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("changes", "error", "message"),
    [
        ({"annotation_id": ""}, ValueError, "annotation_id must not be empty"),
        ({"annotation_id": " id"}, ValueError, "padded"),
        ({"annotation_id": "x" * 65}, ValueError, "at most 64"),
        ({"annotation_id": 7}, TypeError, "annotation_id must be a string"),
        ({"time_ns": True}, TypeError, "time_ns must be an integer"),
        ({"time_ns": 1.0}, TypeError, "time_ns must be an integer"),
        ({"time_ns": "1"}, TypeError, "time_ns must be an integer"),
        ({"time_ns": 2**63}, ValueError, "signed 64-bit"),
        ({"time_ns": -(2**63) - 1}, ValueError, "signed 64-bit"),
        ({"text": ""}, ValueError, "text must not be empty"),
        ({"text": " \n\t"}, ValueError, "text must not be empty"),
        ({"text": "x" * 4001}, ValueError, "at most 4000"),
        ({"text": None}, TypeError, "text must be a string"),
        ({"category": "x" * 65}, ValueError, "at most 64"),
        ({"category": None}, TypeError, "category must be a string"),
        ({"source_id": "x" * 257}, ValueError, "at most 256"),
        ({"source_id": 3}, TypeError, "source_id must be a string"),
    ],
)
def test_annotation_validation(
    changes: dict[str, Any], error: type[Exception], message: str
) -> None:
    fields: dict[str, Any] = {
        "annotation_id": "id",
        "time_ns": 0,
        "text": "note",
        "category": "",
        "source_id": "",
    }
    fields.update(changes)
    with pytest.raises(error, match=message):
        Annotation(**fields)


def test_annotation_accepts_limits() -> None:
    annotation = Annotation("x" * 64, -(2**63), "x" * 4000, "c" * 64, "s" * 256)
    assert annotation.time_ns == -(2**63)


@pytest.mark.parametrize(
    ("name", "size", "digest", "error", "message"),
    [
        (None, 0, "0" * 64, TypeError, "name must be a string"),
        ("log", True, "0" * 64, TypeError, "size must be an integer"),
        ("log", 1.0, "0" * 64, TypeError, "size must be an integer"),
        ("log", -1, "0" * 64, ValueError, "must not be negative"),
        ("log", 0, None, TypeError, "sha256 must be a string"),
        ("log", 0, "0" * 63, ValueError, "64 lowercase"),
        ("log", 0, "A" * 64, ValueError, "64 lowercase"),
    ],
)
def test_fingerprint_validation(
    name: Any, size: Any, digest: Any, error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        LogFingerprint(name, size, digest)


def test_fingerprint_matches_by_content_not_name(tmp_path: Path) -> None:
    log = _write_log(tmp_path)
    copy = _write_log(tmp_path, "renamed.csv")
    other = _write_log(tmp_path, "other.csv", LOG_BYTES + b"2000000,1000,30.30\r\n")
    assert fingerprint(log).name == "run 1.csv"
    assert fingerprint(log).size == len(LOG_BYTES)
    assert fingerprint(log).matches(fingerprint(copy))
    assert not fingerprint(log).matches(fingerprint(other))
    assert not fingerprint(log).matches(LogFingerprint("run 1.csv", len(LOG_BYTES), "0" * 64))


def test_fingerprint_reads_large_logs_in_chunks(tmp_path: Path) -> None:
    data = bytes(range(256)) * 9000  # Larger than one read chunk.
    log = _write_log(tmp_path, "big.bin", data)
    expected = LogFingerprint("big.bin", len(data), hashlib.sha256(data).hexdigest())
    assert fingerprint(log) == expected


def test_sidecar_path_sits_next_to_log(tmp_path: Path) -> None:
    log = tmp_path / "with spaces" / "run 1.csv"
    assert sidecar_path(log) == tmp_path / "with spaces" / "run 1.csv.annotations.json"


# Set operations ---------------------------------------------------------------


def _set(*items: Annotation) -> AnnotationSet:
    return AnnotationSet(LogFingerprint("run.csv", 3, "0" * 64), items)


def test_set_sorts_by_time_then_id() -> None:
    annotations = _set(Annotation("b", 10, "x"), Annotation("a", 10, "x"), Annotation("c", -1, "x"))
    assert [item.annotation_id for item in annotations.annotations] == ["c", "a", "b"]
    assert len(annotations) == 3


def test_set_rejects_duplicates_and_wrong_types() -> None:
    with pytest.raises(ValueError, match="Duplicate annotation id 'a'"):
        _set(Annotation("a", 1, "x"), Annotation("a", 2, "y"))
    with pytest.raises(TypeError, match="only Annotation objects"):
        _set("a")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="LogFingerprint"):
        AnnotationSet("log")  # type: ignore[arg-type]


def test_add_update_remove_return_new_sets() -> None:
    empty = _set()
    first = new_annotation(100, "Brake check", source_id="brake_pressure")
    one = empty.add(first)
    assert len(empty) == 0
    assert one.get(first.annotation_id) == first

    moved = one.update(first.annotation_id, time_ns=50, text="Brake fade", category="driver")
    changed = moved.get(first.annotation_id)
    assert (changed.time_ns, changed.text, changed.category, changed.source_id) == (
        50,
        "Brake fade",
        "driver",
        "brake_pressure",
    )
    assert one.get(first.annotation_id) == first
    assert moved.update(first.annotation_id, source_id="").get(first.annotation_id).source_id == ""
    assert moved.update(first.annotation_id) == moved

    removed = moved.remove(first.annotation_id)
    assert len(removed) == 0
    assert len(moved) == 1


def test_edit_errors_name_the_id_and_keep_the_set() -> None:
    annotations = _set(Annotation("a", 1, "x"))
    with pytest.raises(KeyError, match="missing"):
        annotations.remove("missing")
    with pytest.raises(KeyError, match="missing"):
        annotations.update("missing", text="y")
    with pytest.raises(ValueError, match="Duplicate annotation id 'a'"):
        annotations.add(Annotation("a", 2, "y"))
    with pytest.raises(ValueError, match="text must not be empty"):
        annotations.update("a", text="  ")
    assert annotations.annotations == (Annotation("a", 1, "x"),)


def test_navigation_moves_between_marker_times() -> None:
    annotations = _set(
        Annotation("a", 10, "x"),
        Annotation("b", 20, "x"),
        Annotation("c", 20, "x"),
        Annotation("d", 40, "x"),
    )
    next_after = annotations.next_after
    previous_before = annotations.previous_before
    assert next_after(-100) == annotations.get("a")
    assert next_after(10) == annotations.get("b")
    assert next_after(20) == annotations.get("d")
    assert next_after(40) is None
    assert previous_before(100) == annotations.get("d")
    assert previous_before(40) == annotations.get("c")
    assert previous_before(20) == annotations.get("a")
    assert previous_before(10) is None
    assert _set().next_after(0) is None
    assert _set().previous_before(0) is None


def test_in_range_is_closed() -> None:
    annotations = _set(Annotation("a", 10, "x"), Annotation("b", 20, "x"), Annotation("c", 30, "x"))
    assert [item.annotation_id for item in annotations.in_range(10, 20)] == ["a", "b"]
    assert [item.annotation_id for item in annotations.in_range(20, 20)] == ["b"]
    assert annotations.in_range(11, 19) == ()
    with pytest.raises(ValueError, match="end_ns"):
        annotations.in_range(5, 4)


def test_at_or_near_picks_closest_within_distance() -> None:
    annotations = _set(Annotation("a", 10, "x"), Annotation("b", 20, "x"), Annotation("c", 31, "x"))
    assert annotations.at_or_near(10, 0) == annotations.get("a")
    assert annotations.at_or_near(12, 5) == annotations.get("a")
    assert annotations.at_or_near(15, 5) == annotations.get("a")  # Tie -> earlier marker.
    assert annotations.at_or_near(29, 5) == annotations.get("c")
    assert annotations.at_or_near(25, 4) is None
    assert annotations.at_or_near(25, 5) == annotations.get("b")
    with pytest.raises(ValueError, match="must not be negative"):
        annotations.at_or_near(0, -1)


def test_merge_adds_new_ids_and_reports_conflicts() -> None:
    mine = _set(Annotation("same", 1, "x"), Annotation("clash", 2, "mine"))
    theirs = _set(
        Annotation("same", 1, "x"),
        Annotation("clash", 2, "theirs"),
        Annotation("new", 3, "imported", "pit", "gps/lat"),
    )
    result = mine.merge(theirs)
    assert result.added == ("new",)
    assert result.conflicts == ("clash",)
    assert result.annotations.get("clash").text == "mine"
    assert result.annotations.get("new") == theirs.get("new")
    assert len(result.annotations) == 3
    assert len(mine) == 2


def test_merge_rejects_other_log() -> None:
    mine = _set()
    other = AnnotationSet(LogFingerprint("run.csv", 4, "0" * 64))
    with pytest.raises(ValueError, match="different log"):
        mine.merge(other)
    renamed = AnnotationSet(LogFingerprint("renamed.csv", 3, "0" * 64))
    assert mine.merge(renamed).added == ()


# Storage --------------------------------------------------------------------


def test_round_trip_preserves_exact_annotations(tmp_path: Path) -> None:
    log = _write_log(tmp_path / "Session folder with spaces")
    annotations = _sample_set(log)
    target = sidecar_path(log)

    save_annotations(target, annotations)
    loaded = load_annotations(target, log_path=log)

    assert loaded.status is LogStatus.OK
    assert loaded.annotations == annotations
    assert loaded.current_fingerprint == annotations.fingerprint
    assert loaded.annotations.get("b" * 32).text == NON_ASCII_TEXT
    assert {item.source_id for item in loaded.annotations.annotations} == {
        "",
        "front_left_rpm",
        "gps/lat",
    }
    assert _temp_files(target.parent) == []


def test_saved_file_is_stable_versioned_utf8_json(tmp_path: Path) -> None:
    log = _write_log(tmp_path)
    target = sidecar_path(log)
    save_annotations(target, _sample_set(log))
    first = target.read_bytes()
    save_annotations(target, _sample_set(log))

    assert target.read_bytes() == first
    assert first.endswith(b"}\n")
    assert b"\r\n" not in first
    assert NON_ASCII_TEXT.encode("utf-8") in first
    document = json.loads(first.decode("utf-8"))
    assert list(document) == ["format", "version", "log", "annotations"]
    assert document["format"] == FORMAT_NAME
    assert document["version"] == 1
    assert document["log"] == {
        "name": "run 1.csv",
        "size": len(LOG_BYTES),
        "sha256": fingerprint(log).sha256,
    }
    assert [list(item) for item in document["annotations"]] == [
        ["id", "time_ns", "category", "source_id", "text"]
    ] * 3
    assert [item["time_ns"] for item in document["annotations"]] == [-5, 1_000_000, 2**63 - 1]


def test_save_and_load_never_modify_the_log(tmp_path: Path) -> None:
    log = _write_log(tmp_path)
    os.chmod(log, stat.S_IREAD)
    try:
        before = log.stat()
        target = sidecar_path(log)
        annotations = AnnotationSet(fingerprint(log)).add(new_annotation(0, "start"))
        save_annotations(target, annotations)
        loaded = load_annotations(target, log_path=log)
        loaded.annotations.remove(annotations.annotations[0].annotation_id)
        after = log.stat()
    finally:
        os.chmod(log, stat.S_IREAD | stat.S_IWRITE)
    assert log.read_bytes() == LOG_BYTES
    assert after.st_mtime_ns == before.st_mtime_ns
    assert after.st_size == before.st_size
    assert loaded.status is LogStatus.OK


def test_save_refuses_to_overwrite_the_log(tmp_path: Path) -> None:
    log = _write_log(tmp_path)
    with pytest.raises(AnnotationFileError, match="overwrite the log"):
        save_annotations(log, AnnotationSet(fingerprint(log)))
    assert log.read_bytes() == LOG_BYTES
    assert _temp_files(tmp_path) == []


def test_missing_sidecar_starts_empty_without_writing(tmp_path: Path) -> None:
    log = _write_log(tmp_path)
    target = sidecar_path(log)
    loaded = load_annotations(target, log_path=log)
    assert loaded.status is LogStatus.OK
    assert loaded.annotations == AnnotationSet(fingerprint(log))
    assert not target.exists()


def test_missing_sidecar_without_log_is_an_error(tmp_path: Path) -> None:
    target = tmp_path / "gone.csv.annotations.json"
    with pytest.raises(AnnotationFileError, match="no log to start from"):
        load_annotations(target, log_path=tmp_path / "gone.csv")
    with pytest.raises(AnnotationFileError, match="no log to start from"):
        load_annotations(target)


def test_moved_log_with_sidecar_still_matches(tmp_path: Path) -> None:
    log = _write_log(tmp_path / "laptop")
    annotations = _sample_set(log)
    save_annotations(sidecar_path(log), annotations)

    moved_dir = tmp_path / "shared drive" / "endurance day"
    moved_dir.mkdir(parents=True)
    moved_log = log.replace(moved_dir / "endurance.csv")
    moved_sidecar = sidecar_path(log).replace(sidecar_path(moved_log))

    loaded = load_annotations(moved_sidecar, log_path=moved_log)
    assert loaded.status is LogStatus.OK
    assert loaded.annotations == annotations
    assert loaded.annotations.fingerprint.name == "run 1.csv"
    assert loaded.current_fingerprint is not None
    assert loaded.current_fingerprint.name == "endurance.csv"


def test_changed_log_is_reported_and_can_be_reattached(tmp_path: Path) -> None:
    log = _write_log(tmp_path)
    annotations = _sample_set(log)
    target = sidecar_path(log)
    save_annotations(target, annotations)
    log.write_bytes(LOG_BYTES + b"2000000,1000,30.30\r\n")

    loaded = load_annotations(target, log_path=log)
    assert loaded.status is LogStatus.LOG_CHANGED
    assert loaded.annotations == annotations
    assert loaded.current_fingerprint == fingerprint(log)

    assert loaded.current_fingerprint is not None
    save_annotations(target, loaded.annotations.with_fingerprint(loaded.current_fingerprint))
    assert load_annotations(target, log_path=log).status is LogStatus.OK


def test_missing_log_is_reported(tmp_path: Path) -> None:
    log = _write_log(tmp_path)
    annotations = _sample_set(log)
    target = sidecar_path(log)
    save_annotations(target, annotations)
    log.unlink()

    loaded = load_annotations(target, log_path=log)
    assert loaded.status is LogStatus.LOG_MISSING
    assert loaded.current_fingerprint is None
    assert loaded.annotations == annotations


def test_load_without_log_is_not_checked(tmp_path: Path) -> None:
    log = _write_log(tmp_path)
    target = tmp_path / "exported notes.json"
    save_annotations(target, _sample_set(log))
    loaded = load_annotations(target)
    assert loaded.status is LogStatus.NOT_CHECKED
    assert loaded.current_fingerprint is None
    assert loaded.annotations == _sample_set(log)


def test_unreadable_log_propagates_os_error(tmp_path: Path) -> None:
    log = _write_log(tmp_path)
    target = sidecar_path(log)
    save_annotations(target, _sample_set(log))
    with pytest.raises(OSError):
        load_annotations(target, log_path=tmp_path)  # A directory, not a log.


# Interrupted saves --------------------------------------------------------------


def _saved_twice(tmp_path: Path) -> tuple[Path, AnnotationSet, bytes]:
    log = _write_log(tmp_path / "dir with spaces")
    target = sidecar_path(log)
    original = _sample_set(log)
    save_annotations(target, original)
    return target, original.add(new_annotation(7, "newer note")), target.read_bytes()


def test_failed_replace_keeps_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, newer, before = _saved_twice(tmp_path)

    def fail_replace(source: object, destination: object) -> None:
        raise PermissionError("file is locked")

    monkeypatch.setattr(module.os, "replace", fail_replace)
    with pytest.raises(AnnotationFileError, match="previous file kept: file is locked") as caught:
        save_annotations(target, newer)
    assert caught.value.path == target
    assert str(target) in str(caught.value)
    assert target.read_bytes() == before
    assert _temp_files(target.parent) == []


def test_failed_flush_keeps_previous_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target, newer, before = _saved_twice(tmp_path)

    def fail_fsync(descriptor: int) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(module.os, "fsync", fail_fsync)
    with pytest.raises(AnnotationFileError, match="No space left"):
        save_annotations(target, newer)
    assert target.read_bytes() == before
    assert _temp_files(target.parent) == []


def test_interrupt_during_save_cleans_up_and_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, newer, before = _saved_twice(tmp_path)

    def interrupted(descriptor: int) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(module.os, "fsync", interrupted)
    with pytest.raises(KeyboardInterrupt):
        save_annotations(target, newer)
    assert target.read_bytes() == before
    assert _temp_files(target.parent) == []


def test_failed_cleanup_still_reports_save_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, newer, before = _saved_twice(tmp_path)

    def fail(*args: object) -> None:
        raise OSError("disk went away")

    monkeypatch.setattr(module.os, "replace", fail)
    monkeypatch.setattr(module.os, "remove", fail)
    with pytest.raises(AnnotationFileError, match="disk went away"):
        save_annotations(target, newer)
    monkeypatch.undo()
    assert target.read_bytes() == before
    leftovers = _temp_files(target.parent)
    assert len(leftovers) == 1
    leftovers[0].unlink()


def test_save_into_missing_directory_fails_cleanly(tmp_path: Path) -> None:
    log = _write_log(tmp_path)
    target = tmp_path / "missing dir" / "notes.json"
    with pytest.raises(AnnotationFileError, match="cannot create temporary file"):
        save_annotations(target, _sample_set(log))
    assert not target.parent.exists()


def test_oversized_save_is_rejected_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, newer, before = _saved_twice(tmp_path)
    monkeypatch.setattr(module, "MAX_FILE_BYTES", len(before))
    with pytest.raises(AnnotationFileError, match="would exceed"):
        save_annotations(target, newer)
    assert target.read_bytes() == before
    assert _temp_files(target.parent) == []


# Load validation --------------------------------------------------------------


def _load_bytes(tmp_path: Path, data: bytes) -> AnnotationSet:
    target = tmp_path / "notes file.json"
    target.write_bytes(data)
    return load_annotations(target).annotations


def _load_document(tmp_path: Path, document: object) -> AnnotationSet:
    return _load_bytes(tmp_path, json.dumps(document).encode("utf-8"))


def test_load_accepts_byte_order_mark_and_crlf(tmp_path: Path) -> None:
    text = json.dumps(_document(), indent=2).replace("\n", "\r\n")
    loaded = _load_bytes(tmp_path, b"\xef\xbb\xbf" + text.encode("utf-8"))
    assert loaded.get("one").source_id == "rpm"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"{not json", "invalid JSON"),
        (b"", "invalid JSON"),
        (b'{"format": "a", "format": "b"}', "duplicate key 'format'"),
        (b'{"x": NaN}', "NaN is not allowed"),
        (b'{"x": -Infinity}', "-Infinity is not allowed"),
        (b"\xff\xfe{}", "not valid UTF-8"),
    ],
)
def test_load_rejects_malformed_bytes(tmp_path: Path, data: bytes, message: str) -> None:
    with pytest.raises(AnnotationFileError, match=message):
        _load_bytes(tmp_path, data)


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ([], "document must be a JSON object"),
        ({"format": FORMAT_NAME}, "document is missing 'version', 'log', 'annotations'"),
        (_document(extra=1), "document has unknown 'extra'"),
        (_document(format="other-tool"), "format must be 'lbr-dashboard-annotations'"),
        (_document(version=2), "unsupported version 2"),
        (_document(version=True), "unsupported version True"),
        (_document(version=1.0), "unsupported version 1.0"),
        (_document(version="1"), "unsupported version '1'"),
        (_document(log=None), "log must be a JSON object"),
        (_document(log={"name": "a", "size": 1}), "log is missing 'sha256'"),
        (_document(log={"name": "a", "size": -1, "sha256": "0" * 64}), "must not be negative"),
        (_document(log={"name": "a", "size": 1, "sha256": "zz"}), "64 lowercase"),
        (_document(annotations={}), "annotations must be a JSON array"),
        (_document(annotations=[1]), "annotation 0 must be a JSON object"),
        (_annotation_document(note="x"), "annotation 0 has unknown 'note'"),
        (_document(annotations=[{"id": "a"}]), "annotation 0 is missing 'time_ns'"),
        (_annotation_document(time_ns=True), "annotation 0: time_ns must be an integer"),
        (_annotation_document(time_ns=1.5), "annotation 0: time_ns must be an integer"),
        (_annotation_document(time_ns=2**63), "annotation 0: time_ns must fit"),
        (_annotation_document(text=""), "annotation 0: text must not be empty"),
        (_annotation_document(text="x" * 4001), "annotation 0: text must be at most 4000"),
        (_annotation_document(id=""), "annotation 0: annotation_id must not be empty"),
        (_annotation_document(category=None), "annotation 0: category must be a string"),
        (_annotation_document(source_id=[]), "annotation 0: source_id must be a string"),
    ],
)
def test_load_rejects_invalid_documents(tmp_path: Path, document: object, message: str) -> None:
    with pytest.raises(AnnotationFileError, match=message):
        _load_document(tmp_path, document)


def test_load_names_index_of_duplicate_id(tmp_path: Path) -> None:
    entry = {"id": "dup", "time_ns": 1, "category": "", "source_id": "", "text": "x"}
    other = {**entry, "id": "ok"}
    with pytest.raises(AnnotationFileError, match="annotation 2: duplicate id 'dup'"):
        _load_document(tmp_path, _document(annotations=[entry, other, entry]))


def test_load_rejects_oversized_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = json.dumps(_document()).encode("utf-8")
    monkeypatch.setattr(module, "MAX_FILE_BYTES", len(data) - 1)
    with pytest.raises(AnnotationFileError, match="larger than"):
        _load_bytes(tmp_path, data)
    monkeypatch.setattr(module, "MAX_FILE_BYTES", len(data))
    assert len(_load_bytes(tmp_path, data)) == 1


def test_load_reports_unreadable_annotation_file(tmp_path: Path) -> None:
    with pytest.raises(AnnotationFileError, match="cannot read file") as caught:
        load_annotations(tmp_path)  # A directory, not a file.
    assert caught.value.path == tmp_path
    assert caught.value.reason.startswith("cannot read file")
