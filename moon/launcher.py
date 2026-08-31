from __future__ import annotations
import argparse,json
from pathlib import Path
from moon.agent_bridge import AgentBridgeService
from moon.core.project import MoonProject
from moon.runner.pipeline import PipelineRunner

def launch(project_root:str|Path)->dict:
    project=MoonProject.open(project_root,create=False);runner=PipelineRunner(project);result=AgentBridgeService(runner).next()
    return {"moon":"v1","project":str(project.root),"result":result,"mcp":{"command":"python","args":["-m","moon","--project",str(project.root),"mcp-stdio"]}}

def main()->int:
    parser=argparse.ArgumentParser(description="Launch/resume one Moon project to its next semantic boundary");parser.add_argument("--project",required=True);args=parser.parse_args();print(json.dumps(launch(args.project),ensure_ascii=False,indent=2));return 0
if __name__=="__main__":raise SystemExit(main())
