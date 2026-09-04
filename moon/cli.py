from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from moon.agent_bridge import AgentBridgeService, parse_bridge_request
from moon.connector import AgentConnectorService, parse_connector_request
from moon.core.project import MoonProject
from moon.mcp_adapter import MoonMCPServer, serve_stdio
from moon.protocol import MoonProtocol
from moon.runner.pipeline import PipelineRunner


def _runner(project_root: str, *, create: bool = False) -> PipelineRunner:
    return PipelineRunner(MoonProject.open(project_root, create=create))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="moon", description="Moon Local deterministic video-editing runtime")
    parser.add_argument("--project", help="Project root (default: current directory)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("status")
    sub.add_parser("resume")
    next_cmd = sub.add_parser("next", help="Run until completion, blockage, or the next semantic agent boundary")
    next_cmd.add_argument("--max-steps", default=20, type=int)
    sub.add_parser("stage-plan")
    sub.add_parser("run-stage")
    handoff = sub.add_parser("handoff", help="Build the current external-agent task package")
    handoff.add_argument("--stage")
    submit_handoff = sub.add_parser("submit-handoff", help="Validate and store an external-agent response from a JSON file")
    submit_handoff.add_argument("stage")
    submit_handoff.add_argument("json_file", type=Path)
    submit_stdin = sub.add_parser("submit-handoff-stdin", help="Validate and store an external-agent response read from stdin")
    submit_stdin.add_argument("stage")
    sub.add_parser("agent-bridge", help="Read one agent bridge JSON request from stdin and write one JSON response")
    sub.add_parser("connector-manifest", help="Print the stable agent connector tool manifest")
    sub.add_parser("connector-call", help="Read one connector tool request from stdin and write one JSON response")
    mcp = sub.add_parser("mcp", help="Serve the canonical host-neutral Moon MCP stdio server")
    mcp.add_argument("--project", dest="command_project", help="Moon project root")
    legacy_mcp = sub.add_parser("mcp-stdio", help="Compatibility alias for 'moon mcp'")
    legacy_mcp.add_argument("--project", dest="command_project", help="Moon project root")
    setup = sub.add_parser("setup", help="Generate or safely install Moon host configuration")
    setup.add_argument("--project", dest="command_project", help="Moon project root")
    setup.add_argument("--host", help="antigravity, claude, codex, or generic")
    setup.add_argument("--write", action="store_true", help="Apply supported merge-safe host configuration")
    setup.add_argument("--output", type=Path, help="Atomically write one generated config artifact")
    setup.add_argument("--config-path", type=Path, help="Override the host config path")
    setup.add_argument("--scope", choices=("global", "workspace"), default="global")
    setup.add_argument("--json", action="store_true", dest="output_json")
    doctor = sub.add_parser("doctor", help="Run read-only Moon runtime, project, and host diagnostics")
    doctor.add_argument("--project", dest="command_project", help="Moon project root")
    doctor.add_argument("--json", action="store_true", dest="output_json")
    sub.add_parser("inspect-reference")
    sub.add_parser("inspect-footage")
    sub.add_parser("discover-artifacts")
    sub.add_parser("bootstrap-legacy")
    import_artifacts = sub.add_parser("import-artifacts")
    import_artifacts.add_argument("--stage")
    complete_artifacts = sub.add_parser("complete-from-artifacts")
    complete_artifacts.add_argument("--stage")
    frames = sub.add_parser("frames")
    frames.add_argument("--source", required=True)
    frames.add_argument("--from", dest="start_seconds", required=True, type=float)
    frames.add_argument("--to", dest="end_seconds", required=True, type=float)
    frames.add_argument("--count", default=8, type=int)
    frames.add_argument("--width", default=320, type=int)
    begin = sub.add_parser("begin")
    begin.add_argument("stage", nargs="?")
    complete = sub.add_parser("complete")
    complete.add_argument("stage")
    complete.add_argument("checkpoint", type=Path)
    submit = sub.add_parser("submit")
    submit.add_argument("name")
    submit.add_argument("json_file", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command_project = getattr(args, "command_project", None)
    selected_project = command_project or args.project

    if args.command == "setup":
        from moon.setup import format_setup_report, setup_integrations

        result = setup_integrations(
            selected_project,
            host=args.host,
            write=args.write,
            output=args.output,
            config_path=args.config_path,
            scope=args.scope,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.output_json else format_setup_report(result))
        return 0
    if args.command == "doctor":
        from moon.doctor import format_doctor_report, run_doctor

        result = run_doctor(selected_project)
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.output_json else format_doctor_report(result))
        return int(result["exit_code"])

    project_root = selected_project or "."
    runner = _runner(project_root, create=args.command == "init")
    protocol = MoonProtocol(runner)

    if args.command in {"mcp", "mcp-stdio"}:
        serve_stdio(MoonMCPServer(AgentConnectorService(runner)))
        return 0
    if args.command == "init":
        result = runner.status()
    elif args.command == "status":
        result = protocol.handle({"action": "status"})["result"]
    elif args.command == "resume":
        result = protocol.handle({"action": "resume"})["result"]
    elif args.command == "next":
        result = protocol.handle({"action": "next", "max_steps": args.max_steps})["result"]
    elif args.command == "stage-plan":
        result = protocol.handle({"action": "stage.plan"})["result"]
    elif args.command == "run-stage":
        result = protocol.handle({"action": "stage.run"})["result"]
    elif args.command == "handoff":
        result = protocol.handle({"action": "handoff.package", "stage": args.stage})["result"]
    elif args.command == "submit-handoff":
        payload = json.loads(args.json_file.read_text(encoding="utf-8"))
        result = protocol.handle({"action": "handoff.submit", "stage": args.stage, "payload": payload})["result"]
    elif args.command == "submit-handoff-stdin":
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            raise ValueError("submit-handoff-stdin requires one JSON object on stdin")
        result = protocol.handle({"action": "handoff.submit", "stage": args.stage, "payload": payload})["result"]
    elif args.command == "agent-bridge":
        request = parse_bridge_request(sys.stdin.read())
        result = AgentBridgeService(runner).request(request)
    elif args.command == "connector-manifest":
        result = AgentConnectorService(runner).manifest()
    elif args.command == "connector-call":
        request = parse_connector_request(sys.stdin.read())
        result = AgentConnectorService(runner).call(request)
    elif args.command == "inspect-reference":
        result = protocol.handle({"action": "media.inspect.reference"})["result"]
    elif args.command == "inspect-footage":
        result = protocol.handle({"action": "media.inspect.footage"})["result"]
    elif args.command == "discover-artifacts":
        result = protocol.handle({"action": "artifact.discover"})["result"]
    elif args.command == "bootstrap-legacy":
        result = protocol.handle({"action": "stage.bootstrap_legacy"})["result"]
    elif args.command == "import-artifacts":
        result = protocol.handle({"action": "artifact.import", "stage": args.stage})["result"]
    elif args.command == "complete-from-artifacts":
        result = protocol.handle({"action": "stage.complete_from_artifacts", "stage": args.stage})["result"]
    elif args.command == "frames":
        result = protocol.handle({
            "action": "media.frames",
            "source": args.source,
            "start_seconds": args.start_seconds,
            "end_seconds": args.end_seconds,
            "count": args.count,
            "width": args.width,
        })["result"]
    elif args.command == "begin":
        result = protocol.handle({"action": "begin", "stage": args.stage})["result"]
    elif args.command == "complete":
        result = protocol.handle({
            "action": "complete",
            "stage": args.stage,
            "checkpoint": json.loads(args.checkpoint.read_text(encoding="utf-8")),
        })["result"]
    elif args.command == "submit":
        result = protocol.handle({
            "action": "artifact.write",
            "name": args.name,
            "payload": json.loads(args.json_file.read_text(encoding="utf-8")),
        })["result"]
    else:  # pragma: no cover
        raise AssertionError(args.command)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
