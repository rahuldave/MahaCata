#!/usr/bin/env python3
"""
DEV experiment example: writes MLflow artifacts and Lance rows INSIDE a lakeFS branch,
and writes a parent manifest. Uses env vars for config (see .env.example).
"""
import os, uuid, json, datetime
import mlflow, pyarrow as pa
from mlflow.tracking import MlflowClient
import lancedb, boto3

ORG = os.getenv("ORG","acme")
TENANT = os.getenv("TENANT","bigbank")
ENV = os.getenv("ENV","dev")
PROJECT = os.getenv("PROJECT","credit-agreements")
PIPELINE = os.getenv("PIPELINE","qa")

REPO = os.getenv("LAKEFS_REPO","agreements-bigbank-dev")
BRANCH = os.getenv("BRANCH","feat/prompt-search-20250928")

# Configure lakeFS S3 gateway for MLflow + boto3
os.environ.setdefault("MLFLOW_S3_ENDPOINT_URL", os.getenv("LAKEFS_ENDPOINT","http://localhost:8000"))
os.environ.setdefault("AWS_ACCESS_KEY_ID", os.getenv("AWS_ACCESS_KEY_ID","lakefs"))
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", os.getenv("AWS_SECRET_ACCESS_KEY","lakefssecret"))
os.environ.setdefault("MLFLOW_TRACKING_URI", os.getenv("MLFLOW_TRACKING_URI","http://localhost:5000"))

run_short = uuid.uuid4().hex[:12]
RUN_ID = f"run://{ORG}/{TENANT}/{ENV}/{PROJECT}/{PIPELINE}/{run_short}"

def ensure_mlflow_experiment():
    artifact_root = f"s3://{REPO}/{BRANCH}/mlflow/{PROJECT}/{PIPELINE}"
    c = MlflowClient()
    name = f"{PROJECT}/{ENV}/{PIPELINE}"
    exp = c.get_experiment_by_name(name)
    exp_id = exp.experiment_id if exp else c.create_experiment(name, artifact_location=artifact_root)
    mlflow.set_experiment(experiment_id=exp_id)
    return exp_id

def write_prompt_artifact():
    with mlflow.start_run(run_name=f"{PIPELINE}-{run_short}"):
        mlflow.set_tags({"run_id": RUN_ID, "org": ORG, "tenant": TENANT, "env": ENV, "project": PROJECT})
        mlflow.log_text("You are prompt_v17 ...", "prompts/prompt_v17.txt")

def write_lance_rows():
    db = lancedb.connect(f"s3://{REPO}/{BRANCH}")
    tbl_name = "qa_outputs.lance"
    try:
        tbl = db.open_table(tbl_name)
    except Exception:
        schema = pa.schema([
            ("org", pa.string()), ("tenant", pa.string()), ("env", pa.string()), ("project", pa.string()),
            ("run_id", pa.string()), ("doc_id", pa.string()), ("question_id", pa.string()),
            ("prompt_id", pa.string()), ("model_name", pa.string()), ("answer", pa.large_string()),
            ("answer_tokens", pa.int32()), ("latency_ms", pa.int32()),
            ("created_at", pa.timestamp("us")), ("prov_ref", pa.string())
        ])
        tbl = db.create_table(tbl_name, schema=schema)

    row = {
        "org": ORG, "tenant": TENANT, "env": ENV, "project": PROJECT,
        "run_id": RUN_ID, "doc_id": "AGR-1211", "question_id": "q-03",
        "prompt_id": "prompt_v17", "model_name": "gpt-4o-mini",
        "answer": "CREDIT AGREEMENT ...", "answer_tokens": 143, "latency_ms": 780,
        "created_at": datetime.datetime.utcnow(),  # arrow will coerce
        "prov_ref": f"s3://{REPO}/{BRANCH}/artifacts/{PROJECT}/{PIPELINE}/{run_short}/manifest.json"
    }
    tbl.add([row])
    return tbl.version()

def write_manifest(lance_snapshot:int):
    s3=boto3.client("s3",
        endpoint_url=os.getenv("MLFLOW_S3_ENDPOINT_URL","http://localhost:8000"),
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID","lakefs"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY","lakefssecret"))
    key=f"artifacts/{PROJECT}/{PIPELINE}/{run_short}/manifest.json"
    manifest={
        "org": ORG, "tenant": TENANT,
        "run_id": RUN_ID,
        "pipeline": f"pipeline://{ORG}/{TENANT}/{ENV}/{PROJECT}/{PIPELINE}",
        "context": {"env": ENV, "repo": REPO, "branch": BRANCH},
        "bindings": {
            "lakefs": {"repo": REPO, "branch": BRANCH, "commit_before": None, "commit_after": None},
            "lance": [{"table": "qa_outputs_dev", "snapshot": int(lance_snapshot)}],
            "mlflow": {"experiment": f"{PROJECT}/{ENV}/{PIPELINE}"}
        },
        "status": "CANDIDATE",
        "outputs": [f"data+table://{ORG}/{TENANT}/{ENV}/{PROJECT}/qa_outputs"]
    }
    s3.put_object(Bucket=REPO, Key=key, Body=json.dumps(manifest).encode("utf-8"))
    print("Manifest written:", f"s3://{REPO}/{BRANCH}/{key}")

def main():
    ensure_mlflow_experiment()
    write_prompt_artifact()
    snap = write_lance_rows()
    write_manifest(snap)
    print("Run complete:", RUN_ID)
    print("Lance dataset path:", f"s3://{REPO}/{BRANCH}/qa_outputs.lance/")

if __name__ == "__main__":
    main()
