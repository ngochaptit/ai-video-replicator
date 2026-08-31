from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from moon.connector import AgentConnectorService
from moon.core.project import MoonProject
from moon.runner.pipeline import PipelineRunner

MCP_PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "moon-local"
SERVER_VERSION = "0.1.0"

_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "moon.status": {"type": "object", "properties": {}, "additionalProperties": False},
    "moon.next": {
        "type": "object",
        "properties": {"max_steps": {"type": "integer", "minimum": 1}},
        "additionalProperties": False,
    },
    "moon.handoff": {
        "type": "object",
        "properties": {"stage": {"type": "string"}},
        "additionalProperties": False,
    },
    "moon.evidence.list": {
        "type": "object",
        "properties": {"stage": {"type": "string"}},
        "additionalProperties": False,
    },
    "moon.evidence.read_json": {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    },
    "moon.frames.sample": {
        "type": "object",
        "properties": {
            "source": {"type": "string"},
            "start_seconds": {"type": "number"},
            "end_seconds": {"type": "number"},
            "count": {"type": "integer", "minimum": 1},
            "width": {"type": "integer", "minimum": 1},
        },
        "required": ["source", "start_seconds", "end_seconds"],
        "additionalProperties": False,
    },
    "moon.submit": {
        "type": "object",
        "properties": {
            "stage": {"type": "string"},
            "payload": {"type": "object"},
            "auto_next": {"type": "boolean"},
        },
        "required": ["stage", "payload"],
        "additionalProperties": False,
    },
}

_TOOL_DESCRIPTIONS = {
    "moon.status": "Read resumable Moon pipeline state.",
    "moon.next": "Run deterministic work until completion, blockage, or the next semantic boundary.",
    "moon.handoff": "Get the current semantic task, evidence references, and validated output contract.",
    "moon.evidence.list": "List typed evidence files for the current semantic stage.",
    "moon.evidence.read_json": "Read JSON evidence inside the local project root.",
    "moon.frames.sample": "Deterministically sample local video frames with FFmpeg; does not choose shots.",
    "moon.submit": "Submit an external-agent semantic decision through Moon validation and optionally advance.",
}


@dataclass
class MoonMCPServer:
    connector: AgentConnectorService

    @classmethod
    def open(cls, project_root: str | Path) -> "MoonMCPServer":
        project = MoonProject.open(project_root)
        return cls(AgentConnectorService(PipelineRunner(project)))

    def tools(self) -> list[dict[str, Any]]:
        manifest_names = [item["name"] for item in self.connector.manifest()["tools"]]
        return [
            {
                "name": name,
                "description": _TOOL_DESCRIPTIONS[name],
                "inputSchema": _TOOL_SCHEMAS[name],
            }
            for name in manifest_names
        ]

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params", {})
        if not isinstance(method, str):
            return self._error(request_id, -32600, "Invalid Request")
        if not isinstance(params, dict):
            return self._error(request_id, -32602, "Invalid params")

        if method == "notifications/initialized":
            return None
        if method == "initialize":
            return self._result(request_id, {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })
        if method == "ping":
            return self._result(request_id, {})
        if method == "tools/list":
            return self._result(request_id, {"tools": self.tools()})
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(arguments, dict):
                return self._error(request_id, -32602, "tools/call requires name and object arguments")
            try:
                data = self.connector.call({"tool": name, "arguments": arguments})
            except Exception as exc:
                return self._result(request_id, {
                    "content": [{"type": "text", "text": json.dumps({"error": str(exc)}, ensure_ascii=False)}],
                    "isError": True,
                })
            return self._result(request_id, {
                "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                "structuredContent": data,
                "isError": False,
            })
        return self._error(request_id, -32601, f"Method not found: {method}")

    @staticmethod
    def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def serve_stdio(server: MoonMCPServer, stdin: BinaryIO | None = None, stdout: BinaryIO | None = None) -> None:
    """Serve newline-delimited JSON-RPC over stdio.

    MCP stdio messages are one JSON-RPC object per line. Protocol output is written only
    to stdout; adapters/tools should send diagnostics to stderr if ever needed.
    """
    source = stdin or sys.stdin.buffer
    sink = stdout or sys.stdout.buffer
    for raw in source:
        if not raw.strip():
            continue
        try:
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            response = server.handle(request)
        except Exception as exc:
            response = MoonMCPServer._error(None, -32700, f"Parse error: {exc}")
        if response is not None:
            sink.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
            sink.flush()
