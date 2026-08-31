# Moon Local Runtime v0.1

Moon is a local, resumable, deterministic video-editing runtime. External agents provide semantic decisions; Moon owns local media access, state, validation, checkpoints, artifacts, and rendering orchestration.

## Scope

Moon Local deliberately does not introduce a web app, cloud database, Drive-backed state, account system, local LLM/VLM, or agent-specific business logic.

```text
GPT / Gemini / Claude / Codex / other agent
                  |
       connector tools / JSON stdin
                  |
              Moon Local
                  |
        local project + FFmpeg
```

Adapters are intentionally thin. Moon Core does not know which model or vendor makes a semantic decision.

## Persistent project contract

```text
.moon/
  project.json
  state.json
  checkpoints/
  artifacts/
  cache/
```

Stage order:

```text
proposal -> analyze -> footage -> match -> timeline -> render -> qc
```

A completed stage writes a durable checkpoint before state advances. If an agent loses quota or exits, another agent can resume from the next incomplete stage.

## Agent-neutral protocol

Core actions include `status`, `next`, `stage.plan`, `stage.run`, `handoff.package`, and `handoff.submit`. `next` is the preferred orchestration action: it runs deterministic work continuously until complete, blocked, or the next semantic boundary.

## CLI

```powershell
python -m moon --project "D:\AI EDIT VIDEO\8.26" status
python -m moon --project "D:\AI EDIT VIDEO\8.26" next
python -m moon --project "D:\AI EDIT VIDEO\8.26" handoff
python -m moon --project "D:\AI EDIT VIDEO\8.26" connector-manifest
```

The CLI prints JSON so coding agents and shell integrations do not need to scrape human-oriented output.

## Agent handoff contract

At a semantic boundary Moon packages current state, deterministic input artifacts with hashes, local evidence paths, output validation rules, submission commands, and a deterministic handoff ID. Moon rejects stale-stage responses and validates required semantic structure before persistence.

## Fileless agent bridge

External agents that can execute a local process can submit JSON over stdin without creating `response.json`.

```powershell
'{"action":"next"}' | python -m moon --project "D:\AI EDIT VIDEO\8.26" agent-bridge
```

The bridge transports decisions only; it never generates them.

## Phase 7 agent connector tools

Phase 7 exposes a small, stable tool vocabulary that a Codex/Claude/Gemini-style local agent adapter can map directly without knowing Moon internals:

```text
moon.status
moon.next
moon.handoff
moon.evidence.list
moon.evidence.read_json
moon.frames.sample
moon.submit
```

Discover the tool contract:

```powershell
python -m moon --project "D:\AI EDIT VIDEO\8.26" connector-manifest
```

Call one tool over stdin/stdout JSON:

```powershell
'{"tool":"moon.evidence.list","arguments":{}}' |
  python -m moon --project "D:\AI EDIT VIDEO\8.26" connector-call
```

Read JSON evidence without giving the connector arbitrary filesystem access:

```json
{"tool":"moon.evidence.read_json","arguments":{"path":"analysis/footage/source_analysis/clip_001/video_analysis_brief.json"}}
```

Sample frames from original local media:

```json
{
  "tool":"moon.frames.sample",
  "arguments":{
    "source":"footage/oneshot.mp4",
    "start_seconds":120,
    "end_seconds":130,
    "count":8,
    "width":320
  }
}
```

Submit an external semantic decision and advance to the next boundary:

```json
{
  "tool":"moon.submit",
  "arguments":{
    "stage":"footage",
    "payload":{"clips":[]},
    "auto_next":true
  }
}
```

The sample payload above is structurally incomplete on purpose; real submissions must satisfy the current handoff contract. Evidence JSON reads are restricted to the project root. Image evidence is listed as local paths for vision-capable local agents, while frame extraction remains deterministic.

This connector is the stable local tool layer. MCP, Apps SDK, shell adapters, or vendor-specific wrappers should translate their tool calls into this surface rather than move semantic logic into Moon Core.

## Local media inspection

Moon uses `ffprobe` for deterministic metadata inspection only. Frame sampling uses `ffmpeg` against the original local source. The original video is not copied, uploaded, or physically pre-cut.

## Existing artifact bridge

Legacy reference-replication JSON can be imported without rerunning semantic work. `bootstrap-legacy` may infer only proposal from a complete canonical analyze pair; no later gaps are inferred.

## Stage execution adapters

```text
footage
  Moon -> footage_profile_builder (measured scaffold)
       -> external agent semantic enrichment
       -> footage_profile_builder (validated canonical profiles)

match
  Moon -> reference_candidate_ranker
       -> external agent final selection/fallback decision
       -> reference_match_validator

timeline
  Moon -> reference_timeline_builder

render
  external agent/user -> approved render_plan
  Moon -> reference_video_renderer

qc
  external agent -> qc_report + decision_log
  Moon -> persistence/checkpoint only
```

Moon never chooses a footage match, invents timestamps, silently switches rendering runtime, or performs semantic QC.

## Invariants

1. Semantic decisions belong to the external agent, not Moon Local.
2. Local media stays local unless an adapter explicitly exposes sampled frames or metadata.
3. A stage cannot be skipped; only the next incomplete stage is resumable.
4. A checkpoint is persisted before its stage becomes complete.
5. State and artifacts remain inspectable UTF-8 JSON without a database.
6. No specific agent vendor is required by Moon Core.
7. Media inspection and frame extraction are deterministic and do not choose shots.
8. Legacy artifacts are reused only when their canonical stage contract is complete.
9. Stage adapters stop at every semantic/editorial boundary.
10. Agent bridges/connectors transport decisions; they never generate them.
