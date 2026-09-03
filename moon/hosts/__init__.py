from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from moon.hosts import antigravity, claude_desktop, codex, generic_mcp
from moon.hosts.base import HostProfile


HOST_ALIASES = {
    "antigravity": "antigravity",
    "claude": "claude",
    "claude-desktop": "claude",
    "codex": "codex",
    "vscode": "codex",
    "generic": "generic",
    "mcp": "generic",
}


def profiles(
    project: str | Path | None = None,
    *,
    home: str | Path | None = None,
    appdata: str | Path | None = None,
    codex_home: str | Path | None = None,
    python_command: str = "python",
) -> dict[str, HostProfile]:
    selected_home = Path(home).expanduser().resolve() if home else Path.home().resolve()
    selected_appdata = appdata if appdata is not None else os.environ.get("APPDATA")
    selected_codex_home = codex_home if codex_home is not None else os.environ.get("CODEX_HOME")
    values = (
        antigravity.profile(project, home=selected_home, python_command=python_command),
        claude_desktop.profile(
            project,
            home=selected_home,
            appdata=selected_appdata,
            python_command=python_command,
        ),
        codex.profile(
            project,
            home=selected_home,
            codex_home=selected_codex_home,
            python_command=python_command,
        ),
        generic_mcp.profile(project, python_command=python_command),
    )
    return {item.id: item for item in values}


def normalize_host_id(value: str) -> str:
    try:
        return HOST_ALIASES[value.strip().lower()]
    except KeyError as exc:
        raise ValueError(f"unknown Moon host: {value}") from exc


def generated_config(profile_value: HostProfile) -> dict[str, Any] | str:
    if profile_value.id == "antigravity":
        return antigravity.config(profile_value)
    if profile_value.id == "claude":
        return claude_desktop.config(profile_value)
    if profile_value.id == "codex":
        return codex.config_text(profile_value)
    if profile_value.id == "generic":
        return generic_mcp.config(profile_value)
    raise ValueError(f"unsupported Moon host profile: {profile_value.id}")


__all__ = [
    "HostProfile",
    "generated_config",
    "normalize_host_id",
    "profiles",
]
