# Stop the tour that tours/provcat/start.sh started: stop moto, delete the
# scratch folder, and put back the variables that start.sh changed.
#   source tours/provcat/stop.sh

_tour_stop() {
  local dir="${_PROVCAT_TOUR_DIR:-${TOUR_DIR:-/tmp/provcat-tour}}"
  if [ ! -f "$dir/.provcat-tour" ]; then
    echo "stop.sh: no tour in $dir" >&2
  else
    if [ -f "$dir/moto.pid" ]; then
      local pid
      pid="$(cat "$dir/moto.pid")"
      # uv runs moto as a child, so stop the children too.
      pkill -P "$pid" 2>/dev/null || true
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
    chmod -R u+w "$dir"
    rm -rf "$dir"
  fi
  if [ -n "${_PROVCAT_TOUR_SAVED:-}" ]; then
    eval "$_PROVCAT_TOUR_SAVED"
    unset _PROVCAT_TOUR_SAVED
    unset _PROVCAT_TOUR_DIR
  fi
}
_tour_stop
unset -f _tour_stop
