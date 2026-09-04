from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from moon.core.project import MoonProject
from moon.runner.pipeline import PipelineRunner


def test_canonical_mcp_cli_serves_pure_jsonrpc_for_unicode_project(tmp_path: Path):
    project = tmp_path / "dự án Moon có khoảng trắng"
    PipelineRunner(MoonProject.open(project, create=True))
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})

    completed = subprocess.run(
        [sys.executable, "-m", "moon", "mcp", "--project", str(project)],
        cwd=Path(__file__).resolve().parents[2],
        env=os.environ.copy(),
        input=request + "\n",
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    response = json.loads(completed.stdout)
    assert response["id"] == 1
    assert response["result"]["serverInfo"] == {"name": "moon-local", "version": "0.2.5"}


def test_legacy_mcp_stdio_spelling_remains_compatible(tmp_path: Path):
    project = tmp_path / "project"
    PipelineRunner(MoonProject.open(project, create=True))
    request = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}})

    completed = subprocess.run(
        [sys.executable, "-m", "moon", "--project", str(project), "mcp-stdio"],
        cwd=Path(__file__).resolve().parents[2],
        input=request + "\n",
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["result"] == {}
