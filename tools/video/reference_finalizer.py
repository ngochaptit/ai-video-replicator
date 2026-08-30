"""Promote a QC-approved replication draft to the final deliverable.

This tool is deliberately boring: it never edits or improves video. It only
copies the latest draft to final.mp4 when canonical Phase 5 QC says the draft is
publishable and no rerender remains. That keeps the final gate deterministic.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolTier,
)


class ReferenceFinalizer(BaseTool):
    name = "reference_finalizer"
    version = "0.1.0"
    tier = ToolTier.CORE
    capability = "reference_replication_finalize"
    provider = "openmontage"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies: list[str] = []
    install_instructions = "No extra dependency beyond the OpenMontage Python environment."
    agent_skills: list[str] = []
    capabilities = ["qc_gated_publish", "promote_draft_to_final", "final_file_hash"]
    best_for = ["Phase 5 final deliverable promotion"]
    not_good_for = ["rendering", "semantic QC", "repairing a rejected draft"]

    input_schema = {
        "type": "object",
        "required": ["replication_qc_path", "draft_video_path", "output_path"],
        "properties": {
            "replication_qc_path": {"type": "string"},
            "draft_video_path": {"type": "string"},
            "output_path": {"type": "string"},
        },
    }
    output_schema = {"type": "object"}
    resource_profile = ResourceProfile(
        cpu_cores=1,
        ram_mb=128,
        vram_mb=0,
        disk_mb=5000,
        network_required=False,
    )
    idempotency_key_fields = ["replication_qc_path", "draft_video_path"]
    side_effects = ["writes final video file"]
    user_visible_verification = ["Play final.mp4", "Review final QC scores and any footage-limited improvement requests"]

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        qc_path = Path(inputs["replication_qc_path"])
        draft_path = Path(inputs["draft_video_path"])
        output_path = Path(inputs["output_path"])
        try:
            qc = self._read_json(qc_path)
            self._validate_publish_gate(qc)
            if not draft_path.is_file():
                raise OSError(f"Draft video not found: {draft_path}")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = output_path.with_name(f".{output_path.name}.tmp")
            shutil.copy2(draft_path, temp_path)
            temp_path.replace(output_path)
            digest = self._sha256(output_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return ToolResult(success=False, error=f"Reference finalization failed: {exc}")

        return ToolResult(
            success=True,
            data={
                "output": str(output_path),
                "qc_status": qc["status"],
                "fidelity_score": qc["scores"]["fidelity_score"],
                "quality_score": qc["scores"]["quality_score"],
                "footage_improvement_requests": qc.get("improvement_requests") or [],
                "sha256": digest,
            },
            artifacts=[str(output_path)],
        )

    @staticmethod
    def _validate_publish_gate(qc: dict[str, Any]) -> None:
        status = qc.get("status")
        if status not in {"pass", "footage_limited"}:
            raise ValueError(f"QC status {status!r} is not publishable; rerender/review is required")
        final_decision = qc.get("final_decision") or {}
        if final_decision.get("publishable") is not True:
            raise ValueError("QC final_decision.publishable must be true")
        if final_decision.get("requires_rerender") is not False:
            raise ValueError("QC final_decision.requires_rerender must be false")
        if status == "footage_limited" and not (qc.get("improvement_requests") or []):
            raise ValueError("footage_limited final requires concrete footage improvement requests")

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise OSError(f"JSON file not found: {path}")
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError(f"Expected JSON object: {path}")
        return value
