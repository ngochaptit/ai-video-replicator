from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from moon.runner.pipeline import PipelineRunner
from moon.semantic_contracts import validate_semantic_submission

HANDOFF_VERSION = "2.0"


class AgentHandoffService:
    def __init__(self, runner: PipelineRunner) -> None: self.runner = runner

    def package(self, stage: str | None = None) -> dict[str, Any]:
        stage = stage or self.runner.state.next_stage()
        if stage is None: return {"done": True, "stage": None}
        if stage != self.runner.state.next_stage(): raise ValueError(f"handoff may only package current stage {self.runner.state.next_stage()!r}")
        task_name = f"{stage}_agent_task"
        if not self.runner.artifacts.exists(task_name): raise FileNotFoundError(f"run-stage first; missing handoff task artifact {task_name!r}")
        project = self.runner.project.root
        package = {"version": HANDOFF_VERSION, "stage": stage, "decision_owner": "external_agent", "project_root": str(project), "pipeline": self.runner.status(), "task": self.runner.artifacts.read(task_name), "inputs": self._inputs(stage), "output_contract": self._output_contract(stage), "submission": {"preferred":"connector_or_mcp","stdin_command":f'python -m moon --project "{project}" submit-handoff-stdin {stage}',"bridge_command":f'python -m moon --project "{project}" agent-bridge',"then":f'python -m moon --project "{project}" next'}}
        package["handoff_id"] = self._handoff_id(package); self.runner.artifacts.write(f"{stage}_handoff", package); return package

    def submit(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.runner.state.next_stage()
        if stage != current: raise ValueError(f"handoff submission is for {stage!r}, current stage is {current!r}")
        self._validate(stage, payload); validate_semantic_submission(self.runner, stage, payload)
        artifact = self._required_artifact(stage); path = self.runner.artifacts.write(artifact, payload)
        return {"accepted":True,"stage":stage,"artifact":artifact,"path":str(path),"next_action":"next"}

    def _inputs(self, stage: str) -> dict[str, Any]:
        names={"footage":["footage_profiles_scaffold"],"match":["reference_blueprint","footage_profiles","candidate_rankings"],"render":["timeline"],"qc":["draft_render"]}.get(stage,[]); result={}
        for name in names:
            if self.runner.artifacts.exists(name):
                path=self.runner.artifacts.path_for(name); result[name]={"path":str(path),"sha256":self._sha256(path)}
        task=self.runner.artifacts.read(f"{stage}_agent_task"); evidence_root=task.get("evidence_root")
        if evidence_root:
            root=Path(evidence_root); files=[str(p) for p in sorted(root.rglob("*")) if p.is_file()][:500] if root.is_dir() else []; result["evidence"]={"root":str(root),"files":files}
        return result

    @staticmethod
    def _output_contract(stage: str) -> dict[str, Any]:
        contracts={
            "footage":{"artifact":"footage_semantic_enrichment","required":["clips"],"rules":["clips must exactly cover measured clip_id values from footage_profiles_scaffold","unusable clips may set usable=false and omit segments","usable clips require measured non-overlapping segments","each segment requires source_in, source_out, boundary_basis and in-range frame evidence","semantic/camera/spatial/motion/quality/confidence fields may be supplied by the external vision agent"]},
            "match":{"artifact":"match_proposal","required":["matches"],"rules":["exactly one match per reference blueprint segment","footage_segment_id must exist in enriched footage profiles","scores require action, interaction, camera, spatial, motion, duration, overall in [0,1] (non-overall may be null)","fallback requires an improvement_request for that reference segment","rationale must be non-empty"]},
            "render":{"artifact":"render_plan","required":["runtime_approved","render_runtime"],"rules":["runtime_approved must be true","render_runtime must be ffmpeg, remotion, or hyperframes"]},
            "qc":{"artifact":"qc_bundle","required":["qc_report","decision_log"],"rules":["qc_report and decision_log must be objects","qc_report.decision may be pass or revise; revise triggers bounded render revision in Moon v1"]}}
        if stage not in contracts: raise ValueError(f"stage {stage!r} does not expose an agent handoff contract")
        return {"type":"object",**contracts[stage]}

    def _validate(self, stage: str, payload: dict[str, Any]) -> None:
        if not isinstance(payload,dict): raise ValueError("handoff response must be a JSON object")
        if stage=="footage":
            if not isinstance(payload.get("clips"),list) or not payload["clips"]: raise ValueError("footage handoff requires non-empty clips[]")
        elif stage=="match":
            if not isinstance(payload.get("matches"),list) or not payload["matches"]: raise ValueError("match handoff requires non-empty matches[]")
        elif stage=="render":
            if payload.get("runtime_approved") is not True: raise ValueError("render handoff requires runtime_approved=true")
            if payload.get("render_runtime") not in {"ffmpeg","remotion","hyperframes"}: raise ValueError("invalid render_runtime")
        elif stage=="qc":
            if not isinstance(payload.get("qc_report"),dict) or not isinstance(payload.get("decision_log"),dict): raise ValueError("qc handoff requires qc_report and decision_log objects")
            decision=payload["qc_report"].get("decision")
            if decision is not None and decision not in {"pass","revise"}: raise ValueError("qc_report.decision must be pass or revise")
        else: raise ValueError(f"stage {stage!r} does not accept an agent handoff submission")

    def _required_artifact(self, stage: str) -> str: return "qc_bundle" if stage=="qc" else str(self._output_contract(stage)["artifact"])
    @staticmethod
    def _sha256(path: Path) -> str:
        digest=hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda:handle.read(1024*1024),b""): digest.update(chunk)
        return digest.hexdigest()
    @staticmethod
    def _handoff_id(package: dict[str,Any])->str:
        canonical=json.dumps(package,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode("utf-8"); return hashlib.sha256(canonical).hexdigest()[:16]
