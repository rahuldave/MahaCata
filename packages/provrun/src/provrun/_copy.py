"""The copy of a sealed run into MLflow, in a process of its own.

``Routes.copy`` runs ``python -m provrun._copy`` with the user's variables,
and writes the request as JSON to its input. provevi's ``copy_run`` changes
``os.environ`` and ``sys.modules`` while it works, and MLflow retries a
place that cannot be reached. In a process of its own, neither reaches
another run of the program, and the copy can be stopped after a time limit.
It prints one line of JSON: the copy, or the error.
"""
from __future__ import annotations

import base64
import json
import pickle
import sys
from dataclasses import asdict
from pathlib import Path

__all__: list[str] = []


def main() -> int:
    try:
        from provevi import FileStore, SqliteStore
        from provevi.mlflow_copy import copy_run

        request = json.loads(sys.stdin.read())
        where = request["store"]
        store = SqliteStore(where["path"]) if where["kind"] == "sqlite" else FileStore(where["root"])
        policy = pickle.loads(base64.b64decode(request["policy"]))  # from provrun's own process
        copy = copy_run(store, request["run_id"], policy, folder=Path(request["folder"]))
        result = {"copy": asdict(copy)}
    except Exception as e:
        from provevi import ProveviError

        result = {"error": str(e) if isinstance(e, ProveviError) else f"{e.__class__.__name__}: {e}"}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
