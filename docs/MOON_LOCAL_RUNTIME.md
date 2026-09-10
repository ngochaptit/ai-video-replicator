# Moon Local Runtime v1.1.1

Moon is a local, resumable, deterministic video-editing runtime. External agents provide semantic decisions; Moon owns local media access, state, validation, checkpoints, artifacts, and rendering orchestration.

## Scope

Moon Local deliberately does not introduce a web app, cloud database, Drive-backed state, account system, local LLM/VLM, or agent-specific business logic.

## Windows operator launcher

For a non-technical operator, double-click `START_AI_EDIT.bat` in the repository.
The launcher opens without a command workflow. Choose a project folder containing
`reference.mp4` and a non-empty `footage/` folder, then click **START AI EDIT**.
The large **CURRENT TASK** card shows the active stage and whether Moon or GPT
owns the next action. The seven Moon stages are shown below it with
operator-friendly status labels. A stage is only shown as **Running** while its
worker lock is live; a stopped resumable stage is shown as **Ready**. The launcher
uses the repository `.venv`, keeps Moon and Drive polling in a background worker,
and can be closed and reopened while that worker continues.

At every external semantic boundary, including analyze and footage, the launcher
shows **CẦN GPT PHÂN TÍCH**. It provides buttons to open ChatGPT, open the current
Drive `AGENT` folder, and copy one short project-aware instruction. The operator
does not run Moon commands, copy request IDs, or edit JSON. When
`output/final.mp4` exists and the pipeline is complete, the launcher offers
**OPEN FINAL VIDEO** and **OPEN OUTPUT FOLDER**.

Moon polls the existing Drive `AGENT` request/response contract and resumes
automatically after a valid response. Operator ownership comes from the canonical
active request route and is always Moon for local processing or GPT while waiting
for external semantic work. GPT reads `request.json` and the request-scoped JSON
and image evidence directly from Drive, then writes `response.json` there.

The ChatGPT destination can be configured by an administrator with
`MOON_OPERATOR_CHATGPT_URL`, or per project in `.moon/operator.json` using
`chatgpt_url`. The button only opens the configured site and does not automate a
browser session.

The install/admin setup must provide `.venv` and the existing per-project
`.moon/bridge.json` Drive configuration. This one-time machine/configuration work
is intentionally outside the operator screen. **Chi tiết kỹ thuật** opens a
separate resizable support window with Moon commands, request identity, stage
internals, stdout, and stderr. Its log view has a vertical scrollbar and does not
increase the height of the main operator screen. The main screen itself also
scrolls vertically when Windows resolution or DPI scaling leaves less room.

The operator architecture is deliberately narrow:

```text
LOCAL PROJECT (canonical media/state) <-> DRIVE EXCHANGE <-> GPT
```

Moon performs all deterministic local work. Drive contains only the active
request, bounded measured evidence, response, and archive history. GPT is the
single external semantic agent, reviewer, and semantic-QC owner. The launcher
does not use an LLM API, browser automation, or another local agent runtime.

## Persistent project contract

`.moon/` contains project/state JSON, checkpoints, artifacts, and cache. Stage order is `proposal -> analyze -> footage -> match -> timeline -> render -> qc`. A completed stage writes a durable checkpoint before state advances.

Analyze is hybrid: its first `run-stage` invokes `reference_blueprint_builder` on the project's `reference.mp4` with deep analysis and 2-second maximum analysis windows. Moon saves `reference_blueprint_scaffold`, `video_analysis_brief`, and measured keyframes. The analyze Drive handoff includes these JSON inputs and timestamped images, never the source video. Publishing fails if the evidence limits cannot accommodate image coverage for every reference window.

The analyze response payload is `semantic_enrichment`, not a recreated blueprint. Supply `segments` with scaffold `id` values and canonical semantic fields; timing and evidence are inherited. Optional refinements must use measured `start_seconds`, `end_seconds`, and `boundary_basis`. Source/evidence overrides are rejected. Moon applies `ReferenceBlueprintBuilder.apply_semantic_enrichment` to the saved scaffold, validates the canonical result, writes `reference_blueprint` with `semantic_enrichment_required=false`, and resumes to footage without reanalyzing the video.

With `local_sync`, publishing a different request ID or stage archives any previous root `response.json` under `AGENT/history/` before advertising the new request. Republishing the same active request preserves its response. Late responses for an older request remain rejected by request identity.

## Agent-neutral protocol

Core actions include `status`, `next`, `stage.plan`, `stage.run`, `handoff.package`, and `handoff.submit`. `next` runs deterministic work continuously until complete, blocked, or the next semantic boundary.

## CLI and handoff

