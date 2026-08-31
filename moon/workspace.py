from __future__ import annotations
import argparse,json
from pathlib import Path
from typing import Any

DEFAULT_PRESETS={"reference-replication":{"max_revisions":2,"frame_count":8,"frame_width":320},"fast-preview":{"max_revisions":1,"frame_count":4,"frame_width":240}}

class MoonWorkspace:
    def __init__(self,root:str|Path)->None:self.root=Path(root).resolve();self.path=self.root/".moon-workspace.json"
    def load(self)->dict[str,Any]:
        if not self.path.exists():return {"version":1,"projects":{},"presets":DEFAULT_PRESETS}
        data=json.loads(self.path.read_text(encoding="utf-8"));data.setdefault("projects",{});data.setdefault("presets",DEFAULT_PRESETS);return data
    def save(self,data:dict[str,Any])->None:
        self.root.mkdir(parents=True,exist_ok=True);tmp=self.path.with_suffix(".tmp");tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8");tmp.replace(self.path)
    def register(self,name:str,project:str|Path,preset:str="reference-replication")->dict[str,Any]:
        data=self.load();path=Path(project).resolve()
        if preset not in data["presets"]:raise ValueError(f"unknown preset {preset!r}")
        data["projects"][name]={"path":str(path),"preset":preset};self.save(data);return data["projects"][name]
    def resolve(self,name:str)->dict[str,Any]:
        data=self.load()
        if name not in data["projects"]:raise KeyError(name)
        item=dict(data["projects"][name]);item["settings"]=dict(data["presets"][item["preset"]]);return item

def main()->int:
    p=argparse.ArgumentParser(description="Moon local multi-project registry");p.add_argument("--workspace",default=".");sub=p.add_subparsers(dest="cmd",required=True);r=sub.add_parser("register");r.add_argument("name");r.add_argument("project");r.add_argument("--preset",default="reference-replication");sub.add_parser("list");g=sub.add_parser("get");g.add_argument("name");a=p.parse_args();ws=MoonWorkspace(a.workspace)
    if a.cmd=="register":out=ws.register(a.name,a.project,a.preset)
    elif a.cmd=="get":out=ws.resolve(a.name)
    else:out=ws.load()
    print(json.dumps(out,ensure_ascii=False,indent=2));return 0
if __name__=="__main__":raise SystemExit(main())
