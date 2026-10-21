
"""Example 01: Create a typed prompt artifact and also capture it under a run.

This example:
  - Creates/updates an experiment card
  - Starts a parent run
  - Writes a prompt as a project-level typed artifact (artifact+prompt://)
  - Writes the exact prompt under the run folder for strong provenance
  - Ends the run

Prereqs:
  - Configure lakeFS endpoint & S3 gateway env vars
  - Have a resolver.yaml mapping experiment to an experiment repo
"""
import os
from provcat.experiments import create_or_update
from provcat.runs import start, end
from provcat.artifacts import write_prompt

EXP_URI = "experiment://acme/teams/prod/proj-credit-agreements/qa"
EXP_BASE = "s3://exp-proj-credit-agreements-qa/main/mlflow/experiments/proj-credit-agreements/qa"
RUN_BASE = "s3://exp-proj-credit-agreements-qa/main/runs/demo-0001"

# Upsert experiment cards
create_or_update(EXP_BASE, uri=EXP_URI, name="QA Experiment (Prod)", description="Prod QA experiment for Credit Agreements")

# Start run
res = start(experiment_name="proj-credit-agreements/qa", run_base_s3=RUN_BASE, run_name="baseline-eval")
run_id = res["run_id"]

# Project-level typed prompt artifact
ART_BASE = "s3://proj-credit-agreements-artifacts/main/acme/teams/prod/proj-credit-agreements/qa/default_v1"
ART_URI = "artifact+prompt://acme/teams/prod/proj-credit-agreements/qa/default_v1"
write_prompt(ART_BASE, name="QA Default Prompt v1", description="Default prompt for QA pipeline (v1).", uri=ART_URI,
             prompt_text="You are a helpful assistant that extracts answers...", meta={"template": "fewshot-3"})

# Exact prompt under the run
write_prompt(f"{RUN_BASE}/inputs/prompt", name="Prompt used by run", description="Frozen copy of prompt used by this run",
             uri=f"run://proj-credit-agreements/qa/{run_id}", prompt_text="You are a helpful assistant that extracts answers...")

end()
print("Example 01 completed.")
