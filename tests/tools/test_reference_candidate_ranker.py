from __future__ import annotations

from tools.analysis.reference_candidate_ranker import ReferenceCandidateRanker


def test_every_reference_segment_gets_candidates_even_for_weak_match() -> None:
    ranker = ReferenceCandidateRanker()
    result = ranker.rank_candidates(_blueprint(), _profiles(), top_k=2)

    assert result["coverage"]["every_reference_has_candidates"] is True
    assert result["coverage"]["reference_segment_count"] == 2
    assert len(result["candidate_sets"]) == 2
    assert all(candidate_set["candidates"] for candidate_set in result["candidate_sets"])


def test_action_aligned_candidate_ranks_above_duration_only_fallback() -> None:
    ranker = ReferenceCandidateRanker()
    result = ranker.rank_candidates(_blueprint(), _profiles(), top_k=2)

    first_candidates = result["candidate_sets"][0]["candidates"]
    assert first_candidates[0]["footage_segment_id"] == "clip_001_pour"
    assert first_candidates[-1]["footage_segment_id"] == "clip_002_walk"


def test_duration_similarity_is_ratio_bounded() -> None:
    ranker = ReferenceCandidateRanker()

    assert ranker._duration_similarity(2.0, 2.0) == 1.0
    assert ranker._duration_similarity(2.0, 4.0) == 0.5
    assert ranker._duration_similarity(0.0, 4.0) == 0.0


def _blueprint() -> dict:
    return {
        "version": "1.0",
        "source": {"path": "reference.mp4", "duration_seconds": 4.0, "resolution": "1080x1920", "fps": 30.0, "orientation": "vertical"},
        "segments": [
            _reference_segment("seg_001", "pour", "bottle-liquid-cup", 2.0),
            _reference_segment("seg_002", "shake", "hands-shaker", 2.0),
        ],
        "choreography": {"summary": "pour then shake", "action_order": ["pour", "shake"], "critical_constraints": [], "soft_constraints": []},
        "analysis_meta": {"generated_by": "reference_blueprint_builder", "semantic_enrichment_required": False, "source_analysis_path": "brief.json", "notes": []},
    }


def _profiles() -> dict:
    return {
        "version": "1.0",
        "source_dir": "footage",
        "clips": [
            {
                "clip_id": "clip_001",
                "path": "footage/pour.mp4",
                "duration_seconds": 5.0,
                "resolution": "1080x1920",
                "fps": 30.0,
                "orientation": "vertical",
                "usable": True,
                "content_summary": "pouring",
                "quality_risks": [],
                "segments": [_footage_segment("clip_001_pour", "pour", "bottle-liquid-cup", 0.0, 2.1)],
            },
            {
                "clip_id": "clip_002",
                "path": "footage/walk.mp4",
                "duration_seconds": 4.0,
                "resolution": "1080x1920",
                "fps": 30.0,
                "orientation": "vertical",
                "usable": True,
                "content_summary": "walking",
                "quality_risks": [],
                "segments": [_footage_segment("clip_002_walk", "walk", "person-floor", 0.0, 2.0)],
            },
        ],
        "analysis_meta": {"generated_by": "footage_profile_builder", "semantic_enrichment_required": False, "file_count": 2, "usable_segment_count": 2, "notes": []},
    }


def _reference_segment(segment_id: str, action: str, interaction: str, duration: float) -> dict:
    return {
        "id": segment_id,
        "start_seconds": 0.0,
        "end_seconds": duration,
        "duration_seconds": duration,
        "boundary_basis": ["action_change"],
        "semantic": {"actor": "hand", "action": action, "object": "cup", "target": "center", "interaction": interaction, "description": f"hand {action}"},
        "camera": {"pov": "first-person", "shot_scale": "close", "angle": "high", "movement": "follow", "steadiness": "handheld", "playback_speed": "real-time"},
        "spatial": {"actor_position": "foreground", "object_position": "center", "entry_direction": "left", "exit_direction": None, "depth": "foreground", "framing_notes": "centered"},
        "motion": {"motion_type": None, "intensity": "medium", "flow_variance": None, "speed_behavior": "steady"},
        "edit": {"transition_in": None, "transition_out": None, "segment_role": "action"},
        "text": {"content": None, "position": None, "timing_notes": ""},
        "audio": {"speech": None, "sound_cue": None, "beat_cue": None, "energy_notes": ""},
        "evidence": {"scene_index": 0, "frame_paths": ["ref.jpg"], "frame_timestamps": [0.5]},
        "confidence": {"timing": 1.0, "semantic": 0.9, "camera": 0.9, "overall": 0.9},
    }


def _footage_segment(segment_id: str, action: str, interaction: str, start: float, end: float) -> dict:
    return {
        "id": segment_id,
        "source_in": start,
        "source_out": end,
        "duration_seconds": end - start,
        "boundary_basis": ["action_change"],
        "semantic": {"actor": "hand", "action": action, "object": "cup", "target": "center", "interaction": interaction, "description": f"hand {action}"},
        "camera": {"pov": "first-person", "shot_scale": "close", "angle": "high", "movement": "follow", "steadiness": "handheld"},
        "spatial": {"actor_position": "foreground", "object_position": "center", "entry_direction": "left", "exit_direction": None, "depth": "foreground", "framing_notes": "centered"},
        "motion": {"motion_type": None, "intensity": "medium", "flow_variance": None, "speed_behavior": "steady"},
        "quality": {"score": 0.9, "issues": [], "usable_notes": "clear"},
        "evidence": {"scene_index": 0, "frame_paths": ["footage.jpg"], "frame_timestamps": [start]},
        "confidence": {"timing": 1.0, "semantic": 0.9, "camera": 0.9, "overall": 0.9},
    }
