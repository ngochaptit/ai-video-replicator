# Moon Local Runtime v1.1.1

Moon is a local, resumable, deterministic video-editing runtime. External agents provide semantic decisions; Moon owns local media access, state, validation, checkpoints, artifacts, and rendering orchestration.

## Scope

Moon Local deliberately does not introduce a web app, cloud database, Drive-backed state, account system, local LLM/VLM, or agent-specific business logic.

```text
Antigravity / Claude Desktop / Codex / generic MCP client
                         |
               one Moon MCP gateway
                         |
       state + evidence + pipeline + local media I/O
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

The Drive API transport writes only those request-scoped JSON, text, and image evidence files under `MON_EDIT/jobs/<project_id>/AGENT/`. Source video and audio extensions are not eligible for copying or upload. A returned `payload` is untrusted input: Moon checks its envelope, age, job/request/stage identity, duplicate-consumption state, and the existing Moon handoff contract before storing it. No response field is interpreted as a shell command.

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

`request.json` contains the exact `job_id`, `request_id`, stage, timestamps, evidence references, and expected response schema. The web agent must create `response.json` in the same Drive `AGENT` folder, copying the identity values exactly and placing its stage response under `payload`:

```json
{
  "version": "1.0",
  "job_id": "my-edit-job",
  "request_id": "COPY_FROM_REQUEST",
  "stage": "footage",
  "status": "COMPLETED",
  "created_at": "2026-09-05T12:00:00Z",
  "payload": { "clips": [] }
}
```

The payload shape above is illustrative; the authoritative requirements are embedded in `request.json`. After successful validation Moon marks both files `CONSUMED`, records an idempotency marker under `.moon/`, and resumes to the next safe boundary. A restart retries only a pending resume and never resubmits an already consumed response.

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

This is evidence generation only; Moon still does not decide what an action means or where a semantic action starts. The external agent scans coarse coverage, requests denser `moon.frames.sample` windows around suspected action/interaction changes, and submits measured action segments. Registered sampled frames are automatically merged into the `footage_profile_builder` evidence catalog on the enrichment pass, so those refined timestamps can become canonical segment boundaries.

The quality goal is to avoid the failure mode where a long single-take clip with few hard scene cuts is reduced to a handful of 60–90 second semantic segments, which later forces extreme speed-up and source reuse during matching/rendering.
