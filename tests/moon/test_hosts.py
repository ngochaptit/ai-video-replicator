from __future__ import annotations

import json
from pathlib import Path

from moon.hosts import generated_config, profiles
from moon.hosts.base import HostProfile, canonical_mcp_launch


def test_host_profiles_are_normalized_and_share_canonical_runtime(tmp_path: Path):
    project = tmp_path / "Moon project"
    project.mkdir()
    values = profiles(project, home=tmp_path / "home", appdata=tmp_path / "appdata")

    assert set(values) == {"antigravity", "claude", "codex", "generic"}
    assert all(isinstance(item, HostProfile) for item in values.values())
    expected = canonical_mcp_launch(project)
    assert all(item.launch_command == expected for item in values.values())
    assert all(item.transport == "stdio" and item.supports_local_stdio for item in values.values())
    assert expected == (
        "python",
        "-m",
        "moon",
        "mcp",
        "--project",
        str(project.resolve()),
    )


def test_canonical_launch_preserves_spaces_and_unicode(tmp_path: Path):
    project = tmp_path / "dự án cà phê" / "video source"
    project.mkdir(parents=True)

    command = canonical_mcp_launch(project)

    assert command[-1] == str(project.resolve())
    assert command[-1] in generated_config(
        profiles(project, home=tmp_path)["generic"]
    )["args"]


def test_antigravity_config_uses_documented_mcp_servers_shape(tmp_path: Path):
    profile = profiles(tmp_path, home=tmp_path / "home")["antigravity"]
    config = generated_config(profile)

    assert config == {
        "mcpServers": {
            "moon": {
                "command": "python",
                "args": ["-m", "moon", "mcp", "--project", str(tmp_path.resolve())],
            }
        }
    }


def test_codex_config_uses_official_mcp_servers_toml_shape(tmp_path: Path):
    project = tmp_path / "Codex project"
    project.mkdir()
    config = generated_config(profiles(project, home=tmp_path / "home")["codex"])

    assert config.startswith("[mcp_servers.moon]\n")
    assert 'command = "python"' in config
    assert json.dumps(str(project.resolve()), ensure_ascii=False) in config
    assert '"mcp", "--project"' in config


def test_claude_profile_remains_mcpb_based(tmp_path: Path):
    profile = profiles(tmp_path, home=tmp_path / "home", appdata=tmp_path / "appdata")["claude"]
    config = generated_config(profile)

    assert profile.install_mode == "mcpb"
    assert config["integration"] == "mcpb"
    assert config["bundle"] == "dist/moon-local.mcpb"
    assert config["runtime"]["args"][-3:] == ["mcp", "--project", str(tmp_path.resolve())]


def test_generic_profile_is_future_host_escape_hatch(tmp_path: Path):
    config = generated_config(profiles(tmp_path, home=tmp_path)["generic"])

    assert config["transport"] == "stdio"
    assert config["command"] == "python"
    assert config["args"] == ["-m", "moon", "mcp", "--project", str(tmp_path.resolve())]


def test_host_detection_is_dependency_injected_and_does_not_require_apps(tmp_path: Path):
    values = profiles(tmp_path, home=tmp_path / "empty-home", appdata=tmp_path / "empty-appdata")

    assert values["antigravity"].detected is False
    assert values["claude"].detected is False
    assert values["codex"].detected is False
    assert values["generic"].detected is False
