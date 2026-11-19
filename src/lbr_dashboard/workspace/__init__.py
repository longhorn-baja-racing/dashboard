"""Portable, declarative dashboard workspaces (format and validation only).

This package is Qt-free. Docking UI (#37), workspace management (#49) and
signal resolution against a registry (#36) build on it.
"""

from .codec import (
    MIGRATIONS,
    dumps_workspace,
    loads_workspace,
    migrate_document,
    workspace_from_dict,
    workspace_to_dict,
)
from .model import (
    CURRENT_VERSION,
    WIDGET_TYPES,
    WORKSPACE_FORMAT,
    AxisPreference,
    BindingResolution,
    BoundSignal,
    LayoutNode,
    SignalBinding,
    SplitLayout,
    TabLayout,
    WidgetConfig,
    WidgetRef,
    Workspace,
    WorkspaceError,
    WorkspaceIssue,
    iter_layout_refs,
    resolve_bindings,
)
from .storage import LoadResult, backup_path, load_workspace, read_workspace, save_workspace

__all__ = [
    "CURRENT_VERSION",
    "MIGRATIONS",
    "WIDGET_TYPES",
    "WORKSPACE_FORMAT",
    "AxisPreference",
    "BindingResolution",
    "BoundSignal",
    "LayoutNode",
    "LoadResult",
    "SignalBinding",
    "SplitLayout",
    "TabLayout",
    "WidgetConfig",
    "WidgetRef",
    "Workspace",
    "WorkspaceError",
    "WorkspaceIssue",
    "backup_path",
    "dumps_workspace",
    "iter_layout_refs",
    "load_workspace",
    "loads_workspace",
    "migrate_document",
    "read_workspace",
    "resolve_bindings",
    "save_workspace",
    "workspace_from_dict",
    "workspace_to_dict",
]
