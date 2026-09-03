from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MOON_SERVER_ID = "moon"
PROJECT_PLACEHOLDER = "<PROJECT_PATH>"


def canonical_mcp_launch(
    project: str | Path | None,
    *,
    python_command: str = "python",
) -> tuple[str, ...]:
    """Return the one host-neutral command used to launch Moon's MCP server."""
    selected = PROJECT_PLACEHOLDER if project is None else str(Path(project).expanduser().resolve())
    return (python_command, "-m", "moon", "mcp", "--project", selected)


@dataclass(frozen=True)
class HostProfile:
    id: str
    display_name: str
    detected: bool
    config_path: Path | None
    transport: str
    install_mode: str
    launch_command: tuple[str, ...]
    project_binding_mode: str
    supports_local_stdio: bool
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id or not self.display_name:
            raise ValueError("host profile id and display_name are required")
        if self.transport != "stdio":
            raise ValueError("Moon v1.1.1 host profiles must use stdio")
        if not self.launch_command:
            raise ValueError("host profile launch_command is required")

    @property
    def command(self) -> str:
        return self.launch_command[0]

    @property
    def args(self) -> list[str]:
        return list(self.launch_command[1:])

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "detected": self.detected,
            "config_path": str(self.config_path) if self.config_path else None,
            "transport": self.transport,
            "install_mode": self.install_mode,
            "launch_command": {"command": self.command, "args": self.args},
            "project_binding_mode": self.project_binding_mode,
            "supports_local_stdio": self.supports_local_stdio,
            "notes": list(self.notes),
        }


def stdio_server_config(profile: HostProfile) -> dict[str, Any]:
    return {"command": profile.command, "args": profile.args}


def atomic_write_text(path: str | Path, text: str) -> Path:
    """Atomically replace one generated config artifact in its target directory."""
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    return destination


def atomic_write_json(path: str | Path, payload: dict[str, Any]) -> Path:
    return atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
