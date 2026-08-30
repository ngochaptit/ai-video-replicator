# Reference Replication — Timeline Director

## Mission

Turn the approved `reference_blueprint` + `reference_matching` artifacts into a complete, runtime-agnostic `replication_timeline` for Phase 4.

Phase 3 is where the edit becomes a concrete sequence: every Reference Blueprint segment receives an exact timeline position and a concrete source file/range. **Do not render in this stage. Do not choose a render runtime in this stage.**

The governing principle remains:

> Coverage > perfect matching.

A weak Phase 2 fallback stays in the timeline so the video remains complete. Surface the weakness; do not turn it back into an empty slot.

## Architecture boundary

The agent is the editor/director. Python is the deterministic timeline calculator and validator.

### Python owns

- validating full Phase 1 timing coverage,
- validating full Phase 2 matching coverage,
- ordering segments by measured reference timestamps,
- resolving selected footage source paths and source ranges,
- computing mechanical source-to-reference timing fit,
- preserving reference transition/text/audio/camera/spatial cues as metadata,
- flagging fallback, extreme-speed, and short-source hold risks,
- writing and schema-validating `replication_timeline.json`.

### The agent owns

- deciding whether Phase 2 choices are editorially acceptable,
- deciding whether a timing warning warrants sending the match back for revision,
- interpreting transition/camera/spatial/text/audio cues for the later render,
- explaining fidelity risks to the user,
- approving the timeline for Phase 4.

Do not move creative judgment into `reference_timeline_builder`.

## Required inputs

1. `reference_blueprint`
2. `reference_matching`

The Phase 2 artifact MUST already have `coverage.full_coverage = true`.

## Required tool

`reference_timeline_builder`

This is a deterministic local tool. It has no local AI/CLIP/VLM dependency and it does not read video pixels.

## Procedure

### 1. Review Phase 2 before timeline construction

Confirm:

- every Reference Blueprint segment appears exactly once in the matching artifact,
- every match resolves to a concrete `source_path`, `source_in`, and `source_out`,
- fallbacks are explicit rather than hidden,
- the reference segment order is still the choreography order from Phase 1.

If matching coverage is incomplete, send the work back to the `match` stage. Do not manufacture a timeline gap or fake source clip.

### 2. Run the timeline builder

Call `reference_timeline_builder` with explicit project artifact paths.

Canonical output path:

`projects/<project-id>/artifacts/replication_timeline.json`

The tool MUST preserve reference timestamps as the final timeline timestamps.

For each segment it computes:

- timeline start/end from the Reference Blueprint,
- selected source file/range from Phase 2,
- target duration from the reference segment,
- mechanical speed fit needed to make the source range occupy the target duration,
- an optional frame-hold duration when the selected source is so short that even 0.1x playback cannot fill the target duration.

### 3. Review timing adaptation

The timeline builder flags a speed as extreme by default when it falls outside roughly `0.5x–2.0x`, or whenever a frame hold is required.

Treat these as **review warnings, not automatic timeline holes**.

When an extreme timing fit exists:

1. check the Phase 2 alternatives for that reference segment,
2. prefer a better-duration alternative only when it preserves action/camera/interaction fidelity,
3. if no better option exists, keep the selected fallback and carry the warning forward,
4. record that better footage could improve fidelity.

Do not optimize duration at the expense of the core action choreography.

### 4. Preserve reference cues

The timeline must carry forward, without inventing values:

- `edit.transition_in`
- `edit.transition_out`
- `camera`
- `spatial`
- `text`
- `audio`

These are render instructions/evidence for Phase 4, not an excuse to generate new measured data.

If a cue is absent in the Reference Blueprint, leave it absent/empty. Do not guess a BPM, transition, crop, camera move, or text treatment.

### 5. Runtime remains unlocked

`replication_timeline.metadata.render_runtime_locked` MUST be `false`.

This is deliberate. The existing OpenMontage governance requires runtime choice to be surfaced to the user when multiple runtimes are available. Phase 3 therefore does **not** silently choose FFmpeg, Remotion, or HyperFrames.

Phase 4 will:

- inspect available composition runtimes,
- present the applicable choices,
- get/record the runtime decision,
- convert the approved replication timeline into the concrete render/edit contract,
- render the draft.

### 6. Human checkpoint

Before Phase 4, present a compact timeline review:

- number of timeline segments,
- full-duration coverage status,
- fallback count,
- extreme-speed count,
- hold-segment count,
- highest-risk segments that may benefit from better footage.

Do not block simply because fallback footage exists. The user already chose the product behavior: produce a complete video first, then improve fidelity by adding footage.

## Hard invariants

The stage is invalid if any of these are false:

- timeline starts at the reference start,
- timeline ends at the reference duration,
- segment order matches the Reference Blueprint,
- no unintended gaps or overlaps exist,
- every reference segment has exactly one concrete source range,
- no final `NO_MATCH` slot exists,
- fallback selections remain visible as fallback,
- no render runtime is silently locked,
- no video render occurs in Phase 3.

## Output

`replication_timeline` conforming to:

`schemas/artifacts/replication_timeline.schema.json`

This artifact is the only Phase 3 output required by the reference-replication pipeline.
