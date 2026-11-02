# Stop the tour that tours/provrun/start.sh started: stop moto and the
# collector, delete the scratch folder, and put back the variables that
# start.sh changed.
#   source tours/provrun/stop.sh

_tour_stop() {
  local dir="${TOUR_DIR:-/tmp/provrun-tour}"
  if [ ! -f "$dir/.provrun-tour" ]; then
    echo "stop.sh: no tour in $dir" >&2
  else
    local server pid
    for server in moto collector; do
      if [ -f "$dir/$server.pid" ]; then
        pid="$(cat "$dir/$server.pid")"
        # uv runs the server as a child, so stop the children too.
        pkill -P "$pid" 2>/dev/null || true
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
      fi
    done
    chmod -R u+w "$dir"
    rm -rf "$dir"
  fi
  if [ -n "${_PROVRUN_TOUR_SAVED:-}" ]; then
    eval "$_PROVRUN_TOUR_SAVED"
    unset _PROVRUN_TOUR_SAVED
  fi
}
_tour_stop
unset -f _tour_stop
