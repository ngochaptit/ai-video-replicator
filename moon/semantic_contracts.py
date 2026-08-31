from __future__ import annotations
from typing import Any
from moon.runner.pipeline import PipelineRunner
SCORE_KEYS=("action","interaction","camera","spatial","motion","duration","overall")

def validate_semantic_submission(runner:PipelineRunner,stage:str,payload:dict[str,Any])->None:
    if stage=="footage" and runner.artifacts.exists("footage_profiles_scaffold"):
        scaffold=runner.artifacts.read("footage_profiles_scaffold")
        if scaffold.get("clips"): _validate_footage(scaffold,payload)
    elif stage=="match" and runner.artifacts.exists("reference_blueprint") and runner.artifacts.exists("footage_profiles"):
        _validate_match(runner.artifacts.read("reference_blueprint"),runner.artifacts.read("footage_profiles"),payload)

def _validate_footage(scaffold:dict[str,Any],payload:dict[str,Any])->None:
    expected={str(c["clip_id"]):c for c in scaffold.get("clips") or []}; clips=payload.get("clips")
    if not isinstance(clips,list) or not clips: raise ValueError("footage enrichment requires non-empty clips[]")
    actual={str(c.get("clip_id") or ""):c for c in clips if isinstance(c,dict)}
    if set(actual)!=set(expected): raise ValueError(f"footage clip_id set must exactly match measured scaffold: expected={sorted(expected)}")
    for clip_id,spec in actual.items():
        measured_clip=expected[clip_id]
        if "path" in spec and str(spec["path"])!=str(measured_clip["path"]): raise ValueError(f"{clip_id} path must match measured scaffold")
        if spec.get("usable",True) is False: continue
        segments=spec.get("segments")
        if not isinstance(segments,list) or not segments: raise ValueError(f"usable clip {clip_id} requires non-empty segments[]")
        duration=float(measured_clip["duration_seconds"]); measured={0.0,duration}; evidence=[]
        for item in measured_clip.get("segments") or []:
            ev=item.get("evidence") or {}; evidence.extend(float(x) for x in ev.get("frame_timestamps") or [])
            if "scene_cut" in (item.get("boundary_basis") or []): measured.add(float(item["source_in"]))
        measured.update(evidence); previous=-1.0
        for index,segment in enumerate(segments,1):
            if not isinstance(segment,dict): raise ValueError(f"{clip_id} segment {index} must be object")
            start,end=segment.get("source_in"),segment.get("source_out")
            if isinstance(start,bool) or isinstance(end,bool) or not isinstance(start,(int,float)) or not isinstance(end,(int,float)) or start<0 or end<=start or end>duration+1e-6: raise ValueError(f"{clip_id} segment {index} has invalid measured range")
            if start<previous-1e-6: raise ValueError(f"{clip_id} segment {index} overlaps previous segment")
            if not segment.get("boundary_basis"): raise ValueError(f"{clip_id} segment {index} requires boundary_basis")
            if not any(abs(float(start)-m)<=1e-6 for m in measured) or not any(abs(float(end)-m)<=1e-6 for m in measured): raise ValueError(f"{clip_id} segment {index} boundaries must be measured timestamps")
            if not any(float(start)-1e-6<=t<=float(end)+1e-6 for t in evidence): raise ValueError(f"{clip_id} segment {index} requires in-range frame evidence")
            previous=float(end)

def _validate_match(blueprint:dict[str,Any],profiles:dict[str,Any],payload:dict[str,Any])->None:
    refs=[str(x["id"]) for x in blueprint.get("segments") or []]; footage={str(s["id"]) for c in profiles.get("clips") or [] if c.get("usable",True) for s in c.get("segments") or []}; matches=payload.get("matches")
    if not isinstance(matches,list): raise ValueError("match proposal requires matches[]")
    by_ref={str(m.get("reference_segment_id") or ""):m for m in matches if isinstance(m,dict)}
    if set(by_ref)!=set(refs) or len(matches)!=len(refs): raise ValueError("match proposal must cover every reference segment exactly once")
    improvements={str(x.get("reference_segment_id") or "") for x in payload.get("improvement_requests") or [] if isinstance(x,dict)}
    for ref in refs:
        item=by_ref[ref]; fid=str(item.get("footage_segment_id") or "")
        if fid not in footage: raise ValueError(f"{ref} selects unknown/unusable footage segment {fid!r}")
        klass=item.get("match_class")
        if klass not in {"good","acceptable","fallback"}: raise ValueError(f"{ref} invalid match_class")
        if klass=="fallback" and ref not in improvements: raise ValueError(f"fallback {ref} requires improvement_request")
        if not str(item.get("rationale") or "").strip(): raise ValueError(f"{ref} requires rationale")
        scores=item.get("scores")
        if not isinstance(scores,dict) or any(k not in scores for k in SCORE_KEYS): raise ValueError(f"{ref} scores must contain {list(SCORE_KEYS)}")
        for key in SCORE_KEYS:
            value=scores[key]
            if value is None and key!="overall": continue
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not 0<=float(value)<=1: raise ValueError(f"{ref} invalid score {key}")
