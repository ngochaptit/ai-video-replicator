"""Validate and canonicalize GPT semantic QC for reference replication.

The agent vision model owns the semantic judgment. This deterministic tool only
enforces score ranges, known segment IDs, technical duration constraints, and
status semantics before the review can gate a rerender or final publish.
"""
from __future__ import annotations

import json
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


class ReferenceQCValidator(BaseTool):
    name = "reference_qc_validator"
    version = "0.1.0"
    tier = ToolTier.ANALYZE
    capability = "reference_replication_qc"
    provider = "openmontage"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies: list[str] = []
    install_instructions = "No extra dependency beyond the OpenMontage Python environment."
    agent_skills: list[str] = []
    capabilities = [
        "validate_gpt_fidelity_review",
        "separate_fidelity_and_quality_gates",
        "route_fixable_revision_actions",
        "validate_footage_limited_completion",
    ]
    best_for = ["Phase 5 canonical QC gate after agent vision review"]
    not_good_for = ["performing vision analysis", "inventing revision actions", "rendering"]

    SCORE_DIMENSIONS = [
        "fidelity_score",
        "quality_score",
        "choreography",
        "timing",
        "camera_framing",
        "motion_speed",
        "transitions",
        "text",
        "audio",
        "technical_quality",
    ]
    REVIEW_DIMENSIONS = {
        "choreography",
        "timing",
        "camera_framing",
        "motion_speed",
        "transitions",
        "text",
        "audio",
        "technical_quality",
    }

    input_schema = {
        "type": "object",
        "required": ["reference_blueprint_path", "qc_evidence_path", "semantic_review_path", "output_path"],
        "properties": {
            "reference_blueprint_path": {"type": "string"},
            "qc_evidence_path": {"type": "string"},
            "semantic_review_path": {"type": "string"},
            "replication_quality_report_path": {"type": "string"},
            "output_path": {"type": "string"},
            "fidelity_pass": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.85},
            "quality_pass": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.80},
            "duration_tolerance_seconds": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.15},
        },
    }
    output_schema = {
        "type": "object",
        "description": "Replication QC — see schemas/artifacts/replication_qc.schema.json",
    }
    resource_profile = ResourceProfile(
        cpu_cores=1,
        ram_mb=256,
        vram_mb=0,
        disk_mb=10,
        network_required=False,
    )
    idempotency_key_fields = ["qc_evidence_path", "semantic_review_path"]
    side_effects = ["writes replication_qc.json"]
    user_visible_verification = [
        "Confirm FidelityScore and QualityScore are reported separately",
        "Review revision routes and footage improvement requests",
        "Confirm publishable status matches the QC gate",
    ]

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        blueprint_path = Path(inputs["reference_blueprint_path"])
        evidence_path = Path(inputs["qc_evidence_path"])
        review_path = Path(inputs["semantic_review_path"])
        output_path = Path(inputs["output_path"])
        try:
            blueprint = self._read_json(blueprint_path)
            evidence = self._read_json(evidence_path)
            review = self._read_json(review_path)
            quality_report_path = inputs.get("replication_quality_report_path")
            quality_report = self._read_json(Path(quality_report_path)) if quality_report_path else None
            qc = self.build_qc(
                blueprint,
                evidence,
                review,
                source_evidence_path=str(evidence_path),
                deterministic_quality_report=quality_report,
                deterministic_quality_report_path=str(quality_report_path or ""),
                fidelity_pass=float(inputs.get("fidelity_pass", 0.85)),
                quality_pass=float(inputs.get("quality_pass", 0.80)),
                duration_tolerance_seconds=float(inputs.get("duration_tolerance_seconds", 0.15)),
            )
            self._validate_schema(qc)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(qc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return ToolResult(success=False, error=f"Reference QC validation failed: {exc}")

        return ToolResult(success=True, data=qc, artifacts=[str(output_path)])

    def build_qc(
        self,
        blueprint: dict[str, Any],
        evidence: dict[str, Any],
        review: dict[str, Any],
        *,
        source_evidence_path: str = "qc_evidence.json",
        deterministic_quality_report: dict[str, Any] | None = None,
        deterministic_quality_report_path: str = "",
        fidelity_pass: float = 0.85,
        quality_pass: float = 0.80,
        duration_tolerance_seconds: float = 0.15,
    ) -> dict[str, Any]:
        if not 0 <= fidelity_pass <= 1 or not 0 <= quality_pass <= 1:
            raise ValueError("QC thresholds must be between 0 and 1")

        known_ids = {str(item.get("id") or "") for item in blueprint.get("segments") or []}
        known_ids.discard("")
        if not known_ids:
            raise ValueError("Reference Blueprint has no segment ids")

        evidence_ids = {str(item.get("reference_segment_id") or "") for item in evidence.get("segments") or []}
        if evidence_ids != known_ids:
            missing = sorted(known_ids - evidence_ids)
            extra = sorted(evidence_ids - known_ids)
            raise ValueError(f"QC evidence coverage mismatch; missing={missing}, extra={extra}")

        duration_delta = float(evidence.get("duration_delta_seconds") or 0.0)
        status = str(review.get("status") or "")
        if status not in {"pass", "revise", "footage_limited"}:
            raise ValueError(f"Invalid QC status: {status!r}")

        scores = self._validate_scores(review.get("scores") or {})
        segment_reviews = self._validate_segment_reviews(review.get("segment_reviews") or [], known_ids)
        revision_actions = self._validate_revision_actions(review.get("revision_actions") or [], known_ids)
        improvement_requests = self._validate_improvement_requests(review.get("improvement_requests") or [], known_ids)
        final_decision = self._validate_final_decision(review.get("final_decision") or {})
        summary = str(review.get("summary") or "").strip()
        if not summary:
            raise ValueError("QC review requires a non-empty summary")

        fidelity = scores["fidelity_score"]
        quality = scores["quality_score"]
        has_high_issue = any(item["severity"] == "high" for item in segment_reviews)

        if deterministic_quality_report is not None:
            render_integrity = str((deterministic_quality_report.get("render_integrity") or {}).get("status") or "fail")
            quality_gate = str(deterministic_quality_report.get("quality_gate") or "fail")
            source_limited = bool((deterministic_quality_report.get("replication_quality") or {}).get("source_limited"))
            if render_integrity != "pass" and status != "revise":
                raise ValueError("deterministic render integrity failure requires status=revise")
            if quality_gate == "fail" and status == "pass":
                raise ValueError("pass contradicts deterministic replication quality failure")
            if source_limited and status == "revise":
                raise ValueError("source-limited quality failure cannot be fixed by a render-only revision")

        if duration_delta > duration_tolerance_seconds and status != "revise":
            raise ValueError(
                f"Draft duration delta {duration_delta:.3f}s exceeds {duration_tolerance_seconds:.3f}s; status must be revise"
            )

        if status == "pass":
            if fidelity < fidelity_pass or quality < quality_pass:
                raise ValueError(
                    f"pass requires fidelity>={fidelity_pass:.2f} and quality>={quality_pass:.2f}"
                )
            if has_high_issue:
                raise ValueError("pass cannot contain a high-severity segment issue")
            if revision_actions:
                raise ValueError("pass cannot contain revision_actions")
            if not final_decision["publishable"] or final_decision["requires_rerender"]:
                raise ValueError("pass requires publishable=true and requires_rerender=false")

        elif status == "revise":
            if not revision_actions:
                raise ValueError("revise requires at least one revision_action")
            if final_decision["publishable"] or not final_decision["requires_rerender"]:
                raise ValueError("revise requires publishable=false and requires_rerender=true")

        elif status == "footage_limited":
            if quality < quality_pass:
                raise ValueError(
                    f"footage_limited requires standalone quality>={quality_pass:.2f}; technical/quality defects must be revised first"
                )
            if not improvement_requests:
                raise ValueError("footage_limited requires at least one concrete footage improvement request")
            if revision_actions:
                raise ValueError("footage_limited cannot retain fixable revision_actions; resolve them before finalizing")
            if not final_decision["publishable"] or final_decision["requires_rerender"]:
                raise ValueError("footage_limited requires publishable=true and requires_rerender=false")

        iteration = int(review.get("iteration") or (evidence.get("metadata") or {}).get("iteration") or 1)
        if iteration < 1:
            raise ValueError("QC iteration must be >= 1")

        notes = [str(item) for item in review.get("notes") or []]
        if status == "footage_limited" and fidelity < fidelity_pass:
            notes.append(
                "Final is technically publishable but reference fidelity is below target because available footage is the limiting factor."
            )

        output = {
            "version": "1.0",
            "iteration": iteration,
            "status": status,
            "scores": scores,
            "summary": summary,
            "segment_reviews": segment_reviews,
            "revision_actions": revision_actions,
            "improvement_requests": improvement_requests,
            "final_decision": final_decision,
            "metadata": {
                "generated_by": self.name,
                "semantic_review_completed": True,
                "source_evidence_path": source_evidence_path,
                "deterministic_quality_report_path": deterministic_quality_report_path,
                "thresholds": {
                    "fidelity_pass": round(fidelity_pass, 6),
                    "quality_pass": round(quality_pass, 6),
                },
                "notes": list(dict.fromkeys(notes)),
            },
        }
        if deterministic_quality_report is not None:
            output["render_integrity"] = dict(deterministic_quality_report["render_integrity"])
            output["replication_quality"] = {
                "quality_gate": deterministic_quality_report["quality_gate"],
                "fallback_count": deterministic_quality_report["fallback_count"],
                "fallback_ratio": deterministic_quality_report["fallback_ratio"],
                "unique_source_segment_count": deterministic_quality_report["unique_source_segment_count"],
                "reuse_ratio": deterministic_quality_report["reuse_ratio"],
                "max_reuse_count": deterministic_quality_report["max_reuse_count"],
                "dominant_source_share": deterministic_quality_report["dominant_source_share"],
                "overlap_reuse_count": deterministic_quality_report["overlap_reuse_count"],
                "speed": dict(deterministic_quality_report["speed"]),
                "chronology": dict(deterministic_quality_report["chronology"]),
                "quality_flags": list(deterministic_quality_report["quality_flags"]),
                **dict(deterministic_quality_report["replication_quality"]),
            }
        return output

    def _validate_scores(self, raw: dict[str, Any]) -> dict[str, Any]:
        scores: dict[str, Any] = {}
        for name in self.SCORE_DIMENSIONS:
            if name not in raw:
                raise ValueError(f"QC scores missing {name}")
            value = raw[name]
            if value is None and name not in {"fidelity_score", "quality_score"}:
                scores[name] = None
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"QC score {name} must be numeric or null") from exc
            if not 0 <= numeric <= 1:
                raise ValueError(f"QC score {name} must be between 0 and 1")
            scores[name] = round(numeric, 6)
        return scores

    def _validate_segment_reviews(
        self,
        raw: list[dict[str, Any]],
        known_ids: set[str],
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw:
            reference_id = str(item.get("reference_segment_id") or "")
            if reference_id not in known_ids:
                raise ValueError(f"segment_review references unknown segment {reference_id!r}")
            if reference_id in seen:
                raise ValueError(f"duplicate segment_review for {reference_id}")
            seen.add(reference_id)
            severity = str(item.get("severity") or "")
            if severity not in {"none", "low", "medium", "high"}:
                raise ValueError(f"invalid severity for {reference_id}")
            dimensions = [str(value) for value in item.get("dimensions") or []]
            if any(value not in self.REVIEW_DIMENSIONS for value in dimensions):
                raise ValueError(f"invalid QC dimension for {reference_id}")
            route = str(item.get("recommended_route") or "")
            if route not in {"none", "match", "timeline", "render", "footage"}:
                raise ValueError(f"invalid recommended_route for {reference_id}")
            output.append(
                {
                    "reference_segment_id": reference_id,
                    "severity": severity,
                    "dimensions": dimensions,
                    "issue": str(item.get("issue") or ""),
                    "evidence_notes": str(item.get("evidence_notes") or ""),
                    "recommended_route": route,
                    "recommended_action": str(item.get("recommended_action") or ""),
                }
            )
        return output

    def _validate_revision_actions(
        self,
        raw: list[dict[str, Any]],
        known_ids: set[str],
    ) -> list[dict[str, Any]]:
        output = []
        for item in raw:
            reference_id = item.get("reference_segment_id")
            if reference_id is not None and str(reference_id) not in known_ids:
                raise ValueError(f"revision_action references unknown segment {reference_id!r}")
            route = str(item.get("route") or "")
            if route not in {"match", "timeline", "render"}:
                raise ValueError(f"invalid revision route: {route!r}")
            priority = str(item.get("priority") or "")
            if priority not in {"low", "medium", "high"}:
                raise ValueError(f"invalid revision priority: {priority!r}")
            instruction = str(item.get("instruction") or "").strip()
            goal = str(item.get("measurable_goal") or "").strip()
            if not instruction or not goal:
                raise ValueError("revision actions require instruction and measurable_goal")
            output.append(
                {
                    "reference_segment_id": str(reference_id) if reference_id is not None else None,
                    "route": route,
                    "priority": priority,
                    "instruction": instruction,
                    "measurable_goal": goal,
                }
            )
        return output

    @staticmethod
    def _validate_improvement_requests(
        raw: list[dict[str, Any]],
        known_ids: set[str],
    ) -> list[dict[str, Any]]:
        output = []
        seen: set[str] = set()
        for item in raw:
            reference_id = str(item.get("reference_segment_id") or "")
            if reference_id not in known_ids:
                raise ValueError(f"improvement_request references unknown segment {reference_id!r}")
            if reference_id in seen:
                raise ValueError(f"duplicate improvement_request for {reference_id}")
            seen.add(reference_id)
            reason = str(item.get("reason") or "").strip()
            suggestion = str(item.get("suggested_footage") or "").strip()
            if not reason or not suggestion:
                raise ValueError("improvement requests require reason and suggested_footage")
            output.append(
                {
                    "reference_segment_id": reference_id,
                    "reason": reason,
                    "suggested_footage": suggestion,
                }
            )
        return output

    @staticmethod
    def _validate_final_decision(raw: dict[str, Any]) -> dict[str, Any]:
        if "publishable" not in raw or "requires_rerender" not in raw:
            raise ValueError("final_decision requires publishable and requires_rerender")
        reason = str(raw.get("reason") or "").strip()
        if not reason:
            raise ValueError("final_decision requires a reason")
        return {
            "publishable": bool(raw["publishable"]),
            "requires_rerender": bool(raw["requires_rerender"]),
            "reason": reason,
        }

    def _validate_schema(self, qc: dict[str, Any]) -> None:
        try:
            import jsonschema
        except ImportError:
            return
        schema_path = Path(__file__).resolve().parents[2] / "schemas" / "artifacts" / "replication_qc.schema.json"
        schema = self._read_json(schema_path)
        try:
            jsonschema.validate(instance=qc, schema=schema)
        except jsonschema.ValidationError as exc:
            raise ValueError(f"Replication QC schema validation failed: {exc.message}") from exc

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise OSError(f"JSON file not found: {path}")
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError(f"Expected JSON object: {path}")
        return value
