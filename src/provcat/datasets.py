
from typing import Tuple
from jinja2 import Environment, FileSystemLoader
import pathlib
from .s3io import put_text

_TPL_DIR = (pathlib.Path(__file__).parent.parent.parent / "templates")

def _env():
    return Environment(loader=FileSystemLoader(str(_TPL_DIR)))

def dataset_base_from_parts(repo: str, branch: str, org: str, domain: str, env: str, family: str, name: str) -> str:
    return f"s3://{repo}/{branch}/{org}/{domain}/{env}/{family}/{name}"

def lance_path_from_parts(repo: str, branch: str, org: str, domain: str, env: str, family: str, name: str) -> str:
    return f"s3://{repo}/{branch}/{org}/{domain}/{env}/{family}/{name}.lance"

def create_or_update(repo: str, branch: str, uri: str, org: str, domain: str, env: str, family: str, name: str,
                     title: str|None=None, description: str|None=None) -> Tuple[dict, str, str]:
    base = dataset_base_from_parts(repo, branch, org, domain, env, family, name)
    lance_path = lance_path_from_parts(repo, branch, org, domain, env, family, name)
    title = title or f"{family}/{name}"
    description = description or uri
    e = _env()
    put_text(f"{base}/ABOUT.json", e.get_template("about_json.j2").render(name=title, description=description, uri=uri))
    put_text(f"{base}/README.md", e.get_template("dataset_readme.md.j2").render(name=title, description=description, uri=uri))
    return {"ok": True, "dataset_base": base, "lance_path": lance_path}, base, lance_path
