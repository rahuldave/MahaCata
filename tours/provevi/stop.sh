# Stop the tour that tours/provevi/start.sh started: delete the scratch
# folder, and put back the variables that start.sh changed.
#   source tours/provevi/stop.sh

_tour_stop() {
  local dir="${_PROVEVI_TOUR_DIR:-${TOUR_DIR:-/tmp/provevi-tour}}"
  if [ ! -f "$dir/.provevi-tour" ]; then
    echo "stop.sh: no tour in $dir" >&2
  else
    chmod -R u+w "$dir"
    rm -rf "$dir"
  fi
  if [ -n "${_PROVEVI_TOUR_SAVED:-}" ]; then
    eval "$_PROVEVI_TOUR_SAVED"
    unset _PROVEVI_TOUR_SAVED
    unset _PROVEVI_TOUR_DIR
  fi
}
_tour_stop
unset -f _tour_stop
