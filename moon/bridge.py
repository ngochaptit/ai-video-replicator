from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from moon.runner.pipeline import PipelineRunner


_STAGE_ARTIFACTS: dict[str, tuple[str, ...]] = {
    "proposal": ("proposal_packet",),
    "analyze": ("reference_blueprint", "semantic_enrichment"),
    "footage": ("footage_profiles",),
    "match": ("match_decisions",),
    "timeline": ("timeline",),
    "render": ("draft_render",),
    "qc": ("qc_report", "decision_log"),
}


@dataclass(frozen=True)
class ImportResult:
    stage: str
    imported: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "imported": self.imported}


def _candidate_paths(project_root: Path, name: str) -> tuple[Path, ...]:
    filename = f"{name}.json"
    return (
        project_root / filename,
        project_root / "analysis" / filename,
        project_root / "output" / filename,
        project_root / "artifacts" / filename,
    )


def discover_existing_artifacts(project_root: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    names = sorted({name for names in _STAGE_ARTIFACTS.values() for name in names})
    for name in names:
        for candidate in _candidate_paths(project_root, name):
            if candidate.is_file():
                found[name] = str(candidate)
                break
    return found


def import_existing_artifacts(runner: PipelineRunner, *, stage: str | None = None) -> ImportResult:
    target_stage = stage or runner.state.next_stage()
    if target_stage is None:
        raise ValueError("pipeline is already complete")
    if target_stage not in _STAGE_ARTIFACTS:
        raise ValueError(f"unsupported bridge stage: {target_stage}")

    discovered = discover_existing_artifacts(runner.project.root)
    imported: dict[str, str] = {}
    for name in _STAGE_ARTIFACTS[target_stage]:
        source = discovered.get(name)
        if source is None:
            continue
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"artifact {name!r} must contain a JSON object")
        runner.artifacts.write(name, payload)
        imported[name] = source
    return ImportResult(stage=target_stage, imported=imported)


def complete_from_imported_artifacts(runner: PipelineRunner, *, stage: str | None = None) -> dict[str, Any]:
    result = import_existing_artifacts(runner, stage=stage)
    required = _STAGE_ARTIFACTS[result.stage]
    missing = [name for name in required if not runner.artifacts.exists(name)]
    if missing:
        raise FileNotFoundError(
            f"cannot complete {result.stage!r}; missing canonical artifacts: {', '.join(missing)}"
        )
    checkpoint = {
        "source": "existing_project_artifacts",
        "artifacts": {name: str(runner.artifacts.path_for(name)) for name in required},
    }
    status = runner.complete(result.stage, checkpoint)
    return {"stage": result.stage, "imported": result.imported, "status": status}
