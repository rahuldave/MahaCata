import json, subprocess, sys, os, re

ROOT = os.path.dirname(os.path.dirname(__file__))

def run_cli(*argv):
    cmd = [sys.executable, "-m", "provenance_resolver.provc", "--cfg", f"{ROOT}/config/resolver.sample.yaml"] + list(argv)
    return subprocess.check_output(cmd, text=True)

def extract_run_id(s):
    try:
        return json.loads(s)["run_id"]
    except Exception:
        m = re.search(r'"run_id"\s*:\s*"([^"]+)"', s)
        return m.group(1) if m else None

def test_parse_run_ids():
    out = run_cli("experiment", "create", "--uri", "experiment://acme/teams/prod/proj-credit-agreements/qa")
    assert "experiment_base" in out
    out1 = run_cli("run", "start", "--experiment-uri", "experiment://acme/teams/prod/proj-credit-agreements/qa", "--name", "parent")
    rid1 = extract_run_id(out1); assert rid1
    out2 = run_cli("run", "start", "--experiment-uri", "experiment://acme/teams/prod/proj-credit-agreements/qa", "--name", "child", "--parent", rid1)
    rid2 = extract_run_id(out2); assert rid2
    run_cli("run", "end")
