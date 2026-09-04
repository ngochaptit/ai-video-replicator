from __future__ import annotations

from pathlib import Path
from typing import Any

from moon.hosts.base import HostProfile, canonical_mcp_launch


def default_config_path(home: str | Path, appdata: str | Path | None = None) -> Path:
    if appdata is not None:
        return Path(appdata).expanduser().resolve() / "Claude" / "claude_desktop_config.json"
    return Path(home).expanduser().resolve() / ".config" / "Claude" / "claude_desktop_config.json"


def profile(
    project: str | Path | None,
    *,
    home: str | Path,
    appdata: str | Path | None = None,
    python_command: str = "python",
) -> HostProfile:
    config_path = default_config_path(home, appdata)
    return HostProfile(
        id="claude",
        display_name="Claude Desktop",
        detected=config_path.exists() or config_path.parent.exists(),
        config_path=config_path,
        transport="stdio",
        install_mode="mcpb",
        launch_command=canonical_mcp_launch(project, python_command=python_command),
        project_binding_mode="mcpb_user_config",
        supports_local_stdio=True,
        notes=("Install dist/moon-local.mcpb and choose the Moon project directory.",),
    )


def config(profile_value: HostProfile) -> dict[str, Any]:
    return {
        "integration": "mcpb",
        "bundle": "dist/moon-local.mcpb",
        "project_binding": "MCPB user_config.project_root",
        "runtime": {
            "transport": profile_value.transport,
            "command": profile_value.command,
            "args": profile_value.args,
        },
    }
