from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path


DEFAULT_PROJECT_ROOT = r"D:\AI EDIT VIDEO\8.26"
PROJECT_ROOT_ENV = "MOON_PROJECT_ROOT"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Launch the bundled Moon MCP stdio server")
    parser.add_argument(
        "--project",
        help=f"Moon project root (overrides {PROJECT_ROOT_ENV} and the development default)",
    )
    return parser


def resolve_project_root(
    project: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    environment = os.environ if environ is None else environ
    selected = project or environment.get(PROJECT_ROOT_ENV) or DEFAULT_PROJECT_ROOT
    root = Path(selected).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"Moon project root does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"Moon project root is not a directory: {root}")
    return root


def resolve_moon_source_root(launcher_file: str | Path = __file__) -> Path:
    """Find the Moon package bundled beside the installed launcher."""
    bundle_root = Path(launcher_file).resolve().parent
    if not (bundle_root / "moon" / "__main__.py").is_file():
        raise FileNotFoundError(f"Bundled Moon package was not found in: {bundle_root}")
    return bundle_root


def moon_arguments(project_root: Path) -> list[str]:
    return ["--project", str(project_root), "mcp-stdio"]


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        project_root = resolve_project_root(args.project)
        source_root = resolve_moon_source_root()
        source = str(source_root)
        if source not in sys.path:
            sys.path.insert(0, source)

        # Delegate to the existing CLI and MCP adapter; this launcher owns no MCP logic.
        from moon.cli import main as moon_main

        return moon_main(moon_arguments(project_root))
    except (ImportError, OSError, ValueError) as exc:
        print(f"moon-local: {exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
