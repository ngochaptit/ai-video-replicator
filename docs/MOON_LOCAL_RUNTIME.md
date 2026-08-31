# Moon Local Runtime v0.1

Moon is a local, resumable, deterministic video-editing runtime. External agents provide semantic decisions; Moon owns local media access, state, validation, checkpoints, artifacts, and rendering orchestration.

## Scope

Moon Local deliberately does not introduce a web app, cloud database, Drive-backed state, account system, local LLM/VLM, or agent-specific business logic.

```text
GPT / Gemini / Claude / Codex / other agent
                  |
        CLI / stdin JSON / protocol
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

Core actions include:

```json
{"action":"status"}
{"action":"next"}
{"action":"stage.plan"}
{"action":"stage.run"}
{"action":"handoff.package"}
{"action":"handoff.submit","stage":"footage","payload":{}}
```

`next` is the preferred orchestration action. It runs deterministic work continuously until the pipeline completes, blocks, or reaches the next semantic boundary. At a semantic boundary it returns the full handoff package instead of pretending Moon can make the decision itself.

## CLI

```powershell
python -m moon --project "D:\AI EDIT VIDEO\8.26" status
python -m moon --project "D:\AI EDIT VIDEO\8.26" next
python -m moon --project "D:\AI EDIT VIDEO\8.26" handoff
python -m moon --project "D:\AI EDIT VIDEO\8.26" inspect-reference
python -m moon --project "D:\AI EDIT VIDEO\8.26" inspect-footage
python -m moon --project "D:\AI EDIT VIDEO\8.26" bootstrap-legacy
python -m moon --project "D:\AI EDIT VIDEO\8.26" frames --source "footage\oneshot.mp4" --from 120 --to 130
```

The CLI prints JSON so coding agents and shell integrations do not need to scrape human-oriented output.

## Agent handoff contract

At a semantic boundary Moon packages:

- current stage and resumable pipeline state
- deterministic input artifacts with SHA-256 hashes
- local evidence paths such as briefs, scenes, and sampled frames
- output contract and validation rules
- file and fileless submission commands
- a deterministic handoff ID

Moon rejects stale-stage responses and validates required semantic structure before persistence.

## Fileless agent bridge

Phase 6 removes the requirement to create a temporary `response.json` file. External agents that can execute a local process can submit JSON over stdin.

PowerShell example:

```powershell
$decision = @'
{"clips":[{"path":"footage/oneshot.mp4","segments":[{"source_in":1.0,"source_out":2.0}]}]}
'@
$decision | python -m moon --project "D:\AI EDIT VIDEO\8.26" submit-handoff-stdin footage
python -m moon --project "D:\AI EDIT VIDEO\8.26" next
```

For a generic agent connector, Moon also exposes one stdin/stdout bridge entrypoint:

```powershell
'{"action":"next"}' | python -m moon --project "D:\AI EDIT VIDEO\8.26" agent-bridge
```

A semantic submission can be accepted and automatically advanced in one bridge request:

```json
{
  "action": "submit",
  "stage": "footage",
  "payload": {
    "clips": [
      {
        "path": "footage/oneshot.mp4",
        "segments": [{"source_in": 1.0, "source_out": 2.0}]
      }
    ]
  },
  "auto_next": true
}
```

This bridge does not call an LLM. It only gives any external agent a stable local transport for reading the next task and returning decisions without temporary files.

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
10. Agent bridge transports decisions; it never generates them.
