from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from moon.evidence import SampledFrameEvidenceStore
from moon.hosts import profiles
from moon.mcp_adapter import MCP_PROTOCOL_VERSION, SERVER_NAME, _TOOL_SCHEMAS


DOCTOR_VERSION = "1.1.1"


@dataclass(frozen=True)
class DoctorCheck:
    category: str
    name: str
    status: str
    message: str
    details: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.status not in {"PASS", "WARN", "FAIL"}:
            raise ValueError(f"invalid doctor status: {self.status}")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "category": self.category,
            "name": self.name,
            "status": self.status,
            "message": self.message,
        }
        if self.details is not None:
            result["details"] = self.details
        return result


def run_doctor(
    project: str | Path | None = None,
    *,
    home: str | Path | None = None,
    appdata: str | Path | None = None,
    codex_home: str | Path | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> dict[str, Any]:
    checks: list[DoctorCheck] = []
    version = sys.version_info
    python_ok = version >= (3, 10)
    checks.append(
        DoctorCheck(
            "Runtime",
            "Python",
            "PASS" if python_ok else "FAIL",
            f"Python {version.major}.{version.minor}.{version.micro}",
            {"executable": sys.executable},
        )
    )
    for executable in ("ffmpeg", "ffprobe"):
        resolved = which(executable)
        checks.append(
            DoctorCheck(
                "Runtime",
                executable,
                "PASS" if resolved else "FAIL",
                resolved or f"{executable} is not available on PATH",
            )
        )

    tools = set(_TOOL_SCHEMAS)
    checks.extend(
        [
            DoctorCheck("Moon", "import", "PASS", "Moon runtime imported successfully"),
            DoctorCheck(
                "Moon",
                "protocol",
                "PASS" if MCP_PROTOCOL_VERSION else "FAIL",
                f"{SERVER_NAME} protocol {MCP_PROTOCOL_VERSION}",
            ),
            DoctorCheck(
                "Moon",
                "MCP stdio",
                "PASS" if {"moon.status", "moon.next", "moon.submit"} <= tools else "FAIL",
                f"{len(tools)} tools registered",
            ),
            DoctorCheck(
                "Moon",
                "evidence images",
                "PASS" if "moon.evidence.read_image" in tools else "FAIL",
                "Native MCP image evidence is registered",
            ),
            DoctorCheck(
                "Moon",
                "sampled evidence",
                "PASS"
                if "moon.evidence.clear_sampled" in tools and SampledFrameEvidenceStore
                else "FAIL",
                "Sampled evidence store and tools are installed",
            ),
        ]
    )
    checks.extend(_project_checks(project))
    checks.extend(_host_checks(home=home, appdata=appdata, codex_home=codex_home))

    statuses = [item.status for item in checks]
    overall = "FAIL" if "FAIL" in statuses else "WARN" if "WARN" in statuses else "PASS"
    return {
        "moon_version": DOCTOR_VERSION,
        "overall": overall,
        "usable": overall != "FAIL",
        "exit_code": 0 if overall != "FAIL" else 1,
        "checks": [item.to_dict() for item in checks],
    }


def _project_checks(project: str | Path | None) -> list[DoctorCheck]:
    if project is None:
        return [DoctorCheck("Project", "selection", "WARN", "No project selected; project checks skipped")]
    root = Path(project).expanduser().resolve()
    if not root.exists():
        return [DoctorCheck("Project", "exists", "FAIL", f"Project does not exist: {root}")]
    if not root.is_dir():
        return [DoctorCheck("Project", "directory", "FAIL", f"Project is not a directory: {root}")]
    state_path = root / ".moon" / "state.json"
    checks = [
        DoctorCheck("Project", "exists", "PASS", str(root)),
        DoctorCheck(
            "Project",
            "readable",
            "PASS" if os.access(root, os.R_OK) else "FAIL",
            "Project directory is readable" if os.access(root, os.R_OK) else "Project directory is not readable",
        ),
        DoctorCheck(
            "Project",
            "writable",
            "PASS" if os.access(root, os.W_OK) else "FAIL",
            "Project directory is writable" if os.access(root, os.W_OK) else "Project directory is not writable",
        ),
    ]
    if not state_path.is_file():
        checks.append(DoctorCheck("Project", ".moon state", "FAIL", f"Missing Moon state: {state_path}"))
    else:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            valid = isinstance(state, dict) and "stages" in state
        except (OSError, json.JSONDecodeError):
            valid = False
        checks.append(
            DoctorCheck(
                "Project",
                ".moon state",
                "PASS" if valid else "FAIL",
                "Moon state is readable" if valid else f"Moon state is invalid: {state_path}",
            )
        )
    return checks


def _host_checks(
    *,
    home: str | Path | None,
    appdata: str | Path | None,
    codex_home: str | Path | None,
) -> list[DoctorCheck]:
    result = []
    for profile_value in profiles(
        None,
        home=home,
        appdata=appdata,
        codex_home=codex_home,
    ).values():
        if profile_value.id == "generic":
            result.append(DoctorCheck("Hosts", profile_value.display_name, "PASS", "Manual stdio configuration is available"))
            continue
        configured = _moon_configured(profile_value.id, profile_value.config_path)
        if configured:
            status = "PASS"
            message = f"Moon configuration found at {profile_value.config_path}"
        elif profile_value.detected:
            status = "WARN"
            message = f"Host detected; Moon is not verifiably configured at {profile_value.config_path}"
        else:
            status = "WARN"
            message = "Host not detected; configuration can still be generated"
        result.append(DoctorCheck("Hosts", profile_value.display_name, status, message))
    return result


def _moon_configured(host_id: str, config_path: Path | None) -> bool:
    if config_path is None or not config_path.is_file():
        return False
    try:
        text = config_path.read_text(encoding="utf-8")
        if host_id == "antigravity":
            data = json.loads(text)
            return isinstance(data, dict) and isinstance(data.get("mcpServers"), dict) and "moon" in data["mcpServers"]
        if host_id == "codex":
            return "[mcp_servers.moon]" in text
        if host_id == "claude":
            data = json.loads(text)
            return isinstance(data, dict) and isinstance(data.get("mcpServers"), dict) and "moon" in data["mcpServers"]
    except (OSError, json.JSONDecodeError):
        return False
    return False


def format_doctor_report(report: dict[str, Any]) -> str:
    symbols = {"PASS": "PASS", "WARN": "WARN", "FAIL": "FAIL"}
    lines = ["Moon Doctor", f"Overall: {report['overall']}"]
    current = None
    for check in report["checks"]:
        if check["category"] != current:
            current = check["category"]
            lines.extend(["", current])
        lines.append(f"{symbols[check['status']]} {check['name']}: {check['message']}")
    return "\n".join(lines)
