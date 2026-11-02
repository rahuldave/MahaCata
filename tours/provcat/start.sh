# Start the provcat tour in this shell. Run it from the top folder of the
# repository:
#   source tours/provcat/start.sh
# It makes a scratch folder, points provcat at the tour's configuration,
# starts moto, a local S3 server, and puts the tour's files in place.
# tours/provcat/stop.sh stops moto, deletes the folder, and puts back the
# variables that this file changed.

_tour_start() {
  if [ ! -f tours/provcat/start.sh ]; then
    echo "start.sh: run it from the top folder of the repository" >&2
    return 1
  fi
  if [ -n "${_PROVCAT_TOUR_SAVED:-}" ]; then
    echo "start.sh: a tour already runs in this shell; run 'source tours/provcat/stop.sh' first" >&2
    return 1
  fi
  local dir="${TOUR_DIR:-/tmp/provcat-tour}" port="${TOUR_S3_PORT:-5055}"
  case "$port" in
    ''|*[!0-9]*) echo "start.sh: TOUR_S3_PORT must be a number, not '$port'" >&2; return 1 ;;
  esac
  if { [ -e "$dir" ] || [ -L "$dir" ]; } && [ ! -f "$dir/.provcat-tour" ]; then
    echo "start.sh: $dir exists and is not a tour folder; set TOUR_DIR to a new folder" >&2
    return 1
  fi
  if [ -f "$dir/.provcat-tour" ]; then
    echo "start.sh: a tour is already in $dir; run 'source tours/provcat/stop.sh' first" >&2
    return 1
  fi
  if uv run python -c 'import socket, sys; sys.exit(socket.socket().connect_ex(("127.0.0.1", int(sys.argv[1]))) != 0)' "$port"; then
    echo "start.sh: port $port is in use; stop what runs there, or set TOUR_S3_PORT" >&2
    return 1
  fi

  # Save the variables that the tour changes, so stop.sh can put them back.
  local saved="" name
  for name in PROVCAT_CONFIG PROVCAT_CACHE PROVCAT_HOOKS PROVCAT_APP PROVCAT_TENANT \
              AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_DEFAULT_REGION AWS_PROFILE AWS_SESSION_TOKEN \
              TOUR_DIR TOUR_S3_PORT; do
    if printenv "$name" > /dev/null; then
      saved="$saved$(printf 'export %s=%q;' "$name" "$(printenv "$name")")"
    elif eval "[ -n \"\${$name+x}\" ]"; then
      saved="$saved$(printf 'unset %s; %s=%q;' "$name" "$name" "$(eval "printf '%s' \"\$$name\"")")"
    else
      saved="${saved}unset $name;"
    fi
  done
  _PROVCAT_TOUR_SAVED="$saved"
  _provcat_tour_mine=1  # from here on, a failed start is stopped
  _PROVCAT_TOUR_DIR="$dir"  # stop.sh stops this tour, whatever TOUR_DIR says then

  mkdir -p "$dir/files/sales" "$dir/files/shared" || return 1
  touch "$dir/.provcat-tour" || return 1
  export TOUR_DIR="$dir" TOUR_S3_PORT="$port"
  cp tours/provcat/provcat.yaml "$TOUR_DIR/provcat.yaml" || return 1
  export PROVCAT_CONFIG="$TOUR_DIR/provcat.yaml" PROVCAT_CACHE="$TOUR_DIR/cache"
  unset PROVCAT_HOOKS PROVCAT_APP PROVCAT_TENANT AWS_PROFILE AWS_SESSION_TOKEN
  # moto takes any keys.
  export AWS_ACCESS_KEY_ID=tour AWS_SECRET_ACCESS_KEY=tour AWS_DEFAULT_REGION=us-east-1

  printf 'region,target\neast,120\nwest,95\n' > "$TOUR_DIR/files/sales/targets.csv"
  printf 'item,price\npen,2\nbook,12\n' > "$TOUR_DIR/files/shared/prices.csv"

  uv run moto_server -p "$TOUR_S3_PORT" > "$TOUR_DIR/moto.log" 2>&1 &
  echo $! > "$TOUR_DIR/moto.pid"
  local i up=""
  for i in $(seq 100); do
    if curl -s -m 2 -o /dev/null "http://localhost:$TOUR_S3_PORT"; then up=1; break; fi
    sleep 0.2
  done
  if [ -z "$up" ]; then
    echo "start.sh: moto did not start; the end of its log, which stop.sh deletes:" >&2
    tail -n 5 "$TOUR_DIR/moto.log" >&2
    return 1
  fi
  uv run python tours/provcat/make_bucket.py tour-sales
}
# If the start fails after its checks, stop what it started and give the
# shell back. A start that fails its checks leaves a running tour alone.
_provcat_tour_mine=""
# The || keeps a shell with set -e from exiting before the clean-up.
_tour_rc=0
_tour_start || _tour_rc=$?
if [ "$_tour_rc" -ne 0 ] && [ -n "$_provcat_tour_mine" ]; then source tours/provcat/stop.sh; fi
unset _provcat_tour_mine
unset -f _tour_start
eval "unset _tour_rc; (exit $_tour_rc)"
