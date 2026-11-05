#!/usr/bin/env bash
# Run the whole provrun tour, and print each command before its output.
# Run it from the top folder of the repository:
#   bash tours/provrun/run.sh            # the steps that need no model and no key
#   bash tours/provrun/run.sh --online   # also the steps that call a real model
set -euo pipefail
cd "$(dirname "$0")/../.."
if [ "${1:-}" = "--online" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "run.sh: --online needs ANTHROPIC_API_KEY" >&2
  exit 1
fi
# Stop the tour on any exit, but only a tour that this run started.
trap 'if [ -n "${_PROVRUN_TOUR_SAVED:-}" ]; then source tours/provrun/stop.sh; fi' EXIT
source tours/provrun/start.sh > /dev/null

show() { echo "\$ $*"; eval "$@" 2>&1; echo; }
step() { echo "==== $*"; echo; }

step "Step 1: Put a file in the store, and fetch it"
show 'echo "Revenue in 2025 was 42 million." > $TOUR_DIR/report.txt'
show uv run provc put data://acme/demo/dev/reports/report.txt \$TOUR_DIR/report.txt
show uv run provc commit data://acme/demo/dev/reports -m "'first report'"
show uv run provc pin data://acme/demo/dev/reports/report.txt
show uv run provc fetch data://acme/demo/dev/reports/report.txt

step "Step 2: Run the agent"
show 'uv run provr run $TOUR_DIR/run.yaml | tee $TOUR_DIR/r1.json'
show 'R1=$(uv run python tours/provrun/field.py $TOUR_DIR/r1.json run_id)'

step "Step 3: Look at the cache"
show 'find $TOUR_DIR/cache -type f -not -path "*/.provcat/locks/*" | sort'
show 'uv run python tours/provrun/modes.py $TOUR_DIR/cache/.provcat/snapshots/file/*/report.txt'

step "Step 4: Look at the run folder"
show 'find $TOUR_DIR/runs/$R1 -type f | sort'
show 'uv run python tours/provrun/modes.py $TOUR_DIR/runs/$R1/input $TOUR_DIR/runs/$R1/input/report.txt'

step "Step 5: Read the evidence"
show 'uv run provr show $R1 | awk '\''{print $1, $3, $4}'\'''
show 'uv run python tours/provrun/event.py $R1 snapshot_materialized'

step "Step 6: Change the file, and run again"
show 'echo "Revenue in 2025 was 45 million, after a restatement." > $TOUR_DIR/report.txt'
show uv run provc put data://acme/demo/dev/reports/report.txt \$TOUR_DIR/report.txt
show uv run provc commit data://acme/demo/dev/reports -m "'restated report'"
show 'uv run provr run $TOUR_DIR/run.yaml > $TOUR_DIR/r2.json'
show 'R2=$(uv run python tours/provrun/field.py $TOUR_DIR/r2.json run_id)'
show 'uv run python tours/provrun/event.py $R2 snapshot_materialized pinned sha256'
show 'for f in $TOUR_DIR/cache/.provcat/snapshots/file/*/report.txt; do echo "$f: $(cat $f)"; done'

step "Step 7: Run again on the first version"
show 'PINNED=$(uv run python tours/provrun/event.py $R1 snapshot_materialized pinned)'
show 'echo "$PINNED"'
show 'sed "s|- data://acme/demo/dev/reports/report.txt|- \"$PINNED\"|" $TOUR_DIR/run.yaml > $TOUR_DIR/rerun.yaml'
show 'uv run provr run $TOUR_DIR/rerun.yaml > $TOUR_DIR/r3.json'
show 'R3=$(uv run python tours/provrun/field.py $TOUR_DIR/r3.json run_id)'
show 'uv run python tours/provrun/event.py $R3 access_granted reason'
show 'cat $TOUR_DIR/runs/$R3/input/report.txt'

step "Step 8: Verify the runs, and break one"
show 'uv run provr verify $R1'
show 'uv run provr verify $R2'
show 'echo "Revenue was 99 million." > $TOUR_DIR/runs/$R2/output/response.txt'
show 'uv run provr verify $R2 || echo "exit status $?"'
show 'BUNDLE=$(uv run python tours/provrun/field.py $TOUR_DIR/r1.json pinned)'
show 'uv run provr verify $R1 --bundle "$BUNDLE" --require-bundle'

step "Step 9: Keep the evidence in SQLite"
show 'S=sqlite:///$TOUR_DIR/evidence.db'
show 'uv run provr --store $S run $TOUR_DIR/run.yaml > $TOUR_DIR/r4.json'
show 'uv run provr --store $S run $TOUR_DIR/run.yaml > /dev/null'
show 'Q=$(uv run python tours/provrun/field.py $TOUR_DIR/r4.json run_id)'
show 'uv run prove --store $S runs --reading data://acme/demo/dev/reports/report.txt'
show 'uv run prove --store $S experiments'
show 'uv run prove --store $S access $Q'
show 'uv run python tours/provrun/drop_event.py $TOUR_DIR/evidence.db $Q 3'
show 'uv run prove --store $S verify $Q || echo "exit status $?"'

step "Step 10: Run under a telemetry policy"
show 'cat $TOUR_DIR/policy.yaml'
show export PROVRUN_TELEMETRY_POLICY=\$TOUR_DIR/policy.yaml
show 'OTEL_EXPORTER_OTLP_ENDPOINT=http://collector.example:4318 \
  OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer example-token" \
  MLFLOW_TRACKING_URI="postgresql://mlflow:example-password@db.example:5432/mlflow" \
  LITELLM_MODE=DEV \
  uv run provr run $TOUR_DIR/run.yaml > $TOUR_DIR/r5.json'
show 'R4=$(uv run python tours/provrun/field.py $TOUR_DIR/r5.json run_id)'
show 'uv run python tours/provrun/event.py $R4 run_created telemetry'
show 'uv run prove telemetry $PROVRUN_RUNS/$R4'
show 'cp $TOUR_DIR/run.yaml $TOUR_DIR/wide.yaml'
show "printf 'telemetry:\n  hosts: [api.openai.com, paste.example]\n' >> \$TOUR_DIR/wide.yaml"
show 'uv run provr run $TOUR_DIR/wide.yaml || echo "exit status $?"'
show unset PROVRUN_TELEMETRY_POLICY

step "Step 11: See what a run turns off"
show 'uv run python tours/provrun/quiet.py > $TOUR_DIR/quiet.out 2> $TOUR_DIR/quiet.err'
show 'cat $TOUR_DIR/quiet.out'
show 'R5=$(head -1 $TOUR_DIR/quiet.out | cut -d" " -f1)'
show 'uv run prove telemetry $PROVRUN_RUNS/$R5'
show 'uv run python tours/provrun/spans.py $R5'

step "Step 12: Send the spans to a collector"
show 'cat $TOUR_DIR/send.yaml'
show 'PROVRUN_TELEMETRY_POLICY=$TOUR_DIR/send.yaml \
  OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:$TOUR_OTLP_PORT \
  HTTPS_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 NO_PROXY=localhost \
  uv run provr run $TOUR_DIR/run.yaml > $TOUR_DIR/r6.json'
show 'R6=$(uv run python tours/provrun/field.py $TOUR_DIR/r6.json run_id)'
show 'uv run python tours/provrun/event.py $R6 run_created telemetry.destinations telemetry.redirects'
show 'cat $TOUR_DIR/collector.log'

step "Step 13: Copy a run into MLflow"
show 'cat $TOUR_DIR/mlflow.yaml'
show 'M=sqlite:///$TOUR_DIR/mlflow-evidence.db'
show 'PROVRUN_TELEMETRY_POLICY=$TOUR_DIR/mlflow.yaml uv run provr --store $M run $TOUR_DIR/run.yaml > $TOUR_DIR/r7.json'
show 'uv run python tours/provrun/field.py $TOUR_DIR/r7.json mlflow'
show 'uv run python tours/provrun/mlflow_spans.py $TOUR_DIR/r7.json'
show 'PROVRUN_TELEMETRY_POLICY=$TOUR_DIR/mlflow.yaml MLFLOW_TRACKING_URI=sqlite:///$TOUR_DIR/elsewhere.db \
  uv run provr --store $M run $TOUR_DIR/run.yaml > $TOUR_DIR/r8.json'
show 'R8=$(uv run python tours/provrun/field.py $TOUR_DIR/r8.json run_id)'
show 'uv run python tours/provrun/event.py $R8 telemetry_blocked detail'
show 'test -e $TOUR_DIR/elsewhere.db || echo "no copy"'

step "Step 14: Ask for the reply in pieces"
show '{ cat $TOUR_DIR/run.yaml; echo "  stream: true"; } > $TOUR_DIR/stream.yaml'
show 'tail -3 $TOUR_DIR/stream.yaml'
show 'uv run provr run $TOUR_DIR/stream.yaml > $TOUR_DIR/r11.json'
show 'R11=$(uv run python tours/provrun/field.py $TOUR_DIR/r11.json run_id)'
show 'uv run python tours/provrun/event.py $R11 model_called streamed input_tokens output_tokens finish_reason'
show 'cat $PROVRUN_RUNS/$R11/output/response.txt; echo'
show 'uv run python tours/provrun/follow.py $TOUR_DIR/stream.yaml'

if [ "${1:-}" = "--online" ]; then
  step "Step 15: Call a real model (needs ANTHROPIC_API_KEY)"
  show 'sed -e "s|openai/gpt-4o-mini|anthropic/claude-haiku-5-5|" -e "/settings:/,\$d" $TOUR_DIR/run.yaml > $TOUR_DIR/real.yaml'
  show 'cat $TOUR_DIR/online.yaml'
  show 'export PROVRUN_TELEMETRY_POLICY=$TOUR_DIR/online.yaml OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:$TOUR_OTLP_PORT LOGFIRE_TOKEN=pylf_v1_us_example-token'
  show 'O=sqlite:///$TOUR_DIR/online.db'
  show 'N=$(wc -l < $TOUR_DIR/collector.log)'
  show 'uv run provr --store $O run $TOUR_DIR/real.yaml > $TOUR_DIR/r9.json'
  show 'R9=$(uv run python tours/provrun/field.py $TOUR_DIR/r9.json run_id)'
  show 'cat $PROVRUN_RUNS/$R9/output/response.txt; echo'
  show 'uv run python tours/provrun/event.py $R9 model_called model input_tokens output_tokens finish_reason'
  show 'uv run prove telemetry $PROVRUN_RUNS/$R9'
  show 'tail -n +$((N + 1)) $TOUR_DIR/collector.log'
  show 'uv run python tours/provrun/field.py $TOUR_DIR/r9.json mlflow'
  show 'uv run provr --store $O verify $R9'
  show 'cat $TOUR_DIR/agent.yaml'
  show 'N=$(wc -l < $TOUR_DIR/collector.log)'
  show 'uv run provr --store $O run $TOUR_DIR/agent.yaml > $TOUR_DIR/r10.json'
  show 'R10=$(uv run python tours/provrun/field.py $TOUR_DIR/r10.json run_id)'
  show 'cat $PROVRUN_RUNS/$R10/output/answer.txt'
  show 'uv run python tours/provrun/event_types.py $R10'
  show 'uv run prove telemetry $PROVRUN_RUNS/$R10'
  show 'tail -n +$((N + 1)) $TOUR_DIR/collector.log'
  show 'uv run python tours/provrun/field.py $TOUR_DIR/r10.json mlflow'
  show 'uv run provr --store $O verify $R10'
  show 'unset PROVRUN_TELEMETRY_POLICY OTEL_EXPORTER_OTLP_ENDPOINT LOGFIRE_TOKEN'

  step "Step 16: Stream a real model in your own agent (needs ANTHROPIC_API_KEY)"
  show 'sed -n "/^model = /,/^    print()/p" tours/provrun/streamed.py'
  show 'uv run python tours/provrun/streamed.py $TOUR_DIR/streamed'
fi
