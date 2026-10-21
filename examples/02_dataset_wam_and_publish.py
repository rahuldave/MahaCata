
"""Example 02: Dataset WAM + stage pointer + model publish (library-only).

- Dataset WAM: branch create -> merge
- Stage pointer set for production
- Publish a model version by writing lineage & model card and registering in MLflow

NOTE: This example focuses on library calls (no CLI).
"""
from provcat.branches import create as br_create, merge as br_merge
from provcat.stages import set_stage
from provcat.models import write_lineage, write_model_card, register_version

# Dataset repo & identifiers
REPO = "agreements-qa_outputs-data"
SOURCE_BRANCH = "main"
FEATURE = "feature/ingest-1"
ENV = "prod"
RESOURCE = "agreements/qa_outputs"

# Create feature branch and merge
br_create(repo=REPO, source_branch=SOURCE_BRANCH, new_branch=FEATURE)
br_merge(repo=REPO, source_branch=FEATURE, dest_branch="main", message="merge dataset ingest 1")

# Set stage pointer (production) to a known commit (stub here)
set_stage(cfg={}, env=ENV, repo=REPO, resource=RESOURCE, stage="production", commit_id="b8a2f9c123456")

# Model publish
VERSION_BASE = "s3://model-qa-extractor/main/versions/v1"
write_lineage(VERSION_BASE, produced_by_run="run://proj-credit-agreements/qa/demo-0002", inputs=[], commit_id="TBD")
write_model_card(VERSION_BASE, model_uri="model://acme/teams/prod/proj-credit-agreements/qa-extractor", name="QA Extractor", description="v1 via provcat example", version=1)
register_version(model_name="qa-extractor", version_base_s3=VERSION_BASE, description="v1 via example")
print("Example 02 completed.")
