
# Placeholder thin wrappers — in a real deployment call lakeFS SDK here.
def create(repo: str, source_branch: str, new_branch: str):
    return {"ok": True, "repo": repo, "created": new_branch, "from": source_branch}

def list(repo: str):
    return {"repo": repo, "branches": ["main", "feature/demo"]}

def merge(repo: str, source_branch: str, dest_branch: str, message: str):
    return {"ok": True, "repo": repo, "from": source_branch, "to": dest_branch, "message": message}
