"""Workspace mode is explicit; legacy clients keep their startup contract."""

import importlib.util
from pathlib import Path

import pytest


def test_unconfigured_server_keeps_workspace_mode_disabled():
    spec = importlib.util.find_spec("once_sport_mcp.workflow_config")
    assert spec is not None, "Workspace configuration boundary is not implemented"
    from once_sport_mcp.workflow_config import load_workspace_config

    assert load_workspace_config({}) is None


def test_explicit_workspace_sets_private_default_input_directory(tmp_path):
    from once_sport_mcp.workflow_config import load_workspace_config

    root = tmp_path / "analysis"
    config = load_workspace_config({"ONCE_SPORT_WORKSPACE": str(root)})
    assert config is not None
    assert config.root == root
    assert config.allowed_source_dirs == (root / "inputs",)
    assert not root.exists(), "Configuration parsing must not create files"


@pytest.mark.parametrize(
    "value", ["", "relative/path", "/", str(Path.home()), "/x/../y", "/x\x00y"]
)
def test_unsafe_or_ambiguous_workspace_is_rejected(value):
    from once_sport_mcp.workflow_config import load_workspace_config

    with pytest.raises(ValueError):
        load_workspace_config({"ONCE_SPORT_WORKSPACE": value})


def test_source_directory_allowlist_is_explicit_and_not_colon_split(tmp_path):
    import json

    from once_sport_mcp.workflow_config import load_workspace_config

    root = tmp_path / "workflow"
    allowed = tmp_path / "video:source"
    config = load_workspace_config(
        {
            "ONCE_SPORT_WORKSPACE": str(root),
            "ONCE_SPORT_ALLOWED_SOURCE_DIRS": json.dumps([str(allowed)]),
        }
    )
    assert config.allowed_source_dirs == (allowed,)
    assert not root.exists()


@pytest.mark.parametrize(
    "value", ["invalid", "{}", "null", '"/videos"', "[true]", '["relative"]', '["/"]']
)
def test_invalid_source_allowlist_is_not_silently_ignored(tmp_path, value):
    from once_sport_mcp.workflow_config import load_workspace_config

    with pytest.raises(ValueError):
        load_workspace_config(
            {
                "ONCE_SPORT_WORKSPACE": str(tmp_path / "workflow"),
                "ONCE_SPORT_ALLOWED_SOURCE_DIRS": value,
            }
        )


def test_empty_allowlist_explicitly_disables_local_source_reads(tmp_path):
    from once_sport_mcp.workflow_config import load_workspace_config

    config = load_workspace_config(
        {
            "ONCE_SPORT_WORKSPACE": str(tmp_path / "workflow"),
            "ONCE_SPORT_ALLOWED_SOURCE_DIRS": "[]",
        }
    )
    assert config.allowed_source_dirs == ()
