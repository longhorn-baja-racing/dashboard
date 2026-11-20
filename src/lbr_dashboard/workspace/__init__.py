"""Portable, declarative dashboard workspaces (format and validation only).

This package is Qt-free. The Workspace menu (lbr_dashboard.ui.workspace_manager)
captures and restores the main window through it.
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
    DOCK_AREAS,
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
from .presets import demo_workspace
from .storage import LoadResult, backup_path, load_workspace, read_workspace, save_workspace

__all__ = [
    "CURRENT_VERSION",
    "DOCK_AREAS",
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
    "demo_workspace",
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
