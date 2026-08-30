"""Render an approved Phase 4 reference-replication plan.

FFmpeg gets a dedicated exact-timeline path because reference replication may
need speed fitting and final-frame holds to guarantee full coverage. Remotion
and HyperFrames continue through the existing VideoCompose governance surface;
this tool never silently swaps runtimes.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
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


class ReferenceVideoRenderer(BaseTool):
    name = "reference_video_renderer"
    version = "0.1.0"
    tier = ToolTier.CORE
    capability = "reference_replication_render"
    provider = "openmontage"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies = ["cmd:ffmpeg", "cmd:ffprobe"]
    install_instructions = "Install FFmpeg/ffprobe. Remotion or HyperFrames additionally require their existing OpenMontage runtime setup."
    agent_skills = ["ffmpeg"]
    capabilities = [
        "render_reference_replication_draft",
        "exact_speed_fit",
        "final_frame_hold",
        "reference_text_overlay",
        "delegate_approved_remotion_or_hyperframes",
    ]
    best_for = ["Phase 4 draft render after explicit runtime approval"]
    not_good_for = ["runtime selection", "footage matching", "semantic QC"]

    input_schema = {
        "type": "object",
        "required": ["render_plan_path"],
        "properties": {
            "render_plan_path": {"type": "string"},
            "output_path": {"type": "string"},
            "crf": {"type": "integer", "minimum": 0, "maximum": 51, "default": 20},
            "preset": {"type": "string", "default": "medium"},
        },
    }
    output_schema = {"type": "object"}
    resource_profile = ResourceProfile(
        cpu_cores=4,
        ram_mb=2048,
        vram_mb=0,
        disk_mb=5000,
        network_required=False,
    )
    idempotency_key_fields = ["render_plan_path"]
    side_effects = ["writes draft video", "writes temporary render files that are cleaned after completion"]
    user_visible_verification = [
        "Play draft.mp4 end to end",
        "Verify duration matches the reference timeline",
        "Review text placement and any warned fallback segments",
    ]

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        plan_path = Path(inputs["render_plan_path"])
        try:
            plan = self._read_json(plan_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return ToolResult(success=False, error=f"Could not load render plan: {exc}")

        if plan.get("runtime_approved") is not True:
            return ToolResult(success=False, error="Render blocked: runtime_approved must be true")

        runtime = str(plan.get("render_runtime") or "").lower()
        if runtime not in {"ffmpeg", "remotion", "hyperframes"}:
            return ToolResult(success=False, error=f"Render blocked: invalid render_runtime {runtime!r}")

        output_path = Path(inputs.get("output_path") or plan.get("output", {}).get("path") or "renders/draft.mp4")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        start = time.time()

        try:
            if runtime == "ffmpeg":
                result = self._render_ffmpeg(
                    plan,
                    output_path=output_path,
                    crf=int(inputs.get("crf", 20)),
                    preset=str(inputs.get("preset", "medium")),
                )
            else:
                result = self._render_via_video_compose(plan, output_path=output_path)
        except Exception as exc:
            return ToolResult(success=False, error=f"Reference render failed: {exc}")

        result.duration_seconds = round(time.time() - start, 2)
        return result

    def _render_via_video_compose(self, plan: dict[str, Any], *, output_path: Path) -> ToolResult:
        hold_count = int((plan.get("metadata") or {}).get("hold_segment_count", 0))
        if hold_count:
            return ToolResult(
                success=False,
                error=(
                    f"Approved runtime {plan['render_runtime']!r} cannot guarantee the {hold_count} final-frame hold(s) "
                    "required by this timeline through the generic composition path. This is a runtime capability blocker, "
                    "not permission to switch to FFmpeg silently. Ask the user to approve a runtime change or improve the footage."
                ),
            )

        from tools.video.video_compose import VideoCompose

        edit_decisions = json.loads(json.dumps(plan["edit_decisions"]))
        temp_ass: Path | None = None
        try:
            if plan.get("text_overlays"):
                temp_ass = output_path.parent / f".{output_path.stem}.reference-text.ass"
                self._write_ass(
                    temp_ass,
                    overlays=plan["text_overlays"],
                    width=int(plan["output"]["width"]),
                    height=int(plan["output"]["height"]),
                )
                edit_decisions["subtitles"] = {
                    "enabled": True,
                    "style": "sentence",
                    "source": str(temp_ass),
                    "font": "Arial",
                    "position": "bottom-center",
                }

            compose_inputs: dict[str, Any] = {
                "operation": "render",
                "edit_decisions": edit_decisions,
                "asset_manifest": plan["asset_manifest"],
                "output_path": str(output_path),
            }
            profile = (plan.get("output") or {}).get("profile")
            if profile:
                compose_inputs["profile"] = profile
            result = VideoCompose().execute(compose_inputs)
            if result.data is None:
                result.data = {}
            result.data["reference_replication"] = True
            result.data["render_runtime"] = plan["render_runtime"]
            result.data["render_plan_warnings"] = plan.get("warnings") or []
            return result
        finally:
            if temp_ass is not None and temp_ass.exists():
                temp_ass.unlink()

    def _render_ffmpeg(
        self,
        plan: dict[str, Any],
        *,
        output_path: Path,
        crf: int,
        preset: str,
    ) -> ToolResult:
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            return ToolResult(success=False, error="FFmpeg/ffprobe are required for the approved FFmpeg render path")

        edit_decisions = plan["edit_decisions"]
        assets = {item["id"]: item for item in plan["asset_manifest"].get("assets", [])}
        cuts = edit_decisions.get("cuts") or []
        if not cuts:
            return ToolResult(success=False, error="Render plan contains no cuts")

        output = plan.get("output") or {}
        width = int(output["width"])
        height = int(output["height"])
        fit = str(output.get("fit") or "cover")
        replication_meta = (edit_decisions.get("metadata") or {}).get("reference_replication") or {}
        holds = replication_meta.get("hold_seconds_by_cut") or {}
        target_durations = replication_meta.get("target_duration_seconds_by_cut") or {}

        temp_dir = output_path.parent / f".{output_path.stem}.replication-tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        segment_paths: list[Path] = []
        concat_list = temp_dir / "concat.txt"
        concat_video = temp_dir / "concat.mp4"
        ass_path = temp_dir / "reference-text.ass"

        try:
            for index, cut in enumerate(cuts):
                asset = assets.get(str(cut.get("source")))
                source_path = Path(asset["path"] if asset else str(cut.get("source") or ""))
                if not source_path.is_file():
                    return ToolResult(success=False, error=f"Cut source not found: {source_path}")

                in_seconds = float(cut["in_seconds"])
                out_seconds = float(cut["out_seconds"])
                source_duration = out_seconds - in_seconds
                speed = max(float(cut.get("speed", 1.0)), 0.1)
                hold_seconds = max(0.0, float(holds.get(cut["id"], 0.0)))
                target_duration = float(
                    target_durations.get(cut["id"], source_duration / speed + hold_seconds)
                )
                if source_duration <= 0 or target_duration <= 0:
                    return ToolResult(success=False, error=f"Invalid timing for cut {cut['id']}")

                segment_path = temp_dir / f"segment_{index:04d}.mp4"
                vf = self._video_filters(
                    width=width,
                    height=height,
                    fit=fit,
                    speed=speed,
                    hold_seconds=hold_seconds,
                )
                has_audio = self._has_audio_stream(source_path)
                cmd = [
                    "ffmpeg", "-y",
                    "-ss", str(in_seconds),
                    "-t", str(source_duration),
                    "-i", str(source_path),
                ]

                if has_audio:
                    af = self._audio_filters(speed=speed, hold_seconds=hold_seconds)
                    cmd.extend(["-filter:v", vf])
                    if af:
                        cmd.extend(["-filter:a", af])
                    cmd.extend([
                        "-map", "0:v:0",
                        "-map", "0:a:0",
                        "-c:v", "libx264",
                        "-crf", str(crf),
                        "-preset", preset,
                        "-pix_fmt", "yuv420p",
                        "-r", "30",
                        "-c:a", "aac",
                        "-b:a", "192k",
                        "-ar", "48000",
                        "-ac", "2",
                        "-t", str(target_duration),
                        str(segment_path),
                    ])
                else:
                    cmd.extend([
                        "-f", "lavfi",
                        "-t", str(target_duration),
                        "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                        "-filter:v", vf,
                        "-map", "0:v:0",
                        "-map", "1:a:0",
                        "-c:v", "libx264",
                        "-crf", str(crf),
                        "-preset", preset,
                        "-pix_fmt", "yuv420p",
                        "-r", "30",
                        "-c:a", "aac",
                        "-b:a", "192k",
                        "-ar", "48000",
                        "-ac", "2",
                        "-t", str(target_duration),
                        str(segment_path),
                    ])

                self.run_command(cmd)
                segment_paths.append(segment_path)

            with concat_list.open("w", encoding="utf-8") as handle:
                for segment_path in segment_paths:
                    safe = str(segment_path.resolve()).replace("\\", "/")
                    handle.write(f"file '{safe}'\n")

            self.run_command([
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(concat_list),
                "-c", "copy",
                str(concat_video),
            ])

            overlays = plan.get("text_overlays") or []
            if overlays:
                self._write_ass(ass_path, overlays=overlays, width=width, height=height)
                ass_filter = self._ass_filter_path(ass_path)
                self.run_command([
                    "ffmpeg", "-y",
                    "-i", str(concat_video),
                    "-vf", f"ass='{ass_filter}'",
                    "-c:v", "libx264",
                    "-crf", str(crf),
                    "-preset", preset,
                    "-pix_fmt", "yuv420p",
                    "-c:a", "copy",
                    str(output_path),
                ])
            else:
                shutil.copy2(concat_video, output_path)

            if not output_path.is_file():
                return ToolResult(success=False, error=f"FFmpeg completed without output: {output_path}")

            actual_duration = self._probe_duration(output_path)
            expected_duration = sum(float(target_durations.get(cut["id"], 0.0)) for cut in cuts)
            if expected_duration <= 0:
                expected_duration = actual_duration
            duration_delta = abs(actual_duration - expected_duration)
            if duration_delta > 0.15:
                return ToolResult(
                    success=False,
                    error=(
                        f"Rendered duration drifted from timeline by {duration_delta:.3f}s "
                        f"(expected {expected_duration:.3f}s, got {actual_duration:.3f}s)"
                    ),
                    data={"output": str(output_path), "duration_seconds": actual_duration},
                    artifacts=[str(output_path)],
                )

            return ToolResult(
                success=True,
                data={
                    "operation": "reference_replication_render",
                    "render_runtime": "ffmpeg",
                    "output": str(output_path),
                    "cut_count": len(cuts),
                    "text_overlay_count": len(overlays),
                    "duration_seconds": round(actual_duration, 6),
                    "expected_duration_seconds": round(expected_duration, 6),
                    "duration_delta_seconds": round(duration_delta, 6),
                    "warnings": plan.get("warnings") or [],
                },
                artifacts=[str(output_path)],
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    @staticmethod
    def _video_filters(*, width: int, height: int, fit: str, speed: float, hold_seconds: float) -> str:
        if fit == "cover":
            parts = [
                f"scale={width}:{height}:force_original_aspect_ratio=increase",
                f"crop={width}:{height}",
            ]
        else:
            parts = [
                f"scale={width}:{height}:force_original_aspect_ratio=decrease",
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black",
            ]
        parts.extend(["setsar=1", "fps=30"])
        if abs(speed - 1.0) > 1e-9:
            parts.append(f"setpts={1.0 / speed}*PTS")
        if hold_seconds > 0:
            parts.append(f"tpad=stop_mode=clone:stop_duration={hold_seconds}")
        return ",".join(parts)

    @staticmethod
    def _audio_filters(*, speed: float, hold_seconds: float) -> str:
        parts: list[str] = []
        if abs(speed - 1.0) > 1e-9:
            parts.append(ReferenceVideoRenderer._build_atempo(speed))
        if hold_seconds > 0:
            parts.append(f"apad=pad_dur={hold_seconds}")
        return ",".join(parts)

    @staticmethod
    def _build_atempo(speed: float) -> str:
        """Build a legal FFmpeg atempo chain for arbitrary positive speed."""
        if speed <= 0:
            raise ValueError("speed must be positive")
        factors: list[float] = []
        remaining = speed
        while remaining > 2.0:
            factors.append(2.0)
            remaining /= 2.0
        while remaining < 0.5:
            factors.append(0.5)
            remaining /= 0.5
        factors.append(remaining)
        return ",".join(f"atempo={factor:.8f}" for factor in factors)

    @staticmethod
    def _has_audio_stream(path: Path) -> bool:
        try:
            output = subprocess.check_output(
                [
                    "ffprobe", "-v", "error",
                    "-select_streams", "a",
                    "-show_entries", "stream=codec_type",
                    "-of", "default=nw=1:nk=1",
                    str(path),
                ],
                text=True,
                stderr=subprocess.STDOUT,
            )
            return "audio" in output
        except Exception:
            return False

    @staticmethod
    def _probe_duration(path: Path) -> float:
        output = subprocess.check_output(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1",
                str(path),
            ],
            text=True,
            stderr=subprocess.STDOUT,
        ).strip()
        return float(output)

    def _write_ass(self, path: Path, *, overlays: list[dict[str, Any]], width: int, height: int) -> None:
        # Reference captions are commonly authored for narrow vertical video.
        # Keep the type proportional to frame height without forcing a 28 px
        # floor that is too large for 576 px-wide outputs.
        font_size = max(24, int(height * 0.024))
        lines = [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {width}",
            f"PlayResY: {height}",
            # Smart word wrapping is required for long reference captions.
            # WrapStyle 2 keeps a dialogue line unwrapped and can clip both
            # sides of narrow vertical renders.
            "WrapStyle: 0",
            "ScaledBorderAndShadow: yes",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            f"Style: Default,Arial,{font_size},&H00FFFFFF,&H000000FF,&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,2,0,2,50,50,80,1",
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]
        for overlay in overlays:
            start = self._ass_time(float(overlay["start_seconds"]))
            end = self._ass_time(float(overlay["end_seconds"]))
            alignment = self._ass_alignment(overlay.get("position"))
            prefix = f"{{\\an{alignment}}}" if alignment else ""
            text = self._escape_ass_text(str(overlay["content"]))
            lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{prefix}{text}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")

    @staticmethod
    def _ass_alignment(position: Any) -> int | None:
        text = str(position or "").lower()
        if not text:
            return None
        vertical = 8 if ("top" in text or "upper" in text) else 2 if "bottom" in text else 5
        if "left" in text:
            return {8: 7, 5: 4, 2: 1}[vertical]
        if "right" in text:
            return {8: 9, 5: 6, 2: 3}[vertical]
        return vertical

    @staticmethod
    def _escape_ass_text(text: str) -> str:
        return (
            text.replace("\\", r"\\")
            .replace("{", r"\{")
            .replace("}", r"\}")
            .replace("\r\n", r"\N")
            .replace("\n", r"\N")
            .replace("\r", r"\N")
        )

    @staticmethod
    def _ass_time(seconds: float) -> str:
        seconds = max(0.0, seconds)
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        remainder = seconds % 60
        return f"{hours}:{minutes:02d}:{remainder:05.2f}"

    @staticmethod
    def _ass_filter_path(path: Path) -> str:
        return str(path.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise OSError(f"JSON file not found: {path}")
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError(f"Expected JSON object: {path}")
        return value
