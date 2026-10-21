
from .stage_pointers import StagePointerLookup
def set_stage(cfg: dict, env: str, repo: str, resource: str, stage: str, commit_id: str):
    st = StagePointerLookup(cfg); st.set(env, repo, resource, stage, commit_id)
    return {"ok": True, "env": env, "repo": repo, "resource": resource, "stage": stage, "commit": commit_id}
def get_stage(cfg: dict, env: str, repo: str, resource: str, stage: str):
    st = StagePointerLookup(cfg); cid = st.get(env, repo, resource, stage)
    return {"env": env, "repo": repo, "resource": resource, "stage": stage, "commit": cid}
