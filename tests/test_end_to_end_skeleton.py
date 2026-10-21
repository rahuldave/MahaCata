import json
import subprocess
import sys
import os
ROOT = os.path.dirname(os.path.dirname(__file__))

def run_cli(*argv):
    cmd = [sys.executable, "-m", "provenance_resolver.provc", "--cfg", f"{ROOT}/config/resolver.sample.yaml"] + list(argv)
    return subprocess.check_output(cmd, text=True)

def test_resolve_dataset_uri():
    out = run_cli("resolve", "data+table://acme/shared/prod/agreements/qa_outputs")
    assert '"repo": "agreements-qa_outputs-data"' in out

def test_branch_commands_exist():
    out = run_cli("branch", "list", "--uri", "data+table://acme/shared/prod/agreements/qa_outputs")
    assert '"repo": "agreements-qa_outputs-data"' in out

def test_experiment_and_run_commands_exist():
    # experiment create (no MLflow required)
    out = run_cli("experiment", "create", "--uri", "experiment://acme/teams/prod/proj-credit-agreements/qa")
    assert "experiment_base" in out
    # run start (will work even without MLflow, using synthetic run id)
    out = run_cli("run", "start", "--experiment-uri", "experiment://acme/teams/prod/proj-credit-agreements/qa", "--name", "t")
    assert "run_id" in out

def test_model_publish_entrypoint_exists():
    out = run_cli("model", "publish", "--from-run", "run://proj-credit-agreements/qa/0001", "--to", "model://acme/teams/prod/proj-credit-agreements/qa-extractor", "--version", "99")
    assert '"ok": true' in out