```powershell
python -m moon --project "D:\AI EDIT VIDEO\8.26" status
python -m moon --project "D:\AI EDIT VIDEO\8.26" next
python -m moon --project "D:\AI EDIT VIDEO\8.26" handoff
python -m moon --project "D:\AI EDIT VIDEO\8.26" connector-manifest
python -m moon mcp --project "D:\AI EDIT VIDEO\8.26"
python -m moon setup --project "D:\AI EDIT VIDEO\8.26"
python -m moon doctor --project "D:\AI EDIT VIDEO\8.26"
```

At a semantic boundary Moon packages current state, deterministic input artifacts with hashes, local evidence paths, output validation rules, submission commands, and a deterministic handoff ID. Moon rejects stale-stage responses and validates required semantic structure before persistence.

## Google Drive agent bridge

The Drive bridge lets a web agent exchange one bounded handoff packet with Moon without becoming the local runtime agent. Local packets use:

```text
<project>/AGENT/
  request.json
  response.json
  evidence/<request_id>/...
```

Moon also keeps a compact local routing cache at `<project>/.moon/agent-state.json`.
The cache is Moon-owned and is reconstructed from the canonical pipeline and
bridge artifacts if it is missing, invalid, or stale; it is not a second source
of pipeline truth. The same state is published once as `request.json.route`, so
a fresh web chat can identify its actor, exact task and inputs, expected output,
terminal acknowledgement, and next actor without relying on prior chat history.

The Drive transport writes only `request.json` plus request-scoped JSON, text,
and image evidence under `MON_EDIT/jobs/<project_id>/AGENT/`. It does not generate
or publish a portable PDF. Source video, source audio, rendered video, and final
output extensions are not eligible for copying or upload. A returned `payload`
is untrusted input: Moon checks its envelope, age, job/request/stage/revision
identity, JSON schema, duplicate-consumption state, and the existing Moon handoff
contract before storing it. No response field is interpreted as a shell command.

### Google OAuth setup

1. Install the bridge clients with `python -m pip install -r requirements.txt`.
2. In Google Cloud Console, create or select a project, enable **Google Drive API**, then configure **Google Auth Platform**. For a personal account choose an External audience and add your Google account as a test user; a Workspace administrator can choose Internal.
3. Under **Google Auth Platform > Clients**, create an OAuth client with application type **Desktop app** and download its JSON. Moon requests the `https://www.googleapis.com/auth/drive` scope because this headless CLI uses a configured folder ID rather than Google Picker. Keep the app in testing/internal use unless you complete any Google verification required for broader distribution.
4. Create a Drive folder named `MON_EDIT`. Copy its folder ID from the URL (`https://drive.google.com/drive/folders/<folder-id>`).
5. Store the downloaded client JSON and generated token outside both the repository and Moon project, for example:

```powershell
New-Item -ItemType Directory -Force "$env:APPDATA\Moon\google-drive"
Move-Item .\client_secret_*.json "$env:APPDATA\Moon\google-drive\client-secret.json"
```

6. Create `<project>/.moon/bridge.json` (this path is gitignored by this repository):

```json
{
  "project_id": "my-edit-job",
  "transport": "google_drive_api",
  "poll_interval_seconds": 10,
  "stale_after_seconds": 86400,
  "drive": {
    "root_folder_id": "PASTE_MON_EDIT_FOLDER_ID",
    "credentials_path": "%APPDATA%\\Moon\\google-drive\\client-secret.json",
    "token_path": "%APPDATA%\\Moon\\google-drive\\token.json"
  }
}
```

The first API command opens the browser once for consent and atomically stores the refresh token at `token_path`. `MOON_DRIVE_CREDENTIALS`, `MOON_DRIVE_TOKEN`, and `MOON_DRIVE_ROOT_FOLDER_ID` may override the corresponding values without putting machine-specific paths in the config.

Service-account JSON is also accepted in `credentials_path`; share the `MON_EDIT` folder directly with that service account before use. For Google Drive for Desktop, use the optional local transport instead:

```json
{
  "project_id": "my-edit-job",
  "transport": "local_sync",
  "poll_interval_seconds": 10,
  "drive": { "sync_root": "G:\\My Drive" }
}
```

Here `sync_root` is the directory containing `MON_EDIT`; Drive for Desktop is optional and never auto-detected.

### Commands and response contract

At an existing semantic boundary, publish and wait with:

```powershell
python -m moon bridge publish "D:\path\to\moon-project" footage
python -m moon bridge watch "D:\path\to\moon-project"
python -m moon bridge status "D:\path\to\moon-project"
```

`request.json` contains the exact `job_id`, `request_id`, stage, revision,
timestamps, evidence references, expected response schema, and canonical `route`
block. GPT creates `response.json` in the same Drive `AGENT` folder for every
external-agent stage, copying the identity values exactly and placing its stage
response under `payload`:

