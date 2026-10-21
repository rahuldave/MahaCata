
from typing import Dict, Any, Optional, List
from jinja2 import Environment, FileSystemLoader
import pathlib, json
from .s3io import put_text
from .backends_mlflow import register_model_version, set_model_stage

_TPL_DIR = (pathlib.Path(__file__).parent.parent.parent / "templates")
def _env(): return Environment(loader=FileSystemLoader(str(_TPL_DIR)))

def write_lineage(version_base_s3: str, produced_by_run: str, inputs: List[str]|None=None, commit_id: str|None=None):
    doc = {
        "produced_by_run": produced_by_run,
        "inputs": inputs or [],
        "commit": commit_id or "TBD"
    }
    put_text(f"{version_base_s3}/lineage.json", json.dumps(doc, indent=2))

def write_model_card(version_base_s3: str, model_uri: str, name: str, description: str, version: str|int):
    e=_env()
    put_text(f"{version_base_s3}/ABOUT.json", e.get_template("about_json.j2").render(name=name, description=description, uri=model_uri))
    put_text(f"{version_base_s3}/model_card.md", e.get_template("model_card.md.j2").render(name=name, description=description, uri=model_uri, model_version=version))

def register_version(model_name: str, version_base_s3: str, description: Optional[str]=None, tags: Optional[Dict[str,str]]=None)->str:
    return register_model_version(model_name=model_name, source_path=version_base_s3, description=description, tags=tags)

def promote_stage(model_name: str, version: str|int, stage: str, archive_existing: bool=False):
    set_model_stage(model_name, version, stage, archive_existing=archive_existing)
