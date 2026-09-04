from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from moon.hosts import generated_config, normalize_host_id, profiles
from moon.hosts import antigravity
from moon.hosts.base import atomic_write_json, atomic_write_text


SETUP_VERSION = "1.1.1"


def setup_integrations(
    project: str | Path | None = None,
    *,
    host: str | None = None,
    write: bool = False,
    output: str | Path | None = None,
    config_path: str | Path | None = None,
    scope: str = "global",
    home: str | Path | None = None,
    appdata: str | Path | None = None,
    codex_home: str | Path | None = None,
    python_command: str = "python",
) -> dict[str, Any]:
    if scope not in {"global", "workspace"}:
        raise ValueError("setup scope must be 'global' or 'workspace'")
    selected_project: Path | None = None
    if project is not None:
        selected_project = Path(project).expanduser().resolve()
        if not selected_project.is_dir():
            raise FileNotFoundError(f"Moon project directory does not exist: {selected_project}")
    if write and selected_project is None:
        raise ValueError("--write requires --project")

    available = profiles(
        selected_project,
        home=home,
        appdata=appdata,
        codex_home=codex_home,
        python_command=python_command,
    )
    selected_ids = [normalize_host_id(host)] if host else list(available)
    if output is not None and len(selected_ids) != 1:
        raise ValueError("--output requires exactly one --host")
    if config_path is not None and len(selected_ids) != 1:
        raise ValueError("--config-path requires exactly one --host")

    integrations = []
    for host_id in selected_ids:
        profile_value = available[host_id]
        rendered = generated_config(profile_value)
        target = Path(config_path).expanduser().resolve() if config_path else profile_value.config_path
        status = "preview"
        written_path: Path | None = None

        if output is not None:
            written_path = _write_generated(output, rendered)
            status = "generated"
        elif write and host_id == "antigravity":
            if config_path is None and scope == "workspace":
                assert selected_project is not None
                target = antigravity.workspace_config_path(selected_project)
            assert target is not None
            rendered = antigravity.merge_config(target, profile_value)
            written_path = target
            status = "configured"
        elif write:
            status = "manual_install_required"

        integrations.append(
            {
                **profile_value.to_dict(),
                "status": status,
                "target": str(target) if target else None,
                "written_path": str(written_path) if written_path else None,
                "config": rendered,
            }
        )

    return {
        "moon_version": SETUP_VERSION,
        "project": str(selected_project) if selected_project else None,
        "write_requested": write,
        "integrations": integrations,
    }


def _write_generated(path: str | Path, rendered: dict[str, Any] | str) -> Path:
    if isinstance(rendered, str):
        return atomic_write_text(path, rendered)
    return atomic_write_json(path, rendered)


def format_setup_report(report: dict[str, Any]) -> str:
    lines = ["Moon Setup", "", f"Project: {report['project'] or '(not selected)'}", "", "Detected hosts:"]
    for item in report["integrations"]:
        marker = "x" if item["detected"] else " "
        lines.append(f"[{marker}] {item['display_name']}")
    lines.extend(["", "Available integrations:"])
    for item in report["integrations"]:
        lines.append(f"- {item['display_name']}: {item['status']}")
        if item["written_path"]:
            lines.append(f"  Wrote: {item['written_path']}")
        elif item["target"]:
            lines.append(f"  Target: {item['target']}")
    return "\n".join(lines)
