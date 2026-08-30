# Reference Replication — Render Director

## Mission

Turn the approved Phase 3 `replication_timeline` into a complete draft video while preserving full timeline coverage and OpenMontage runtime governance.

Phase 4 has two sub-jobs:

1. build a canonical `replication_render_plan`,
2. render `draft.mp4` with the explicitly approved runtime.

Do not silently choose or switch runtimes.

## Required inputs

- `reference_blueprint`
- `replication_timeline`

The timeline must already declare:

- `coverage.full_coverage = true`
- `coverage.timeline_contiguous = true`

## Required tools

- `reference_render_plan_builder`
- `reference_video_renderer`
- `video_compose`

`reference_video_renderer` uses a dedicated FFmpeg exact-timeline path when FFmpeg is approved. For approved Remotion/HyperFrames plans it delegates through the existing `video_compose` governance path.

## 1. Preflight render runtimes — mandatory

Before building the render plan, inspect `video_compose.get_info()["render_engines"]`.

Follow `AGENT_GUIDE.md` runtime governance exactly:

- if both Remotion and HyperFrames are available, present both,
- include FFmpeg as an option when it is available/applicable,
- give one plain-language strength and one tradeoff for each applicable runtime,
- recommend one for this specific replication,
- wait for explicit user approval before locking `render_runtime`.

### Recommendation for reference replication

For a footage-led reference reconstruction dominated by source-video cuts, speed fitting, and exact timing, **recommend FFmpeg** when available because it is the most direct and deterministic path.

Tradeoff: the Phase 4 FFmpeg path deliberately does not invent transition durations that were not measured in Phase 1, and its text treatment is functional rather than a bespoke motion-graphics system.

Remotion can be a better choice when the reference relies heavily on animated text/compositing and the selected timeline does not require final-frame holds. HyperFrames is better suited to HTML/CSS/GSAP motion-design treatments than to a pure source-footage reconstruction.

These are recommendations only. The user still approves the runtime.

## 2. Composition mode is a separate decision

Record `composition_mode` separately from runtime.

For the standard reference-replication cut pipeline:

- FFmpeg → `templated` (technical cut contract; atelier does not apply),
- Remotion → normally `templated` with `documentary-montage` renderer family,
- HyperFrames → use only when explicitly approved and appropriate for the reference treatment.

Do not introduce a bespoke atelier redesign merely because it is available. The product goal here is reference fidelity, not a fresh visual language.

## 3. Record the runtime decision

Append the approved runtime to `decision_log` using the existing governance contract:

- category: `render_runtime_selection`
- subject: `Reference replication render runtime`
- include every runtime considered,
- record rejected reasons,
- if the choice later changes, append a new entry using the same category + subject.

Do the same for composition mode with category `composition_mode`.

## 4. Build the render plan

Run `reference_render_plan_builder` only after approval.

Canonical output:

`projects/<project-id>/artifacts/replication_render_plan.json`

Required caller decisions:

- `render_runtime`
- `renderer_family`
- `composition_mode`
- `runtime_approved=true`

The builder converts each Phase 3 segment into canonical `edit_decisions.cuts`, creates an `asset_manifest` for user footage, derives the output aspect/resolution from the Reference Blueprint unless overridden, and preserves text cues separately.

The tool MUST fail if runtime approval is absent.

## 5. Text behavior

Reference text is carried from the Phase 1 Blueprint through Phase 3 and into the render plan.

For the FFmpeg draft path, `reference_video_renderer` burns text as UTF-8 ASS overlays at the reference segment timestamps. Position is honored when the Blueprint gives a recognizable top/center/bottom + left/center/right position.

If the Blueprint only has free-form text timing notes, Phase 4 can guarantee segment-level timing but not invent a more precise sub-segment timestamp. Keep the warning for Phase 5 QC.

## 6. Timing behavior

The renderer must realize Phase 3 timing exactly:

- trim the Phase 2 source range,
- apply the computed playback speed,
- if an extremely short source requires a hold, FFmpeg clones the final video frame and pads audio silence for the declared hold duration,
- output segment duration equals the Phase 3 target duration.

This is how `Coverage > perfect matching` remains true in the rendered draft.

### Hold capability rule

The dedicated FFmpeg path supports final-frame hold.

If the approved Remotion/HyperFrames path encounters a timeline with required holds that the generic composition path cannot guarantee, surface a blocker. **Do not silently switch to FFmpeg.** The user may approve a runtime change or later upload better footage.

## 7. Transition behavior

Carry reference transition labels into `edit_decisions`.

Phase 1 currently records transition type but not a measured transition duration. Therefore:

- hard cuts are exact,
- non-cut transition labels are preserved for review,
- do not invent dissolve/wipe duration merely to make the draft look polished.

Phase 5 may flag these moments for a better measured/reference-informed revision.

## 8. Render the draft

Run `reference_video_renderer` with the approved `replication_render_plan`.

Canonical output:

`projects/<project-id>/renders/draft.mp4`

The FFmpeg path verifies rendered duration against the sum of target timeline durations and fails when drift exceeds the allowed technical tolerance.

## 9. Blockers

If the approved runtime fails or is unavailable, use the normal blocker contract:

1. what was attempted,
2. what failed,
3. auth/runtime/tool/design classification,
4. available next options,
5. recommended option.

Wait for approval before switching runtime.

## 10. Human checkpoint

Before Phase 5 QC, report:

- approved runtime,
- output path,
- rendered duration,
- expected duration,
- segment count,
- fallback count inherited from Phase 3,
- hold count,
- text overlay count,
- unresolved transition/text timing warnings.

A draft may contain weak footage and still pass Phase 4 if it is technically complete. Semantic/reference fidelity is judged in Phase 5.

## Output

- `replication_render_plan` conforming to `schemas/artifacts/replication_render_plan.schema.json`
- `draft_render` at `projects/<project-id>/renders/draft.mp4`

Phase 4 does not declare the draft final.
