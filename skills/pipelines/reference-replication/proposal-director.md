# Reference Replication — Proposal Director

## Mission

Establish the user-visible execution contract before reference analysis begins. Confirm the replication goal, preserve the rule that GPT/agent makes semantic and editorial decisions, and record how render runtime selection will be handled later without silently defaulting to a renderer.

This stage does not analyze footage, choose semantic matches, build a timeline, or render video.

## Required planning contract

Before the pipeline proceeds, explain that the core workflow is:

`Reference -> Blueprint -> Footage Profiles -> Matching -> Timeline -> Render -> GPT QC -> Final`

The pipeline must preserve full timeline coverage. If source footage is weaker than the reference, later matching may use an explicit fallback rather than leaving a reference segment empty.

## Render runtime governance

`render_runtime` is a user-approved execution decision, not an automatic semantic choice.

At planning time, **Present both** OpenMontage composition runtimes when they are available as a real choice:

- Remotion — useful for React-driven composition and motion graphics.
- HyperFrames — useful for HTML/CSS/GSAP-oriented motion-design treatments.

Also surface FFmpeg when it is available/applicable for reference replication. For footage-led exact-cut reconstruction, FFmpeg is normally the recommended deterministic runtime, but recommendation is not approval.

Do not silently select Remotion, HyperFrames, or FFmpeg. Do not lock the runtime in this proposal stage merely because one runtime is recommended.

The eventual approved decision must be recorded in `decision_log` under category `render_runtime_selection`, including runtimes considered and rejected reasons. The Phase 4 Render Director owns the final runtime preflight and approval gate.

## Semantic/editorial ownership

GPT/agent remains the brain for:

- interpreting the reference,
- judging footage meaning and suitability,
- choosing acceptable versus fallback matches,
- deciding editorial revisions during QC.

Python/OpenMontage tools remain responsible for deterministic measurement, validation, persistence, timeline realization, and rendering. Do not introduce a local LLM, VLM, or CLIP model as the semantic decision-maker for this pipeline.

## Output

Produce a concise planning acknowledgement for the user containing:

- the intended reference-replication goal,
- confirmation that full coverage is preferred over empty segments,
- confirmation that runtime will be explicitly approved later,
- the applicable runtime options known at planning time,
- any immediate blocker before reference analysis.

No Phase 1 artifact contract is changed by this stage.