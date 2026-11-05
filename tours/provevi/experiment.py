"""Write an experiment in the catalog twice, and read each version back.

The catalog's bucket is S3, which moto mocks in this process, so the step
needs no server and no network. A commit of the experiment's folder is a
version, and an old version still reads at its pin.
"""
import os
import sys
import tempfile

import boto3
from moto import mock_aws

from provcat import Catalog
from provevi import Experiment, ProveviError, read_experiment, write_experiment

for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
    os.environ[name] = "tour"
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"

URI = "experiment://acme/risk/dev/credit/revenue"
SCHEMES = {"experiment": {"parts": ["org", "team", "env", "project", "name"], "rest": "path", "ref": "main",
                          "target": "s3://experiments/{ref}/{org}/{team}/{env}/{project}/{name}/{path}"}}

with mock_aws(), tempfile.TemporaryDirectory() as cache:
    s3 = boto3.client("s3")
    s3.create_bucket(Bucket="experiments")
    s3.put_bucket_versioning(Bucket="experiments", VersioningConfiguration={"Status": "Enabled"})
    catalog = Catalog({"schemes": SCHEMES}, cache_root=cache)

    first = write_experiment(catalog, URI, Experiment(
        "agent-eval", "Does a retrieval step raise accuracy on the revenue questions?",
        ("data+table://acme/risk/dev/evals/revenue",), ("accuracy",)), "Ask about accuracy")
    print(f"version 1: sha256 {first.sha256[:12]}, metrics {' '.join(first.experiment.metrics)}")
    second = write_experiment(catalog, URI, Experiment(
        "agent-eval", "Does a retrieval step raise accuracy and recall on the revenue questions?",
        first.experiment.inputs, ("accuracy", "recall")), "Ask about recall too")
    print(f"version 2: sha256 {second.sha256[:12]}, metrics {' '.join(second.experiment.metrics)}")
    print("the pins differ:", first.pin != second.pin)
    print("the newest version is version 2:", read_experiment(catalog, URI).pin == second.pin)
    print("version 1 at its pin:", read_experiment(catalog, first.pin).experiment.description)
    print(f"the file at {URI}:")
    sys.stdout.write("  " + catalog.read(URI + "/experiment.yaml").decode().replace("\n", "\n  ").rstrip() + "\n")

    try:
        write_experiment(catalog, URI + "/experiment.yaml", first.experiment, "x")
    except ProveviError as e:
        print("refused:", e)
