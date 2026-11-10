"""Ask the evidence store the questions of the tutorial from Python.
    query.py STORE FILE
STORE is a store URI, such as sqlite:///evidence.db, and FILE is the URI of
an input, such as file:///tmp/provevi-tutorial/q3.txt."""
import sys

from provevi import open_store, verify

store = open_store(sys.argv[1])
for run_id in store.runs_reading(sys.argv[2]):
    run = store.run(run_id)
    [staged] = store.inputs(run_id)
    problems = verify(store.read_run(run_id), run_id)
    print(run_id, run["status"], staged["sha256"][:12], "verifies" if not problems else f"{len(problems)} problems")
