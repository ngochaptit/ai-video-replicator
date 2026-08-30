"""Validate and canonicalize the agent's Phase 2 reference-to-footage choices.

The agent remains the editor/director and chooses each match. This tool performs
only deterministic contract work: every reference segment must be covered,
selected footage ids must exist, source ranges are resolved from Footage Profiles,
and fallback choices remain explicit instead of becoming empty timeline slots.
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


class ReferenceMatchValidator(BaseTool):
    name = "reference_match_validator"
    version = "0.1.0"
    tier = ToolTier.ANALYZE
    capability = "reference_replication_match_validation"
    provider = "openmontage"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies: list[str] = []
    install_instructions = "No additional dependencies."
    capabilities = [
        "validate_full_reference_coverage",
        "resolve_selected_source_ranges",
        "allow_explicit_fallback_matches",
        "reject_empty_timeline_slots",
    ]
    best_for = ["finalizing Phase 2 matching.json after agent selection"]
    not_good_for = ["choosing footage", "creative ranking", "rendering"]

    input_schema = {
        "type": "object",
        "required": ["reference_blueprint_path", "footage_profiles_path", "proposal_path"],
        "properties": {
            "reference_blueprint_path": {"type": "string"},
            "footage_profiles_path": {"type": "string"},
            "proposal_path": {"type": "string"},
            "output_path": {"type": "string", "default": "matching.json"},
        },
    }
    output_schema = {
        "type": "object",
        "description": "Reference Matching Plan — see schemas/artifacts/reference_matching.schema.json",
    }
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=128, vram_mb=0, disk_mb=10, network_required=False)
    side_effects = ["writes canonical matching.json"]

    SCORE_KEYS = ["action", "interaction", "camera", "spatial", "motion", "duration", "overall"]
    MATCH_CLASSES = {"good", "acceptable", "fallback"}

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        try:
            blueprint = self._read_json(Path(inputs["reference_blueprint_path"]))
            profiles = self._read_json(Path(inputs["footage_profiles_path"]))
            proposal = self._read_json(Path(inputs["proposal_path"]))
            canonical = self.build_canonical_plan(
                blueprint=blueprint,
                profiles=profiles,
                proposal=proposal,
                reference_blueprint_path=str(inputs["reference_blueprint_path"]),
                footage_profiles_path=str(inputs["footage_profiles_path"]),
            )
            out_path = Path(inputs.get("output_path") or "matching.json")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(
                json.dumps(canonical, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            return ToolResult(success=True, data=canonical, artifacts=[str(out_path)])
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return ToolResult(success=False, error=f"Reference match validation failed: {exc}")

    def build_canonical_plan(
        self,
        blueprint: dict[str, Any],
        profiles: dict[str, Any],
        proposal: dict[str, Any],
        reference_blueprint_path: str = "reference_blueprint.json",
        footage_profiles_path: str = "footage_profiles.json",
    ) -> dict[str, Any]:
        if blueprint.get("analysis_meta", {}).get("semantic_enrichment_required", True):
            raise ValueError("reference blueprint must be semantically enriched")
        if profiles.get("analysis_meta", {}).get("semantic_enrichment_required", True):
            raise ValueError("footage profiles must be semantically enriched")

        reference_ids = [str(segment["id"]) for segment in blueprint.get("segments") or []]
        if not reference_ids:
            raise ValueError("reference blueprint contains zero segments")
        footage_by_id = self._footage_segment_map(profiles)
        if not footage_by_id:
            raise ValueError("footage profiles contain zero usable segments")

        proposal_matches = proposal.get("matches")
        if not isinstance(proposal_matches, list):
            raise ValueError("proposal must contain matches[]")

        match_by_reference: dict[str, dict[str, Any]] = {}
        for match in proposal_matches:
            reference_id = str(match.get("reference_segment_id") or "")
            if reference_id not in reference_ids:
                raise ValueError(f"proposal references unknown reference segment: {reference_id}")
            if reference_id in match_by_reference:
                raise ValueError(f"duplicate proposal match for reference segment: {reference_id}")
            match_by_reference[reference_id] = match

        missing = [reference_id for reference_id in reference_ids if reference_id not in match_by_reference]
        if missing:
            raise ValueError(f"coverage incomplete; missing reference segments: {missing}")
        if len(match_by_reference) != len(reference_ids):
            raise ValueError("proposal match count must equal reference segment count")

        improvement_requests = proposal.get("improvement_requests") or []
        improvement_by_reference: dict[str, list[dict[str, Any]]] = {}
        for item in improvement_requests:
            reference_id = str(item.get("reference_segment_id") or "")
            if reference_id not in reference_ids:
                raise ValueError(f"improvement request references unknown segment: {reference_id}")
            improvement_by_reference.setdefault(reference_id, []).append(
                {
                    "reference_segment_id": reference_id,
                    "reason": str(item.get("reason") or ""),
                    "suggested_footage": str(item.get("suggested_footage") or ""),
                }
            )

        canonical_matches: list[dict[str, Any]] = []
        fallback_count = 0
        selected_ids: list[str] = []
        for reference_id in reference_ids:
            proposed = match_by_reference[reference_id]
            footage_segment_id = str(proposed.get("footage_segment_id") or "")
            if footage_segment_id not in footage_by_id:
                raise ValueError(
                    f"{reference_id} selects unknown or unusable footage segment: {footage_segment_id}"
                )
            selected = footage_by_id[footage_segment_id]
            match_class = str(proposed.get("match_class") or "")
            if match_class not in self.MATCH_CLASSES:
                raise ValueError(f"{reference_id} has invalid match_class: {match_class}")
            if match_class == "fallback":
                fallback_count += 1
                if reference_id not in improvement_by_reference:
                    raise ValueError(
                        f"fallback {reference_id} requires an improvement_request describing better footage"
                    )

            scores = self._validate_scores(reference_id, proposed.get("scores"))
            rationale = str(proposed.get("rationale") or "").strip()
            if not rationale:
                raise ValueError(f"{reference_id} requires a non-empty rationale")

            alternatives = [str(item) for item in proposed.get("alternatives") or []]
            unknown_alternatives = [item for item in alternatives if item not in footage_by_id]
            if unknown_alternatives:
                raise ValueError(f"{reference_id} has unknown alternatives: {unknown_alternatives}")

            canonical_matches.append(
                {
                    "reference_segment_id": reference_id,
                    "selected": {
                        "footage_segment_id": footage_segment_id,
                        "source_path": selected["source_path"],
                        "source_in": selected["source_in"],
                        "source_out": selected["source_out"],
                        "duration_seconds": selected["duration_seconds"],
                    },
                    "match_class": match_class,
                    "scores": scores,
                    "rationale": rationale,
                    "tradeoffs": [str(item) for item in proposed.get("tradeoffs") or []],
                    "alternatives": alternatives,
                }
            )
            selected_ids.append(footage_segment_id)

        repeated = sorted({segment_id for segment_id in selected_ids if selected_ids.count(segment_id) > 1})
        notes = [str(item) for item in proposal.get("notes") or []]
        notes.append("Coverage > perfect matching: every reference segment has a selected footage segment.")
        if repeated:
            notes.append(
                "Footage reuse was accepted to preserve full coverage: " + ", ".join(repeated)
            )

        flattened_improvements = [
            item
            for reference_id in reference_ids
            for item in improvement_by_reference.get(reference_id, [])
        ]
        return {
            "version": "1.0",
            "reference_blueprint_path": reference_blueprint_path,
            "footage_profiles_path": footage_profiles_path,
            "matches": canonical_matches,
            "coverage": {
                "reference_segment_count": len(reference_ids),
                "matched_segment_count": len(canonical_matches),
                "full_coverage": True,
                "fallback_count": fallback_count,
            },
            "improvement_requests": flattened_improvements,
            "notes": notes,
        }

    def _footage_segment_map(self, profiles: dict[str, Any]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for clip in profiles.get("clips") or []:
            if not clip.get("usable", True):
                continue
            for segment in clip.get("segments") or []:
                segment_id = str(segment["id"])
                if segment_id in result:
                    raise ValueError(f"duplicate footage segment id: {segment_id}")
                result[segment_id] = {
                    **segment,
                    "source_path": clip["path"],
                }
        return result

    def _validate_scores(self, reference_id: str, raw_scores: Any) -> dict[str, float | None]:
        if not isinstance(raw_scores, dict):
            raise ValueError(f"{reference_id} requires scores object")
        missing = [key for key in self.SCORE_KEYS if key not in raw_scores]
        if missing:
            raise ValueError(f"{reference_id} scores missing keys: {missing}")
        result: dict[str, float | None] = {}
        for key in self.SCORE_KEYS:
            value = raw_scores.get(key)
            if value is None and key != "overall":
                result[key] = None
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{reference_id} score {key} must be numeric or null")
            numeric = float(value)
            if numeric < 0 or numeric > 1:
                raise ValueError(f"{reference_id} score {key} outside [0,1]: {numeric}")
            result[key] = numeric
        return result

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8-sig"))
