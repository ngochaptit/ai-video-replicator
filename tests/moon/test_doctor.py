from __future__ import annotations

import json
from pathlib import Path

from moon.cli import main
from moon.core.project import MoonProject
from moon.doctor import run_doctor
from moon.runner.pipeline import PipelineRunner


def _available(name: str) -> str:
    return f"/tools/{name}"


def test_doctor_is_read_only_and_reports_protocol_evidence_and_project(tmp_path: Path):
    project = tmp_path / "Moon project"
    PipelineRunner(MoonProject.open(project, create=True))
    before = {path.relative_to(project): path.read_bytes() for path in project.rglob("*") if path.is_file()}

    report = run_doctor(
        project,
        home=tmp_path / "empty-home",
        appdata=tmp_path / "empty-appdata",
        which=_available,
    )

    after = {path.relative_to(project): path.read_bytes() for path in project.rglob("*") if path.is_file()}
    assert report["overall"] == "WARN"
    assert report["usable"] is True
    assert report["exit_code"] == 0
    assert before == after
    checks = {item["name"]: item for item in report["checks"]}
    assert checks["MCP stdio"]["status"] == "PASS"
    assert checks["evidence images"]["status"] == "PASS"
    assert checks["sampled evidence"]["status"] == "PASS"
    assert checks[".moon state"]["status"] == "PASS"


def test_doctor_warning_does_not_fail_without_project_or_hosts(tmp_path: Path):
    report = run_doctor(
        home=tmp_path / "empty-home",
        appdata=tmp_path / "empty-appdata",
        which=_available,
    )

    assert report["overall"] == "WARN"
    assert report["exit_code"] == 0
    assert any(item["name"] == "selection" and item["status"] == "WARN" for item in report["checks"])


def test_doctor_blocking_runtime_or_project_failure_has_nonzero_exit(tmp_path: Path):
    report = run_doctor(
        tmp_path / "missing",
        home=tmp_path / "home",
        appdata=tmp_path / "appdata",
        which=lambda name: None,
    )

    assert report["overall"] == "FAIL"
    assert report["usable"] is False
    assert report["exit_code"] == 1
    assert any(item["name"] == "ffmpeg" and item["status"] == "FAIL" for item in report["checks"])
    assert any(item["name"] == "exists" and item["status"] == "FAIL" for item in report["checks"])


def test_doctor_reports_known_host_configuration_state(tmp_path: Path):
    home = tmp_path / "home"
    antigravity = home / ".gemini" / "config" / "mcp_config.json"
    antigravity.parent.mkdir(parents=True)
    antigravity.write_text('{"mcpServers":{"moon":{"command":"python"}}}', encoding="utf-8")
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text("[mcp_servers.moon]\ncommand = \"python\"\n", encoding="utf-8")
    appdata = tmp_path / "appdata"
    claude = appdata / "Claude" / "claude_desktop_config.json"
    claude.parent.mkdir(parents=True)
    claude.write_text('{"mcpServers":{"moon":{"command":"python"}}}', encoding="utf-8")

    report = run_doctor(
        home=home,
        appdata=appdata,
        codex_home=codex_home,
        which=_available,
    )

    hosts = {item["name"]: item for item in report["checks"] if item["category"] == "Hosts"}
    assert hosts["Antigravity"]["status"] == "PASS"
    assert hosts["Claude Desktop"]["status"] == "PASS"
    assert hosts["Codex in VS Code"]["status"] == "PASS"


def test_doctor_json_cli_is_machine_readable(tmp_path: Path, capsys):
    exit_code = main(["doctor", "--json"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code in {0, 1}
    assert output["exit_code"] == exit_code
    assert output["overall"] in {"PASS", "WARN", "FAIL"}
    assert isinstance(output["checks"], list)
