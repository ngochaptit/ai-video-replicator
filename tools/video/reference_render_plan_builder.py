"""Bridge a Phase 3 replication timeline to OpenMontage render contracts.

The builder does not choose a runtime. The caller must provide an explicitly
approved runtime/family/mode. Python only converts the approved timeline into
canonical edit_decisions + asset_manifest data and preserves reference text
cues for the technical renderer.
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


class ReferenceRenderPlanBuilder(BaseTool):
    name = "reference_render_plan_builder"
    version = "0.1.0"
    tier = ToolTier.CORE
    capability = "reference_replication_render_plan"
    provider = "openmontage"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies: list[str] = []
    install_instructions = "No extra dependency beyond the OpenMontage Python environment."
    agent_skills: list[str] = []
    capabilities = [
        "timeline_to_edit_decisions",
        "build_source_asset_manifest",
        "preserve_reference_text_cues",
        "enforce_explicit_runtime_approval",
    ]
    best_for = ["Phase 4 reference replication render planning"]
    not_good_for = ["choosing a runtime", "matching footage", "rendering pixels"]

    input_schema = {
        "type": "object",
        "required": [
            "replication_timeline_path",
            "reference_blueprint_path",
            "output_path",
            "render_runtime",
            "renderer_family",
            "composition_mode",
            "runtime_approved",
        ],
        "properties": {
            "replication_timeline_path": {"type": "string"},
            "reference_blueprint_path": {"type": "string"},
            "output_path": {"type": "string"},
            "output_video_path": {"type": "string"},
            "render_runtime": {
                "type": "string",
                "enum": ["ffmpeg", "remotion", "hyperframes"],
            },
            "renderer_family": {
                "type": "string",
                "enum": [
                    "explainer-data",
                    "explainer-teacher",
                    "cinematic-trailer",
                    "documentary-montage",
                    "product-reveal",
                    "screen-demo",
                    "presenter",
                    "animation-first",
                ],
            },
            "composition_mode": {"type": "string", "enum": ["templated", "atelier"]},
            "runtime_approved": {"type": "boolean", "const": True},
            "output_profile": {"type": "string"},
            "output_width": {"type": "integer", "minimum": 2},
            "output_height": {"type": "integer", "minimum": 2},
            "fit": {"type": "string", "enum": ["pad", "cover"], "default": "cover"},
        },
    }
    output_schema = {
        "type": "object",
        "description": "Replication Render Plan — see schemas/artifacts/replication_render_plan.schema.json",
    }
    resource_profile = ResourceProfile(
        cpu_cores=1,
        ram_mb=256,
        vram_mb=0,
        disk_mb=10,
        network_required=False,
    )
    idempotency_key_fields = [
        "replication_timeline_path",
        "render_runtime",
        "renderer_family",
        "composition_mode",
    ]
    side_effects = ["writes replication_render_plan.json"]
    user_visible_verification = [
        "Confirm render_runtime matches the user's approved choice",
        "Review all source paths and target dimensions before rendering",
        "Review hold and non-cut-transition warnings",
    ]

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        timeline_path = Path(inputs["replication_timeline_path"])
        blueprint_path = Path(inputs["reference_blueprint_path"])
        output_path = Path(inputs["output_path"])
        try:
            timeline = self._read_json(timeline_path)
            blueprint = self._read_json(blueprint_path)
            plan = self.build_plan(
                timeline,
                blueprint,
                replication_timeline_path=str(timeline_path),
                reference_blueprint_path=str(blueprint_path),
                output_video_path=str(
                    inputs.get("output_video_path")
                    or output_path.parent.parent / "renders" / "draft.mp4"
                ),
                render_runtime=str(inputs["render_runtime"]),
                renderer_family=str(inputs["renderer_family"]),
                composition_mode=str(inputs["composition_mode"]),
                runtime_approved=bool(inputs["runtime_approved"]),
                output_profile=inputs.get("output_profile"),
                output_width=inputs.get("output_width"),
                output_height=inputs.get("output_height"),
                fit=str(inputs.get("fit", "cover")),
            )
            self._validate_schemas(plan)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return ToolResult(success=False, error=f"Reference render plan build failed: {exc}")

        return ToolResult(success=True, data=plan, artifacts=[str(output_path)])

    def build_plan(
        self,
        timeline: dict[str, Any],
        blueprint: dict[str, Any],
        *,
        replication_timeline_path: str = "replication_timeline.json",
        reference_blueprint_path: str = "reference_blueprint.json",
        output_video_path: str = "renders/draft.mp4",
        render_runtime: str,
        renderer_family: str,
        composition_mode: str,
        runtime_approved: bool,
        output_profile: str | None = None,
        output_width: int | None = None,
        output_height: int | None = None,
        fit: str = "cover",
    ) -> dict[str, Any]:
        allowed_runtimes = {"ffmpeg", "remotion", "hyperframes"}
        allowed_families = {
            "explainer-data",
            "explainer-teacher",
            "cinematic-trailer",
            "documentary-montage",
            "product-reveal",
            "screen-demo",
            "presenter",
            "animation-first",
        }
        if not runtime_approved:
            raise ValueError("render runtime must be explicitly user-approved before Phase 4 planning")
        if render_runtime not in allowed_runtimes:
            raise ValueError(f"unsupported render_runtime: {render_runtime!r}")
        if renderer_family not in allowed_families:
            raise ValueError(f"unsupported renderer_family: {renderer_family!r}")
        if composition_mode not in {"templated", "atelier"}:
            raise ValueError(f"unsupported composition_mode: {composition_mode!r}")
        if render_runtime == "ffmpeg" and composition_mode == "atelier":
            raise ValueError("FFmpeg reference replication uses composition_mode='templated'; atelier is not applicable")
        if fit not in {"pad", "cover"}:
            raise ValueError("fit must be 'pad' or 'cover'")

        coverage = timeline.get("coverage") or {}
        segments = timeline.get("segments") or []
        if coverage.get("full_coverage") is not True or coverage.get("timeline_contiguous") is not True:
            raise ValueError("Phase 4 requires a full, contiguous Phase 3 timeline")
        if not segments:
            raise ValueError("Phase 4 requires non-empty timeline segments")

        width, height, resolution_warning = self._resolve_dimensions(
            blueprint,
            explicit_width=output_width,
            explicit_height=output_height,
        )

        path_to_asset_id: dict[str, str] = {}
        assets: list[dict[str, Any]] = []
        cuts: list[dict[str, Any]] = []
        hold_seconds_by_cut: dict[str, float] = {}
        target_duration_by_cut: dict[str, float] = {}
        text_overlays: list[dict[str, Any]] = []
        warnings = [str(item) for item in timeline.get("warnings") or []]
        if resolution_warning:
            warnings.append(resolution_warning)

        non_cut_transition_count = 0
        hold_segment_count = 0

        for index, segment in enumerate(segments, start=1):
            source = segment.get("source") or {}
            source_path = str(source.get("path") or "")
            if not source_path:
                raise ValueError(f"timeline segment {segment.get('id')} is missing source.path")

            if source_path not in path_to_asset_id:
                asset_id = f"replication_source_{len(path_to_asset_id) + 1:03d}"
                path_to_asset_id[source_path] = asset_id
                assets.append(
                    {
                        "id": asset_id,
                        "type": "video",
                        "path": source_path,
                        "source_tool": "reference_match_validator",
                        "scene_id": str(segment.get("id") or f"timeline_{index:03d}"),
                        "generation_summary": "User footage selected by Phase 2 reference matching.",
                    }
                )
            asset_id = path_to_asset_id[source_path]

            cut_id = f"replication_cut_{index:03d}"
            timing_fit = segment.get("timing_fit") or {}
            speed = float(timing_fit.get("speed", 1.0))
            if speed < 0.1:
                raise ValueError(f"{cut_id}: speed below supported floor 0.1x")
            hold_seconds = max(0.0, float(timing_fit.get("hold_seconds", 0.0)))
            if hold_seconds > 0:
                hold_segment_count += 1
                hold_seconds_by_cut[cut_id] = round(hold_seconds, 6)

            cues = segment.get("reference_cues") or {}
            transition_in = self._nullable_string(cues.get("transition_in"))
            transition_out = self._nullable_string(cues.get("transition_out"))
            for transition in (transition_in, transition_out):
                if transition and transition.lower() not in {"cut", "none", "hard_cut", "hard cut"}:
                    non_cut_transition_count += 1

            cut = {
                "id": cut_id,
                "source": asset_id,
                "in_seconds": float(source["in_seconds"]),
                "out_seconds": float(source["out_seconds"]),
                "speed": round(speed, 6),
                "layer": "primary",
                "reason": (
                    f"Reference {segment.get('reference_segment_id')}; "
                    f"match={segment.get('match', {}).get('class', 'unknown')}; "
                    f"score={float(segment.get('match', {}).get('overall_score', 0.0)):.3f}"
                ),
            }
            if transition_in:
                cut["transition_in"] = transition_in
            if transition_out:
                cut["transition_out"] = transition_out
            cuts.append(cut)
            target_duration_by_cut[cut_id] = round(float(segment["target_duration_seconds"]), 6)

            text = cues.get("text") or {}
            content = str(text.get("content") or "").strip()
            if content:
                text_overlays.append(
                    {
                        "reference_segment_id": str(segment["reference_segment_id"]),
                        "start_seconds": round(float(segment["timeline_start"]), 6),
                        "end_seconds": round(float(segment["timeline_end"]), 6),
                        "content": content,
                        "position": self._nullable_string(text.get("position")),
                        "timing_notes": str(text.get("timing_notes") or ""),
                    }
                )
                if str(text.get("timing_notes") or "").strip():
                    warnings.append(
                        f"{segment['reference_segment_id']}: text has free-form timing notes; Phase 4 can only guarantee segment-level overlay timing."
                    )

        if non_cut_transition_count:
            warnings.append(
                "Reference contains non-cut transition labels but Phase 1 has no measured transition duration; "
                "the draft preserves cut timing and carries the labels for QC rather than inventing transition lengths."
            )
        if hold_segment_count and render_runtime != "ffmpeg":
            warnings.append(
                f"{hold_segment_count} segment(s) require final-frame hold. The generic {render_runtime} path cannot guarantee hold semantics; "
                "Phase 4 renderer will surface a blocker instead of silently swapping runtimes."
            )

        edit_decisions = {
            "version": "1.0",
            "cuts": cuts,
            "renderer_family": renderer_family,
            "render_runtime": render_runtime,
            "composition_mode": composition_mode,
            "metadata": {
                "proposal_render_runtime": render_runtime,
                "compose_target": {"width": width, "height": height, "fit": fit},
                "reference_replication": {
                    "timeline_path": replication_timeline_path,
                    "hold_seconds_by_cut": hold_seconds_by_cut,
                    "target_duration_seconds_by_cut": target_duration_by_cut,
                    "text_overlay_count": len(text_overlays),
                },
            },
        }

        asset_manifest = {
            "version": "1.0",
            "assets": assets,
            "total_cost_usd": 0.0,
            "metadata": {"source": "reference-replication-phase4", "generated_assets": False},
        }

        return {
            "version": "1.0",
            "render_runtime": render_runtime,
            "renderer_family": renderer_family,
            "composition_mode": composition_mode,
            "runtime_approved": True,
            "edit_decisions": edit_decisions,
            "asset_manifest": asset_manifest,
            "text_overlays": text_overlays,
            "output": {
                "path": output_video_path,
                "profile": output_profile,
                "width": width,
                "height": height,
                "fit": fit,
            },
            "warnings": list(dict.fromkeys(warnings)),
            "metadata": {
                "generated_by": self.name,
                "replication_timeline_path": replication_timeline_path,
                "reference_blueprint_path": reference_blueprint_path,
                "hold_segment_count": hold_segment_count,
                "non_cut_transition_count": non_cut_transition_count,
                "notes": [
                    "Runtime/family/mode are caller-supplied approved decisions; this tool never chooses them.",
                    "Text overlays are preserved separately because generic edit_decisions has no canonical text-overlay cut field.",
                    "Phase 4 draft uses user footage only; no generative video asset is introduced by this builder.",
                ],
            },
        }

    def _resolve_dimensions(
        self,
        blueprint: dict[str, Any],
        *,
        explicit_width: int | None,
        explicit_height: int | None,
    ) -> tuple[int, int, str | None]:
        if (explicit_width is None) != (explicit_height is None):
            raise ValueError("output_width and output_height must be supplied together")
        warning = None
        if explicit_width is not None and explicit_height is not None:
            width, height = int(explicit_width), int(explicit_height)
        else:
            source = blueprint.get("source") or {}
            parsed = self._parse_resolution(source.get("resolution"))
            if parsed:
                width, height = parsed
            else:
                orientation = str(source.get("orientation") or "unknown")
                if orientation == "vertical":
                    width, height = 1080, 1920
                elif orientation == "square":
                    width, height = 1080, 1080
                else:
                    width, height = 1920, 1080
                warning = (
                    f"Reference resolution was unavailable; using {width}x{height} from orientation={orientation!r}."
                )
        if width < 2 or height < 2:
            raise ValueError("output dimensions must be positive")
        even_width = width if width % 2 == 0 else width - 1
        even_height = height if height % 2 == 0 else height - 1
        if (even_width, even_height) != (width, height):
            extra = f"Adjusted odd reference dimensions {width}x{height} to codec-safe {even_width}x{even_height}."
            warning = f"{warning} {extra}".strip() if warning else extra
        return even_width, even_height, warning

    @staticmethod
    def _parse_resolution(value: Any) -> tuple[int, int] | None:
        if isinstance(value, dict):
            try:
                return int(value["width"]), int(value["height"])
            except (KeyError, ValueError, TypeError):
                return None
        text = str(value or "").lower().replace(" ", "")
        if "x" not in text:
            return None
        left, _, right = text.partition("x")
        try:
            return int(left), int(right)
        except ValueError:
            return None

    def _validate_schemas(self, plan: dict[str, Any]) -> None:
        try:
            import jsonschema
        except ImportError:
            return
        root = Path(__file__).resolve().parents[2]
        validations = [
            (plan, root / "schemas" / "artifacts" / "replication_render_plan.schema.json"),
            (plan["edit_decisions"], root / "schemas" / "artifacts" / "edit_decisions.schema.json"),
            (plan["asset_manifest"], root / "schemas" / "artifacts" / "asset_manifest.schema.json"),
        ]
        for instance, schema_path in validations:
            schema = self._read_json(schema_path)
            try:
                jsonschema.validate(instance=instance, schema=schema)
            except jsonschema.ValidationError as exc:
                raise ValueError(f"Schema validation failed for {schema_path.name}: {exc.message}") from exc

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise OSError(f"JSON file not found: {path}")
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError(f"Expected JSON object: {path}")
        return value

    @staticmethod
    def _nullable_string(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None
