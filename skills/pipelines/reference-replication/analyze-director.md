# Reference Replication — Analyze Director

## Mission

Convert one reference video into a schema-valid `reference_blueprint` that captures
its **editing DNA**: action order, choreography, timing, camera relationship,
framing, transitions, text timing, and audio cues.

This stage does **not** match user footage and does **not** render video.

## Hard separation of responsibilities

- Deterministic tools own: media duration, scene boundaries, sampling timestamps,
  frame paths, motion measurements, transcript timing, and other measurable facts.
- The multimodal agent owns: semantic action, actor/object interaction,
  choreography, camera/framing interpretation, segment role, and constraint meaning.

Never invent measured values because they "look about right".

## Required tool first

Run `reference_blueprint_builder` with deep analysis:

```python
reference_blueprint_builder.execute({
    "source": "<reference path>",
    "analysis_depth": "deep",
    "max_keyframes": 40,
    "max_analysis_window_seconds": 2.0,
    "output_dir": "<project>/artifacts/reference_analysis"
})
```

The returned `reference_blueprint.json` is a **scaffold**, not the finished artifact.
Its semantic fields are intentionally blank.

## Core rule: choreography beats hard-cut detection

A detected scene is not automatically one semantic segment.

Example: a 5-second continuous POV shot can contain:

1. hand approaches cup
2. hand grips cup
3. cup moves to center
4. liquid is poured
5. cup is placed down

If a window contains multiple meaningful actions/interactions, refine it into
multiple semantic segments. Preserve measured time coverage and use additional
`frame_sampler` timestamps when evidence is insufficient.

Do not split merely because motion exists. Split when the **editorial/action role**
changes in a way that matters for later footage matching.

## Per-segment analysis

For every segment fill all fields. Use `null` only when genuinely not observable.

### Semantic

- `actor`: who/what performs the action
- `action`: concise action verb (`approach`, `pick_up`, `pour`, `handoff`, `react`)
- `object`: manipulated or focal object
- `target`: destination/person/object receiving the action
- `interaction`: relation such as hand-object, person-person, object-container
- `description`: one factual sentence describing temporal action

Prefer action verbs that will later be searchable in user footage.

### Camera

Use the canonical visual vocabulary already used by OpenMontage:

- POV
- shot scale (ECU/CU/MS/WS/EWS)
- angle
- movement (push/pull/pan/tilt/dolly/truck/orbit/follow/locked)
- steadiness (locked/handheld/gimbal/etc.)
- playback speed (real-time/slow-mo/time-lapse/speed-ramp when visible)

Separate **subject motion** from **camera motion**.

### Spatial framing

Record spatial relationships that matter to reconstruction:

- actor/object start position
- entry/exit direction
- foreground/midground/background
- framing change through the action

Example: `object_position = "right -> center"` is more useful than `"cup visible"`.

### Motion

Keep deterministic `motion_type` and `flow_variance` when present. Add:

- intensity
- speed behavior

Do not overwrite measured values with subjective guesses.

### Edit

Fill transition types only when visible/grounded. Set `segment_role` using editorial
function, e.g. `establish`, `approach`, `interaction`, `main_action`, `reaction`,
`secondary_action`, `ending`.

### Text and audio

Capture on-screen text and its timing role separately from scene description.
For audio, capture speech, meaningful SFX cues, beat/cut cues, and energy notes.
Do not claim BPM/beat timestamps unless a tool measured them or the evidence is explicit.

## Choreography summary

After all segments are enriched, fill:

- `summary`: one concise description of the action progression
- `action_order`: ordered segment IDs + actions, e.g. `seg_001: establish product`
- `critical_constraints`: facts the reconstructed video should preserve strongly
- `soft_constraints`: details GPT may creatively adjust later

### Critical constraint examples

- action order
- handoff before reaction
- main action begins near a specific measured time
- close POV relationship during interaction
- text appears only after product reveal
- cut occurs on a specific interaction boundary

### Soft constraint examples

- exact crop percentage
- minor speed adjustment
- transition flavor when reference transition is not essential
- color polish
- choice among equally suitable takes

## Confidence policy

`timing` remains tool-grounded.

Set `semantic`, `camera`, and `overall` from 0 to 1 based on visible evidence:

- 0.90-1.00: directly visible across multiple supporting frames
- 0.70-0.89: clear but partially sampled
- 0.50-0.69: plausible with ambiguity
- below 0.50: insufficient evidence; sample more frames before accepting

Do not hide uncertainty with confident prose.

## Completion gate

The stage passes only when:

1. the full reference duration is covered without accidental gaps or overlaps,
2. long shots are not blindly treated as one action,
3. every segment has action/camera/spatial/evidence fields reviewed,
4. choreography action order is explicit,
5. critical vs soft constraints are separated,
6. `semantic_enrichment_required` is set to `false`,
7. the result validates against `schemas/artifacts/reference_blueprint.schema.json`.

Stop after `reference_blueprint.json`. Footage matching and rendering belong to later phases.
