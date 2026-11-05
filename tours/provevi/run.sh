#!/usr/bin/env bash
# Run the whole provevi tour, and print each command before its output.
# Run it from the top folder of the repository:
#   bash tours/provevi/run.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
# Stop the tour on any exit, but only a tour that this run started.
trap 'if [ -n "${_PROVEVI_TOUR_SAVED:-}" ]; then source tours/provevi/stop.sh; fi' EXIT
source tours/provevi/start.sh > /dev/null

show() { echo "\$ $*"; eval "$@" 2>&1; echo; }
step() { echo "==== $*"; echo; }

step "Step 1: Run an agent, and keep its evidence in SQLite"
show 'uv run provr --store $STORE run $TOUR_DIR/run.yaml 2> /dev/null > $TOUR_DIR/first.json'
show 'RUN1=$(uv run python tours/provevi/run_id.py $TOUR_DIR/first.json)'
show "printf 'Revenue in 2025 was 45 million.\n' > \$TOUR_DIR/report.txt"
show 'uv run provr --store $STORE run $TOUR_DIR/run.yaml 2> /dev/null > $TOUR_DIR/second.json'
show 'RUN2=$(uv run python tours/provevi/run_id.py $TOUR_DIR/second.json)'
show 'echo $RUN1 $RUN2'

step "Step 2: Ask which runs read a file"
show uv run prove --store \$STORE runs
show uv run prove --store \$STORE experiments
show uv run prove --store \$STORE runs --reading file://\$TOUR_DIR/report.txt
show uv run prove --store \$STORE runs --experiment acme/demo/dev/revenue-question

step "Step 3: Read the events of a run"
show "uv run prove --store \$STORE show \$RUN1 | awk '{print \$1, \$3, \$4}'"
show uv run prove --store \$STORE access \$RUN1

step "Step 4: Check a run, and break one"
show uv run prove --store \$STORE verify \$RUN1
show uv run python tours/provevi/break_row.py \$TOUR_DIR/evidence.db \$RUN1 3
show "uv run prove --store \$STORE verify \$RUN1 || echo exit \$?"
show uv run prove verify \$RUN1

step "Step 5: Check a telemetry policy"
show uv run prove policy tours/provevi/policy.yaml
show uv run prove --store \$STORE telemetry \$RUN2

step "Step 6: Copy a run into MLflow"
show "uv run prove --store \$STORE mlflow \$RUN2 || echo exit \$?"
show uv run prove --store \$STORE mlflow \$RUN2 --policy tours/provevi/policy.yaml
show uv run prove --store \$STORE mlflow \$RUN2 --policy tours/provevi/policy.yaml
show "uv run prove --store \$STORE mlflow \$RUN1 --policy tours/provevi/policy.yaml || echo exit \$?"

step "Step 7: Ask the same questions from Python"
show uv run python tours/provevi/query.py

step "Step 8: Show a finished run to a live viewer"
show uv run python tours/provevi/replay.py \$RUN2 names
show uv run python tours/provevi/replay.py \$RUN2 bodies
