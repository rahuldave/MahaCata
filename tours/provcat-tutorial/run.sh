#!/usr/bin/env bash
# Run the whole provcat tutorial, and print each command before its output.
# Run it from the top folder of the repository:
#   bash tours/provcat-tutorial/run.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
export TOUR_DIR="${TOUR_DIR:-/tmp/provcat-tutorial}" TOUR_S3_PORT="${TOUR_S3_PORT:-5055}"
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

show() { echo "\$ $*"; eval "$@" 2>&1; echo; }
step() { echo "==== $*"; echo; }

step "Step 1: Write the configuration"
show 'cp tours/provcat-tutorial/provcat.yaml $TOUR_DIR/'
show 'export PROVCAT_CONFIG=$TOUR_DIR/provcat.yaml PROVCAT_CACHE=$TOUR_DIR/cache'

step "Step 2: Read a local file by its name"
show 'mkdir -p $TOUR_DIR/files/sales'
show "printf 'region,target\\neast,40\\nwest,25\\n' > \$TOUR_DIR/files/sales/targets.csv"
show uv run provc resolve notes://sales/targets.csv
show uv run provc cat notes://sales/targets.csv

step "Step 3: Make a bucket, and write a file to it"
show 'export AWS_ACCESS_KEY_ID=tutorial AWS_SECRET_ACCESS_KEY=tutorial AWS_DEFAULT_REGION=us-east-1 AWS_CONFIG_FILE=/dev/null'
show uv run python tours/provcat-tutorial/make_bucket.py sales
show "printf 'order,region,amount\\n1,east,30\\n2,west,12\\n' > \$TOUR_DIR/day1.csv"
show uv run provc put data://sales/orders/day1.csv \$TOUR_DIR/day1.csv
show uv run provc resolve data://sales/orders/day1.csv
show uv run provc ls data://sales/orders

step "Step 4: Commit the folder"
show uv run provc commit data://sales/orders -m "'first day'"

step "Step 5: Pin the file, change it, and read the old version"
show 'PINNED=$(uv run provc pin data://sales/orders/day1.csv)'
show 'echo "$PINNED"'
show "printf 'order,region,amount\\n1,east,30\\n2,west,15\\n' > \$TOUR_DIR/day1.csv"
show uv run provc put data://sales/orders/day1.csv \$TOUR_DIR/day1.csv
show uv run provc cat data://sales/orders/day1.csv
show 'uv run provc cat "$PINNED"'

step "Step 6: Make a branch, and change it"
show uv run provc branch "'data://sales?at=branch:dev'" --from main
show "printf 'order,region,amount\\n1,east,30\\n2,west,15\\n3,north,8\\n' > \$TOUR_DIR/day1.csv"
show uv run provc put "'data://sales/orders/day1.csv?at=branch:dev'" \$TOUR_DIR/day1.csv
show uv run provc cat "'data://sales/orders/day1.csv?at=branch:dev'"
show uv run provc cat data://sales/orders/day1.csv

step "Step 7: Read from the cache, without asking the store"
show uv run provc fetch data://sales/orders/day1.csv
show 'uv run provc --offline cat "$PINNED"'

step "Step 8: Read by name from Python"
show uv run python tours/provcat-tutorial/read.py data://sales/orders/day1.csv
