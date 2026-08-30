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

The protocol surface remains vendor-neutral:

```json
{"action":"status"}
{"action":"resume"}
{"action":"begin","stage":"match"}
{"action":"complete","stage":"match","checkpoint":{...}}
{"action":"artifact.write","name":"match_decision_seg_007","payload":{...}}
{"action":"artifact.read","name":"match_decision_seg_007"}
{"action":"artifact.discover"}
{"action":"artifact.import","stage":"analyze"}
{"action":"stage.complete_from_artifacts","stage":"analyze"}
{"action":"stage.bootstrap_legacy"}
{"action":"media.inspect.reference"}
{"action":"media.inspect.footage"}
{"action":"media.frames","source":"footage/oneshot.mp4","start_seconds":120,"end_seconds":130,"count":8,"width":320}
```

An MCP adapter can be added later by translating MCP tool calls into these same protocol requests. MCP is not part of the core runtime.

## CLI

The local adapter is available through Python:

```powershell
python -m moon --project "D:\AI EDIT VIDEO\8.26" init
python -m moon --project "D:\AI EDIT VIDEO\8.26" status
python -m moon --project "D:\AI EDIT VIDEO\8.26" inspect-reference
python -m moon --project "D:\AI EDIT VIDEO\8.26" inspect-footage
python -m moon --project "D:\AI EDIT VIDEO\8.26" discover-artifacts
python -m moon --project "D:\AI EDIT VIDEO\8.26" bootstrap-legacy
python -m moon --project "D:\AI EDIT VIDEO\8.26" import-artifacts --stage analyze
python -m moon --project "D:\AI EDIT VIDEO\8.26" complete-from-artifacts --stage analyze
python -m moon --project "D:\AI EDIT VIDEO\8.26" frames --source "footage\oneshot.mp4" --from 120 --to 130
python -m moon --project "D:\AI EDIT VIDEO\8.26" begin proposal
python -m moon --project "D:\AI EDIT VIDEO\8.26" complete proposal proposal.json
python -m moon --project "D:\AI EDIT VIDEO\8.26" resume
python -m moon --project "D:\AI EDIT VIDEO\8.26" submit match_decision_seg_007 decision.json
```

The CLI prints JSON so coding agents and shell integrations can consume it without scraping human-oriented output.

## Local media inspection

Moon uses `ffprobe` for deterministic metadata inspection only. The normalized inspection contract includes duration, dimensions, orientation, frame rate, codecs, audio/video presence, file size, and path metadata. No semantic decisions are made during probing.

Frame sampling uses `ffmpeg` against the original local source. An agent requests a bounded timestamp window and Moon writes a small set of JPEG samples under `.moon/cache/frames/`. The original video is not copied, uploaded, or physically pre-cut. Sources passed through the protocol must remain inside the Moon project root.

## Existing artifact bridge

Phase 3 bridges already-produced reference-replication JSON into Moon without rerunning semantic work. Moon discovers canonical artifacts in the project root, `analysis/`, `output/`, or `artifacts/`, imports the JSON into `.moon/artifacts/`, then checkpoints the matching runtime stage.

The initial canonical mapping is:

```text
proposal -> proposal_packet
analyze  -> reference_blueprint + semantic_enrichment
footage  -> footage_profiles
match    -> match_decisions
timeline -> timeline
render   -> draft_render
qc       -> qc_report + decision_log
```

A stage still cannot be skipped. `complete-from-artifacts` only succeeds for the current resumable stage and only when all canonical artifacts for that stage exist. This preserves the existing Phase 1 artifacts while making them durable resume inputs for Moon.

### Legacy bootstrap

Older real runs may have valid downstream artifacts but no `proposal_packet.json`. `bootstrap-legacy` exists only for a pristine Moon state still at `proposal`. It advances through the longest contiguous sequence of stages proven by canonical artifacts.

There is exactly one inference rule: if both canonical analyze artifacts (`reference_blueprint` and `semantic_enrichment`) exist, Moon may infer that the legacy run necessarily crossed the proposal gate. The resulting proposal checkpoint is marked `source: legacy_downstream_evidence` and records the exact analyze artifact paths used as evidence. Moon does not fabricate a proposal packet.

No other missing stage is inferred. Partial analyze evidence is insufficient, a missing footage artifact stops migration at `footage`, and a later match/timeline/render artifact cannot jump over that gap. Bootstrap also refuses to rewrite a Moon project that has already advanced beyond a pristine proposal state.

For the current `8.26` legacy project, the expected migration from its known analyze artifacts is:

```text
proposal (inferred from complete analyze evidence)
analyze  (imported + checkpointed)
-> resume at footage
```

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

## Next implementation slice

After the artifact bridge is proven against the real `8.26` project, connect stage execution adapters to the existing reference-replication tools so Moon can invoke deterministic stage work through `PipelineRunner` instead of only importing prior artifacts. Keep semantic decisions external and add MCP only after the CLI/JSON contract remains stable under a real resumed E2E run.
