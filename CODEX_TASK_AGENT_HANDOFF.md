# Codex task: persistent Gemini ↔ GPT ↔ Moon handoff state protocol

## Baseline
- Branch: `feat/moon-google-drive-bridge`
- Start from commit: `2064514a03307f08bbbf2992f1764657d1c16eae`
- Existing Drive bridge, cold-start proposal/analyze handoff, and analyze evidence coverage must remain intact.

## Goal
Implement a persistent, chat-independent state/handoff protocol so a completely fresh Gemini or GPT web chat can read the Drive handoff package and immediately know:
1. who its current actor/role is,
2. what job/stage/request it is working on,
3. what files/evidence it must inspect,
4. what output it must produce,
5. the exact completion acknowledgement it must return,
6. who gets the next turn and what that next actor must do.

This task is for the **current human-triggered web workflow**. Do not add OpenAI/Gemini APIs, Cloud Run, Apps Script, or browser automation.

## Required design

### Canonical local state
Add a compact state artifact owned by Moon, preferably `.moon/agent-state.json` (do not overload `bridge-state.json`). Minimum fields:
- `version`
- `job_id`
- `stage`
- `revision`
- `request_id` when applicable
- `status`
- `current_actor`
- `next_actor`
- `next_action`
- `task`
- `required_inputs`
- `expected_output`
- `completion_contract`
- `updated_at`

The state must be reconstructable from canonical pipeline/bridge artifacts if missing or stale. It must not become a second conflicting source of truth.

### Published routing state
Every Drive handoff request must expose the current routing/state to a fresh agent. Either embed a single canonical route block in `request.json` or publish one compact `agent_state.json` next to it. Avoid duplicated/conflicting routing data.

The published state must explicitly answer:
- Who am I right now?
- What exact task do I perform?
- Which exact inputs do I read?
- What output/artifact do I return?
- What terminal acknowledgement format do I use?
- Who is next?
- What happens on approval?
- What happens on revision?

### Deterministic completion contract
Include a short terminal acknowledgement safe to paste between fresh chats. Example shape (syntax may be improved):

`TASK_COMPLETED job_id=8.26 stage=analyze actor=gemini decision=COMPLETED output=semantic_enrichment next_actor=gpt next_action=REVIEW_GEMINI_ANALYSIS`

For GPT review support at least:
- `APPROVED` -> `next_actor=moon`, `next_action=CONSUME_RESPONSE`
- `REVISION_REQUIRED` -> `next_actor=gemini`, `next_action=RECHECK_TARGETS`, with explicit target segment IDs/reasons in structured metadata

Do not rely on prose-only parsing where a structured field can be used.

### State machine
At minimum implement the analyze route:

`MOON_PREPARE -> WAITING_GEMINI -> GEMINI_DONE -> WAITING_GPT -> GPT_APPROVED -> WAITING_MOON -> MOON_CONTINUE`

Revision loop:

`GPT_REVISION_REQUIRED -> WAITING_GEMINI -> GEMINI_RECHECK_DONE -> WAITING_GPT`

Preserve job/stage/request/revision identity across retry/resume.

### Human-triggered web compatibility
Current constraints discovered in real PoC:
- Gemini web can read Drive evidence by direct link.
- Gemini web cannot reliably overwrite raw `application/json`.
- ChatGPT Drive connector can write raw `response.json`.

Therefore **do not require Gemini to write raw JSON**. Gemini's handoff must tell it exactly what to return in chat and what the user must hand to GPT. A brand-new GPT chat must be able to reconstruct the task from Drive state plus the pasted Gemini result.

### Bridge integration
Integrate with existing:
- `moon bridge publish`
- `moon bridge watch`
- `moon bridge status`
- stage execution/resume

`moon bridge status` should expose current actor, next actor, and next action compactly.

Do not weaken:
- request identity validation
- staleness checks
- schema validation
- idempotency
- evidence coverage requirements

## Tests
Add regression tests covering at least:
- state creation on proposal/analyze cold start
- analyze publish routes to Gemini first
- Gemini completion routes to GPT review
- GPT approval routes to Moon consume
- GPT revision routes back to Gemini with explicit targets
- idempotent retry does not corrupt routing state
- stale request/response cannot advance routing state
- missing local state reconstructs from canonical pipeline/bridge artifacts

Run targeted tests for bridge/handoff/analyze/state protocol and report exact counts. Do not claim GitHub CI unless an actual workflow run exists.

## Non-goals
- No OpenAI API
- No Gemini API
- No Cloud Run / Apps Script
- No browser automation
- No product-logic changes to matching/timeline/rendering
- No per-video hacks

## Acceptance test
A completely fresh Gemini or GPT chat, given only the Drive job/request link plus `Continue MON EDIT job 8.26`, can read the published state and correctly state:
- its role,
- required inputs,
- required output,
- required completion acknowledgement,
- next actor and next action.

Keep implementation minimal and deterministic.