# Reference Replication — Footage Director

## Mission

Convert the user's `footage/` folder into schema-valid `footage_profiles.json` for later reference matching.

This stage does **not** choose final matches and does **not** render video.

The goal is to understand what usable actions actually exist in the user's footage, with measured source ranges and visual evidence.

## Responsibility split

- Deterministic tools own: file discovery, media duration, scene boundaries, sampled frame timestamps, frame paths, measured motion values, source ranges, JSON persistence, invariant validation.
- The multimodal agent owns: action meaning, actor/object/target/interaction, camera interpretation, spatial relationships, segment usefulness, visible quality issues, and confidence.

Do not invent timestamps. Do not install or depend on a local vision/LLM model.

## Required first pass

Run `footage_profile_builder` without semantic enrichment:

```python
footage_profile_builder.execute({
    "footage_dir": "<project>/footage",
    "analysis_depth": "deep",
    "max_keyframes_per_file": 30,
    "max_analysis_window_seconds": 2.0,
    "output_dir": "<project>/analysis/footage"
})
```

The resulting `footage_profiles.json` is an evidence scaffold. Fixed analysis windows are for sampling only and are not final usable action segments.

## Semantic review

Inspect each analyzed clip's evidence frames. Moon v1.2 seeds long footage with deterministic adaptive coverage (target spacing about 4 seconds, bounded per clip) before the handoff. These samples are **measured evidence**, not semantic decisions.

Use a coarse-to-fine review:

1. Scan the seeded coverage across the full clip.
2. Whenever an action/interaction appears to change between coarse samples, call `moon.frames.sample` (or `frame_sampler` outside Moon) on that narrower window at denser timestamps.
3. Use those persisted sampled timestamps as the final measured `source_in` / `source_out` boundaries.
4. Do not collapse a long continuous take into a few analyzer keyframe spans merely because hard scene cuts are absent.

The Moon execution adapter automatically bridges registered `moon.frames.sample` evidence into the builder's `evidence_catalog` on the enrichment pass, so agent-selected sampled boundaries remain valid when canonical `footage_profiles.json` is built.

For every clip decide:

- what the clip visibly contains,
- whether any portion is usable,
- action / actor / object / target / interaction,
- camera POV, shot scale, angle, movement, steadiness,
- actor/object positions, direction of travel, depth, framing relationship,
- motion intensity and visible speed behavior,
- quality issues that matter for matching,
- confidence.

A clip may be marked `usable: false` when nothing in it is useful. Do not manufacture semantic segments just to keep every file represented.

## Action segmentation

Final footage segments should be useful matching units, not arbitrary two-second chunks.

Good boundaries include:

- hand begins/ends a meaningful manipulation,
- object changes target,
- interaction changes,
- camera role changes enough to alter matching usefulness,
- distinct motion action starts or stops,
- measured scene cut.

Gaps are allowed. A source clip may contain unusable dead time between good action segments.

Every final `source_in` and `source_out` must be a measured timestamp from sampled evidence or a measured scene/video boundary.

## Enrichment file

Write a UTF-8 semantic enrichment JSON, for example:

```json
{
  "defaults": {
    "confidence": {"timing": 1.0, "semantic": 0.82, "camera": 0.85, "overall": 0.82}
  },
  "evidence_catalog": [
    {
      "clip_id": "clip_001",
      "path": "<measured-frame-path>",
      "timestamp": 3.4,
      "scene_index": 0
    }
  ],
  "clips": [
    {
      "clip_id": "clip_001",
      "usable": true,
      "content_summary": "First-person hand prepares and pours a drink.",
      "quality_risks": ["brief motion blur during fast reach"],
      "segments": [
        {
          "id": "clip_001_seg_action_01",
          "source_in": 3.4,
          "source_out": 5.1,
          "boundary_basis": ["action_change"],
          "semantic": {
            "actor": "barista hand",
            "action": "pour",
            "object": "liquid bottle",
            "target": "shaker",
            "interaction": "bottle-liquid-shaker",
            "description": "Hand pours liquid into the shaker."
          },
          "camera": {
            "pov": "first-person",
            "shot_scale": "close over-hands",
            "angle": "high downward",
            "movement": "task-following handheld",
            "steadiness": "handheld"
          },
          "spatial": {
            "actor_position": "lower-left foreground",
            "object_position": "left -> center",
            "entry_direction": "left",
            "exit_direction": null,
            "depth": "hand/bottle foreground; shaker mid-foreground",
            "framing_notes": "pour stream remains visible"
          },
          "motion": {
            "intensity": "medium",
            "speed_behavior": "steady continuous pour"
          },
          "quality": {
            "score": 0.88,
            "issues": [],
            "usable_notes": "clear action and target"
          }
        }
      ]
    }
  ],
  "analysis_notes": []
}
```

Then rerun `footage_profile_builder` with `semantic_enrichment_path`.

## Canonical action language

Use short search-friendly verbs where possible:

`reach`, `grab`, `lift`, `place`, `pour`, `stir`, `shake`, `open`, `close`, `transfer`, `scoop`, `measure`, `garnish`, `present`, `walk`, `turn`, `enter`, `exit`.

Do not force the footage to use the reference's wording if the action is visibly different. Profiles describe the user's footage truthfully; matching happens in the next stage.

## Quality rule

A technically imperfect clip can still be usable. Record blur, occlusion, framing mismatch, or weak exposure as quality risks instead of discarding useful footage automatically.

## Completion gate

Before checkpointing this stage, verify:

1. `semantic_enrichment_required == false`.
2. Every usable segment has positive duration and measured boundaries.
3. Every evidence timestamp lies inside its segment source range.
4. No final usable segments overlap inside the same clip.
5. `flow_variance` is `null` when unavailable, never a negative sentinel.
6. There is at least one usable footage segment overall.
7. `footage_profiles.json` validates against `schemas/artifacts/footage_profiles.schema.json`.

Do not proceed to matching until these conditions hold.
