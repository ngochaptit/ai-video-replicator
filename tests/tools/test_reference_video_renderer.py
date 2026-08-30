from __future__ import annotations

from pathlib import Path

from tools.video.reference_video_renderer import ReferenceVideoRenderer


def test_ffmpeg_video_filter_contains_speed_and_final_frame_hold() -> None:
    value = ReferenceVideoRenderer._video_filters(
        width=1080,
        height=1920,
        fit="cover",
        speed=2.0,
        hold_seconds=0.75,
    )

    assert "scale=1080:1920" in value
    assert "crop=1080:1920" in value
    assert "setpts=0.5*PTS" in value
    assert "tpad=stop_mode=clone:stop_duration=0.75" in value


def test_atempo_builder_supports_speeds_outside_single_filter_range() -> None:
    fast = ReferenceVideoRenderer._build_atempo(8.0)
    slow = ReferenceVideoRenderer._build_atempo(0.1)

    assert fast.count("atempo=") >= 3
    assert slow.count("atempo=") >= 2
    assert all(0.5 <= float(part.split("=")[1]) <= 2.0 for part in fast.split(","))
    assert all(0.5 <= float(part.split("=")[1]) <= 2.0 for part in slow.split(","))


def test_ass_writer_preserves_utf8_and_reference_position(tmp_path: Path) -> None:
    path = tmp_path / "reference-text.ass"
    ReferenceVideoRenderer()._write_ass(
        path,
        overlays=[
            {
                "reference_segment_id": "seg_001",
                "start_seconds": 1.0,
                "end_seconds": 2.5,
                "content": "Đổ sữa vào ly",
                "position": "upper-center",
                "timing_notes": "",
            }
        ],
        width=1080,
        height=1920,
    )

    text = path.read_text(encoding="utf-8-sig")
    assert "Đổ sữa vào ly" in text
    assert r"{\an8}" in text
    assert "WrapStyle: 0" in text
    assert "Style: Default,Arial,46," in text
    assert "0:00:01.00" in text
    assert "0:00:02.50" in text


def test_ass_writer_uses_readable_font_floor_for_narrow_vertical_output(tmp_path: Path) -> None:
    path = tmp_path / "narrow-reference-text.ass"
    ReferenceVideoRenderer()._write_ass(
        path,
        overlays=[
            {
                "reference_segment_id": "seg_001",
                "start_seconds": 0.0,
                "end_seconds": 1.0,
                "content": "A long reference caption that needs smart wrapping",
                "position": "upper-center",
                "timing_notes": "",
            }
        ],
        width=576,
        height=1024,
    )

    text = path.read_text(encoding="utf-8-sig")
    assert "Style: Default,Arial,24," in text
    assert r"{\an8}" in text


def test_non_ffmpeg_runtime_with_required_hold_returns_blocker(tmp_path: Path) -> None:
    renderer = ReferenceVideoRenderer()
    plan = {
        "render_runtime": "remotion",
        "runtime_approved": True,
        "metadata": {"hold_segment_count": 1},
        "edit_decisions": {},
        "asset_manifest": {},
        "text_overlays": [],
        "output": {"width": 1080, "height": 1920},
    }

    result = renderer._render_via_video_compose(plan, output_path=tmp_path / "draft.mp4")

    assert result.success is False
    assert "cannot guarantee" in result.error
    assert "silently" in result.error
