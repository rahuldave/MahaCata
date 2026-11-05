"""Print the live feed of a finished run, as a viewer at a level would see it.

    uv run python tours/provevi/replay.py RUN LEVEL

An event shows as its type and the keys of the data that the level lets
through. The text of an answer and the end show in full.
"""
import json
import os
import sys

from provevi import SqliteStore, replay

store = SqliteStore(os.environ["TOUR_DIR"] + "/evidence.db", create=False)
run_id, level = sys.argv[1], sys.argv[2]
for item in replay(store, run_id, level):
    if item["type"] == "event":
        print(f"{item['event']:<24} {' '.join(sorted(item['data']))}".rstrip())
    else:
        print(json.dumps(item, sort_keys=True))
