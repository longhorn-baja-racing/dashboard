"""User session annotations stored in a sidecar file next to the raw log.

Annotations are notes a person attaches to a session time. They are kept
apart from the log and from any firmware/log events in it: this module opens
the log only for reading (to fingerprint it) and never writes to it.

Times are integer nanoseconds on the same axis as the session's sample times,
so a saved marker selects exactly the session time it was placed at. Range
queries use CLOSED intervals: an annotation at exactly ``start_ns`` or
``end_ns`` is inside.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from bisect import bisect_left, bisect_right
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any

FORMAT_NAME = "lbr-dashboard-annotations"
FORMAT_VERSION = 1
SIDECAR_SUFFIX = ".annotations.json"

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_ID_LENGTH = 64
MAX_TEXT_LENGTH = 4000
MAX_CATEGORY_LENGTH = 64
MAX_SOURCE_ID_LENGTH = 256

_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
_HASH_CHUNK_BYTES = 1024 * 1024
_HEX_DIGITS = frozenset("0123456789abcdef")


class AnnotationFileError(Exception):
    """An annotation file could not be read or written; names the path and reason."""

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


def _check_int64(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not _INT64_MIN <= value <= _INT64_MAX:
        raise ValueError(f"{name} must fit in a signed 64-bit integer")
    return value


def _check_str(name: str, value: object, max_length: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if required and not value.strip():
        raise ValueError(f"{name} must not be empty")
    if len(value) > max_length:
        raise ValueError(f"{name} must be at most {max_length} characters")
    return value


@dataclass(frozen=True, slots=True)
class Annotation:
    """One timestamped user note.

    ``source_id`` names the signal or source the note is about and may be empty
    for a note about the whole session. ``category`` is a free-form label and
    may be empty. ``text`` must contain something other than whitespace.
    """

    annotation_id: str
    time_ns: int
    text: str
    category: str = ""
    source_id: str = ""

    def __post_init__(self) -> None:
        identifier = _check_str("annotation_id", self.annotation_id, MAX_ID_LENGTH, required=True)
        if identifier != identifier.strip():
            raise ValueError("annotation_id must not be padded with whitespace")
        _check_int64("time_ns", self.time_ns)
        _check_str("text", self.text, MAX_TEXT_LENGTH, required=True)
        _check_str("category", self.category, MAX_CATEGORY_LENGTH)
        _check_str("source_id", self.source_id, MAX_SOURCE_ID_LENGTH)


def new_annotation(
    time_ns: int, text: str, *, category: str = "", source_id: str = ""
) -> Annotation:
    """Create an annotation with a new random ID."""

    return Annotation(uuid.uuid4().hex, time_ns, text, category, source_id)


@dataclass(frozen=True, slots=True)
class LogFingerprint:
    """Identity of a log file's content.

    Two fingerprints match when size and SHA-256 are equal. ``name`` is only
    informational, so a log moved or renamed together with its sidecar still
    matches.
    """

    name: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str):
            raise TypeError("log name must be a string")
        if isinstance(self.size, bool) or not isinstance(self.size, int):
            raise TypeError("log size must be an integer")
        if self.size < 0:
            raise ValueError("log size must not be negative")
        if not isinstance(self.sha256, str):
            raise TypeError("log sha256 must be a string")
        if len(self.sha256) != 64 or not set(self.sha256) <= _HEX_DIGITS:
            raise ValueError("log sha256 must be 64 lowercase hexadecimal digits")

    def matches(self, other: LogFingerprint) -> bool:
        return self.size == other.size and self.sha256 == other.sha256


def fingerprint(log_path: Path) -> LogFingerprint:
    """Fingerprint a log by reading it in chunks; the file is opened read-only."""

    digest = hashlib.sha256()
    size = 0
    with open(log_path, "rb") as stream:
        while chunk := stream.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
    return LogFingerprint(log_path.name, size, digest.hexdigest())


def sidecar_path(log_path: Path) -> Path:
    """Return the default annotation file for a log: ``<log name>.annotations.json``."""

    return log_path.with_name(log_path.name + SIDECAR_SUFFIX)


def _sort_key(annotation: Annotation) -> tuple[int, str]:
    return (annotation.time_ns, annotation.annotation_id)


@dataclass(frozen=True, slots=True)
class MergeResult:
    """Outcome of merging imported annotations into an existing set."""

    annotations: AnnotationSet
    added: tuple[str, ...]
    conflicts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnnotationSet:
    """Immutable annotations for one log, sorted by ``(time_ns, annotation_id)``.

    Editing methods return a new set and leave this one unchanged.
    """

    fingerprint: LogFingerprint
    annotations: tuple[Annotation, ...] = ()
    _times: tuple[int, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.fingerprint, LogFingerprint):
            raise TypeError("fingerprint must be a LogFingerprint")
        items = tuple(self.annotations)
        if any(not isinstance(item, Annotation) for item in items):
            raise TypeError("annotations must contain only Annotation objects")
        seen: set[str] = set()
        for item in items:
            if item.annotation_id in seen:
                raise ValueError(f"Duplicate annotation id '{item.annotation_id}'")
            seen.add(item.annotation_id)
        ordered = tuple(sorted(items, key=_sort_key))
        object.__setattr__(self, "annotations", ordered)
        object.__setattr__(self, "_times", tuple(item.time_ns for item in ordered))

    def __len__(self) -> int:
        return len(self.annotations)

    def get(self, annotation_id: str) -> Annotation:
        """Return the annotation with this ID or raise ``KeyError``."""

        for item in self.annotations:
            if item.annotation_id == annotation_id:
                return item
        raise KeyError(annotation_id)

    def add(self, annotation: Annotation) -> AnnotationSet:
        return AnnotationSet(self.fingerprint, (*self.annotations, annotation))

    def update(
        self,
        annotation_id: str,
        *,
        time_ns: int | None = None,
        text: str | None = None,
        category: str | None = None,
        source_id: str | None = None,
    ) -> AnnotationSet:
        """Replace the given fields of one annotation; ``None`` keeps a field."""

        current = self.get(annotation_id)
        changed = replace(
            current,
            time_ns=current.time_ns if time_ns is None else time_ns,
            text=current.text if text is None else text,
            category=current.category if category is None else category,
            source_id=current.source_id if source_id is None else source_id,
        )
        return self._replace_items(annotation_id, (changed,))

    def remove(self, annotation_id: str) -> AnnotationSet:
        self.get(annotation_id)
        return self._replace_items(annotation_id, ())

    def with_fingerprint(self, log_fingerprint: LogFingerprint) -> AnnotationSet:
        """Attach these annotations to another log, e.g. after the user accepts a changed log."""

        return AnnotationSet(log_fingerprint, self.annotations)

    def next_after(self, time_ns: int) -> Annotation | None:
        """First annotation strictly after ``time_ns``."""

        index = bisect_right(self._times, time_ns)
        return self.annotations[index] if index < len(self.annotations) else None

    def previous_before(self, time_ns: int) -> Annotation | None:
        """Last annotation strictly before ``time_ns``."""

        index = bisect_left(self._times, time_ns)
        return self.annotations[index - 1] if index > 0 else None

    def in_range(self, start_ns: int, end_ns: int) -> tuple[Annotation, ...]:
        """Annotations in the CLOSED interval ``[start_ns, end_ns]``."""

        if end_ns < start_ns:
            raise ValueError("end_ns must not be before start_ns")
        low = bisect_left(self._times, start_ns)
        high = bisect_right(self._times, end_ns)
        return self.annotations[low:high]

    def at_or_near(self, time_ns: int, max_distance_ns: int) -> Annotation | None:
        """Closest annotation within ``max_distance_ns`` (inclusive); ties pick the earlier."""

        if max_distance_ns < 0:
            raise ValueError("max_distance_ns must not be negative")
        candidates = self.in_range(time_ns - max_distance_ns, time_ns + max_distance_ns)
        if not candidates:
            return None
        return min(candidates, key=lambda item: abs(item.time_ns - time_ns))

    def merge(self, other: AnnotationSet) -> MergeResult:
        """Add imported annotations whose IDs are new.

        An ID present in both sets with identical content is skipped. An ID
        present in both with different content keeps this set's annotation and
        is reported in ``conflicts``; nothing is overwritten silently. Both sets
        must belong to the same log content.
        """

        if not self.fingerprint.matches(other.fingerprint):
            raise ValueError("Cannot merge annotations recorded for a different log")
        existing = {item.annotation_id: item for item in self.annotations}
        added: list[str] = []
        conflicts: list[str] = []
        for item in other.annotations:
            mine = existing.get(item.annotation_id)
            if mine is None:
                added.append(item.annotation_id)
            elif mine != item:
                conflicts.append(item.annotation_id)
        new_ids = set(added)
        new_items = [item for item in other.annotations if item.annotation_id in new_ids]
        merged = AnnotationSet(self.fingerprint, (*self.annotations, *new_items))
        return MergeResult(merged, tuple(added), tuple(conflicts))

    def _replace_items(self, annotation_id: str, items: Iterable[Annotation]) -> AnnotationSet:
        kept = (item for item in self.annotations if item.annotation_id != annotation_id)
        return AnnotationSet(self.fingerprint, (*kept, *items))


class LogStatus(Enum):
    """How a loaded annotation file relates to the log it was given."""

    OK = "ok"
    LOG_CHANGED = "log_changed"
    LOG_MISSING = "log_missing"
    NOT_CHECKED = "not_checked"


@dataclass(frozen=True, slots=True)
class LoadedAnnotations:
    """Annotations read from disk and their relation to the current log."""

    annotations: AnnotationSet
    status: LogStatus
    current_fingerprint: LogFingerprint | None = None


def _to_document(annotation_set: AnnotationSet) -> dict[str, Any]:
    log = annotation_set.fingerprint
    return {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "log": {"name": log.name, "size": log.size, "sha256": log.sha256},
        "annotations": [
            {
                "id": item.annotation_id,
                "time_ns": item.time_ns,
                "category": item.category,
                "source_id": item.source_id,
                "text": item.text,
            }
            for item in annotation_set.annotations
        ],
    }


def _encode(annotation_set: AnnotationSet) -> bytes:
    text = json.dumps(_to_document(annotation_set), ensure_ascii=False, indent=2)
    return (text + "\n").encode("utf-8")


def save_annotations(path: Path, annotation_set: AnnotationSet) -> None:
    """Atomically write ``annotation_set`` to ``path``.

    The data goes to a temporary file in the same directory, is flushed to
    disk, and then replaces ``path`` in one step. If anything fails, the
    previous file is left as it was, the temporary file is removed, and
    ``AnnotationFileError`` is raised. Refuses to overwrite the log itself.
    """

    data = _encode(annotation_set)
    if len(data) > MAX_FILE_BYTES:
        raise AnnotationFileError(path, f"annotation file would exceed {MAX_FILE_BYTES} bytes")
    _refuse_to_overwrite_log(path, annotation_set.fingerprint)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(temp_path, flags, 0o666)
    except OSError as exc:
        raise AnnotationFileError(path, f"cannot create temporary file: {exc}") from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    except BaseException as exc:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        if isinstance(exc, OSError):
            raise AnnotationFileError(path, f"save failed, previous file kept: {exc}") from exc
        raise


def _refuse_to_overwrite_log(path: Path, log: LogFingerprint) -> None:
    try:
        is_log = path.stat().st_size == log.size and fingerprint(path).matches(log)
    except OSError:
        return  # Nothing readable there; the save itself reports real problems.
    if is_log:
        raise AnnotationFileError(path, "refusing to overwrite the log file itself")


def load_annotations(path: Path, *, log_path: Path | None = None) -> LoadedAnnotations:
    """Read an annotation file and compare it with the current log.

    A missing annotation file is not an error: an empty set is returned for
    ``log_path``. That needs the log's fingerprint, so ``AnnotationFileError``
    is raised when neither the annotation file nor the log exists (or no
    ``log_path`` was given). Without ``log_path`` the status is
    ``NOT_CHECKED``. Errors other than "not found" while reading the log
    propagate as ``OSError``.
    """

    current: LogFingerprint | None = None
    if log_path is not None:
        try:
            current = fingerprint(log_path)
        except FileNotFoundError:
            current = None

    data = _read_limited(path)
    if data is None:
        if current is None:
            raise AnnotationFileError(path, "file not found and there is no log to start from")
        return LoadedAnnotations(AnnotationSet(current), LogStatus.OK, current)

    annotation_set = _decode(path, data)
    if log_path is None:
        status = LogStatus.NOT_CHECKED
    elif current is None:
        status = LogStatus.LOG_MISSING
    elif annotation_set.fingerprint.matches(current):
        status = LogStatus.OK
    else:
        status = LogStatus.LOG_CHANGED
    return LoadedAnnotations(annotation_set, status, current)


def _read_limited(path: Path) -> bytes | None:
    try:
        with open(path, "rb") as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AnnotationFileError(path, f"cannot read file: {exc}") from exc
    if len(data) > MAX_FILE_BYTES:
        raise AnnotationFileError(path, f"file is larger than {MAX_FILE_BYTES} bytes")
    return data


def _reject_constant(name: str) -> None:
    raise ValueError(f"{name} is not allowed")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key '{key}'")
        result[key] = value
    return result


_DOCUMENT_KEYS = ("format", "version", "log", "annotations")
_LOG_KEYS = ("name", "size", "sha256")
_ANNOTATION_KEYS = ("id", "time_ns", "category", "source_id", "text")


def _require_object(value: object, keys: tuple[str, ...], where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be a JSON object")
    missing = [key for key in keys if key not in value]
    if missing:
        raise ValueError(f"{where} is missing {', '.join(repr(key) for key in missing)}")
    unknown = sorted(key for key in value if key not in keys)
    if unknown:
        raise ValueError(f"{where} has unknown {', '.join(repr(key) for key in unknown)}")
    return value


def _decode(path: Path, data: bytes) -> AnnotationSet:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AnnotationFileError(path, f"not valid UTF-8: {exc}") from exc
    try:
        document = json.loads(
            text, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except ValueError as exc:
        raise AnnotationFileError(path, f"invalid JSON: {exc}") from exc
    try:
        document = _require_object(document, _DOCUMENT_KEYS, "document")
        if document["format"] != FORMAT_NAME:
            raise ValueError(f"format must be '{FORMAT_NAME}'")
        version = document["version"]
        if type(version) is not int or version != FORMAT_VERSION:
            raise ValueError(f"unsupported version {version!r}; expected {FORMAT_VERSION}")
        log = _require_object(document["log"], _LOG_KEYS, "log")
        log_fingerprint = LogFingerprint(log["name"], log["size"], log["sha256"])
        if not isinstance(document["annotations"], list):
            raise ValueError("annotations must be a JSON array")
    except (TypeError, ValueError) as exc:
        raise AnnotationFileError(path, str(exc)) from exc

    items: list[Annotation] = []
    seen: set[str] = set()
    for index, raw in enumerate(document["annotations"]):
        where = f"annotation {index}"
        try:
            entry = _require_object(raw, _ANNOTATION_KEYS, where)
            item = Annotation(
                entry["id"], entry["time_ns"], entry["text"], entry["category"], entry["source_id"]
            )
        except (TypeError, ValueError) as exc:
            reason = str(exc) if str(exc).startswith(where) else f"{where}: {exc}"
            raise AnnotationFileError(path, reason) from exc
        if item.annotation_id in seen:
            raise AnnotationFileError(path, f"{where}: duplicate id '{item.annotation_id}'")
        seen.add(item.annotation_id)
        items.append(item)
    return AnnotationSet(log_fingerprint, tuple(items))
