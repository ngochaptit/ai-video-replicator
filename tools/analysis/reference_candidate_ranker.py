"""Deterministic pre-ranking of footage candidates for reference segments.

This tool intentionally does not make the final editorial decision. It narrows a
potentially large footage library into evidence-grounded candidates so the agent
can compare semantics and choose a primary or fallback match.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
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


class ReferenceCandidateRanker(BaseTool):
    name = "reference_candidate_ranker"
    version = "0.1.0"
    tier = ToolTier.ANALYZE
    capability = "reference_replication_candidate_retrieval"
    provider = "openmontage"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies: list[str] = []
    install_instructions = "No additional dependencies. Uses enriched JSON artifacts only."
    capabilities = [
        "rank_reference_footage_candidates",
        "score_duration_camera_motion_semantic_overlap",
        "always_return_fallback_candidates",
    ]
    best_for = ["reducing Phase 2 footage search before agent matching"]
    not_good_for = ["final creative match selection", "rendering", "vision inference"]

    input_schema = {
        "type": "object",
        "required": ["reference_blueprint_path", "footage_profiles_path"],
        "properties": {
            "reference_blueprint_path": {"type": "string"},
            "footage_profiles_path": {"type": "string"},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            "output_path": {"type": "string"},
        },
    }
    output_schema = {"type": "object", "description": "Candidate rankings for each reference segment"}
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=256, vram_mb=0, disk_mb=25, network_required=False)
    side_effects = ["writes candidate_rankings.json when output_path is supplied"]

    WEIGHTS = {
        "action": 0.30,
        "interaction": 0.20,
        "camera": 0.15,
        "spatial": 0.10,
        "motion": 0.10,
        "duration": 0.15,
    }

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        try:
            blueprint = self._read_json(Path(inputs["reference_blueprint_path"]))
            profiles = self._read_json(Path(inputs["footage_profiles_path"]))
            top_k = int(inputs.get("top_k", 10))
            rankings = self.rank_candidates(blueprint, profiles, top_k=top_k)
            rankings["reference_blueprint_path"] = str(inputs["reference_blueprint_path"])
            rankings["footage_profiles_path"] = str(inputs["footage_profiles_path"])
            artifacts: list[str] = []
            if inputs.get("output_path"):
                out_path = Path(inputs["output_path"])
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(
                    json.dumps(rankings, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                artifacts.append(str(out_path))
            return ToolResult(success=True, data=rankings, artifacts=artifacts)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return ToolResult(success=False, error=f"Candidate ranking failed: {exc}")

    def rank_candidates(
        self,
        blueprint: dict[str, Any],
        profiles: dict[str, Any],
        top_k: int = 10,
    ) -> dict[str, Any]:
        if blueprint.get("analysis_meta", {}).get("semantic_enrichment_required", True):
            raise ValueError("reference blueprint must be semantically enriched before matching")
        if profiles.get("analysis_meta", {}).get("semantic_enrichment_required", True):
            raise ValueError("footage profiles must be semantically enriched before matching")

        footage_segments = self._flatten_footage_segments(profiles)
        if not footage_segments:
            raise ValueError("footage profiles contain zero usable segments")

        reference_segments = blueprint.get("segments") or []
        if not reference_segments:
            raise ValueError("reference blueprint contains zero segments")

        candidate_sets: list[dict[str, Any]] = []
        for reference in reference_segments:
            scored = [self._score_pair(reference, candidate) for candidate in footage_segments]
            scored.sort(key=lambda item: (-item["pre_score"], item["footage_segment_id"]))
            selected = scored[: min(top_k, len(scored))]
            if not selected:
                raise ValueError(f"no fallback candidate available for {reference.get('id')}")
            candidate_sets.append(
                {
                    "reference_segment_id": reference["id"],
                    "reference_duration_seconds": float(reference.get("duration_seconds") or 0.0),
                    "candidates": selected,
                }
            )

        return {
            "version": "1.0",
            "candidate_sets": candidate_sets,
            "coverage": {
                "reference_segment_count": len(reference_segments),
                "candidate_set_count": len(candidate_sets),
                "every_reference_has_candidates": len(candidate_sets) == len(reference_segments),
            },
            "notes": [
                "Pre-scores are deterministic retrieval hints, not final editorial judgments.",
                "Even low-scoring candidates remain eligible as fallbacks so the final timeline can stay complete.",
                "The agent must inspect evidence and choose the final match class and rationale.",
            ],
        }

    def _flatten_footage_segments(self, profiles: dict[str, Any]) -> list[dict[str, Any]]:
        flattened: list[dict[str, Any]] = []
        for clip in profiles.get("clips") or []:
            if not clip.get("usable", True):
                continue
            for segment in clip.get("segments") or []:
                flattened.append(
                    {
                        **segment,
                        "clip_id": clip["clip_id"],
                        "source_path": clip["path"],
                        "source_orientation": clip.get("orientation", "unknown"),
                    }
                )
        return flattened

    def _score_pair(self, reference: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
        component_scores = {
            "action": self._token_similarity(
                self._join(reference.get("semantic", {}), ["action", "description"]),
                self._join(candidate.get("semantic", {}), ["action", "description"]),
            ),
            "interaction": self._token_similarity(
                self._join(reference.get("semantic", {}), ["interaction", "object", "target", "actor"]),
                self._join(candidate.get("semantic", {}), ["interaction", "object", "target", "actor"]),
            ),
            "camera": self._token_similarity(
                self._join(reference.get("camera", {}), ["pov", "shot_scale", "angle", "movement", "steadiness"]),
                self._join(candidate.get("camera", {}), ["pov", "shot_scale", "angle", "movement", "steadiness"]),
            ),
            "spatial": self._token_similarity(
                self._join(reference.get("spatial", {}), ["actor_position", "object_position", "entry_direction", "exit_direction", "depth", "framing_notes"]),
                self._join(candidate.get("spatial", {}), ["actor_position", "object_position", "entry_direction", "exit_direction", "depth", "framing_notes"]),
            ),
            "motion": self._motion_similarity(reference.get("motion") or {}, candidate.get("motion") or {}),
            "duration": self._duration_similarity(
                float(reference.get("duration_seconds") or 0.0),
                float(candidate.get("duration_seconds") or 0.0),
            ),
        }
        pre_score = self._weighted_score(component_scores)
        if pre_score >= 0.75:
            retrieval_tier = "strong"
        elif pre_score >= 0.50:
            retrieval_tier = "plausible"
        else:
            retrieval_tier = "fallback"
        return {
            "footage_segment_id": candidate["id"],
            "clip_id": candidate["clip_id"],
            "source_path": candidate["source_path"],
            "source_in": candidate["source_in"],
            "source_out": candidate["source_out"],
            "duration_seconds": candidate["duration_seconds"],
            "pre_score": pre_score,
            "retrieval_tier": retrieval_tier,
            "component_scores": component_scores,
            "evidence": candidate.get("evidence") or {},
            "quality": candidate.get("quality") or {},
        }

    def _weighted_score(self, scores: dict[str, float | None]) -> float:
        weighted = 0.0
        used_weight = 0.0
        for key, weight in self.WEIGHTS.items():
            score = scores.get(key)
            if score is None:
                continue
            weighted += float(score) * weight
            used_weight += weight
        return round(weighted / used_weight, 6) if used_weight > 0 else 0.0

    @staticmethod
    def _join(section: dict[str, Any], fields: list[str]) -> str:
        return " ".join(str(section.get(field) or "") for field in fields).strip()

    @staticmethod
    def _tokens(text: str) -> set[str]:
        normalized = text.lower().replace("_", " ").replace("-", " ")
        return {token for token in re.findall(r"\w+", normalized, flags=re.UNICODE) if len(token) > 1}

    @classmethod
    def _token_similarity(cls, left: str, right: str) -> float | None:
        left_tokens = cls._tokens(left)
        right_tokens = cls._tokens(right)
        if not left_tokens and not right_tokens:
            return None
        if not left_tokens or not right_tokens:
            return 0.0
        union = left_tokens | right_tokens
        return round(len(left_tokens & right_tokens) / len(union), 6) if union else None

    @classmethod
    def _motion_similarity(cls, left: dict[str, Any], right: dict[str, Any]) -> float | None:
        parts: list[float] = []
        left_type = str(left.get("motion_type") or "").strip()
        right_type = str(right.get("motion_type") or "").strip()
        type_similarity = cls._token_similarity(left_type, right_type)
        if type_similarity is not None:
            parts.append(type_similarity)

        levels = {"low": 0, "medium": 1, "high": 2}
        left_level = levels.get(str(left.get("intensity") or "").lower())
        right_level = levels.get(str(right.get("intensity") or "").lower())
        if left_level is not None and right_level is not None:
            parts.append(max(0.0, 1.0 - abs(left_level - right_level) / 2.0))

        speed_similarity = cls._token_similarity(
            str(left.get("speed_behavior") or ""),
            str(right.get("speed_behavior") or ""),
        )
        if speed_similarity is not None:
            parts.append(speed_similarity)
        return round(sum(parts) / len(parts), 6) if parts else None

    @staticmethod
    def _duration_similarity(reference_duration: float, candidate_duration: float) -> float:
        if reference_duration <= 0 or candidate_duration <= 0:
            return 0.0
        return round(min(reference_duration, candidate_duration) / max(reference_duration, candidate_duration), 6)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8-sig"))
