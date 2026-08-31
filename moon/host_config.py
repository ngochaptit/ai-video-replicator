from __future__ import annotations
from pathlib import Path
from typing import Any

def local_mcp_config(project:str|Path)->dict[str,Any]:
    root=str(Path(project).resolve())
    return {"transport":"stdio","command":"python","args":["-m","moon","--project",root,"mcp-stdio"],"tools_prefix":"moon.","semantic_owner":"external_agent"}

def remote_tunnel_descriptor(project:str|Path)->dict[str,Any]:
    """Describe, but do not provision, a secure remote/tunnel wrapper.

    Moon intentionally keeps credentials and public exposure outside core. A product-specific
    adapter may tunnel the local MCP host while preserving the same seven tool contracts.
    """
    return {"local":local_mcp_config(project),"remote":{"provisioned":False,"requires_secure_tunnel":True,"requires_authentication":True,"note":"Use a supported MCP tunnel/app adapter; Moon never opens a public listener itself."}}
