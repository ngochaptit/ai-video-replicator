from pathlib import Path
import pytest
from moon.core.project import MoonProject
from moon.handoff import AgentHandoffService
from moon.host_config import local_mcp_config,remote_tunnel_descriptor
from moon.runner.pipeline import PipelineRunner
from moon.workspace import MoonWorkspace

def runner(tmp_path):
    r=PipelineRunner(MoonProject.open(tmp_path,create=True));r.complete("proposal",{});r.complete("analyze",{});return r

def test_exact_footage_contract_uses_clip_ids_and_measured_boundaries(tmp_path):
    r=runner(tmp_path);r.artifacts.write("footage_profiles_scaffold",{"clips":[{"clip_id":"clip_001","path":"x.mp4","duration_seconds":2.0,"segments":[{"source_in":0.0,"boundary_basis":["scene_cut"],"evidence":{"frame_timestamps":[1.0]}}]}]})
    good={"clips":[{"clip_id":"clip_001","path":"x.mp4","segments":[{"source_in":0.0,"source_out":2.0,"boundary_basis":["scene_cut","video_end"]}]}]}
    assert AgentHandoffService(r).submit("footage",good)["accepted"]
    with pytest.raises(ValueError):AgentHandoffService(r).submit("footage",{"clips":[{"clip_id":"wrong","segments":[]}]})

def test_revision_reset_keeps_upstream_and_increments(tmp_path):
    r=runner(tmp_path);r.complete("footage",{});r.complete("match",{});r.complete("timeline",{});r.complete("render",{})
    status=r.request_revision(from_stage="render",reason="qc")
    assert status["revision"]==1 and status["next_stage"]=="render" and "timeline" in status["completed"] and "render" not in status["completed"]

def test_workspace_registry_and_presets(tmp_path):
    ws=MoonWorkspace(tmp_path);item=ws.register("8.26",tmp_path/"8.26")
    assert item["preset"]=="reference-replication" and ws.resolve("8.26")["settings"]["max_revisions"]==2

def test_transport_descriptors_never_claim_public_endpoint(tmp_path):
    local=local_mcp_config(tmp_path);remote=remote_tunnel_descriptor(tmp_path)
    assert local["transport"]=="stdio" and local["args"][-1]=="mcp-stdio"
    assert remote["remote"]["provisioned"] is False and remote["remote"]["requires_authentication"] is True
