# Moon Local Runtime v0.1

Moon is a local, resumable, deterministic video-editing runtime. External agents provide semantic decisions; Moon owns local media access, state, validation, checkpoints, artifacts, and rendering orchestration.

## Scope

Moon Local deliberately does not introduce a web app, cloud database, Drive-backed state, account system, local LLM/VLM, or agent-specific business logic.

```text
GPT / Gemini / Claude / Codex / other agent
                  |
     MCP / connector tools / JSON stdin
                  |
              Moon Local
                  |
        local project + FFmpeg
```

Adapters are intentionally thin. Moon Core does not know which model or vendor makes a semantic decision.

## Persistent project contract

`.moon/` contains project/state JSON, checkpoints, artifacts, and cache. Stage order is `proposal -> analyze -> footage -> match -> timeline -> render -> qc`. A completed stage writes a durable checkpoint before state advances.

## Agent-neutral protocol

Core actions include `status`, `next`, `stage.plan`, `stage.run`, `handoff.package`, and `handoff.submit`. `next` runs deterministic work continuously until complete, blocked, or the next semantic boundary.

## CLI and handoff

```powershell
python -m moon --project "D:\AI EDIT VIDEO\8.26" status
python -m moon --project "D:\AI EDIT VIDEO\8.26" next
python -m moon --project "D:\AI EDIT VIDEO\8.26" handoff
python -m moon --project "D:\AI EDIT VIDEO\8.26" connector-manifest
```

At a semantic boundary Moon packages current state, deterministic input artifacts with hashes, local evidence paths, output validation rules, submission commands, and a deterministic handoff ID. Moon rejects stale-stage responses and validates required semantic structure before persistence.

## Phase 7 agent connector tools

The stable tool vocabulary is:

```text
moon.status
moon.next
moon.handoff
moon.evidence.list
moon.evidence.read_json
moon.frames.sample
moon.submit
```

`connector-manifest` discovers the contract and `connector-call` invokes one tool over stdin/stdout JSON. Evidence JSON reads are project-root confined. Frame extraction uses the existing deterministic FFmpeg path. Semantic submissions reuse the handoff validator.

## Phase 8 MCP stdio adapter

Phase 8 maps the Phase 7 connector surface to a thin local MCP server. It does not contain semantic logic and does not call a model.

Start the server for one project:

```powershell
python -m moon --project "D:\AI EDIT VIDEO\8.26" mcp-stdio
```

An MCP-capable local host can launch that command as a stdio server. The adapter implements the MCP initialization handshake, `ping`, `tools/list`, and `tools/call`. Tool calls are delegated directly to `AgentConnectorService`; tool failures are returned as MCP tool errors rather than mutating Moon semantics.

Conceptual MCP host configuration:

```json
{
  "command": "python",
  "args": [
    "-m", "moon",
    "--project", "D:\\AI EDIT VIDEO\\8.26",
    "mcp-stdio"
  ]
}
```

The adapter advertises JSON Schema for every Moon tool, including required fields for evidence reads, frame sampling, and semantic submission. Local media remains local: MCP only exposes paths/JSON and deterministic frame samples through the same connector contract.

Important: this is a local stdio MCP server for MCP hosts that support launching local servers. It does not imply that every ChatGPT plan/product can connect directly to localhost; product-specific remote/tunnel/app adapters remain separate thin wrappers.

## Local media inspection

Moon uses `ffprobe` for deterministic metadata inspection only. Frame sampling uses `ffmpeg` against the original local source. The original video is not copied, uploaded, or physically pre-cut.

## Existing artifact bridge

Legacy reference-replication JSON can be imported without rerunning semantic work. `bootstrap-legacy` may infer only proposal from a complete canonical analyze pair; no later gaps are inferred.

## Stage execution adapters

```text
footage: Moon measured scaffold -> external semantic enrichment -> validated profiles
match: deterministic pre-rank -> external final selection/fallback -> validation
timeline: deterministic timeline builder
render: external approved render plan -> deterministic renderer
qc: external qc_report + decision_log -> persistence/checkpoint
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
10. Agent bridges/connectors/MCP transport decisions; they never generate them.
