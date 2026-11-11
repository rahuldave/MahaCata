#!/usr/bin/env bash
# Run the whole provevi tutorial, and print each command before its output.
# Run it from the top folder of the repository:
#   bash tours/provevi-tutorial/run.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
export TOUR_DIR="${TOUR_DIR:-/tmp/provevi-tutorial}"
if [ -e "$TOUR_DIR" ]; then
  echo "run.sh: $TOUR_DIR exists; delete it, or set TOUR_DIR to a new folder" >&2
  exit 1
fi
# The variables that would change what a run records, or where provcat looks,
# and those of telemetry, Postgres and proxies, which a run records.
unset PROVCAT_CONFIG PROVCAT_CACHE PROVCAT_HOOKS PROVCAT_APP PROVCAT_TENANT PROVRUN_RUNS PROVRUN_TELEMETRY_POLICY PYDANTIC_AI_NO_BANNER
for v in $(env | command grep -oiE '^((OTEL|LOGFIRE|MLFLOW|LITELLM)_|PG|[A-Z_]*PROXY)[A-Z0-9_]*'); do unset $v; done
mkdir -p "$TOUR_DIR"
# Staged inputs are read-only, so the folder needs write access back first.
trap 'chmod -R u+w "$TOUR_DIR"; rm -rf "$TOUR_DIR"' EXIT
export PROVCAT_CACHE="$TOUR_DIR/cache"
export PYTHONWARNINGS="ignore:coroutine 'Logging.async_success_handler' was never awaited:RuntimeWarning"

show() { echo "\$ $*"; eval "$@" 2>&1; echo; }
step() { echo "==== $*"; echo; }

step "Step 1: Make the evidence of two runs"
show 'sed "s|TOUR_DIR|$TOUR_DIR|" tours/provevi-tutorial/run.yaml > $TOUR_DIR/run.yaml'
show 'export PROVRUN_RUNS=$TOUR_DIR/runs S=sqlite:///$TOUR_DIR/evidence.db'
show 'echo "Revenue in the third quarter was 42 million." > $TOUR_DIR/q3.txt'
show 'uv run provr --store $S run $TOUR_DIR/run.yaml > $TOUR_DIR/r1.json'
show 'echo "Revenue in the third quarter was 45 million, after a restatement." > $TOUR_DIR/q3.txt'
show 'uv run provr --store $S run $TOUR_DIR/run.yaml > $TOUR_DIR/r2.json'
show 'R1=$(uv run python tours/provevi-tutorial/field.py $TOUR_DIR/r1.json run_id)'
show 'R2=$(uv run python tours/provevi-tutorial/field.py $TOUR_DIR/r2.json run_id)'

step "Step 2: List the runs and the agents"
show uv run prove --store \$S runs
show uv run prove --store \$S agents
show uv run prove --store \$S agent show acme/finance/dev/revenue-question

step "Step 3: Find the runs that read a file"
show uv run prove --store \$S runs --reading file://\$TOUR_DIR/q3.txt

step "Step 4: Read the events of a run"
show "uv run prove --store \$S show \$R1 | awk '{print \$1, \$3, \$4}'"
show uv run prove --store \$S access \$R1

step "Step 5: Check a run"
show uv run prove --store \$S verify \$R1

step "Step 6: Break the store's copy, and check again"
show uv run python tours/provevi-tutorial/break_row.py \$TOUR_DIR/evidence.db \$R1 3
show "uv run prove --store \$S verify \$R1 || echo exit status \$?"
show uv run prove verify \$TOUR_DIR/runs/\$R1

step "Step 7: Ask the same questions from Python"
show uv run python tours/provevi-tutorial/query.py \$S file://\$TOUR_DIR/q3.txt

step "Step 8: Run under a telemetry policy"
show 'cp tours/provevi-tutorial/policy.yaml $TOUR_DIR/'
show uv run prove policy \$TOUR_DIR/policy.yaml
show 'uv run provr --store $S run --policy $TOUR_DIR/policy.yaml $TOUR_DIR/run.yaml > $TOUR_DIR/r3.json'
show 'R3=$(uv run python tours/provevi-tutorial/field.py $TOUR_DIR/r3.json run_id)'
show uv run python tours/provevi-tutorial/policy_of.py \$PROVRUN_RUNS \$R2 \$R3
show uv run prove --store \$S telemetry \$R3
