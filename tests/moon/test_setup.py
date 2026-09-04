from __future__ import annotations

import json
from pathlib import Path

from moon.hosts import base
from moon.setup import setup_integrations


def test_antigravity_setup_preserves_existing_config_and_is_idempotent(tmp_path: Path):
    project = tmp_path / "dự án Moon"
    project.mkdir()
    config_path = tmp_path / "mcp_config.json"
    original = {
        "theme": "dark",
        "mcpServers": {
            "existing": {"command": "existing-server", "args": ["--safe"]},
            "moon": {"command": "old-moon"},
        },
    }
    config_path.write_text(json.dumps(original), encoding="utf-8")

    first = setup_integrations(
        project,
        host="antigravity",
        write=True,
        config_path=config_path,
        home=tmp_path / "home",
    )
    first_bytes = config_path.read_bytes()
    second = setup_integrations(
        project,
        host="antigravity",
        write=True,
        config_path=config_path,
        home=tmp_path / "home",
    )

    stored = json.loads(config_path.read_text(encoding="utf-8"))
    assert stored["theme"] == "dark"
    assert stored["mcpServers"]["existing"] == original["mcpServers"]["existing"]
    assert stored["mcpServers"]["moon"]["args"][-1] == str(project.resolve())
    assert first_bytes == config_path.read_bytes()
    assert first["integrations"][0]["status"] == "configured"
    assert second["integrations"][0]["status"] == "configured"


def test_workspace_antigravity_setup_writes_only_workspace_config(tmp_path: Path):
    project = tmp_path / "workspace"
    project.mkdir()

    report = setup_integrations(
        project,
        host="antigravity",
        write=True,
        scope="workspace",
        home=tmp_path / "home",
    )

    expected = project / ".agents" / "mcp_config.json"
    assert Path(report["integrations"][0]["written_path"]) == expected
    assert json.loads(expected.read_text(encoding="utf-8"))["mcpServers"]["moon"]


def test_generated_output_is_atomic_and_leaves_no_temporary_file(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    output = tmp_path / "generated" / "moon.json"
    replacements = []
    real_replace = base.os.replace

    def tracking_replace(source, destination):
        replacements.append((Path(source), Path(destination)))
        return real_replace(source, destination)

    monkeypatch.setattr(base.os, "replace", tracking_replace)

    report = setup_integrations(
        project,
        host="generic",
        output=output,
        home=tmp_path / "home",
    )

    assert report["integrations"][0]["status"] == "generated"
    assert json.loads(output.read_text(encoding="utf-8"))["args"][-1] == str(project.resolve())
    assert len(replacements) == 1
    assert replacements[0][0].parent == output.parent.resolve()
    assert replacements[0][1] == output.resolve()
    assert list(output.parent.glob(f".{output.name}.*.tmp")) == []


def test_codex_write_request_never_overwrites_existing_toml(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    config_path = tmp_path / "config.toml"
    original = 'model = "custom"\n'
    config_path.write_text(original, encoding="utf-8")

    report = setup_integrations(
        project,
        host="codex",
        write=True,
        config_path=config_path,
        home=tmp_path / "home",
    )

    assert report["integrations"][0]["status"] == "manual_install_required"
    assert config_path.read_text(encoding="utf-8") == original


def test_setup_without_installed_hosts_still_generates_deterministic_preview(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()

    first = setup_integrations(project, home=tmp_path / "empty-home", appdata=tmp_path / "empty-appdata")
    second = setup_integrations(project, home=tmp_path / "empty-home", appdata=tmp_path / "empty-appdata")

    assert first == second
    assert len(first["integrations"]) == 4
    assert all(item["status"] == "preview" for item in first["integrations"])
    assert all(item["written_path"] is None for item in first["integrations"])
