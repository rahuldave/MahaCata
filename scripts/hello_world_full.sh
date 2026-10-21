#!/usr/bin/env bash
set -euo pipefail
CFG="config/resolver.sample.yaml"

echo "=== DATASET REGISTER ==="
python -m provcat.provc --cfg "$CFG" dataset create --uri "data+table://acme/shared/prod/agreements/qa_outputs" --name "QA Outputs" --desc "Extracted QA outputs"

echo "=== ENSURE LANCE TABLE ==="
python -m provcat.provc --cfg "$CFG" data ensure-table --uri "data+table://acme/shared/prod/agreements/qa_outputs" --schema-json '{"fields":[{"name":"id","type":"int64"},{"name":"ts","type":"timestamp"}]}'

echo "=== RESOLVE ARTIFACT PROMPT ==="
python -m provcat.provc --cfg "$CFG" resolve "artifact+prompt://acme/teams/prod/proj-credit-agreements/qa/default_v1"
