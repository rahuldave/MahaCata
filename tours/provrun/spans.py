"""Print the span ID and the parent span ID of each event of a run that has
one, and then each trace ID that those events hold.
    spans.py RUN"""
import json
import os
import sys

traces = []
path = os.path.join(os.environ["PROVRUN_RUNS"], sys.argv[1], "evidence", "events.jsonl")
for line in open(path):
    event = json.loads(line)
    if event["data"].get("span_id"):
        print(event["type"], event["data"]["span_id"], event["data"].get("parent_span_id"))
        if event["data"].get("trace_id") not in traces:
            traces.append(event["data"].get("trace_id"))
print("trace IDs:", " ".join(map(str, traces)))
