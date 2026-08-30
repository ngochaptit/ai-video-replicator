# Reference Replication — QC Director

## Mission

Compare the rendered draft against the reference, fix what the engine can fix, and promote the latest acceptable draft to `final.mp4`.

This is the phase where **GPT is explicitly the brain**. Deterministic tools prepare paired evidence and enforce gates; the agent vision model judges semantic/editing fidelity.

The product rule remains:

> Always produce a complete video first. If fidelity is limited only by missing footage, finalize the best complete version and tell the user exactly what footage would improve it.

## Required inputs

- `reference_blueprint`
- `replication_timeline`
- `replication_render_plan`
- latest `draft_render`

## Required tools

- `replication_qc_evidence_builder`
- `reference_qc_validator`
- `reference_finalizer`

Revision loop may reuse earlier tools:

- `reference_match_validator`
- `reference_timeline_builder`
- `reference_render_plan_builder`
- `reference_video_renderer`

No local LLM/VLM/CLIP dependency is permitted. The agent's cloud multimodal capability performs semantic comparison.

## Two scores — never collapse them

Every QC iteration MUST report separately:

### FidelityScore

How closely the draft reproduces the reference's editing DNA:

- choreography/action order,
- segment timing and relative durations,
- camera role and framing,
- spatial direction,
- motion/speed behavior,
- transition behavior,
- text content/timing/placement,
- audio cue/timing behavior when evidence exists.

### QualityScore

How good and technically usable the output is on its own:

- no broken/blank frames,
- no accidental timeline gaps,
- acceptable crop/scale,
- readable text,
- stable audio/video,
- no obvious render corruption,
- complete beginning-to-end video.

Default gates:

- FidelityScore pass target: `0.85`
- QualityScore pass target: `0.80`

These are engine defaults, not a claim of guaranteed perceptual equivalence.

## 1. Build paired QC evidence

For each iteration run `replication_qc_evidence_builder`.

Canonical path:

`projects/<project-id>/artifacts/qc/iteration-<N>/qc_evidence.json`

The tool samples equivalent absolute timeline timestamps from reference and draft. It reuses Phase 1 reference evidence when available and extracts missing frames with FFmpeg.

The tool also measures reference-vs-draft duration delta.

It does **not** judge similarity.

## 2. Perform GPT multimodal review

Inspect all paired reference/draft frames and the relevant Blueprint fields for each segment.

Review the full sequence, not isolated pretty frames.

For each segment ask:

1. Is the same action/choreography role represented?
2. Does the action occur at the same point in the sequence?
3. Is POV/shot scale/angle/framing functionally equivalent?
4. Is motion direction and intensity equivalent enough?
5. Does speed adaptation preserve the intended action read?
6. Are text cues present at the correct segment timing and usable position?
7. Are transition labels reflected where measurable evidence supports them?
8. Are audio/sound cues materially aligned when they are part of the reference evidence?

Use explicit uncertainty when the evidence does not support a confident judgment.

## 3. Produce semantic QC review

Prepare a semantic review JSON for `reference_qc_validator` containing:

- `iteration`
- `status`: `pass`, `revise`, or `footage_limited`
- `scores`
- `summary`
- `segment_reviews`
- `revision_actions`
- `improvement_requests`
- `final_decision`

### Revision routing

Use the earliest stage that can actually fix the defect:

- `match` — wrong action/take/camera candidate; choose a better Phase 2 candidate if one exists.
- `timeline` — artifact/timing construction defect; do **not** casually change measured reference choreography.
- `render` — crop/fit/text placement/technical rendering/same-runtime composition issue.
- `footage` — no available clip can satisfy the reference role. This is not an auto-fix action; emit an `improvement_request`.

Do not route a footage absence to render tricks just to raise the fidelity score.

## 4. Validate the review

Run `reference_qc_validator`.

Canonical output:

`projects/<project-id>/artifacts/replication_qc.json`

The validator enforces:

- known segment IDs only,
- score ranges,
- duration tolerance,
- pass threshold semantics,
- revise requires concrete revision actions + rerender,
- footage-limited final requires good standalone quality + concrete footage requests,
- a draft with technical quality below threshold cannot hide behind `footage_limited`.

## 5. Status behavior

### `pass`

Use when:

- FidelityScore >= target,
- QualityScore >= target,
- no high-severity unresolved defect,
- no rerender remains.

Then finalize.

### `revise`

Use when an engine-side change can materially improve the draft.

`revision_actions` must be concrete and measurable, for example:

- `match`: "Use candidate clip_014 for seg_009 because it preserves the same pour interaction and POV; rebuild matching/timeline."
- `render`: "Change seg_006 crop from center-cover to preserve the hand entering from frame-right; verify hand is visible in the 12.2s paired frame."
- `render`: "Move text for seg_011 to top-center; verify it no longer covers the cup."

Then rerun the affected stage(s), render a new draft, increment iteration, and build fresh QC evidence.

### `footage_limited`

Use only when:

- the video is technically complete and QualityScore passes,
- remaining fidelity loss comes from footage that does not exist in the user's source set,
- no engine-side revision action remains worth doing.

This status is **publishable**. It must include concrete `improvement_requests`, e.g.:

- action needed,
- camera/POV needed,
- approximate duration/useful range,
- movement direction/framing requirement.

Do not leave timeline holes and do not refuse to produce the final video.

## 6. Automatic revision budget

Default to at most **2 automatic rerender iterations after the first draft** unless the user explicitly requests more.

Recommended loop:

`draft v1 -> QC -> revise -> draft v2 -> QC -> revise -> draft v3 -> QC`

At each loop:

- preserve the approved render runtime unless the user explicitly approves a change,
- preserve Reference Blueprint measured timing unless there is evidence the artifact itself is wrong,
- do not degrade a strong action match merely to make crop/duration metrics look better.

If the revision budget is exhausted and fixable high-severity technical defects remain, surface a blocker instead of falsely finalizing.

If only footage-limited fidelity gaps remain and QualityScore passes, use `footage_limited` and finalize.

## 7. Runtime changes during QC

A runtime failure or a proposed switch from FFmpeg/Remotion/HyperFrames is a major production change.

Follow the existing governance:

- investigate alternatives,
- explain tradeoffs,
- get user approval,
- append a revised `render_runtime_selection` decision using the same category + subject,
- rebuild the render plan,
- rerender.

Never silently change runtime inside the QC loop.

## 8. Finalize

When canonical QC status is `pass` or publishable `footage_limited`, run `reference_finalizer`.

Canonical output:

`projects/<project-id>/renders/final.mp4`

The finalizer only promotes the latest approved draft. It does not modify video pixels.

## 9. Final report

Report compactly:

- final path,
- QC status,
- FidelityScore,
- QualityScore,
- number of QC iterations,
- remaining fidelity limitations,
- exact footage improvement requests, if any.

A `footage_limited` final is still a successful complete render; it simply gives the user a concrete path to higher reference fidelity on the next run.

## Outputs

- `replication_qc_evidence`
- `replication_qc`
- `final_render`

Core workflow is complete when `final_render` exists behind a valid QC gate.
