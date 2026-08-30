# Reference Replication — Match Director

## Mission

Map **every** enriched Reference Blueprint segment to one usable user-footage segment and produce schema-valid `matching.json`.

This is the Phase 2 editorial brain stage. It does not render video.

## Core product rule

**Coverage > perfect matching.**

The timeline must never be left with an empty reference slot just because the user's footage is imperfect.

For every reference segment:

1. Prefer a genuinely close action/camera/spatial match.
2. If the best option is only moderately similar, still choose it and mark it `acceptable`.
3. If nothing is close, choose the most editorially sensible footage fallback, mark it `fallback`, explain the compromise, and tell the user what additional footage would improve fidelity.

There is no `NO_MATCH` final state when usable footage exists.

Reuse of the same footage segment is allowed only when necessary to preserve a complete timeline. Record the tradeoff explicitly.

## Inputs

Required canonical artifacts:

- `reference_blueprint.json` from Phase 1, with `semantic_enrichment_required == false`.
- `footage_profiles.json` from the Phase 2 footage stage, with `semantic_enrichment_required == false`.

## Step 1 — Deterministic candidate retrieval

Run:

```python
reference_candidate_ranker.execute({
    "reference_blueprint_path": "<project>/analysis/reference_blueprint.json",
    "footage_profiles_path": "<project>/analysis/footage/footage_profiles.json",
    "top_k": 10,
    "output_path": "<project>/analysis/candidate_rankings.json"
})
```

The deterministic `pre_score` is only a shortlist signal. It is not the final editorial score.

A low pre-score does **not** authorize an empty slot. It means the agent must inspect the evidence and decide the best fallback.

## Step 2 — Agent evidence review

For each reference segment, compare the reference semantics and evidence against the candidate footage evidence.

Prioritize in this order unless the reference's critical constraints imply otherwise:

1. **Action** — is the useful physical/semantic action compatible?
2. **Interaction** — actor/object/target relationship and action ordering.
3. **Camera role** — POV, shot scale, angle, task-following behavior.
4. **Spatial relationship** — object position, entry/exit direction, foreground/background relationship.
5. **Motion character** — high/low energy, sustained vs quick gesture, movement direction.
6. **Duration fit** — can Phase 3 trim or modestly retime the segment without destroying the action?
7. **Image quality** — prefer cleaner footage when semantic fit is similar.

Do not mechanically choose the highest deterministic pre-score if visual evidence shows another candidate is editorially better.

## Match classes

### `good`

The selected footage preserves the important action and most critical camera/spatial relationships.

### `acceptable`

The selected footage is not a close replica, but it preserves the story/action flow well enough to keep the video coherent.

### `fallback`

The reference action or camera relationship is materially missing. Choose the best available replacement anyway so Phase 3 can build a full video.

Every fallback requires an `improvement_request` describing what footage the user should add if they want higher fidelity.

## Scores

For every selected match provide:

- `action`
- `interaction`
- `camera`
- `spatial`
- `motion`
- `duration`
- `overall`

Scores are 0–1. Use `null` for a component only when it genuinely cannot be judged. `overall` is always required.

These are agent judgments grounded in the inspected artifacts; do not pretend they are deterministic measurements.

## Proposal format

Write a UTF-8 proposal JSON before validation:

```json
{
  "matches": [
    {
      "reference_segment_id": "seg_003",
      "footage_segment_id": "clip_007_seg_action_02",
      "match_class": "good",
      "scores": {
        "action": 0.94,
        "interaction": 0.91,
        "camera": 0.88,
        "spatial": 0.84,
        "motion": 0.90,
        "duration": 0.96,
        "overall": 0.91
      },
      "rationale": "Same scoop-and-drop interaction in a close first-person workbench view.",
      "tradeoffs": ["object starts slightly farther right than the reference"],
      "alternatives": ["clip_003_seg_action_01"]
    },
    {
      "reference_segment_id": "seg_004",
      "footage_segment_id": "clip_002_seg_action_03",
      "match_class": "fallback",
      "scores": {
        "action": 0.32,
        "interaction": 0.40,
        "camera": 0.76,
        "spatial": 0.70,
        "motion": 0.61,
        "duration": 0.90,
        "overall": 0.52
      },
      "rationale": "No matching flair action exists; this is the strongest same-POV tool movement and preserves visual flow.",
      "tradeoffs": ["missing toss/catch flourish"],
      "alternatives": []
    }
  ],
  "improvement_requests": [
    {
      "reference_segment_id": "seg_004",
      "reason": "Current footage has no toss/catch flair action.",
      "suggested_footage": "Add a first-person clip showing the shaker toss/catch near the center prep board."
    }
  ],
  "notes": []
}
```

## Step 3 — Full-coverage validation

Run:

```python
reference_match_validator.execute({
    "reference_blueprint_path": "<project>/analysis/reference_blueprint.json",
    "footage_profiles_path": "<project>/analysis/footage/footage_profiles.json",
    "proposal_path": "<project>/analysis/matching_proposal.json",
    "output_path": "<project>/analysis/matching.json"
})
```

The validator resolves canonical source paths and source in/out ranges from Footage Profiles. Do not hand-copy those values into the final artifact.

## Hard completion gates

Phase 2 is complete only when:

1. `matched_segment_count == reference_segment_count`.
2. `full_coverage == true`.
3. Every match selects a real usable footage segment.
4. There are no duplicate reference slots and no missing slots.
5. Every fallback has a concrete `improvement_request`.
6. Reused footage is explicitly noted when reuse was necessary.
7. `matching.json` validates against `schemas/artifacts/reference_matching.schema.json`.
8. No render, timeline composition, speed-ramping, or transition execution occurs in this phase.

## What the user should learn from Phase 2

The user should have a complete mapping ready for Phase 3 **and** a clear list of the specific reference moments that could become more faithful if better footage is uploaded.
