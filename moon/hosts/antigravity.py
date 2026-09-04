from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from moon.hosts.base import (
    MOON_SERVER_ID,
    HostProfile,
    atomic_write_json,
    canonical_mcp_launch,
    stdio_server_config,
)


def default_config_path(home: str | Path) -> Path:
    return Path(home).expanduser().resolve() / ".gemini" / "config" / "mcp_config.json"


def workspace_config_path(workspace: str | Path) -> Path:
    return Path(workspace).expanduser().resolve() / ".agents" / "mcp_config.json"


def profile(
    project: str | Path | None,
    *,
    home: str | Path,
    python_command: str = "python",
) -> HostProfile:
    config_path = default_config_path(home)
    return HostProfile(
        id="antigravity",
        display_name="Antigravity",
        detected=config_path.exists() or config_path.parent.parent.exists(),
        config_path=config_path,
        transport="stdio",
        install_mode="merge_safe_json",
        launch_command=canonical_mcp_launch(project, python_command=python_command),
        project_binding_mode="global_or_workspace",
        supports_local_stdio=True,
        notes=("Workspace configuration may be placed at .agents/mcp_config.json.",),
    )


def config(profile_value: HostProfile) -> dict[str, Any]:
    return {"mcpServers": {MOON_SERVER_ID: stdio_server_config(profile_value)}}


def merge_config(path: str | Path, profile_value: HostProfile) -> dict[str, Any]:
    destination = Path(path).expanduser().resolve()
    if destination.exists():
        try:
            existing = json.loads(destination.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Antigravity config is not valid JSON: {destination}") from exc
        if not isinstance(existing, dict):
            raise ValueError("Antigravity config root must be an object")
    else:
        existing = {}
    servers = existing.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("Antigravity mcpServers must be an object")
    servers[MOON_SERVER_ID] = stdio_server_config(profile_value)
    atomic_write_json(destination, existing)
    return existing
