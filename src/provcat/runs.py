
from typing import Optional, Dict, Any
from jinja2 import Environment, FileSystemLoader
import pathlib
from .s3io import put_text
from .backends_mlflow import ensure_experiment, start_run as ml_start, end_run as ml_end

_TPL_DIR = (pathlib.Path(__file__).parent.parent.parent / "templates")
def _env(): return Environment(loader=FileSystemLoader(str(_TPL_DIR)))

def start(experiment_name: str, run_base_s3: str, run_name: Optional[str]=None, run_desc: Optional[str]=None,
          parent_run_id: Optional[str]=None, tags: Optional[Dict[str,str]]=None, params: Optional[Dict[str,Any]]=None):
    ensure_experiment(experiment_name)
    run_id = ml_start(experiment_name, run_name=run_name, parent_run_id=parent_run_id, tags=tags, params=params)
    e=_env()
    name = run_name or f"Run {run_id}"
    desc = run_desc or f"Run for {experiment_name}"
    put_text(f"{run_base_s3}/{run_id}/ABOUT.json", e.get_template("about_json.j2").render(name=name, description=desc, uri=f"run://{experiment_name}/{run_id}"))
    put_text(f"{run_base_s3}/{run_id}/README.md", e.get_template("artifact_readme.md.j2").render(name=name, description=desc, uri=f"run://{experiment_name}/{run_id}"))
    return {"ok": True, "run_id": run_id, "run_base": f"{run_base_s3}/{run_id}"}

def end(status: str = "FINISHED"):
    ml_end(status=status)
    return {"ok": True}
