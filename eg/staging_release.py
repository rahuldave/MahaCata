#!/usr/bin/env python3
"""
STAGING release example: deterministically evaluates a chosen prompt and (in real life)
would validate then publish (merge). Here we just write Lance rows and print next steps.
"""
import os, uuid
import lancedb, pyarrow as pa

ORG=os.getenv("ORG","acme")
TENANT=os.getenv("TENANT","bigbank")
ENV="staging"
PROJECT=os.getenv("PROJECT","credit-agreements")
PIPELINE="qa"

REPO=os.getenv("LAKEFS_REPO","agreements-bigbank-staging")
BRANCH=os.getenv("BRANCH","release/qa-v17")

run_short = uuid.uuid4().hex[:12]
RUN_ID = f"run://{ORG}/{TENANT}/{ENV}/{PROJECT}/{PIPELINE}/{run_short}"

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

    tbl.add([{
        "org": ORG, "tenant": TENANT, "env": ENV, "project": PROJECT,
        "run_id": RUN_ID, "doc_id": "AGR-1211", "question_id": "q-03",
        "prompt_id": "prompt_v17", "model_name": "gpt-4o-mini",
        "answer": "CREDIT AGREEMENT (staging)...", "answer_tokens": 142, "latency_ms": 620,
        "created_at": pa.scalar("2025-09-28T18:00:00Z", type=pa.timestamp('us')).as_py(),
        "prov_ref": f"s3://{REPO}/{BRANCH}/artifacts/{PROJECT}/{PIPELINE}/{run_short}/manifest.json"
    }])
    return tbl.version()

def main():
    snap = write_lance_rows()
    print("STAGING run:", RUN_ID)
    print("Wrote Lance snapshot:", snap)
    print("Next: run validators, then publish via:")
    print(f"  lakefs merge --repository {REPO} --source {BRANCH} --destination main")

if __name__ == "__main__":
    main()
