"""Atomic local workspace files with a last-known-good backup."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .codec import MAX_DOCUMENT_BYTES, dumps_workspace, loads_workspace
from .model import Workspace, WorkspaceError, WorkspaceIssue


def backup_path(path: Path) -> Path:
    """Return where the previous valid version of ``path`` is kept."""

    return path.with_name(path.name + ".bak")


def _io_error(path: Path, action: str, error: OSError) -> WorkspaceError:
    reason = error.strerror or type(error).__name__
    return WorkspaceError([WorkspaceIssue(str(path), "io", f"could not {action}: {reason}")])


def read_workspace(path: Path, **options: Any) -> Workspace:
    """Read and validate one workspace file without consulting its backup."""

    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_DOCUMENT_BYTES + 1)
    except OSError as error:
        raise _io_error(path, "read the workspace", error) from None
    return loads_workspace(raw, **options)


def _write_atomically(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` so readers see the old or new file, never half."""

    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


#: Existing files with these problems are someone else's data, not a damaged
#: copy of ours, so saving refuses to replace them.
_PROTECTED = frozenset({"future_version", "unknown_format"})


def _refuse_overwrite(path: Path, error: WorkspaceError) -> WorkspaceError:
    reason = (
        "it was written by a newer dashboard version"
        if "future_version" in error.codes
        else "it is not a dashboard workspace file"
    )
    message = f"will not replace this file because {reason}; choose another file name"
    return WorkspaceError([WorkspaceIssue(str(path), "overwrite_refused", message)])


def save_workspace(path: Path, workspace: Workspace) -> None:
    """Validate, then atomically write ``workspace`` to ``path``.

    If ``path`` already holds a valid workspace it is first copied to the
    backup file, so a later corrupt or truncated primary can be recovered.
    A file written by a newer dashboard, or a file that is not a workspace at
    all, is never replaced. A failure at any point leaves the existing file
    untouched.
    """

    data = dumps_workspace(workspace).encode("ascii")
    try:
        if path.exists():
            try:
                read_workspace(path)
            except WorkspaceError as error:
                if error.codes & _PROTECTED:
                    raise _refuse_overwrite(path, error) from None
                # Otherwise a damaged primary is replaced, but never copied
                # over a good backup.
            else:
                _write_atomically(backup_path(path), path.read_bytes())
        _write_atomically(path, data)
    except OSError as error:
        raise _io_error(path, "save the workspace", error) from None


@dataclass(frozen=True, slots=True)
class LoadResult:
    """A loaded workspace and whether it came from the backup file."""

    workspace: Workspace
    source: Path
    recovered: bool = False
    primary_error: WorkspaceError | None = None


def load_workspace(path: Path, *, use_backup: bool = True, **options: Any) -> LoadResult:
    """Load ``path``, falling back to its backup when the primary is unusable.

    When recovery happens, ``primary_error`` explains what was wrong with the
    primary so the UI can tell the user. If both fail, the primary's error is
    raised. A primary from a newer dashboard version is reported as such and
    never replaced by its backup. Nothing on disk is modified.
    """

    try:
        return LoadResult(read_workspace(path, **options), path)
    except WorkspaceError as primary:
        backup = backup_path(path)
        # A newer file is not damaged; an older backup must not stand in for it.
        if not use_backup or not backup.exists() or "future_version" in primary.codes:
            raise
        try:
            workspace = read_workspace(backup, **options)
        except WorkspaceError:
            raise primary from None
        return LoadResult(workspace, backup, recovered=True, primary_error=primary)
