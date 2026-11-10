"""Print the data of the first event of a type in a run's evidence, or the
keys of it that a dotted path names, as JSON.
    event.py RUN TYPE [PATH [PATH...]]
For example, event.py "$R1" run_created telemetry.redirects."""
import json
import os
import sys

path = os.path.join(os.environ["PROVRUN_RUNS"], sys.argv[1], "evidence", "events.jsonl")
for line in open(path):
    event = json.loads(line)
    if event["type"] != sys.argv[2]:
        continue
    for key in sys.argv[3:] or [""]:
        data = event["data"]
        for part in filter(None, key.split(".")):
            data = data[part]
        print(data if isinstance(data, str) else json.dumps(data, indent=2, sort_keys=True))
    break
