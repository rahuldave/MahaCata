#!/usr/bin/env bash
# Run the whole provcat tour, and print each command before its output.
# Run it from the top folder of the repository:
#   bash tours/provcat/run.sh            # the steps that need no network
#   bash tours/provcat/run.sh --online   # also the steps that read GitHub and Hugging Face
set -euo pipefail
cd "$(dirname "$0")/../.."
# Stop the tour on any exit, but only a tour that this run started.
trap 'if [ -n "${_PROVCAT_TOUR_SAVED:-}" ]; then source tours/provcat/stop.sh; fi' EXIT
source tours/provcat/start.sh > /dev/null

show() { echo "\$ $*"; eval "$@" 2>&1; echo; }
step() { echo "==== $*"; echo; }

step "Step 1: Read a local file by its name"
show uv run provc resolve notes://sales/targets.csv
show uv run provc cat notes://sales/targets.csv

step "Step 2: Write to a bucket"
show "printf 'order,region,amount\n1,east,30\n2,west,12\n' > \$TOUR_DIR/day1.csv"
show uv run provc put files://sales/orders/day1.csv \$TOUR_DIR/day1.csv
show uv run provc resolve files://sales/orders/day1.csv
show uv run provc cat files://sales/orders/day1.csv

step "Step 3: Commit a folder, and read the commit after a change"
show 'FIRST=$(uv run provc commit files://sales/orders -m "first day" --meta source=till)'
show 'echo "$FIRST"'
show "printf 'order,region,amount\n1,east,30\n2,west,15\n' > \$TOUR_DIR/day1.csv"
show uv run provc put files://sales/orders/day1.csv \$TOUR_DIR/day1.csv
show uv run provc cat files://sales/orders/day1.csv
show 'ID=${FIRST#*commit:}'
show 'uv run provc cat "tour-s3://tour-sales/main/orders/day1.csv?at=commit:$ID"'

step "Step 4: Pin a name to a fixed version"
show uv run provc pin files://sales/orders/day1.csv
show uv run provc pin files://sales/orders
show 'VERSION=$(uv run provc pin files://sales/orders/day1.csv); VERSION=${VERSION#*version:}'
show 'uv run provc cat "files://sales/orders/day1.csv?at=version:$VERSION"'
show 'uv run provc cat "files://sales/orders/day1.csv?at=commit:$ID"'
show "uv run provc cat 'files://sales/orders/day1.csv?at=tag:v1' || true"

step "Step 5: Make a branch"
show uv run provc branch "'files://sales?at=branch:dev'" --from main
show uv run provc ls "'files://sales/orders?at=branch:dev'"

step "Step 6: Send one name somewhere else"
show uv run provc resolve files://sales/prices
show uv run provc cat files://sales/prices

step "Step 7: Fetch into the cache, and read offline"
show uv run provc fetch files://sales/orders
show "find \$TOUR_DIR/cache -type f -not -path '*/.provcat/*' | sort"
show uv run provc --offline cat files://sales/orders/day1.csv
show 'uv run provc --offline cat "tour-s3://tour-sales/main/orders/day1.csv?at=commit:$ID"'

step "Step 8: Give a program a snapshot"
show 'uv run python tours/provcat/snapshot.py "$FIRST"'

step "Step 9: Read through fsspec"
show uv run python tours/provcat/read_fsspec.py

step "Step 10: Watch and limit an app with hooks"
show export PROVCAT_HOOKS=tours/provcat/hooks.yaml PROVCAT_APP=tour
show uv run provc cat files://sales/prices
show "uv run provc cat tour-files:///shared/prices.csv || true"
show unset PROVCAT_HOOKS PROVCAT_APP

if [ "${1:-}" = "--online" ]; then
  step "Step 11: Read GitHub and Hugging Face (needs the network)"
  show uv run provc pin "'code://octocat/Hello-World/master/README'"
  show uv run provc cat "'code://octocat/Hello-World/master/README'"
  show uv run provc pin "'hf://models/hf-internal-testing/tiny-random-gpt2?at=ts:2023-01-01'"
  show uv run provc stat "'hf://models/hf-internal-testing/tiny-random-gpt2/config.json?at=ts:2023-01-01'"
fi
