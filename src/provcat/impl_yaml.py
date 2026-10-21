
from typing import Any, Dict
from .interface import Resolution, split_logical_id, Selector
import os, json

try:
    from omegaconf import OmegaConf
    def _load_yaml(path: str) -> Dict[str, Any]:
        return OmegaConf.to_container(OmegaConf.load(path), resolve=True)
except Exception:
    import yaml
    def _load_yaml(path: str) -> Dict[str, Any]:
        with open(path, 'r') as f:
            return yaml.safe_load(f) or {}

class OmegaConfResolver:
    def __init__(self, cfg_path: str):
        self.cfg_path = cfg_path
        self.cfg = _load_yaml(cfg_path)

    def resolve(self, logical_id: str, context: Dict[str, Any]) -> Resolution:
        scheme, parts, selector = split_logical_id(logical_id)
        routing = self.cfg.get('routing', {})
        repos = self.cfg.get('repos', {})
        paths = self.cfg.get('paths', {})

        # figure mapping
        route = routing.get(scheme) or {}
        repo_kind = route.get('repo_kind')
        path_kind = route.get('path_kind')

        # derive repo name
        # Allow simple templating in repo templates:
        def apply_template(tpl: str, keys: Dict[str, Any]) -> str:
            try:
                return tpl.format(**keys)
            except Exception:
                return tpl

        values = {}
        # Common placeholders we might support:
        # from parts depending on scheme
        if scheme == 'data+table':
            # org/domain/env/family/name
            org, domain, env, family, name = parts
            values.update(dict(org=org, domain=domain, env=env, family=family, name=name, project=org+"-"+family, model_name=name))
        elif scheme == 'experiment':
            # org/team/env/project/pipeline
            org, team, env, project, pipeline = parts
            values.update(dict(org=org, team=team, env=env, project=project, pipeline=pipeline, model_name=pipeline))
        elif scheme == 'model':
            # org/team/env/project/model_name
            org, team, env, project, model_name = parts
            values.update(dict(org=org, team=team, env=env, project=project, model_name=model_name))
        elif scheme.startswith('artifact+'):
            # artifact+type://org/team/env/project/name
            art_type = scheme.split('+',1)[1]
            org, team, env, project, name = parts
            values.update(dict(org=org, team=team, env=env, project=project, name=name, type=art_type))
        elif scheme == 'run':
            # run://project/pipeline/run_id
            project, pipeline, run_id = parts
            values.update(dict(project=project, pipeline=pipeline, run_id=run_id))
        else:
            values['raw_parts'] = parts

        # repo name from template
        repo_tpl = (repos.get(repo_kind, {}) or {}).get('template', 'provcat-generic')
        repo = apply_template(repo_tpl, values)

        # branch default main (could use selector or context in future)
        branch = 'main'
        if selector and selector.kind == 'branch':
            branch = selector.value

        # compute base path
        path_tpl = (paths.get(path_kind, {}) or {}).get('base', '')
        base_rel = apply_template(path_tpl, values).strip('/')

        # build scheme-specific 'paths'
        lakefs_paths = {}
        if scheme == 'data+table':
            # dataset base and lance file
            base = f"s3://{repo}/{branch}/{base_rel}"
            lakefs_paths['dataset_base'] = base
            lakefs_paths['lance_path'] = f"{base}.lance"
        elif scheme == 'experiment':
            base = f"s3://{repo}/{branch}/{base_rel}"
            lakefs_paths['experiment_base'] = base
        elif scheme == 'model':
            base = f"s3://{repo}/{branch}/{base_rel}"
            lakefs_paths['model_base'] = base
        elif scheme.startswith('artifact+'):
            base = f"s3://{repo}/{branch}/{base_rel}"
            lakefs_paths['artifact_base'] = base
        elif scheme == 'run':
            base = f"s3://{repo}/{branch}/{base_rel}"
            lakefs_paths['run_base'] = base
        else:
            base = f"s3://{repo}/{branch}/{base_rel}"
            lakefs_paths['base'] = base

        lakefs = dict(repo=repo, branch=branch, paths=lakefs_paths)
        lance = {}
        mlflow = {}

        return Resolution(logical=logical_id, selector=selector, lakefs=lakefs, lance=lance, mlflow=mlflow)
