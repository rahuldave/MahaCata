#!/usr/bin/env bash
# Run the whole tutorial, and print each command before its output.
# Run it from the top folder of the repository:
#   bash tours/tutorial/run.sh            # every step but the last, with no model and no key
#   bash tours/tutorial/run.sh --online   # also the last step, which calls a real model
set -euo pipefail
cd "$(dirname "$0")/../.."
if [ "${1:-}" = "--online" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "run.sh: --online needs ANTHROPIC_API_KEY" >&2
  exit 1
fi
export TOUR_DIR="${TOUR_DIR:-/tmp/mahacata-tutorial}" TOUR_S3_PORT="${TOUR_S3_PORT:-5055}"
if [ -e "$TOUR_DIR" ]; then
  echo "run.sh: $TOUR_DIR exists; delete it, or set TOUR_DIR to a new folder" >&2
  exit 1
fi
# The variables that would change what a run records, or where provcat looks,
# and those of telemetry, Postgres and proxies, which a run records.
unset PROVCAT_CONFIG PROVCAT_CACHE PROVCAT_HOOKS PROVCAT_APP PROVCAT_TENANT PROVRUN_RUNS PROVRUN_TELEMETRY_POLICY PYDANTIC_AI_NO_BANNER AWS_PROFILE AWS_SESSION_TOKEN
for v in $(env | command grep -oiE '^((OTEL|LOGFIRE|MLFLOW|LITELLM)_|PG|[A-Z_]*PROXY)[A-Z0-9_]*'); do unset $v; done
if (exec 3<> "/dev/tcp/127.0.0.1/$TOUR_S3_PORT") 2> /dev/null; then
  echo "run.sh: port $TOUR_S3_PORT is in use; stop what runs there, or set TOUR_S3_PORT to another port" >&2
  exit 1
fi
mkdir -p "$TOUR_DIR"
uv run moto_server -p "$TOUR_S3_PORT" > "$TOUR_DIR/moto.log" 2>&1 &
MOTO=$!
# Snapshots and staged inputs are read-only, so the folder needs write access back first.
trap 'kill $MOTO 2> /dev/null; wait $MOTO 2> /dev/null; chmod -R u+w "$TOUR_DIR"; rm -rf "$TOUR_DIR"' EXIT
for _ in $(seq 100); do
  curl -s "http://localhost:$TOUR_S3_PORT" > /dev/null && break
  sleep 0.1
done
if ! curl -s "http://localhost:$TOUR_S3_PORT" > /dev/null; then
  echo "run.sh: moto did not start on port $TOUR_S3_PORT" >&2
  exit 1
fi
export PYTHONWARNINGS="ignore:coroutine 'Logging.async_success_handler' was never awaited:RuntimeWarning"

show() { echo "\$ $*"; eval "$@" 2>&1; echo; }
step() { echo "==== $*"; echo; }

step "Step 1: Make a bucket"
show 'export AWS_ACCESS_KEY_ID=tutorial AWS_SECRET_ACCESS_KEY=tutorial AWS_DEFAULT_REGION=us-east-1 AWS_CONFIG_FILE=/dev/null'
show uv run python tours/tutorial/make_bucket.py data

step "Step 2: Name the report"
show 'cp tours/tutorial/provcat.yaml $TOUR_DIR/'
show 'export PROVCAT_CONFIG=$TOUR_DIR/provcat.yaml PROVCAT_CACHE=$TOUR_DIR/cache'
show uv run provc resolve data://acme/finance/dev/reports/q3.txt

step "Step 3: Put the report in the bucket, and commit it"
show 'echo "Revenue in the third quarter was 42 million." > $TOUR_DIR/q3.txt'
show uv run provc put data://acme/finance/dev/reports/q3.txt \$TOUR_DIR/q3.txt
show uv run provc commit data://acme/finance/dev/reports -m "'first report'"
show uv run provc pin data://acme/finance/dev/reports/q3.txt

step "Step 4: Write the run file"
show 'cp tours/tutorial/run.yaml $TOUR_DIR/'

step "Step 5: Run the agent"
show 'export PROVRUN_RUNS=$TOUR_DIR/runs S=sqlite:///$TOUR_DIR/evidence.db'
show 'uv run provr --store $S run $TOUR_DIR/run.yaml | tee $TOUR_DIR/r1.json'
show 'R1=$(uv run python tours/tutorial/field.py $TOUR_DIR/r1.json run_id)'

step "Step 6: Look at what the agent read"
show 'find $TOUR_DIR/runs/$R1 -type f | sort'
show 'cat $TOUR_DIR/runs/$R1/input/q3.txt'
show 'uv run python tours/tutorial/modes.py $TOUR_DIR/runs/$R1/input $TOUR_DIR/runs/$R1/input/q3.txt'

step "Step 7: Read the evidence"
show 'uv run prove --store $S show $R1 | awk '\''{print $1, $3, $4}'\'''
show 'uv run prove --store $S access $R1'

step "Step 8: Change the report, and run again"
show 'echo "Revenue in the third quarter was 45 million, after a restatement." > $TOUR_DIR/q3.txt'
show uv run provc put data://acme/finance/dev/reports/q3.txt \$TOUR_DIR/q3.txt
show uv run provc commit data://acme/finance/dev/reports -m "'restated report'"
show 'uv run provr --store $S run $TOUR_DIR/run.yaml > $TOUR_DIR/r2.json'
show 'R2=$(uv run python tours/tutorial/field.py $TOUR_DIR/r2.json run_id)'
show 'uv run prove --store $S runs --reading data://acme/finance/dev/reports/q3.txt'
show 'uv run python tours/tutorial/pins.py $S $R1 $R2'

step "Step 9: Check the runs, and break one"
show 'uv run prove --store $S verify $R1'
show 'echo "Revenue was 99 million." > $TOUR_DIR/runs/$R2/output/response.txt'
show 'uv run prove --store $S verify $R2 || echo "exit status $?"'

if [ "${1:-}" = "--online" ]; then
  step "Step 10: Ask a real model (needs ANTHROPIC_API_KEY)"
  show 'grep -v mock_response $TOUR_DIR/run.yaml | grep -v "^  settings:" > $TOUR_DIR/real.yaml'
  show 'uv run provr --store $S run $TOUR_DIR/real.yaml > $TOUR_DIR/r3.json'
  show 'R3=$(uv run python tours/tutorial/field.py $TOUR_DIR/r3.json run_id)'
  show 'cat $TOUR_DIR/runs/$R3/output/response.txt'
fi
