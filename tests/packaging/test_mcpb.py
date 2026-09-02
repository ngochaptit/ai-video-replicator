from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from moon.core.project import MoonProject
from moon.runner.pipeline import PipelineRunner


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPOSITORY_ROOT / "packaging" / "mcpb" / "manifest.json"
LAUNCHER_PATH = REPOSITORY_ROOT / "packaging" / "mcpb" / "launcher.py"
BUILD_SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "build_mcpb.py"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_is_valid_mcpb_v04_uv():
    builder = _load_module("moon_mcpb_builder", BUILD_SCRIPT_PATH)
    manifest = builder.load_and_validate_manifest()

    assert manifest["manifest_version"] == "0.4"
    assert manifest["version"] == "0.2.5"
    assert manifest["server"]["type"] == "uv"
    assert manifest["server"]["entry_point"] == "launcher.py"
    assert manifest["server"]["mcp_config"]["command"] == "uv"
    assert manifest["server"]["mcp_config"]["args"] == [
        "run",
        "launcher.py",
        "--project",
        "${user_config.project_root}",
    ]
    assert "${__dirname}" not in json.dumps(manifest["server"]["mcp_config"])
    assert manifest["user_config"]["project_root"]["default"] == r"D:\AI EDIT VIDEO\8.26"
    assert {tool["name"] for tool in manifest["tools"]} == {
        "moon.status",
        "moon.next",
        "moon.handoff",
        "moon.evidence.list",
        "moon.evidence.read_json",
        "moon.evidence.read_image",
        "moon.evidence.clear_sampled",
        "moon.frames.sample",
        "moon.submit",
    }


def test_launcher_resolves_argument_then_environment_and_validates(tmp_path):
    launcher = _load_module("moon_mcpb_launcher", LAUNCHER_PATH)
    argument_project = tmp_path / "argument project"
    environment_project = tmp_path / "environment project"
    argument_project.mkdir()
    environment_project.mkdir()

    assert launcher.resolve_project_root(
        str(argument_project), environ={launcher.PROJECT_ROOT_ENV: str(environment_project)}
    ) == argument_project.resolve()
    assert launcher.resolve_project_root(
        environ={launcher.PROJECT_ROOT_ENV: str(environment_project)}
    ) == environment_project.resolve()
    with pytest.raises(FileNotFoundError, match="does not exist"):
        launcher.resolve_project_root(str(tmp_path / "missing"), environ={})


