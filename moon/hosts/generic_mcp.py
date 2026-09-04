from __future__ import annotations

from pathlib import Path
from typing import Any

from moon.hosts.base import HostProfile, canonical_mcp_launch


def profile(
    project: str | Path | None,
    *,
    python_command: str = "python",
) -> HostProfile:
    return HostProfile(
        id="generic",
        display_name="Generic MCP client",
        detected=False,
        config_path=None,
        transport="stdio",
        install_mode="generated_json",
        launch_command=canonical_mcp_launch(project, python_command=python_command),
        project_binding_mode="launch_argument",
        supports_local_stdio=True,
        notes=("Copy the command and args into any MCP client with local stdio support.",),
    )


def config(profile_value: HostProfile) -> dict[str, Any]:
    return {
        "transport": "stdio",
        "command": profile_value.command,
        "args": profile_value.args,
    }
