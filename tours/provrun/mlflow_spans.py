"""Print the spans of a run's copy in MLflow, each with the names of its
attributes, from the JSON that provr run printed.
    mlflow_spans.py FILE"""
import json
import os
import sys

os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")  # MLflow's note to coding agents

from mlflow import MlflowClient

copy = json.loads(open(sys.argv[1]).read())["mlflow"]
trace = MlflowClient(tracking_uri=copy["tracking_uri"]).get_trace(copy["trace_id"])
for span in sorted(trace.data.spans, key=lambda s: s.start_time_ns):
    print(span.name, " ".join(sorted(span.attributes)))
