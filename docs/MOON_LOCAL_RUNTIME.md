# Moon Local Runtime v0.1

Moon is a local, resumable, deterministic video-editing runtime. External agents provide semantic decisions; Moon owns local media access, state, validation, checkpoints, artifacts, and rendering orchestration.

## Scope

Moon Local v0.1 deliberately does not introduce a web app, cloud database, Drive-backed state, account system, local LLM/VLM, or agent-specific business logic.

The runtime boundary is:

```text
GPT / Gemini / Claude / Codex / other agent
                  |
             JSON protocol
                  |
              Moon Local
                  |
        local project + FFmpeg
```

Adapters are intentionally thin. The core must not know which model or vendor is making a semantic decision.

## Persistent project contract

Initializing a project creates only local state under `.moon/`:

```text
.moon/
  project.json
  state.json
  checkpoints/
  artifacts/
  cache/
```

`state.json` records pipeline progress. Each completed stage writes a durable checkpoint before the stage is marked complete. This is the resume boundary: if an agent loses quota or a process exits during `analyze`, a new agent can inspect status and continue from `analyze` without repeating `proposal`.

The v0.1 stage order mirrors the current reference-replication flow:

```text
proposal -> analyze -> footage -> match -> timeline -> render -> qc
```

## Agent-neutral protocol

The protocol surface remains vendor-neutral. In addition to state, artifact, and media actions, Phase 4 adds:

```json
{"action":"stage.plan"}
{"action":"stage.run"}
```

`stage.plan` tells an agent whether the next stage is deterministic, agent-owned, or hybrid. `stage.run` executes only deterministic work and returns `awaiting_agent` when semantic/editorial input is required.

An MCP adapter can be added later by translating MCP tool calls into these same protocol requests. MCP is not part of the core runtime.

## CLI

The local adapter is available through Python:

```powershell
python -m moon --project "D:\AI EDIT VIDEO\8.26" status
python -m moon --project "D:\AI EDIT VIDEO\8.26" stage-plan
python -m moon --project "D:\AI EDIT VIDEO\8.26" run-stage
python -m moon --project "D:\AI EDIT VIDEO\8.26" inspect-reference
python -m moon --project "D:\AI EDIT VIDEO\8.26" inspect-footage
python -m moon --project "D:\AI EDIT VIDEO\8.26" bootstrap-legacy
python -m moon --project "D:\AI EDIT VIDEO\8.26" frames --source "footage\oneshot.mp4" --from 120 --to 130
python -m moon --project "D:\AI EDIT VIDEO\8.26" submit footage_semantic_enrichment footage_semantic_enrichment.json
python -m moon --project "D:\AI EDIT VIDEO\8.26" submit match_proposal match_proposal.json
python -m moon --project "D:\AI EDIT VIDEO\8.26" submit render_plan render_plan.json
```

The CLI prints JSON so coding agents and shell integrations can consume it without scraping human-oriented output.

## Local media inspection

Moon uses `ffprobe` for deterministic metadata inspection only. Frame sampling uses `ffmpeg` against the original local source. The original video is not copied, uploaded, or physically pre-cut.

## Existing artifact bridge

Phase 3 bridges already-produced reference-replication JSON into Moon without rerunning semantic work. `bootstrap-legacy` may infer only proposal from a complete canonical analyze pair; no later gaps are inferred.

For the current `8.26` legacy project, the known analyze artifacts migrate proposal + analyze and resume at footage.

## Stage execution adapters

Phase 4 wraps the existing reference-replication tools instead of reimplementing them.

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

Moon writes `*_agent_task` artifacts whenever it needs semantic input. These tasks describe the required output artifact and evidence location. `run-stage` can then be called again after the agent submits that artifact. The stage stays incomplete until deterministic validation succeeds.

The execution adapter never chooses a footage match, never invents timestamps, never silently chooses or switches a rendering runtime, and never performs semantic QC.

## Invariants

1. Semantic decisions belong to the external agent, not Moon Local.
2. Local media stays local unless an adapter explicitly exposes sampled frames or metadata.
3. A stage cannot be skipped. The next incomplete stage is the only resumable stage.
4. A checkpoint is persisted before its stage becomes complete.
5. State and artifacts are plain UTF-8 JSON and must remain inspectable without a database.
6. Codex or another coding agent is a developer/debugger of Moon; it is not a required runtime component.
7. Media inspection and frame extraction are deterministic operations; they do not choose shots or score semantic similarity.
8. Existing reference-replication artifacts are reused only when their expected canonical stage contract is complete.
9. Legacy migration may infer only proposal from a complete canonical analyze pair; no other stage gaps are inferred.
10. Stage adapters may execute deterministic existing tools, but must stop and emit an explicit agent task at every semantic/editorial boundary.

## Next implementation slice

Prove Phase 4 against the real `8.26` footage stage, including one agent handoff and resume. After the CLI/JSON contract remains stable under that run, add a thin MCP or other agent connector without changing Moon Core.
