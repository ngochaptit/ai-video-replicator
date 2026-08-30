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

The first protocol surface is intentionally small:

```json
{"action":"status"}
{"action":"resume"}
{"action":"begin","stage":"match"}
{"action":"complete","stage":"match","checkpoint":{...}}
{"action":"artifact.write","name":"match_decision_seg_007","payload":{...}}
{"action":"artifact.read","name":"match_decision_seg_007"}
```

An MCP adapter can be added later by translating MCP tool calls into these same protocol requests. MCP is not part of the core runtime.

## CLI

The initial local adapter is available through Python:

```powershell
python -m moon --project "D:\AI EDIT VIDEO\8.26" init
python -m moon --project "D:\AI EDIT VIDEO\8.26" status
python -m moon --project "D:\AI EDIT VIDEO\8.26" begin proposal
python -m moon --project "D:\AI EDIT VIDEO\8.26" complete proposal proposal.json
python -m moon --project "D:\AI EDIT VIDEO\8.26" resume
python -m moon --project "D:\AI EDIT VIDEO\8.26" submit match_decision_seg_007 decision.json
```

The CLI prints JSON so coding agents and shell integrations can consume it without scraping human-oriented output.

## Invariants

1. Semantic decisions belong to the external agent, not Moon Local.
2. Local media stays local unless an adapter explicitly exposes sampled frames or metadata.
3. A stage cannot be skipped. The next incomplete stage is the only resumable stage.
4. A checkpoint is persisted before its stage becomes complete.
5. State and artifacts are plain UTF-8 JSON and must remain inspectable without a database.
6. Codex or another coding agent is a developer/debugger of Moon; it is not a required runtime component.

## Next implementation slice

After this foundation is proven locally, add deterministic media inspection and frame sampling behind the same protocol, then bridge existing OpenMontage/reference-replication stages into `PipelineRunner`. Only after the CLI/JSON contract is stable should an MCP adapter be added.
