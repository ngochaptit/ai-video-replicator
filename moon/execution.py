from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from moon.runner.pipeline import PipelineRunner


class StageExecutionService:
    """Run deterministic reference-replication work while leaving semantics to agents."""
    def __init__(self, runner: PipelineRunner) -> None: self.runner = runner
    def plan(self) -> dict[str, Any]:
        stage = self.runner.state.next_stage()
        if stage is None: return {"stage": None, "done": True, "mode": "complete"}
        plans = {"proposal":("agent","proposal_packet"),"analyze":("agent","reference_blueprint + semantic_enrichment"),"footage":("hybrid","footage_semantic_enrichment"),"match":("hybrid","match_proposal"),"timeline":("deterministic",None),"render":("hybrid","render_plan"),"qc":("agent","qc_report + decision_log")}
        mode, required = plans[stage]
        return {"stage":stage,"done":False,"mode":mode,"required_agent_artifact":required}
    def run(self) -> dict[str, Any]:
        stage=self.runner.state.next_stage()
        if stage is None: return {"status":"complete","pipeline":self.runner.status()}
        fn={"footage":self._run_footage,"match":self._run_match,"timeline":self._run_timeline,"render":self._run_render,"qc":self._run_qc}.get(stage)
        return fn() if fn else {"status":"awaiting_agent","stage":stage,"required_agent_artifact":self.plan()["required_agent_artifact"],"pipeline":self.runner.status()}
    def _run_footage(self)->dict[str,Any]:
        self.runner.begin("footage"); output_dir=self.runner.project.root/"analysis"/"footage"
        inputs={"footage_dir":str(self.runner.project.root/"footage"),"analysis_depth":"deep","max_keyframes_per_file":30,"max_analysis_window_seconds":2.0,"output_dir":str(output_dir)}
        enrichment=None
        if self.runner.artifacts.exists("footage_semantic_enrichment"):
            enrichment=self._materialize_artifact("footage_semantic_enrichment"); inputs["semantic_enrichment_path"]=str(enrichment)
        result=self._execute_tool("footage_profile_builder",inputs)
        if not result["success"]: self.runner.fail("footage",result["error"]); return {"status":"blocked","stage":"footage",**result}
        if not enrichment:
            self.runner.artifacts.write("footage_profiles_scaffold",result["data"])
            task={"stage":"footage","decision_owner":"external_agent","required_output_artifact":"footage_semantic_enrichment","evidence_root":str(output_dir),"instruction":"Inspect measured evidence and submit usable semantic action segments with measured boundaries."}
            self.runner.artifacts.write("footage_agent_task",task); return {"status":"awaiting_agent","stage":"footage","task":task,"pipeline":self.runner.status()}
        self.runner.artifacts.write("footage_profiles",result["data"])
        cp={"source":"stage_execution_adapter","tool":"footage_profile_builder","artifacts":{"footage_profiles":str(self.runner.artifacts.path_for("footage_profiles"))}}
        return {"status":"completed","stage":"footage","pipeline":self.runner.complete("footage",cp)}
    def _run_match(self)->dict[str,Any]:
        self.runner.begin("match"); blueprint=self._require_artifact("reference_blueprint"); profiles=self._require_artifact("footage_profiles"); rankings=self.runner.project.cache_dir/"candidate_rankings.json"
        rank=self._execute_tool("reference_candidate_ranker",{"reference_blueprint_path":str(blueprint),"footage_profiles_path":str(profiles),"top_k":10,"output_path":str(rankings)})
        if not rank["success"]: self.runner.fail("match",rank["error"]); return {"status":"blocked","stage":"match",**rank}
        self.runner.artifacts.write("candidate_rankings",rank["data"])
        if not self.runner.artifacts.exists("match_proposal"):
            task={"stage":"match","decision_owner":"external_agent","required_output_artifact":"match_proposal","candidate_rankings":str(self.runner.artifacts.path_for("candidate_rankings")),"instruction":"Choose one real footage segment for every reference segment; use explicit fallback when needed."}
            self.runner.artifacts.write("match_agent_task",task); return {"status":"awaiting_agent","stage":"match","task":task,"pipeline":self.runner.status()}
        proposal=self._materialize_artifact("match_proposal"); out=self.runner.project.root/"analysis"/"matching.json"
        val=self._execute_tool("reference_match_validator",{"reference_blueprint_path":str(blueprint),"footage_profiles_path":str(profiles),"proposal_path":str(proposal),"output_path":str(out)})
        if not val["success"]: self.runner.fail("match",val["error"]); return {"status":"blocked","stage":"match",**val}
        self.runner.artifacts.write("match_decisions",val["data"]); cp={"source":"stage_execution_adapter","tool":"reference_match_validator","artifacts":{"match_decisions":str(self.runner.artifacts.path_for("match_decisions"))}}
        return {"status":"completed","stage":"match","pipeline":self.runner.complete("match",cp)}
    def _run_timeline(self)->dict[str,Any]:
        self.runner.begin("timeline"); blueprint=self._require_artifact("reference_blueprint"); matching=self._require_artifact("match_decisions"); out=self.runner.project.root/"analysis"/"replication_timeline.json"
        result=self._execute_tool("reference_timeline_builder",{"reference_blueprint_path":str(blueprint),"reference_matching_path":str(matching),"output_path":str(out)})
        if not result["success"]: self.runner.fail("timeline",result["error"]); return {"status":"blocked","stage":"timeline",**result}
        self.runner.artifacts.write("timeline",result["data"]); cp={"source":"stage_execution_adapter","tool":"reference_timeline_builder","artifacts":{"timeline":str(self.runner.artifacts.path_for("timeline"))}}
        return {"status":"completed","stage":"timeline","pipeline":self.runner.complete("timeline",cp)}
    def _run_render(self)->dict[str,Any]:
        self.runner.begin("render")
        if not self.runner.artifacts.exists("render_plan"):
            task={"stage":"render","decision_owner":"external_agent/user","required_output_artifact":"render_plan","instruction":"Submit an approved render plan with runtime_approved=true. Moon will never choose or switch render runtime silently."}; self.runner.artifacts.write("render_agent_task",task); return {"status":"awaiting_agent","stage":"render","task":task,"pipeline":self.runner.status()}
        plan=self._materialize_artifact("render_plan"); out=self.runner.project.root/"output"/"draft.mp4"; result=self._execute_tool("reference_video_renderer",{"render_plan_path":str(plan),"output_path":str(out)})
        if not result["success"]: self.runner.fail("render",result["error"]); return {"status":"blocked","stage":"render",**result}
        self.runner.artifacts.write("draft_render",{"output":str(out),"tool_result":result["data"]}); cp={"source":"stage_execution_adapter","tool":"reference_video_renderer","artifacts":{"draft_render":str(self.runner.artifacts.path_for("draft_render"))}}
        return {"status":"completed","stage":"render","pipeline":self.runner.complete("render",cp)}
    def _run_qc(self)->dict[str,Any]:
        self.runner.begin("qc")
        if self.runner.artifacts.exists("qc_bundle"):
            bundle=self.runner.artifacts.read("qc_bundle"); self.runner.artifacts.write("qc_report",bundle["qc_report"]); self.runner.artifacts.write("decision_log",bundle["decision_log"])
        missing=[n for n in ("qc_report","decision_log") if not self.runner.artifacts.exists(n)]
        if missing:
            task={"stage":"qc","decision_owner":"external_agent","required_output_artifacts":missing,"instruction":"Review the rendered draft semantically and submit QC plus decision log. Moon only persists and checkpoints them."}; self.runner.artifacts.write("qc_agent_task",task); return {"status":"awaiting_agent","stage":"qc","task":task,"pipeline":self.runner.status()}
        cp={"source":"external_agent_qc","artifacts":{"qc_report":str(self.runner.artifacts.path_for("qc_report")),"decision_log":str(self.runner.artifacts.path_for("decision_log"))}}
        return {"status":"completed","stage":"qc","pipeline":self.runner.complete("qc",cp)}
    def _require_artifact(self,name:str)->Path:
        if not self.runner.artifacts.exists(name): raise FileNotFoundError(f"Moon stage requires artifact {name!r}")
        return self.runner.artifacts.path_for(name)
    def _materialize_artifact(self,name:str)->Path:
        source=self._require_artifact(name); target=self.runner.project.cache_dir/"stage-inputs"/f"{name}.json"; target.parent.mkdir(parents=True,exist_ok=True); target.write_text(source.read_text(encoding="utf-8"),encoding="utf-8"); return target
    def _execute_tool(self,name:str,inputs:dict[str,Any])->dict[str,Any]:
        factories={"footage_profile_builder":lambda:__import__("tools.analysis.footage_profile_builder",fromlist=["FootageProfileBuilder"]).FootageProfileBuilder(),"reference_candidate_ranker":lambda:__import__("tools.analysis.reference_candidate_ranker",fromlist=["ReferenceCandidateRanker"]).ReferenceCandidateRanker(),"reference_match_validator":lambda:__import__("tools.analysis.reference_match_validator",fromlist=["ReferenceMatchValidator"]).ReferenceMatchValidator(),"reference_timeline_builder":lambda:__import__("tools.video.reference_timeline_builder",fromlist=["ReferenceTimelineBuilder"]).ReferenceTimelineBuilder(),"reference_video_renderer":lambda:__import__("tools.video.reference_video_renderer",fromlist=["ReferenceVideoRenderer"]).ReferenceVideoRenderer()}
        result=factories[name]().execute(inputs); return {"success":bool(result.success),"data":result.data or {},"artifacts":list(result.artifacts or []),"error":result.error}
