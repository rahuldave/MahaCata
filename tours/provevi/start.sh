# Start the provevi tour in this shell. Run it from the top folder of the
# repository:
#   source tours/provevi/start.sh
# It makes a scratch folder with a document and a run file, and points
# provrun and provevi at the folder. It needs no server and no network.
# tours/provevi/stop.sh deletes the folder, and puts back the variables that
# this file changed.

_tour_start() {
  if [ ! -f tours/provevi/start.sh ]; then
    echo "start.sh: run it from the top folder of the repository" >&2
    return 1
  fi
  if [ -n "${_PROVEVI_TOUR_SAVED:-}" ]; then
    echo "start.sh: a tour already runs in this shell; run 'source tours/provevi/stop.sh' first" >&2
    return 1
  fi
  local dir="${TOUR_DIR:-/tmp/provevi-tour}"
  if { [ -e "$dir" ] || [ -L "$dir" ]; } && [ ! -f "$dir/.provevi-tour" ]; then
    echo "start.sh: $dir exists and is not a tour folder; set TOUR_DIR to a new folder" >&2
    return 1
  fi
  if [ -f "$dir/.provevi-tour" ]; then
    echo "start.sh: a tour is already in $dir; run 'source tours/provevi/stop.sh' first" >&2
    return 1
  fi
  case "$dir" in
    /*) ;;
    *) echo "start.sh: TOUR_DIR must be an absolute path, not '$dir'" >&2; return 1 ;;
  esac

  # Save the variables that the tour changes, so stop.sh can put them back.
  # A run records the telemetry variables of the shell, so the tour unsets
  # each one, as well as provrun's and provcat's own.
  local saved="" name
  for name in PROVRUN_RUNS PROVRUN_TELEMETRY_POLICY PROVCAT_CONFIG PROVCAT_CACHE PROVCAT_HOOKS PROVCAT_APP \
              PROVCAT_TENANT TOUR_DIR STORE \
              $(env | awk -F= '/^(MLFLOW_|OTEL_|LOGFIRE_|LITELLM_)[A-Za-z0-9_]*=/ {print $1}'); do
    if printenv "$name" > /dev/null; then
      saved="$saved$(printf 'export %s=%q;' "$name" "$(printenv "$name")")"
    elif eval "[ -n \"\${$name+x}\" ]"; then
      saved="$saved$(printf 'unset %s; %s=%q;' "$name" "$name" "$(eval "printf '%s' \"\$$name\"")")"
    else
      saved="${saved}unset $name;"
    fi
  done
  _PROVEVI_TOUR_SAVED="$saved"
  _provevi_tour_mine=1  # from here on, a failed start is stopped
  _PROVEVI_TOUR_DIR="$dir"  # stop.sh stops this tour, whatever TOUR_DIR says then

  mkdir -p "$dir" || return 1
  touch "$dir/.provevi-tour" || return 1
  unset PROVRUN_TELEMETRY_POLICY PROVCAT_CONFIG PROVCAT_HOOKS PROVCAT_APP PROVCAT_TENANT
  for name in $(env | awk -F= '/^(MLFLOW_|OTEL_|LOGFIRE_|LITELLM_)[A-Za-z0-9_]*=/ {print $1}'); do
    unset "$name"
  done
  export TOUR_DIR="$dir"
  export PROVRUN_RUNS="$TOUR_DIR/runs" PROVCAT_CACHE="$TOUR_DIR/cache"
  export STORE="sqlite:///$TOUR_DIR/evidence.db"

  printf 'Revenue in 2025 was 42 million.\n' > "$TOUR_DIR/report.txt" || return 1
  sed "s|TOUR_DIR|$TOUR_DIR|" tours/provevi/run.yaml > "$TOUR_DIR/run.yaml" || return 1
}
# If the start fails after its checks, stop what it started and give the
# shell back. A start that fails its checks leaves a running tour alone.
_provevi_tour_mine=""
# The || keeps a shell with set -e from exiting before the clean-up.
_tour_rc=0
_tour_start || _tour_rc=$?
if [ "$_tour_rc" -ne 0 ] && [ -n "$_provevi_tour_mine" ]; then source tours/provevi/stop.sh; fi
unset _provevi_tour_mine
unset -f _tour_start
eval "unset _tour_rc; return $_tour_rc"
