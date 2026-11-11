#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd -P)
fixture=$(mktemp -d "${TMPDIR:-/tmp}/assetizer-hook-tunnel-test.XXXXXX")
cleanup() {
  if [ -n "${tunnel_pid:-}" ] && kill -0 "$tunnel_pid" 2>/dev/null; then
    kill -KILL "$tunnel_pid" 2>/dev/null || true
  fi
  rm -rf "$fixture"
}
trap cleanup 0 1 2 3 15

# Stands in for cloudflared: records every start with its arguments and, on
# TERM, takes a second to "unregister" before it exits, like a graceful stop.
fake=$fixture/cloudflared
cat >"$fake" <<'EOF'
#!/bin/sh
printf 'start %s\n' "$*" >>"$HOOK_FAKE_MARKER"
trap 'echo term >>"$HOOK_FAKE_MARKER"; sleep 1; echo unregistered >>"$HOOK_FAKE_MARKER"; exit 0' TERM
if [ "${HOOK_FAKE_EXIT_FIRST:-0}" = 1 ] && [ "$(grep -c '^start ' "$HOOK_FAKE_MARKER")" -eq 1 ]; then
  exit 3
fi
while :; do sleep 0.1; done
EOF
chmod 0755 "$fake"

fail() {
  echo "hook-tunnel test: $*" >&2
  exit 1
}

wait_for_starts() {
  expected=$1
  deadline=$(( $(date +%s) + $2 ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    count=0
    if [ -f "$marker" ]; then
      count=$(grep -c '^start ' "$marker" || true)
    fi
    [ "$count" -ge "$expected" ] && return 0
    sleep 0.05
  done
  fail "timed out waiting for $expected cloudflared starts"
}

start_tunnel() {
  marker=$fixture/$1.marker
  stdout=$fixture/$1.stdout
  ASSETIZER_HOOK_TUNNEL_TOKEN_FILE="$2" \
  ASSETIZER_HOOK_TUNNEL_CLOUDFLARED="$fake" \
  HOOK_FAKE_MARKER="$marker" \
  HOOK_FAKE_EXIT_FIRST="${3:-0}" \
    sh "$root/scripts/lzc/hook-tunnel.sh" >"$stdout" 2>"$fixture/$1.stderr" &
  tunnel_pid=$!
}

stop_tunnel() {
  stop_started=$(date +%s)
  kill -TERM "$tunnel_pid"
  # LazyCat kills the service after its ten-second stop grace period; a
  # service that is still waiting by then has not forwarded the signal.
  while kill -0 "$tunnel_pid" 2>/dev/null; do
    if [ $(( $(date +%s) - stop_started )) -ge 12 ]; then
      kill -KILL "$tunnel_pid" 2>/dev/null || true
      pkill -KILL -f "$fake" 2>/dev/null || true
      fail "service still running 12s after TERM"
    fi
    sleep 0.05
  done
  set +e
  wait "$tunnel_pid"
  status=$?
  set -e
  tunnel_pid=''
  elapsed=$(( $(date +%s) - stop_started ))
}

# Without a token the service never starts cloudflared and stops at once
# instead of sitting out its 30-second poll until LazyCat kills it.
# Mirror the service's only mount, /ingress; no application data is in it.
mkdir "$fixture/ingress"
token=$fixture/ingress/tunnel-token
start_tunnel idle "$token"
sleep 0.5
stop_tunnel
[ "$status" -eq 0 ] || fail "idle stop returned $status"
[ "$elapsed" -le 2 ] || fail "idle stop took ${elapsed}s"
[ ! -f "$marker" ] || fail "cloudflared started without a token"

# An empty file is also inactive.
: >"$token"
start_tunnel empty "$token"
sleep 0.3
stop_tunnel
[ ! -f "$marker" ] || fail "cloudflared started with an empty token"

# With a token, TERM reaches cloudflared and the service waits for its
# graceful shutdown, inside LazyCat's ten-second stop grace period.
printf '%s' 'synthetic-token' >"$token"
start_tunnel stop "$token"
wait_for_starts 1 5
for flag in '--protocol http2' '--metrics 0.0.0.0:20241' '--grace-period 5s' "run --token-file $token"; do
  grep -Fq -- "$flag" "$marker" || fail "cloudflared started without '$flag'"
done
stop_tunnel
[ "$status" -eq 0 ] || fail "TERM stop returned $status"
[ "$elapsed" -lt 10 ] || fail "TERM stop took ${elapsed}s"
[ "$(sed -n '2p;3p' "$marker" | tr '\n' ' ')" = 'term unregistered ' ] \
  || fail "cloudflared was not stopped gracefully before the service exited"
grep -q '^hook-tunnel: cloudflared stopped code=0$' "$stdout" \
  || fail "missing the stop exit code in the service log"

# An unexpected exit is logged with its real status and restarted after 30
# seconds; a stop after the restart is still graceful.
start_tunnel restart "$token" 1
wait_for_starts 2 40
grep -q '^hook-tunnel: cloudflared exited code=3, retrying in 30s\.\.\.$' "$stdout" \
  || fail "missing the exit code in the restart log"
stop_tunnel
[ "$status" -eq 0 ] || fail "TERM stop after restart returned $status"
grep -q '^unregistered$' "$marker" || fail "restarted cloudflared was not stopped gracefully"

echo 'hook-tunnel process test OK'
