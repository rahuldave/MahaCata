"""Ask the evidence file questions from a program, with no command line."""
import os
import sys

from provevi import SqliteStore, verify

store = SqliteStore(os.environ["TOUR_DIR"] + "/evidence.db", create=False)
report = "file://" + os.environ["TOUR_DIR"] + "/report.txt"
for run_id in store.runs_reading(report):
    run = store.run(run_id)
    [staged] = store.inputs(run_id)
    problems = verify(store.read_run(run_id), run_id)
    print(run_id, run["status"], staged["sha256"][:12], "verifies" if not problems else f"{len(problems)} problems")
sys.exit(0)
