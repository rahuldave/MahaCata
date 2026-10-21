
from typing import Dict, Optional
from jinja2 import Environment, FileSystemLoader
import pathlib, json
from .s3io import put_text

_TPL_DIR = (pathlib.Path(__file__).parent.parent.parent / "templates")

def _env():
    return Environment(loader=FileSystemLoader(str(_TPL_DIR)))

def write_card(base_s3_dir: str, name: str, description: str, uri: str, artifact_type: str, extra: Dict|None=None):
    e = _env()
    about = e.get_template("about_json.j2").render(name=name, description=description, uri=uri)
    put_text(f"{base_s3_dir}/ABOUT.json", about)
    readme = e.get_template("artifact_readme.md.j2").render(name=name, description=description, uri=uri)
    put_text(f"{base_s3_dir}/README.md", readme)

def write_prompt(base_s3_dir: str, name: str, description: str, uri: str, prompt_text: str, meta: Dict|None=None):
    write_card(base_s3_dir, name, description, uri, artifact_type="prompt", extra=meta or {})
    put_text(f"{base_s3_dir}/prompt.txt", prompt_text)
