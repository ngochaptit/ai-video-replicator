from __future__ import annotations

import io
import json
import subprocess
import sys

from moon.connector import AgentConnectorService
from moon.core.project import MoonProject
from moon.execution import StageExecutionService
from moon.mcp_adapter import MoonMCPServer, serve_stdio
from moon.runner.pipeline import PipelineRunner
from tools.analysis.footage_profile_builder import FootageProfileBuilder


def _server(tmp_path) -> MoonMCPServer:
    runner = PipelineRunner(MoonProject.open(tmp_path, create=True))
    return MoonMCPServer(AgentConnectorService(runner))


def _assert_mcp_response(message: dict) -> None:
    assert message["jsonrpc"] == "2.0"
    assert not isinstance(message["id"], bool)
    assert isinstance(message["id"], (str, int))
    assert ("result" in message) != ("error" in message)
    if "error" in message:
        assert set(message["error"]) <= {"code", "message", "data"}
        assert isinstance(message["error"]["code"], int)
        assert isinstance(message["error"]["message"], str)


def _serve(server: MoonMCPServer, lines: list[str]) -> tuple[list[dict], str, str]:
    stdin = io.BytesIO("".join(line + "\n" for line in lines).encode())
    stdout = io.BytesIO()
    stderr = io.StringIO()
    serve_stdio(server, stdin=stdin, stdout=stdout, stderr=stderr)
    raw_stdout = stdout.getvalue().decode()
    responses = [json.loads(line) for line in raw_stdout.splitlines() if line]
    for response in responses:
        _assert_mcp_response(response)
    return responses, raw_stdout, stderr.getvalue()


def test_cancelled_notification_emits_no_response_and_server_stays_responsive(tmp_path):
    server = _server(tmp_path)
    requests = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "moon.status", "arguments": {}}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1, "reason": "timeout"}}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "moon.status", "arguments": {}}}),
    ]

    responses, _, _ = _serve(server, requests)

    assert [response["id"] for response in responses] == [1, 2]
    assert all(response["result"]["isError"] is False for response in responses)


def test_next_routes_python_and_captured_child_output_to_stderr(
    tmp_path, monkeypatch
):
    server = _server(tmp_path)

    def noisy_run(self):
        completed = FootageProfileBuilder().run_command(
            [
                sys.executable,
                "-c",
                "import sys; print('child stdout'); print('child stderr', file=sys.stderr)",
            ]
        )
        print("tool progress")
        print(completed.stdout.strip())
        print(completed.stderr.strip(), file=sys.stderr)
        return {
            "status": "blocked",
            "stage": self.runner.state.next_stage(),
            "error": "transport fixture completed",
        }

    monkeypatch.setattr(StageExecutionService, "run", noisy_run)
    requests = [
        json.dumps({"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "moon.next", "arguments": {}}}),
        json.dumps({"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {"name": "moon.status", "arguments": {}}}),
    ]

    responses, raw_stdout, raw_stderr = _serve(server, requests)

    assert [response["id"] for response in responses] == [10, 11]
    assert "tool progress" not in raw_stdout
    assert "child stdout" not in raw_stdout
    assert "child stderr" not in raw_stdout
    assert "tool progress" in raw_stderr
    assert "child stdout" in raw_stderr
    assert "child stderr" in raw_stderr


def test_transport_exception_and_malformed_input_never_emit_ad_hoc_json(tmp_path):
    server = _server(tmp_path)
    requests = [
        json.dumps({"jsonrpc": "2.0", "id": "unknown", "method": "not-a-method", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "id": "tool-error", "method": "tools/call", "params": {"name": "moon.evidence.read_json", "arguments": {"path": "../outside.json"}}}),
        "not-json",
        json.dumps({"jsonrpc": "2.0", "id": "after-error", "method": "tools/call", "params": {"name": "moon.status", "arguments": {}}}),
    ]

    responses, raw_stdout, raw_stderr = _serve(server, requests)

    assert [response["id"] for response in responses] == ["unknown", "tool-error", "after-error"]
    assert responses[0]["error"] == {"code": -32601, "message": "Method not found: not-a-method"}
    assert responses[1]["result"]["isError"] is True
    assert responses[1]["result"]["content"][0]["text"].startswith("connector evidence path")
    assert "\n{\"error\"" not in raw_stdout
    assert "moon-local MCP input error" in raw_stderr


def test_requests_without_valid_ids_never_generate_id_null_responses(tmp_path):
    server = _server(tmp_path)

    assert server.handle({"jsonrpc": "2.0", "method": "unknown", "params": {}}) is None
    assert server.handle({"jsonrpc": "2.0", "id": None, "method": "unknown", "params": {}}) is None
    assert server.handle({"jsonrpc": "2.0", "id": True, "method": "unknown", "params": {}}) is None
