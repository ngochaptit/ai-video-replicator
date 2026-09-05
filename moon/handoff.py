from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from jsonschema import ValidationError
from schemas.artifacts import load_schema, validate_artifact

from moon.runner.pipeline import PipelineRunner
from moon.evidence import SampledFrameEvidenceStore
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
        self._validate(stage, payload)
        if stage == "proposal" and payload["approval"]["status"] not in {"approved", "approved_with_changes"}:
            raise ValueError("proposal handoff requires recorded user approval before consumption")
        if stage in {"footage", "match"} and self._semantic_artifacts_ready(stage):
            validate_semantic_submission(self.runner, stage, payload)
        artifact = self._required_artifact(stage); path = self.runner.artifacts.write(artifact, payload)
        return {"accepted":True,"stage":stage,"artifact":artifact,"path":str(path),"next_action":"next"}

    def _semantic_artifacts_ready(self, stage: str) -> bool:
        required = {
            "footage": ("footage_profiles_scaffold",),
            "match": ("reference_blueprint", "footage_profiles"),
        }.get(stage, ())
        return all(self.runner.artifacts.exists(name) for name in required)

    def _inputs(self, stage: str) -> dict[str, Any]:
        names={"footage":["footage_profiles_scaffold"],"match":["reference_blueprint","footage_profiles","candidate_rankings"],"render":["timeline"],"qc":["draft_render","timeline","match_decisions","replication_quality_report"]}.get(stage,[]); result={}
        if stage == "proposal": names = ["research_brief", "brief"]
        if stage == "analyze": names = ["reference_blueprint_scaffold", "video_analysis_brief", "proposal_packet"]
        for name in names:
            if self.runner.artifacts.exists(name):
                path=self.runner.artifacts.path_for(name); result[name]={"path":str(path),"sha256":self._sha256(path)}
        task=self.runner.artifacts.read(f"{stage}_agent_task"); evidence_root=task.get("evidence_root")
        sampled_store=SampledFrameEvidenceStore(self.runner.project,self.runner.state.revision); sampled=sampled_store.exported(stage)
        files=[]
        if evidence_root:
            root=Path(evidence_root); files=[str(p) for p in sorted(root.rglob("*")) if p.is_file()][:500] if root.is_dir() else []
        else: root=self.runner.project.evidence_dir
        reference_frames = []
        if stage == "analyze" and self.runner.artifacts.exists("reference_blueprint_scaffold"):
            for segment in self.runner.artifacts.read("reference_blueprint_scaffold")["segments"]:
                evidence = segment["evidence"]
                reference_frames.extend({"path": path, "timestamp_seconds": timestamp}
                    for path, timestamp in zip(evidence["frame_paths"], evidence["frame_timestamps"]))
            files = list(dict.fromkeys(frame["path"] for frame in reference_frames))
        if sampled["groups"]:
            files.append(sampled["registry_path"])
            files.extend(str(frame["absolute_path"]) for group in sampled["groups"] for frame in group.get("frames") or [])
        if evidence_root or sampled["groups"]:
            result["evidence"]={"root":str(root),"sampled_root":str(self.runner.project.evidence_dir),"files":list(dict.fromkeys(files)),"sampled_frames":sampled}
            if reference_frames: result["evidence"]["reference_frames"] = reference_frames
        return result

    @staticmethod
    def _output_contract(stage: str) -> dict[str, Any]:
        if stage == "analyze":
            from moon.reference_analysis import enrichment_contract
            return enrichment_contract()
        if stage == "proposal":
            return {**load_schema("proposal_packet"), "artifact": "proposal_packet",
                    "rules": ["Record user approval before submitting the completed proposal."]}
        contracts={
            "footage":{"artifact":"footage_semantic_enrichment","required":["clips"],"rules":["clips must exactly cover measured clip_id values from footage_profiles_scaffold","unusable clips may set usable=false and omit segments","usable clips require measured non-overlapping segments","each segment requires source_in, source_out, boundary_basis and in-range frame evidence","semantic/camera/spatial/motion/quality/confidence fields may be supplied by the external vision agent","adaptive sampled frames are measured evidence and may be used as source_in/source_out boundaries","for long clips, inspect coarse coverage first and call moon.frames.sample on narrower windows before finalizing ambiguous action boundaries"]},
            "match":{"artifact":"match_proposal","required":["matches"],"rules":["exactly one match per reference blueprint segment","footage_segment_id must exist in enriched footage profiles","scores require action, interaction, camera, spatial, motion, duration, overall in [0,1] (non-overall may be null)","fallback requires an improvement_request for that reference segment","rationale must be non-empty"]},
            "render":{"artifact":"render_plan","required":["runtime_approved","render_runtime"],"rules":["runtime_approved must be true","render_runtime must be ffmpeg, remotion, or hyperframes"]},
            "qc":{"artifact":"qc_bundle","required":["qc_report","decision_log"],"rules":["qc_report and decision_log must be objects","qc_report.decision may be pass, revise, or footage_limited","pass cannot contradict deterministic replication_quality_report quality_gate=fail","render integrity failure requires revise","fixable failures rewind to the earliest actionable match, timeline, or render stage","source-limited quality failure must not trigger an engine revision loop"]}}
        if stage not in contracts: raise ValueError(f"stage {stage!r} does not expose an agent handoff contract")
        return {"type":"object",**contracts[stage]}

    def _validate(self, stage: str, payload: dict[str, Any]) -> None:
        if not isinstance(payload,dict): raise ValueError("handoff response must be a JSON object")
        if stage == "analyze":
            from moon.reference_analysis import enrich_reference
            if not self.runner.artifacts.exists("reference_blueprint_scaffold"):
                raise ValueError("run analyze first; missing measured reference_blueprint_scaffold")
            enrich_reference(self.runner.artifacts.read("reference_blueprint_scaffold"), payload)
        elif stage == "proposal":
            artifact = self._required_artifact(stage)
            try:
                validate_artifact(artifact, payload)
            except ValidationError as exc:
                raise ValueError(f"{stage} handoff requires valid {artifact}: {exc.message}") from exc
        elif stage=="footage":
            clips=payload.get("clips")
            if not isinstance(clips,list) or not clips: raise ValueError("footage handoff requires non-empty clips[]")
            for clip in clips:
                if not isinstance(clip,dict): raise ValueError("footage clips must be objects")
                if "clip_id" not in clip and not str(clip.get("path") or "").strip(): raise ValueError("each footage clip requires clip_id or path")
                segments=clip.get("segments")
                if clip.get("usable",True) is False: continue
                if segments is not None:
                    if not isinstance(segments,list) or not segments: raise ValueError("usable footage clips with segments must use non-empty segments[]")
                    for segment in segments:
                        if not isinstance(segment,dict): raise ValueError("footage segments must be objects")
                        start,end=segment.get("source_in"),segment.get("source_out")
                        if isinstance(start,bool) or isinstance(end,bool) or not isinstance(start,(int,float)) or not isinstance(end,(int,float)) or start < 0 or end <= start: raise ValueError("each footage segment requires valid source_in/source_out")
        elif stage=="match":
            matches=payload.get("matches")
            if not isinstance(matches,list) or not matches: raise ValueError("match handoff requires non-empty matches[]")
            for item in matches:
                if not isinstance(item,dict): raise ValueError("matches must contain objects")
                for field in ("reference_segment_id","footage_segment_id","rationale"):
                    if not str(item.get(field) or "").strip(): raise ValueError(f"each match requires {field}")
                if item.get("match_class") not in {"good","acceptable","fallback"}: raise ValueError("match_class must be good, acceptable, or fallback")
                if not isinstance(item.get("scores"),dict): raise ValueError("each match requires scores object")
        elif stage=="render":
            if payload.get("runtime_approved") is not True: raise ValueError("render handoff requires runtime_approved=true")
            if payload.get("render_runtime") not in {"ffmpeg","remotion","hyperframes"}: raise ValueError("invalid render_runtime")
        elif stage=="qc":
            if not isinstance(payload.get("qc_report"),dict) or not isinstance(payload.get("decision_log"),dict): raise ValueError("qc handoff requires qc_report and decision_log objects")
            decision=payload["qc_report"].get("decision",payload["qc_report"].get("verdict"))
            if decision not in {"pass","revise","footage_limited"}: raise ValueError("qc_report.decision must be pass, revise, or footage_limited")
            if self.runner.artifacts.exists("replication_quality_report"):
                quality=self.runner.artifacts.read("replication_quality_report");integrity=str((quality.get("render_integrity") or {}).get("status") or "fail");gate=str(quality.get("quality_gate") or "fail");source_limited=bool((quality.get("replication_quality") or {}).get("source_limited"))
                if integrity!="pass" and decision!="revise":raise ValueError("render integrity failure requires qc_report.decision=revise")
                if gate=="fail" and decision=="pass":raise ValueError("qc_report.decision=pass contradicts deterministic replication quality failure")
                if source_limited and decision=="revise":raise ValueError("source-limited quality failure cannot be fixed by a Moon engine revision; use footage_limited or provide better footage")
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