def test_packaged_launcher_starts_existing_mcp_server_outside_cwd(tmp_path):
    builder = _load_module("moon_mcpb_startup_builder", BUILD_SCRIPT_PATH)
    archive_path = builder.build_mcpb(tmp_path / "moon-local.mcpb")
    extracted = tmp_path / "installed extension"
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(extracted)

    project = tmp_path / "moon project"
    PipelineRunner(MoonProject.open(project, create=True))
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    initialize = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    completed = subprocess.run(
        [sys.executable, str(extracted / "launcher.py"), "--project", str(project)],
        cwd=tmp_path,
        env=env,
        input=initialize + "\n",
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    response = json.loads(completed.stdout)
    assert response["result"]["serverInfo"]["name"] == "moon-local"


@pytest.mark.skipif(shutil.which("uv") is None, reason="UV is not installed on PATH")
def test_claude_uv_launch_uses_installed_directory_as_cwd(tmp_path):
    builder = _load_module("moon_mcpb_uv_builder", BUILD_SCRIPT_PATH)
    archive_path = builder.build_mcpb(tmp_path / "moon-local.mcpb")
    extracted = tmp_path / "installed extension"
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(extracted)

    manifest = json.loads((extracted / "manifest.json").read_text(encoding="utf-8"))
    project = tmp_path / "moon project"
    PipelineRunner(MoonProject.open(project, create=True))
    configured_args = [
        str(project) if value == "${user_config.project_root}" else value
        for value in manifest["server"]["mcp_config"]["args"]
    ]

    assert manifest["server"]["mcp_config"]["command"] == "uv"
    assert configured_args == ["run", "launcher.py", "--project", str(project)]
    assert (extracted / configured_args[1]).is_file()

    initialize = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    completed = subprocess.run(
        ["uv", *configured_args],
        cwd=extracted,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
        input=initialize + "\n",
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["result"]["serverInfo"]["name"] == "moon-local"


def test_bundled_moon_is_importable_from_extracted_archive(tmp_path):
    builder = _load_module("moon_mcpb_import_builder", BUILD_SCRIPT_PATH)
    archive_path = builder.build_mcpb(tmp_path / "moon-local.mcpb")
    extracted = tmp_path / "installed extension"
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(extracted)

    unrelated_cwd = tmp_path / "unrelated cwd"
    unrelated_cwd.mkdir()
    code = (
        "import pathlib, sys; "
        f"sys.path.insert(0, {str(extracted)!r}); "
        "import moon; "
        "from moon.evidence import SampledFrameEvidenceStore; "
        "from tools.analysis.footage_profile_builder import FootageProfileBuilder; "
        "print(pathlib.Path(moon.__file__).resolve()); "
        "print(pathlib.Path(sys.modules['moon.evidence'].__file__).resolve()); "
        "print(SampledFrameEvidenceStore.__name__); "
        "print(FootageProfileBuilder.__name__)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=unrelated_cwd,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    output_lines = completed.stdout.splitlines()
    assert Path(output_lines[0]).is_relative_to(extracted.resolve())
    assert Path(output_lines[1]) == (extracted / "moon" / "evidence.py").resolve()
    assert output_lines[2] == "SampledFrameEvidenceStore"
    assert output_lines[3] == "FootageProfileBuilder"


def test_packaged_submit_reaches_stage_tool_after_semantic_validation(tmp_path):
    builder = _load_module("moon_mcpb_submit_builder", BUILD_SCRIPT_PATH)
    archive_path = builder.build_mcpb(tmp_path / "moon-local.mcpb")
    extracted = tmp_path / "installed extension"
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(extracted)

    project = tmp_path / "moon project"
    runner = PipelineRunner(MoonProject.open(project, create=True))
    runner.complete("proposal", {})
    runner.complete("analyze", {})
    (project / "footage").mkdir()
    runner.artifacts.write(
        "footage_profiles_scaffold",
        {
            "clips": [
                {
                    "clip_id": "clip_001",
                    "path": "footage/clip.mp4",
                    "duration_seconds": 2.0,
                    "segments": [
                        {
                            "source_in": 0.0,
                            "boundary_basis": ["scene_cut"],
                            "evidence": {"frame_timestamps": [1.0]},
                        }
                    ],
                }
            ]
        },
    )
    runner.artifacts.write("footage_agent_task", {"stage": "footage"})
    payload = {
        "clips": [
            {
                "clip_id": "clip_001",
                "path": "footage/clip.mp4",
                "segments": [
                    {
                        "source_in": 0.0,
                        "source_out": 2.0,
                        "boundary_basis": ["scene_cut", "video_end"],
                    }
                ],
            }
        ]
    }
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "moon.submit",
                "arguments": {"stage": "footage", "payload": payload, "auto_next": False},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "moon.next", "arguments": {}},
        },
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "moon.status", "arguments": {}},
        },
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 5, "reason": "transport regression fixture"},
        },
    ]
    completed = subprocess.run(
        [sys.executable, str(extracted / "launcher.py"), "--project", str(project)],
        cwd=tmp_path,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
        input="".join(json.dumps(request) + "\n" for request in requests),
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert "No module named 'tools'" not in completed.stdout
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    assert responses[0]["result"]["serverInfo"]["name"] == "moon-local"
    assert responses[0]["result"]["serverInfo"]["version"] == "0.2.5"
    assert any(tool["name"] == "moon.submit" for tool in responses[1]["result"]["tools"])
    submit = responses[2]["result"]
    assert submit["isError"] is False
    assert submit["structuredContent"]["status"] == "accepted"
    next_result = responses[3]["result"]
    assert next_result["isError"] is False
    assert next_result["structuredContent"]["status"] == "blocked"
    assert "No supported video files" in next_result["structuredContent"]["result"]["error"]
    assert responses[4]["result"]["isError"] is False
    assert [response["id"] for response in responses] == [1, 2, 3, 4, 5]
    assert json.loads(
        (project / ".moon" / "artifacts" / "footage_semantic_enrichment.json").read_text(
            encoding="utf-8"
        )
    ) == payload


def test_generated_archive_contains_required_files(tmp_path):
    builder = _load_module("moon_mcpb_archive_builder", BUILD_SCRIPT_PATH)
    output = builder.build_mcpb(tmp_path / "moon-local.mcpb")

    with zipfile.ZipFile(output) as archive:
        ordered_names = archive.namelist()
        names = set(ordered_names)
        archived_manifest = json.loads(archive.read("manifest.json"))
    entry_point = archived_manifest["server"]["entry_point"]
    launch_path = archived_manifest["server"]["mcp_config"]["args"][1]
    assert ordered_names.count("manifest.json") == 1
    assert entry_point == "launcher.py"
    assert entry_point in names
    assert launch_path == entry_point
    assert {
        "manifest.json",
        "launcher.py",
        "pyproject.toml",
        "README.md",
        "LICENSE",
        "moon/__main__.py",
        "moon/evidence.py",
        "moon/mcp_adapter.py",
        "tools/__init__.py",
        "tools/base_tool.py",
        "tools/analysis/footage_profile_builder.py",
        "tools/analysis/reference_candidate_ranker.py",
        "tools/analysis/reference_match_validator.py",
        "tools/video/reference_timeline_builder.py",
        "tools/video/reference_video_renderer.py",
        "lib/__init__.py",
    } <= names
    assert archived_manifest == json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert builder.verify_archive(output) == archived_manifest
