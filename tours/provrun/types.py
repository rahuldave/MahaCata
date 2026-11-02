"""Print the sequence number and the type of each event of a run, and the
tool of each tool call.
    types.py RUN"""
import json
import os
import sys

path = os.path.join(os.environ["PROVRUN_RUNS"], sys.argv[1], "evidence", "events.jsonl")
for line in open(path):
    event = json.loads(line)
    tool = event["data"].get("tool") if event["type"] == "tool_called" else None
    print(event["seq"], event["type"], tool or "")
