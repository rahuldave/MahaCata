
from .s3io import put_text
from jinja2 import Environment, FileSystemLoader
import pathlib
_TPL_DIR = (pathlib.Path(__file__).parent.parent.parent / "templates")
def _env(): return Environment(loader=FileSystemLoader(str(_TPL_DIR)))

def create_or_update(experiment_base_s3: str, uri: str, name: str|None=None, description: str|None=None):
    name = name or "Experiment"
    description = description or uri
    e = _env()
    put_text(f"{experiment_base_s3}/ABOUT.json", e.get_template("about_json.j2").render(name=name, description=description, uri=uri))
    put_text(f"{experiment_base_s3}/README.md", e.get_template("artifact_readme.md.j2").render(name=name, description=description, uri=uri))
    return {"ok": True, "experiment_base": experiment_base_s3}