```json
{
  "version": "1.0",
  "job_id": "my-edit-job",
  "request_id": "COPY_FROM_REQUEST",
  "stage": "footage",
  "revision": 0,
  "status": "COMPLETED",
  "created_at": "2026-09-05T12:00:00Z",
  "payload": { "clips": [] },
  "review": { "actor": "gpt", "decision": "APPROVED", "revision": 0 }
}
```

The payload shape above is illustrative; the authoritative requirements are embedded in `request.json`. After successful validation Moon marks both files `CONSUMED`, records an idempotency marker under `.moon/`, and resumes to the next safe boundary. A restart retries only a pending resume and never resubmits an already consumed response.

The analyze and footage stages use the same direct GPT route as proposal, match,
timeline, render, and QC. GPT reads the listed evidence from Drive, performs the
semantic work and review, then writes `response.json` with structured review
data. Canonical project media and pipeline artifacts remain in the local project;
Drive is only the message and measured-evidence exchange.

```json
{
  "version": "1.0",
  "job_id": "my-edit-job",
  "request_id": "COPY_FROM_REQUEST",
  "stage": "analyze",
  "revision": 0,
  "status": "COMPLETED",
  "created_at": "2026-09-05T12:00:00Z",
  "payload": { "segments": [] },
  "review": { "actor": "gpt", "decision": "APPROVED", "revision": 0 }
}
```

`APPROVED` routes the response to Moon for consumption. `REVISION_REQUIRED`
must include a non-empty `revision_targets` array whose entries each contain a
`segment_id` and `reason`. Moon keeps the same job, stage, and request identity,
increments the handoff revision, republishes the explicit targets and measured
evidence, and waits for GPT again.

A pending request is idempotently republished only while its identity matches
the active bridge request and its expiry is still in the future. Publishing the
same pending stage after expiry archives any stale `response.json`, issues a new
request ID and timestamps, rebuilds the route and local agent state, and retains
the current revision. Responses carrying the replaced request ID remain invalid.

Existing `.moon/bridge.json` files need no manual migration. On the first publish,
a still-pending legacy Gemini route is archived/replaced with a fresh GPT request
and identity; a matching fresh GPT request remains idempotent. The reusable
defaults allow up to 500 evidence files and 512 MiB per request, while response
payloads retain a 50 MiB safety ceiling. Per-project configuration can still set
stricter limits.

Minimal credentials/connectivity test (it creates `jobs/<project_id>/AGENT` if absent but does not run video work):

```powershell
python -m moon bridge status "D:\path\to\moon-project"
```

Google's current setup references are the [Drive Python quickstart](https://developers.google.com/workspace/drive/api/quickstart/python) and [Drive scope guide](https://developers.google.com/workspace/drive/api/guides/api-specific-auth).

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

Start the canonical host-neutral server for one project:

```powershell
python -m moon mcp --project "D:\AI EDIT VIDEO\8.26"
```

The older `python -m moon --project <path> mcp-stdio` form remains compatible.

An MCP-capable local host can launch that command as a stdio server. The adapter implements the MCP initialization handshake, `ping`, `tools/list`, and `tools/call`. Tool calls are delegated directly to `AgentConnectorService`; tool failures are returned as MCP tool errors rather than mutating Moon semantics.

Conceptual MCP host configuration:

```json
{
  "command": "python",
  "args": [
    "-m", "moon",
    "mcp", "--project", "D:\\AI EDIT VIDEO\\8.26"
  ]
}
```

The adapter advertises JSON Schema for every Moon tool, including required fields for evidence reads, frame sampling, and semantic submission. Local media remains local: MCP only exposes paths/JSON and deterministic frame samples through the same connector contract.

Host detection, setup, and diagnostics are documented in
[`docs/moon-hosts.md`](moon-hosts.md). Host profiles contain no editing logic.

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

## Adaptive footage evidence (Moon v1.2 quality track)

The `footage` stage now seeds deterministic full-clip frame coverage before asking an external vision agent for semantic segmentation. The default target is roughly one measured frame every 4 seconds, bounded to 120 initial frames per clip and chunked into FFmpeg sampling groups of at most 24 frames.

This is evidence generation only; Moon still does not decide what an action means
or where a semantic action starts. GPT scans the coarse coverage directly from
Drive. If a boundary remains ambiguous, GPT returns a strict
`footage_refinement_request` with measured `clip_id`, `start_seconds`,
`end_seconds`, and `reason` values. Moon runs the deterministic sampler, appends
the new measured frames, advances the handoff revision, and republishes the route
and evidence for `RECHECK_TARGETS`. Registered sampled frames are automatically
merged into the `footage_profile_builder` evidence catalog on the enrichment pass,
so those refined timestamps can become canonical segment boundaries.

The quality goal is to avoid the failure mode where a long single-take clip with few hard scene cuts is reduced to a handful of 60–90 second semantic segments, which later forces extreme speed-up and source reuse during matching/rendering.
