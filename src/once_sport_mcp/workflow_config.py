"""Opt-in configuration for the durable video workflow."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkspaceConfig:
    root: Path
    allowed_source_dirs: tuple[Path, ...]
    native_once_executable: Path | None = None
    tracking_enabled: bool = False


def _absolute_directory(value: str, name: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{name} must be a nonempty absolute directory")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path in (Path("/"), Path.home()):
        raise ValueError(f"{name} must be a dedicated absolute directory")
    return path


def _absolute_executable(value: str, name: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{name} must be a nonempty absolute executable path")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(
            f"{name} must be an absolute executable path without parent traversal"
        )
    return path


def native_export_descriptor() -> dict:
    """Describe the explicitly configured, pinned native file/CLI export path."""
    from .native_rendering import APPHOST_SHA256, DLL_SHA256

    return {
        "configured": True,
        "headless": True,
        "cursor_required": False,
        "display_required": False,
        "live_database_writes": False,
        "supported_kinds": ["arrow", "ellipse"],
        "tested_build": "Once Sport 4.9.6",
        "runtime_pin": {
            "apphost_sha256": APPHOST_SHA256,
            "dll_sha256": DLL_SHA256,
        },
        "outputs": {
            "compatible_reel": {
                "file": "reel.mp4",
                "role": "H.264/AAC compatibility transcode of the native original",
            },
            "native_original_reel": {
                "file": "reel.native.mp4",
                "role": "original native Once CLI output retained without compatibility transcode",
            },
            "native_analysis_xml": {
                "file": "reel.once.xml",
                "role": "native editable interchange file; GUI import unverified",
            },
            "drawings_sidecar": {
                "file": "reel.drawings.json",
                "semantics": "native-once-headless",
            },
        },
        "native_import_verified": False,
        "native_editable_drawings": False,
        "gui_project_list_import_verified": False,
        "boundary": (
            "Headless native file/CLI export, not the official public API; no desktop "
            "operation or live database writes. GUI project-list import remains unverified."
        ),
    }


def load_workspace_config(environ: Mapping[str, str]) -> WorkspaceConfig | None:
    """Parse startup configuration without reading media or creating directories."""
    if "ONCE_SPORT_WORKSPACE" not in environ:
        return None
    root = _absolute_directory(environ["ONCE_SPORT_WORKSPACE"], "ONCE_SPORT_WORKSPACE")
    raw_tracking = environ.get("ONCE_SPORT_TRACKING_ENABLED", "0")
    if raw_tracking not in ("0", "1"):
        raise ValueError("ONCE_SPORT_TRACKING_ENABLED must be exactly '0' or '1'")
    tracking_enabled = raw_tracking == "1"
    raw_executable = environ.get("ONCE_SPORT_NATIVE_EXECUTABLE")
    native_executable = (
        _absolute_executable(raw_executable, "ONCE_SPORT_NATIVE_EXECUTABLE")
        if raw_executable is not None
        else None
    )
    raw = environ.get("ONCE_SPORT_ALLOWED_SOURCE_DIRS")
    if raw is None:
        return WorkspaceConfig(
            root, (root / "inputs",), native_executable, tracking_enabled
        )
    try:
        values = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("ONCE_SPORT_ALLOWED_SOURCE_DIRS must be a JSON array") from exc
    if not isinstance(values, list):
        # JSON has the wrong value shape; the caller still supplied an environment string.
        raise ValueError("ONCE_SPORT_ALLOWED_SOURCE_DIRS must be a JSON array")  # noqa: TRY004
    directories = tuple(
        _absolute_directory(value, "ONCE_SPORT_ALLOWED_SOURCE_DIRS") for value in values
    )
    return WorkspaceConfig(root, directories, native_executable, tracking_enabled)


def statistics_descriptor() -> dict:
    """Describe the media-independent statistics export tool."""
    return {
        "tool": "request_analysis_statistics",
        "formats": ["xlsx", "html", "json"],
        "requires_media": False,
    }


def playlist_export_descriptor() -> dict:
    """Describe playlist-scoped filtering for custom exports."""
    return {
        "tool": "request_analysis_export",
        "note": "Optional playlist filtering applies only to custom exports.",
    }


def tracking_descriptor() -> dict:
    """Describe the opt-in seeded CSRT evidence path without probing dependencies."""
    return {
        "configured": True,
        "backend": "opencv-csrt",
        "seed_bbox_format": "normalized-xywh",
        "mode": "seeded-single-target",
        "max_window_seconds": 12,
        "max_source_frames": 600,
        "review_required": True,
        "automatic_reidentification": False,
        "optional_dependency_extra": "tracking",
        "consistency": "forward-backward IoU heuristic, not calibrated identity confidence",
        "outputs": {
            "result": "tracking.json",
            "preview": "tracking.preview.mp4",
        },
        "formation": {
            "tool": "build_formation_drawings",
            "max_targets": 11,
            "renderer": "custom_only",
            "stops_before_overlap": True,
            "automatic_reidentification": False,
        },
    }
