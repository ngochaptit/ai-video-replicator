from __future__ import annotations

import argparse
from collections.abc import Sequence

from moon.operator import OperatorWorker


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI Video Replicator operator worker")
    parser.add_argument("project_root")
    args = parser.parse_args(argv)
    return OperatorWorker(args.project_root).run()


if __name__ == "__main__":
    raise SystemExit(main())
