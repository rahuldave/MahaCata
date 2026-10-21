
# Minimal local stage pointer store (json file under /mnt/data for demo)
import json, os
STORE = "/mnt/data/_stage_pointers.json"

class StagePointerLookup:
    def __init__(self, cfg: dict):
        self.cfg = cfg

    def set(self, env: str, repo: str, resource: str, stage: str, commit_id: str):
        db = self._load()
        db.setdefault(env, {}).setdefault(repo, {}).setdefault(resource, {})[stage] = commit_id
        self._save(db)

    def get(self, env: str, repo: str, resource: str, stage: str):
        db = self._load()
        return (((db.get(env) or {}).get(repo) or {}).get(resource) or {}).get(stage)

    def _load(self):
        if os.path.exists(STORE):
            with open(STORE, 'r') as f:
                try: return json.load(f)
                except Exception: return {}
        return {}

    def _save(self, db):
        with open(STORE, 'w') as f:
            json.dump(db, f, indent=2)
