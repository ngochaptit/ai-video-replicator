from __future__ import annotations

import base64
import io
import json

from moon.connector import AgentConnectorService
from moon.core.project import MoonProject
from moon.mcp_adapter import MCP_PROTOCOL_VERSION, MoonMCPServer, serve_stdio
from moon.runner.pipeline import PipelineRunner


def _server(tmp_path):
    project = MoonProject.open(tmp_path, create=True)
    return MoonMCPServer(AgentConnectorService(PipelineRunner(project)))


def test_initialize_advertises_tools(tmp_path):
    server = _server(tmp_path)
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert response["result"]["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert response["result"]["capabilities"] == {"tools": {}}
    assert response["result"]["serverInfo"]["name"] == "moon-local"


def test_tools_list_maps_connector_surface(tmp_path):
    server = _server(tmp_path)
    response = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    tools = {tool["name"]: tool for tool in response["result"]["tools"]}
    assert set(tools) == {
        "moon.status", "moon.next", "moon.handoff", "moon.evidence.list",
        "moon.evidence.read_json", "moon.evidence.read_image", "moon.evidence.clear_sampled",
        "moon.frames.sample", "moon.submit",
    }
    assert tools["moon.submit"]["inputSchema"]["required"] == ["stage", "payload"]
    assert tools["moon.evidence.read_image"]["inputSchema"]["required"] == ["path"]


def test_tools_call_delegates_to_connector(tmp_path):
    server = _server(tmp_path)
    response = server.handle({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "moon.status", "arguments": {}},
    })
    assert response["result"]["isError"] is False
    assert response["result"]["structuredContent"]["next_stage"] == "proposal"
    text = json.loads(response["result"]["content"][0]["text"])
    assert text["next_stage"] == "proposal"


def test_read_image_returns_native_mcp_image_content(tmp_path):
    server = _server(tmp_path)
    image = tmp_path / "analysis" / "frame.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"fake-jpeg-bytes")
    response = server.handle({
        "jsonrpc": "2.0", "id": 6, "method": "tools/call",
        "params": {"name": "moon.evidence.read_image", "arguments": {"path": "analysis/frame.jpg"}},
    })
    result = response["result"]
    assert result["isError"] is False
    assert result["content"][1]["type"] == "image"
    assert result["content"][1]["mimeType"] == "image/jpeg"
    assert base64.b64decode(result["content"][1]["data"]) == b"fake-jpeg-bytes"
    assert "data_base64" not in result["structuredContent"]


def test_tool_failure_is_mcp_tool_error(tmp_path):
    server = _server(tmp_path)
    response = server.handle({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "moon.evidence.read_json", "arguments": {"path": "../outside.json"}},
    })
    assert response["result"]["isError"] is True
    assert "project root" in response["result"]["content"][0]["text"]


def test_stdio_handles_initialize_list_and_notification(tmp_path):
    server = _server(tmp_path)
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    stdin = io.BytesIO("".join(json.dumps(item) + "\n" for item in requests).encode())
    stdout = io.BytesIO()
    serve_stdio(server, stdin=stdin, stdout=stdout)
    responses = [json.loads(line) for line in stdout.getvalue().decode().splitlines()]
    assert [item["id"] for item in responses] == [1, 2]
    assert responses[1]["result"]["tools"]


def test_unknown_method_returns_jsonrpc_error(tmp_path):
    server = _server(tmp_path)
    response = server.handle({"jsonrpc": "2.0", "id": 5, "method": "nope", "params": {}})
    assert response["error"]["code"] == -32601
