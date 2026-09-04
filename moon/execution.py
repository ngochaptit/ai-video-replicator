from __future__ import annotations
import json
import shutil
from pathlib import Path
from typing import Any
from moon.footage_evidence import FootageEvidencePlanner
from moon.runner.pipeline import PipelineRunner

class StageExecutionService:
    def __init__(self,runner:PipelineRunner)->None:self.runner=runner
    def plan(self)->dict[str,Any]:
        stage=self.runner.state.next_stage()
        if stage is None:return {"stage":None,"done":True,"mode":"complete"}
        plans={"proposal":("agent","proposal_packet"),"analyze":("agent","reference_blueprint + semantic_enrichment"),"footage":("hybrid","footage_semantic_enrichment"),"match":("hybrid","match_proposal"),"timeline":("deterministic",None),"render":("hybrid","render_plan"),"qc":("agent","qc_bundle")};mode,required=plans[stage];return {"stage":stage,"done":False,"mode":mode,"required_agent_artifact":required}
    def run(self)->dict[str,Any]:
        stage=self.runner.state.next_stage()
        if stage is None:return {"status":"complete","pipeline":self.runner.status()}
        fn={"footage":self._run_footage,"match":self._run_match,"timeline":self._run_timeline,"render":self._run_render,"qc":self._run_qc}.get(stage);return fn() if fn else {"status":"awaiting_agent","stage":stage,"required_agent_artifact":self.plan()["required_agent_artifact"],"pipeline":self.runner.status()}
    def _run_footage(self)->dict[str,Any]:
        self.runner.begin("footage");out=self.runner.project.root/"analysis"/"footage";inputs={"footage_dir":str(self.runner.project.root/"footage"),"analysis_depth":"deep","max_keyframes_per_file":30,"max_analysis_window_seconds":2.0,"output_dir":str(out)};enrichment=None
        if self.runner.artifacts.exists("footage_semantic_enrichment"):
            enrichment=self._materialize_footage_enrichment();inputs["semantic_enrichment_path"]=str(enrichment)
        result=self._execute_tool("footage_profile_builder",inputs)
        if not result["success"]:self.runner.fail("footage",result["error"]);return {"status":"blocked","stage":"footage",**result}
        if not enrichment:
            scaffold=result["data"];self.runner.artifacts.write("footage_profiles_scaffold",scaffold);planner=FootageEvidencePlanner(self.runner.project,self.runner.state.revision);sampling=planner.seed(scaffold);catalog=planner.evidence_catalog(scaffold);self.runner.artifacts.write("footage_evidence_catalog",{"version":"1.0","entries":catalog,"coverage":sampling["coverage"],"policy":sampling["policy"]})
            task={"stage":"footage","decision_owner":"external_agent","required_output_artifact":"footage_semantic_enrichment","evidence_root":str(out),"sampling":sampling,"instruction":"Inspect the adaptive sampled evidence, not analyzer keyframes alone. Treat fixed windows/keyframes as scaffolding only. Use moon.frames.sample to refine any window where an action or interaction boundary is ambiguous, then submit measured semantic action segments. Long clips must be reviewed coarse-to-fine before final segmentation."};self.runner.artifacts.write("footage_agent_task",task);return {"status":"awaiting_agent","stage":"footage","task":task,"pipeline":self.runner.status()}
        self.runner.artifacts.write("footage_profiles",result["data"]);return {"status":"completed","stage":"footage","pipeline":self.runner.complete("footage",{"source":"stage_execution_adapter","tool":"footage_profile_builder"})}
    def _run_match(self)->dict[str,Any]:
        self.runner.begin("match");bp=self._require_artifact("reference_blueprint");profiles=self._require_artifact("footage_profiles");rankings=self.runner.project.cache_dir/"candidate_rankings.json";rank=self._execute_tool("reference_candidate_ranker",{"reference_blueprint_path":str(bp),"footage_profiles_path":str(profiles),"top_k":10,"output_path":str(rankings)})
        if not rank["success"]:self.runner.fail("match",rank["error"]);return {"status":"blocked","stage":"match",**rank}
        self.runner.artifacts.write("candidate_rankings",rank["data"])
        if not self.runner.artifacts.exists("match_proposal"):
            task={"stage":"match","decision_owner":"external_agent","required_output_artifact":"match_proposal","candidate_rankings":str(self.runner.artifacts.path_for("candidate_rankings")),"instruction":"Choose exactly one real footage segment per reference segment; fallback requires an improvement request."};self.runner.artifacts.write("match_agent_task",task);return {"status":"awaiting_agent","stage":"match","task":task,"pipeline":self.runner.status()}
        proposal=self._materialize_artifact("match_proposal");out=self.runner.project.root/"analysis"/"matching.json";val=self._execute_tool("reference_match_validator",{"reference_blueprint_path":str(bp),"footage_profiles_path":str(profiles),"proposal_path":str(proposal),"output_path":str(out)})
        if not val["success"]:self.runner.fail("match",val["error"]);return {"status":"blocked","stage":"match",**val}
        self.runner.artifacts.write("match_decisions",val["data"]);return {"status":"completed","stage":"match","pipeline":self.runner.complete("match",{"source":"stage_execution_adapter","tool":"reference_match_validator"})}
    def _run_timeline(self)->dict[str,Any]:
        self.runner.begin("timeline");bp=self._require_artifact("reference_blueprint");matching=self._require_artifact("match_decisions");out=self.runner.project.root/"analysis"/"replication_timeline.json";result=self._execute_tool("reference_timeline_builder",{"reference_blueprint_path":str(bp),"reference_matching_path":str(matching),"output_path":str(out)})
        if not result["success"]:self.runner.fail("timeline",result["error"]);return {"status":"blocked","stage":"timeline",**result}
        self.runner.artifacts.write("timeline",result["data"]);return {"status":"completed","stage":"timeline","pipeline":self.runner.complete("timeline",{"source":"stage_execution_adapter","tool":"reference_timeline_builder"})}
    def _run_render(self)->dict[str,Any]:
        self.runner.begin("render");revision=self.runner.state.revision
        if not self.runner.artifacts.exists("render_plan"):
            task={"stage":"render","revision":revision,"decision_owner":"external_agent/user","required_output_artifact":"render_plan","instruction":"Submit runtime_approved=true and an explicit render runtime. For revisions, incorporate the latest QC decision log."};self.runner.artifacts.write("render_agent_task",task);return {"status":"awaiting_agent","stage":"render","task":task,"pipeline":self.runner.status()}
        approval=self.runner.artifacts.read("render_plan");out=self.runner.project.root/"output"/f"draft_r{revision}.mp4"
        if "edit_decisions" in approval:
            plan=self._materialize_artifact("render_plan")
        else:
            timeline=self._require_artifact("timeline");blueprint=self._require_artifact("reference_blueprint");plan_out=self.runner.project.root/"analysis"/"replication_render_plan.json"
            built=self._execute_tool("reference_render_plan_builder",{"replication_timeline_path":str(timeline),"reference_blueprint_path":str(blueprint),"output_path":str(plan_out),"output_video_path":str(out),"render_runtime":approval.get("render_runtime"),"renderer_family":approval.get("renderer_family","documentary-montage"),"composition_mode":approval.get("composition_mode","templated"),"runtime_approved":approval.get("runtime_approved")})
            if not built["success"]:self.runner.fail("render",built["error"]);return {"status":"blocked","stage":"render",**built}
            self.runner.artifacts.write("replication_render_plan",built["data"]);plan=self._materialize_artifact("replication_render_plan")
        result=self._execute_tool("reference_video_renderer",{"render_plan_path":str(plan),"output_path":str(out)})
        if not result["success"]:self.runner.fail("render",result["error"]);return {"status":"blocked","stage":"render",**result}
        self.runner.artifacts.write("draft_render",{"output":str(out),"revision":revision,"tool_result":result["data"]});return {"status":"completed","stage":"render","pipeline":self.runner.complete("render",{"source":"stage_execution_adapter","tool":"reference_video_renderer","revision":revision})}
    def _run_qc(self)->dict[str,Any]:
        self.runner.begin("qc")
        quality_result=self._ensure_quality_report()
        if not quality_result["success"]:self.runner.fail("qc",quality_result["error"]);return {"status":"blocked","stage":"qc",**quality_result}
        quality=self.runner.artifacts.read("replication_quality_report")
        if self.runner.artifacts.exists("qc_bundle"):
            bundle=self.runner.artifacts.read("qc_bundle");report=dict(bundle["qc_report"]);report["render_integrity"]=quality["render_integrity"];report["replication_quality"]={"quality_gate":quality["quality_gate"],"fallback_count":quality["fallback_count"],"fallback_ratio":quality["fallback_ratio"],"unique_source_segment_count":quality["unique_source_segment_count"],"reuse_ratio":quality["reuse_ratio"],"max_reuse_count":quality["max_reuse_count"],"dominant_source_share":quality["dominant_source_share"],"overlap_reuse_count":quality["overlap_reuse_count"],"speed":quality["speed"],"chronology":quality["chronology"],"quality_flags":quality["quality_flags"],**quality["replication_quality"]};self.runner.artifacts.write("qc_report",report);self.runner.artifacts.write("decision_log",bundle["decision_log"])
        missing=[name for name in ("qc_report","decision_log") if not self.runner.artifacts.exists(name)]
        if missing:
            task={"stage":"qc","revision":self.runner.state.revision,"decision_owner":"external_agent","required_output_artifact":"qc_bundle","required_output_artifacts":missing,"quality_gate":quality["quality_gate"],"render_integrity":quality["render_integrity"]["status"],"instruction":"Review the draft semantically using the deterministic replication_quality_report. Use pass only when it does not contradict the quality gate, revise only for an actionable engine-side change, or footage_limited when render integrity passes but current source footage cannot meet replication quality."};self.runner.artifacts.write("qc_agent_task",task);return {"status":"awaiting_agent","stage":"qc","task":task,"pipeline":self.runner.status()}
        report=self.runner.artifacts.read("qc_report");decision=report.get("decision",report.get("verdict","pass"))
        if decision in {"revise","revision","fail"}:
            if quality["replication_quality"]["source_limited"]:return {"status":"blocked","stage":"qc","error":"QC requested an engine revision for a source-limited quality failure; use footage_limited or provide better footage instead of rerunning unchanged source evidence","pipeline":self.runner.status()}
            if self.runner.state.revision>=2:return {"status":"blocked","stage":"qc","error":"QC requested revision but revision limit (2) is reached","pipeline":self.runner.status()}
            revision_stage=self._revision_stage(quality,report)
            return {"status":"revision_requested","stage":"qc","revision_stage":revision_stage,"pipeline":self.runner.request_revision(from_stage=revision_stage,reason="external_agent_qc",max_revisions=2)}
        if quality["render_integrity"]["status"]!="pass":return {"status":"blocked","stage":"qc","error":"QC cannot finalize because deterministic render integrity failed","pipeline":self.runner.status()}
        if quality["quality_gate"]=="fail" and decision in {"pass","approved","accept"}:return {"status":"blocked","stage":"qc","error":"QC pass contradicts deterministic replication quality failure","pipeline":self.runner.status()}
        if decision not in {"pass","approved","accept","footage_limited"}:return {"status":"blocked","stage":"qc","error":f"unknown QC decision: {decision!r}"}
        draft=Path(self.runner.artifacts.read("draft_render")["output"]);final=self.runner.project.root/"output"/"final.mp4";final.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(draft,final);self.runner.artifacts.write("final_render",{"output":str(final),"revision":self.runner.state.revision,"source":str(draft)})
        return {"status":"completed","stage":"qc","final":str(final),"quality_gate":quality["quality_gate"],"pipeline":self.runner.complete("qc",{"source":"external_agent_qc","decision":decision,"quality_gate":quality["quality_gate"],"final":str(final)})}
    def _ensure_quality_report(self)->dict[str,Any]:
        if self.runner.artifacts.exists("replication_quality_report"):
            report=self.runner.artifacts.read("replication_quality_report")
            if int(report.get("revision",-1))==self.runner.state.revision:return {"success":True,"data":report,"artifacts":[str(self.runner.artifacts.path_for("replication_quality_report"))],"error":None}
        timeline=self._require_artifact("timeline");draft=self._require_artifact("draft_render");out=self.runner.project.root/"analysis"/f"replication_quality_report_r{self.runner.state.revision}.json";result=self._execute_tool("replication_quality_evaluator",{"replication_timeline_path":str(timeline),"draft_render_path":str(draft),"output_path":str(out),"revision":self.runner.state.revision})
        if result["success"]:self.runner.artifacts.write("replication_quality_report",result["data"])
        return result
    def _revision_stage(self,quality:dict[str,Any],report:dict[str,Any])->str:
        route_map={"match_or_timeline":"match","match":"match","timeline":"timeline","render":"render"}
        routes=[]
        recommended=str((quality.get("replication_quality") or {}).get("recommended_route") or "").strip().lower()
        if recommended in route_map:routes.append(route_map[recommended])
        for action in report.get("revision_actions") or []:
            if isinstance(action,dict):
                route=str(action.get("route") or "").strip().lower()
                if route in route_map:routes.append(route_map[route])
        if not routes:return "render"
        order={stage:index for index,stage in enumerate(self.runner.state.stages)}
        return min(routes,key=lambda stage:order[stage])
    def _materialize_footage_enrichment(self)->Path:
        source=self._require_artifact("footage_semantic_enrichment");payload=json.loads(source.read_text(encoding="utf-8"));scaffold=self.runner.artifacts.read("footage_profiles_scaffold") if self.runner.artifacts.exists("footage_profiles_scaffold") else {"clips":[]}
        planner=FootageEvidencePlanner(self.runner.project,self.runner.state.revision);catalog=planner.evidence_catalog(scaffold);existing=payload.get("evidence_catalog") or [];merged={(str(item.get("clip_id") or ""),str(item.get("path") or ""),round(float(item.get("timestamp",0.0)),6)):item for item in [*existing,*catalog] if isinstance(item,dict) and item.get("path")};payload["evidence_catalog"]=sorted(merged.values(),key=lambda item:(str(item.get("clip_id") or ""),float(item.get("timestamp",0.0)),str(item.get("path") or "")));self.runner.artifacts.write("footage_evidence_catalog",{"version":"1.0","entries":payload["evidence_catalog"],"coverage":planner.coverage_summary(scaffold),"policy":"adaptive_uniform_seed_v1"});target=self.runner.project.cache_dir/"stage-inputs"/"footage_semantic_enrichment_with_sampled_evidence.json";target.parent.mkdir(parents=True,exist_ok=True);target.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n",encoding="utf-8");return target
    def _require_artifact(self,name:str)->Path:
        if not self.runner.artifacts.exists(name):raise FileNotFoundError(f"Moon stage requires artifact {name!r}")
        return self.runner.artifacts.path_for(name)
    def _materialize_artifact(self,name:str)->Path:
        source=self._require_artifact(name);target=self.runner.project.cache_dir/"stage-inputs"/f"{name}.json";target.parent.mkdir(parents=True,exist_ok=True);target.write_text(source.read_text(encoding="utf-8"),encoding="utf-8");return target
    def _execute_tool(self,name:str,inputs:dict[str,Any])->dict[str,Any]:
        factories={"footage_profile_builder":lambda:__import__("tools.analysis.footage_profile_builder",fromlist=["FootageProfileBuilder"]).FootageProfileBuilder(),"reference_candidate_ranker":lambda:__import__("tools.analysis.reference_candidate_ranker",fromlist=["ReferenceCandidateRanker"]).ReferenceCandidateRanker(),"reference_match_validator":lambda:__import__("tools.analysis.reference_match_validator",fromlist=["ReferenceMatchValidator"]).ReferenceMatchValidator(),"reference_timeline_builder":lambda:__import__("tools.video.reference_timeline_builder",fromlist=["ReferenceTimelineBuilder"]).ReferenceTimelineBuilder(),"reference_render_plan_builder":lambda:__import__("tools.video.reference_render_plan_builder",fromlist=["ReferenceRenderPlanBuilder"]).ReferenceRenderPlanBuilder(),"reference_video_renderer":lambda:__import__("tools.video.reference_video_renderer",fromlist=["ReferenceVideoRenderer"]).ReferenceVideoRenderer(),"replication_quality_evaluator":lambda:__import__("tools.analysis.replication_quality_evaluator",fromlist=["ReplicationQualityEvaluator"]).ReplicationQualityEvaluator()};result=factories[name]().execute(inputs);return {"success":bool(result.success),"data":result.data or {},"artifacts":list(result.artifacts or []),"error":result.error}
