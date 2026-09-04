# Moon Local for Claude Desktop

This MCPB extension launches the existing Moon stdio server for one local,
initialized Moon project. It does not contain a second MCP implementation and
does not add an AI provider or network service.

The bundle includes the existing `moon`, `tools`, and `lib` Python packages
needed by validated submissions and subsequent deterministic pipeline stages.

## Build

From the repository root:

```powershell
python scripts/build_mcpb.py
```

The output is `dist/moon-local.mcpb`.

## Install

1. Open Claude Desktop.
2. Open **Settings** and select **Extensions**.
3. Remove the older Moon Local extension if it is installed.
4. Drag `dist/moon-local.mcpb` into the Extensions window.
5. Confirm installation and choose the initialized Moon project directory when
   prompted. Moon does not bind the bundle to a development project.
6. Enable or restart the extension if Claude Desktop prompts you to do so.

The selected directory is passed directly to the launcher with `--project`.
`MOON_PROJECT_ROOT` remains available as a fallback for direct invocation; one
of it or `--project` is required.

## Runtime requirements

- Claude Desktop with MCPB manifest 0.4 managed-UV support. UV provides Python
  3.10 or newer for the extension.
- FFmpeg on `PATH` when using `moon.frames.sample`.
- An initialized project containing `.moon/state.json`.

Claude starts the bundle with its installed extension directory as the working
directory and runs `uv run launcher.py`. The manifest intentionally does not
pass `${__dirname}` to the child process because Microsoft Store/AppX builds of
Claude can virtualize that absolute path differently for Claude and for an
external runtime.

The launcher delegates to `python -m moon mcp --project <path>` semantics inside
the bundle. All diagnostics are written to stderr. Standard output is reserved
for MCP JSON-RPC messages from `moon.mcp_adapter`.
