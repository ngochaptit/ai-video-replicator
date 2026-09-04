from __future__ import annotations

import json
from pathlib import Path

from moon.hosts.base import HostProfile, canonical_mcp_launch


def default_config_path(home: str | Path, codex_home: str | Path | None = None) -> Path:
    root = Path(codex_home).expanduser().resolve() if codex_home else Path(home).expanduser().resolve() / ".codex"
    return root / "config.toml"


def profile(
    project: str | Path | None,
    *,
    home: str | Path,
    codex_home: str | Path | None = None,
    python_command: str = "python",
) -> HostProfile:
    config_path = default_config_path(home, codex_home)
    return HostProfile(
        id="codex",
        display_name="Codex in VS Code",
        detected=config_path.exists() or config_path.parent.exists(),
        config_path=config_path,
        transport="stdio",
        install_mode="generated_toml",
        launch_command=canonical_mcp_launch(project, python_command=python_command),
        project_binding_mode="user_or_trusted_project_config",
        supports_local_stdio=True,
        notes=("Moon generates a TOML snippet and does not overwrite existing Codex config.",),
    )


def config_text(profile_value: HostProfile) -> str:
    args = ", ".join(json.dumps(value, ensure_ascii=False) for value in profile_value.args)
    return (
        "[mcp_servers.moon]\n"
        f"command = {json.dumps(profile_value.command, ensure_ascii=False)}\n"
        f"args = [{args}]\n"
        "enabled = true\n"
    )
