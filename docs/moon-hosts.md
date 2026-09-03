# Moon universal local hosts

Moon v1.1.1 is one local runtime. Antigravity, Claude Desktop, Codex in VS Code,
and other MCP clients are clients of the same `MoonMCPServer`; host adapters only
describe detection, configuration, and packaging.

## Canonical MCP entrypoint

```powershell
python -m moon mcp --project "D:\path to\Moon project"
```

An installed OpenMontage package also exposes the equivalent `moon` console
command. The older `python -m moon --project <path> mcp-stdio` spelling remains
available for compatibility.

Moon reserves stdout for newline-delimited MCP JSON-RPC. Diagnostics and captured
child-process output go to stderr. Every host launches this same stdio server.

## Setup

Preview all integrations without writing configuration:

```powershell
python -m moon setup --project "D:\path to\Moon project"
```

Generate one host artifact:

```powershell
python -m moon setup --project "D:\path to\Moon project" --host antigravity --output moon-antigravity.json
python -m moon setup --project "D:\path to\Moon project" --host codex --output moon-codex.toml
python -m moon setup --project "D:\path to\Moon project" --host generic --output moon-mcp.json
```

Generated files are replaced atomically. Moon does not overwrite Codex TOML or
install Claude extensions automatically.

### Antigravity

Antigravity uses a JSON `mcpServers` entry. Its documented global config is
`~/.gemini/config/mcp_config.json`; workspace configuration is
`.agents/mcp_config.json`. Moon can merge only its own `mcpServers.moon` entry
while retaining other servers and top-level keys:

```powershell
python -m moon setup --project "D:\path to\Moon project" --host antigravity --write
python -m moon setup --project "D:\path to\Moon project" --host antigravity --write --scope workspace
```

Use `--config-path <temporary-or-explicit-path>` when the desired target differs.
See the [Antigravity MCP documentation](https://www.antigravity.google/docs/mcp).

### Claude Desktop

Build and install the existing managed-UV MCPB bundle:

```powershell
python scripts/build_mcpb.py
```

Install `dist/moon-local.mcpb` in Claude Desktop and select the project directory.
The MCPB launcher imports the bundled Moon runtime and calls the canonical `mcp`
CLI command. It retains native image evidence, sampled evidence, and MCP 0.2.5
transport behavior.

### Codex in VS Code

Moon generates a `[mcp_servers.moon]` TOML snippet for user-level
`~/.codex/config.toml` or trusted project-level `.codex/config.toml`. It does not
merge automatically because rewriting arbitrary TOML could discard comments or
unrelated settings. Add the generated block manually. See the
[official Codex configuration reference](https://developers.openai.com/codex/config-file/config-reference).

### Generic MCP clients

The generic JSON artifact contains `transport`, `command`, and `args`. Copy those
fields into any client that supports launching a local stdio MCP process.

## Doctor

Doctor is read-only. It checks Python, FFmpeg/ffprobe, Moon protocol and evidence
support, optional project state, and known host configuration:

```powershell
python -m moon doctor
python -m moon doctor --project "D:\path to\Moon project"
python -m moon doctor --project "D:\path to\Moon project" --json
```

`PASS` means available, `WARN` means optional setup or detection is incomplete,
and `FAIL` means Moon is not usable for the requested project. Warnings retain
exit code 0; blocking failures return non-zero.
