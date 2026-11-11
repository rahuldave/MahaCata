#!/usr/bin/env bash
# Run the whole provrun tutorial, and print each command before its output.
# Run it from the top folder of the repository:
#   bash tours/provrun-tutorial/run.sh            # every step but the last, with no model and no key
#   bash tours/provrun-tutorial/run.sh --online   # also the last step, which calls a real model
set -euo pipefail
cd "$(dirname "$0")/../.."
if [ "${1:-}" = "--online" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "run.sh: --online needs ANTHROPIC_API_KEY" >&2
  exit 1
fi
export TOUR_DIR="${TOUR_DIR:-/tmp/provrun-tutorial}"
if [ -e "$TOUR_DIR" ]; then
  echo "run.sh: $TOUR_DIR exists; delete it, or set TOUR_DIR to a new folder" >&2
  exit 1
fi
# The variables that would change what a run records, or where provcat looks,
# and those of telemetry, Postgres and proxies, which a run records.
unset PROVCAT_CONFIG PROVCAT_CACHE PROVCAT_HOOKS PROVCAT_APP PROVCAT_TENANT PROVRUN_RUNS PROVRUN_TELEMETRY_POLICY PYDANTIC_AI_NO_BANNER
for v in $(env | command grep -oiE '^((OTEL|LOGFIRE|MLFLOW|LITELLM)_|PG|[A-Z_]*PROXY)[A-Z0-9_]*'); do unset $v; done
mkdir -p "$TOUR_DIR"
# Snapshots and staged inputs are read-only, so the folder needs write access back first.
trap 'chmod -R u+w "$TOUR_DIR"; rm -rf "$TOUR_DIR"' EXIT
export PYTHONWARNINGS="ignore:coroutine 'Logging.async_success_handler' was never awaited:RuntimeWarning"

show() { echo "\$ $*"; eval "$@" 2>&1; echo; }
step() { echo "==== $*"; echo; }

step "Step 1: Write the report, and the run file"
show 'echo "Revenue in the third quarter was 42 million." > $TOUR_DIR/q3.txt'
show 'sed "s|TOUR_DIR|$TOUR_DIR|" tours/provrun-tutorial/run.yaml > $TOUR_DIR/run.yaml'

step "Step 2: Run the agent"
show 'export PROVRUN_RUNS=$TOUR_DIR/runs PROVCAT_CACHE=$TOUR_DIR/cache'
show 'uv run provr run $TOUR_DIR/run.yaml | tee $TOUR_DIR/r1.json'
show 'R1=$(uv run python tours/provrun-tutorial/field.py $TOUR_DIR/r1.json run_id)'

step "Step 3: Look at the run folder"
show 'find $PROVRUN_RUNS/$R1 -type f | sort'
show 'uv run python tours/provrun-tutorial/modes.py $PROVRUN_RUNS/$R1/input/q3.txt $PROVRUN_RUNS/$R1/output/response.txt'
show 'cat $PROVRUN_RUNS/$R1/output/response.txt; echo'

step "Step 4: Read the events"
show 'uv run provr show $R1 | awk '\''{print $1, $3, $4}'\'''
show 'uv run python tours/provrun-tutorial/event.py $R1 snapshot_materialized'

step "Step 5: Read the prompt and the answer"
show 'uv run python tours/provrun-tutorial/payload.py $R1 prompt_rendered'
show 'uv run python tours/provrun-tutorial/payload.py $R1 model_called'

step "Step 6: Ask for an input that the run may not read"
show 'sed "s/env: dev/env: prod/" $TOUR_DIR/run.yaml > $TOUR_DIR/prod.yaml'
show 'uv run provr run $TOUR_DIR/prod.yaml > $TOUR_DIR/r2.json || echo "exit status $?"'
show 'uv run python tours/provrun-tutorial/field.py $TOUR_DIR/r2.json error'
show 'R2=$(uv run python tours/provrun-tutorial/field.py $TOUR_DIR/r2.json run_id)'
show 'uv run provr show $R2 | awk '\''{print $1, $3, $4}'\'''
show 'uv run python tours/provrun-tutorial/event.py $R2 access_denied reason'

step "Step 7: Check a run"
show 'uv run provr verify $R1'
show 'uv run provr verify $R2'

step "Step 8: Ask for the reply in pieces"
show '{ cat $TOUR_DIR/run.yaml; echo "  stream: true"; } > $TOUR_DIR/stream.yaml'
show 'uv run provr run $TOUR_DIR/stream.yaml > $TOUR_DIR/r3.json'
show 'R3=$(uv run python tours/provrun-tutorial/field.py $TOUR_DIR/r3.json run_id)'
show 'uv run python tours/provrun-tutorial/event.py $R3 model_called streamed output_tokens'

step "Step 9: Run it from Python"
show 'cat tours/provrun-tutorial/ask.py'
show 'uv run python tours/provrun-tutorial/ask.py $TOUR_DIR/run.yaml'

if [ "${1:-}" = "--online" ]; then
  step "Step 10: Ask a real model (needs ANTHROPIC_API_KEY)"
  show 'grep -v mock_response $TOUR_DIR/run.yaml | grep -v "^  settings:" > $TOUR_DIR/real.yaml'
  show 'uv run python tours/provrun-tutorial/ask.py $TOUR_DIR/real.yaml'
fi
