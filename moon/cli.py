from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from moon.core.project import MoonProject
from moon.protocol import MoonProtocol
from moon.runner.pipeline import PipelineRunner


def _runner(project_root: str, *, create: bool = False) -> PipelineRunner:
    return PipelineRunner(MoonProject.open(project_root, create=create))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="moon", description="Moon Local deterministic video-editing runtime")
    parser.add_argument("--project", default=".", help="Project root (default: current directory)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Initialize .moon state in a project")
    sub.add_parser("status", help="Print resumable pipeline status as JSON")
    sub.add_parser("resume", help="Clear transient blocked/running state and resume from the next incomplete stage")
    sub.add_parser("inspect-reference", help="Probe the configured reference video")
    sub.add_parser("inspect-footage", help="Probe every video in the configured footage directory")

    frames = sub.add_parser("frames", help="Extract timestamped local frames for an external agent")
    frames.add_argument("--source", required=True, help="Project-relative source video path")
    frames.add_argument("--from", dest="start_seconds", required=True, type=float)
    frames.add_argument("--to", dest="end_seconds", required=True, type=float)
    frames.add_argument("--count", default=8, type=int)
    frames.add_argument("--width", default=320, type=int)

    begin = sub.add_parser("begin", help="Mark the next stage as running")
    begin.add_argument("stage", nargs="?")

    complete = sub.add_parser("complete", help="Complete a stage with a checkpoint JSON file")
    complete.add_argument("stage")
    complete.add_argument("checkpoint", type=Path)

    submit = sub.add_parser("submit", help="Write a named agent artifact from a JSON file")
    submit.add_argument("name")
    submit.add_argument("json_file", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    create = args.command == "init"
    runner = _runner(args.project, create=create)
    protocol = MoonProtocol(runner)

    if args.command == "init":
        result = runner.status()
    elif args.command == "status":
        result = protocol.handle({"action": "status"})["result"]
    elif args.command == "resume":
        result = protocol.handle({"action": "resume"})["result"]
    elif args.command == "inspect-reference":
        result = protocol.handle({"action": "media.inspect.reference"})["result"]
    elif args.command == "inspect-footage":
        result = protocol.handle({"action": "media.inspect.footage"})["result"]
    elif args.command == "frames":
        result = protocol.handle(
            {
                "action": "media.frames",
                "source": args.source,
                "start_seconds": args.start_seconds,
                "end_seconds": args.end_seconds,
                "count": args.count,
                "width": args.width,
            }
        )["result"]
    elif args.command == "begin":
        result = protocol.handle({"action": "begin", "stage": args.stage})["result"]
    elif args.command == "complete":
        checkpoint = json.loads(args.checkpoint.read_text(encoding="utf-8"))
        result = protocol.handle({"action": "complete", "stage": args.stage, "checkpoint": checkpoint})["result"]
    elif args.command == "submit":
        payload = json.loads(args.json_file.read_text(encoding="utf-8"))
        result = protocol.handle({"action": "artifact.write", "name": args.name, "payload": payload})["result"]
    else:  # pragma: no cover
        raise AssertionError(args.command)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
