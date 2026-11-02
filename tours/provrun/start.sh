# Start the provrun tour in this shell. Run it from the top folder of the
# repository:
#   source tours/provrun/start.sh
# It makes a scratch folder, points provcat and provrun at it, starts moto, a
# local S3 server, and a small OpenTelemetry collector, and puts the tour's
# files in place. tours/provrun/stop.sh stops both servers, deletes the
# folder, and puts back the variables that this file changed.

_tour_start() {
  local dir="${TOUR_DIR:-/tmp/provrun-tour}" port="${TOUR_S3_PORT:-5055}" otlp="${TOUR_OTLP_PORT:-4318}"
  case "$port" in
    ''|*[!0-9]*) echo "start.sh: TOUR_S3_PORT must be a number, not '$port'" >&2; return 1 ;;
  esac
  case "$otlp" in
    ''|*[!0-9]*) echo "start.sh: TOUR_OTLP_PORT must be a number, not '$otlp'" >&2; return 1 ;;
  esac
  if [ -n "${_PROVRUN_TOUR_SAVED:-}" ]; then
    echo "start.sh: a tour already runs in this shell; run 'source tours/provrun/stop.sh' first" >&2
    return 1
  fi
  if { [ -e "$dir" ] || [ -L "$dir" ]; } && [ ! -f "$dir/.provrun-tour" ]; then
    echo "start.sh: $dir exists and is not a tour folder; set TOUR_DIR to a new folder" >&2
    return 1
  fi
  if [ -f "$dir/.provrun-tour" ]; then
    echo "start.sh: a tour is already in $dir; run 'source tours/provrun/stop.sh' first" >&2
    return 1
  fi
  local p
  for p in "$port" "$otlp"; do
    if uv run python -c 'import socket, sys; sys.exit(socket.socket().connect_ex(("127.0.0.1", int(sys.argv[1]))) != 0)' "$p"; then
      echo "start.sh: port $p is in use; stop what runs there, or set TOUR_S3_PORT or TOUR_OTLP_PORT" >&2
      return 1
    fi
  done

  # Save the variables that the tour changes, so stop.sh can put them back.
  # The values stay in a shell variable that is not exported, and are never
  # printed; a token in OTEL_EXPORTER_OTLP_HEADERS is one of them.
  # The telemetry and proxy variables would change what the runs record, so
  # the tour unsets them.
  local saved="" name
  for name in PROVCAT_CONFIG PROVCAT_CACHE PROVCAT_HOOKS PROVCAT_APP PROVCAT_TENANT \
              PROVRUN_RUNS PROVRUN_TELEMETRY_POLICY PYTHONWARNINGS \
              AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_DEFAULT_REGION AWS_PROFILE AWS_SESSION_TOKEN \
              HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY http_proxy https_proxy all_proxy no_proxy grpc_proxy GRPC_PROXY no_grpc_proxy \
              PGHOST PGHOSTADDR PGPORT PGSERVICE PGSERVICEFILE \
              TOUR_DIR TOUR_S3_PORT TOUR_OTLP_PORT \
              $(env | grep -Eo '^(OTEL|LOGFIRE|MLFLOW|LITELLM)_[A-Za-z0-9_]*=' | tr -d =); do
    if printenv "$name" > /dev/null; then
      saved="$saved$(printf 'export %s=%q;' "$name" "$(printenv "$name")")"
    elif eval "[ -n \"\${$name+x}\" ]"; then
      # A shell-only variable comes back unexported: unset drops the export.
      saved="$saved$(printf 'unset %s; %s=%q;' "$name" "$name" "$(eval "printf '%s' \"\$$name\"")")"
    else
      saved="${saved}unset $name;"
    fi
  done
  _PROVRUN_TOUR_SAVED="$saved"
  # From here on, a failed start is this start's to undo.
  _provrun_tour_mine=1

  mkdir -p "$dir" && touch "$dir/.provrun-tour" || return 1
  export TOUR_DIR="$dir" TOUR_S3_PORT="$port" TOUR_OTLP_PORT="$otlp"
  for name in PROVCAT_HOOKS PROVCAT_APP PROVCAT_TENANT PROVRUN_TELEMETRY_POLICY AWS_PROFILE AWS_SESSION_TOKEN \
              HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY http_proxy https_proxy all_proxy no_proxy grpc_proxy GRPC_PROXY no_grpc_proxy \
              PGHOST PGHOSTADDR PGPORT PGSERVICE PGSERVICEFILE \
              $(env | grep -Eo '^(OTEL|LOGFIRE|MLFLOW|LITELLM)_[A-Za-z0-9_]*=' | tr -d =); do
    unset "$name"
  done
  cp tours/provrun/provcat.yaml tours/provrun/run.yaml tours/provrun/policy.yaml tours/provrun/mlflow.yaml \
    tours/provrun/agent.yaml "$TOUR_DIR/" || return 1
  export PROVCAT_CONFIG="$TOUR_DIR/provcat.yaml" PROVCAT_CACHE="$TOUR_DIR/cache" PROVRUN_RUNS="$TOUR_DIR/runs"
  # moto takes any keys.
  export AWS_ACCESS_KEY_ID=tour AWS_SECRET_ACCESS_KEY=tour AWS_DEFAULT_REGION=us-east-1
  # LiteLLM's logging can leave a coroutine unawaited as the process ends.
  # The warning says nothing about the run, so the tour hides it.
  export PYTHONWARNINGS="ignore:coroutine 'Logging.async_success_handler' was never awaited:RuntimeWarning"
  # The policy of Step 12 names the collector's port.
  printf 'version: 1\nlibraries:\n  litellm:\n    versions: "==1.83.0"\ndestinations:\n  collector:\n    kind: otlp\n    receives: names\n    endpoint: http://127.0.0.1:%s/v1/traces\nhosts:\n  - api.openai.com\n  - 127.0.0.1\n' \
    "$TOUR_OTLP_PORT" > "$TOUR_DIR/send.yaml" || return 1
  # The policy of Step 14: the collector, and a copy in MLflow, both with
  # token counts, for runs that call Anthropic.
  printf 'version: 1\nlibraries:\n  litellm:\n    versions: "==1.83.0"\ndestinations:\n  collector:\n    kind: otlp\n    receives: usage\n    endpoint: http://127.0.0.1:%s/v1/traces\n  own:\n    kind: mlflow\n    receives: usage\nhosts:\n  - api.anthropic.com\n  - 127.0.0.1\n' \
    "$TOUR_OTLP_PORT" > "$TOUR_DIR/online.yaml" || return 1

  uv run moto_server -p "$TOUR_S3_PORT" > "$TOUR_DIR/moto.log" 2>&1 &
  echo $! > "$TOUR_DIR/moto.pid"
  uv run python tours/provrun/collector.py "$TOUR_OTLP_PORT" > "$TOUR_DIR/collector.log" 2>&1 &
  echo $! > "$TOUR_DIR/collector.pid"
  local i up=""
  for i in $(seq 100); do
    if curl -s -m 2 -o /dev/null "http://localhost:$TOUR_S3_PORT" && \
       curl -s -m 2 -o /dev/null "http://127.0.0.1:$TOUR_OTLP_PORT"; then up=1; break; fi
    sleep 0.2
  done
  if [ -z "$up" ]; then
    echo "start.sh: moto or the collector did not start. The end of their logs:" >&2
    tail -n 5 "$TOUR_DIR/moto.log" "$TOUR_DIR/collector.log" >&2
    return 1
  fi
  uv run python tours/provrun/make_bucket.py data runs
}
# If the start fails halfway, stop what it started and give the shell back.
# A start that a check refused, such as a second start, leaves the tour that
# runs alone.
_provrun_tour_mine=""
# The || keeps a shell with set -e from exiting before the clean-up.
_tour_rc=0
_tour_start || _tour_rc=$?
if [ "$_tour_rc" -ne 0 ] && [ -n "$_provrun_tour_mine" ]; then source tours/provrun/stop.sh; fi
unset _provrun_tour_mine
unset -f _tour_start
# The status of the start is the status of this file.
# exit stands in for return when the file is run instead of sourced.
eval "unset _tour_rc; return $_tour_rc 2> /dev/null || exit $_tour_rc"
