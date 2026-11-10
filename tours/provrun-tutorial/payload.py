"""Print the payload of the first event of a type in a run's evidence.
    payload.py RUN TYPE
A payload is a file in the run's evidence/payloads, named by its SHA-256."""
import json
import os
import sys

evidence = os.path.join(os.environ["PROVRUN_RUNS"], sys.argv[1], "evidence")
for line in open(os.path.join(evidence, "events.jsonl")):
    event = json.loads(line)
    if event["type"] == sys.argv[2]:
        print(f"payload {event['payload']}")
        print(open(os.path.join(evidence, "payloads", event["payload"])).read())
        break
