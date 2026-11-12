#!/bin/sh
set -u

# PID 1 of the hook-tunnel service (docs/operations.md).  It idles until the
# tunnel token is installed (it looks every few seconds), so a box without a
# token never connects to Cloudflare, and restarts cloudflared 30 seconds
# after it exits.  A token replaced or removed while cloudflared runs
# (设置 → Notion) stops it within a few seconds; it starts again at once with
# the new token, or idles.
#
# A deployment stop sends TERM to this shell, not to cloudflared.  Forward it
# so cloudflared unregisters its edge connections inside LazyCat's ten-second
# stop grace period.  A killed connector leaves dead connections behind, and
# the edge keeps routing readers to them (502) until they time out.
token_file=${ASSETIZER_HOOK_TUNNEL_TOKEN_FILE:-/ingress/tunnel-token}
cloudflared=${ASSETIZER_HOOK_TUNNEL_CLOUDFLARED:-/usr/local/bin/cloudflared}
every=${ASSETIZER_HOOK_TUNNEL_WATCH:-5}
child_pid=''
sleep_pid=''
watch_pid=''
stopping='0'

forward_signal() {
  stopping='1'
  if [ -n "$child_pid" ]; then
    kill -TERM "$child_pid" 2>/dev/null || true
  fi
  if [ -n "$sleep_pid" ]; then
    kill "$sleep_pid" 2>/dev/null || true
  fi
  if [ -n "$watch_pid" ]; then
    kill "$watch_pid" 2>/dev/null || true
  fi
}

# A checksum, so the token itself is never held in a variable.
token_sum() {
  cksum <"$token_file" 2>/dev/null || echo none
}

trap forward_signal TERM INT

# A foreground sleep would hold the trap until it ends; waiting on a
# background sleep lets TERM stop an idle or backing-off service at once.
pause() {
  sleep "$1" &
  sleep_pid=$!
  wait "$sleep_pid" 2>/dev/null || true
  sleep_pid=''
}

while [ "$stopping" = '0' ]; do
  if [ ! -s "$token_file" ]; then
    pause "$every"
    continue
  fi
  started=$(token_sum)
  # HTTP/2 over TCP 7844: QUIC over UDP from the box's uplink drops every
  # connection in bursts.  The five-second grace period bounds in-flight
  # reads so the unregistration also fits in the stop grace period.
  "$cloudflared" tunnel --no-autoupdate --loglevel info --protocol http2 \
    --metrics 0.0.0.0:20241 --grace-period 5s \
    run --token-file "$token_file" &
  child_pid=$!
  (
    trap - TERM INT
    while kill -0 "$child_pid" 2>/dev/null; do
      sleep "$every"
      if [ "$(token_sum)" != "$started" ]; then
        kill -TERM "$child_pid" 2>/dev/null || true
        break
      fi
    done
  ) &
  watch_pid=$!
  # TERM may arrive after the loop check but before the PID is published.
  if [ "$stopping" = '1' ]; then
    kill -TERM "$child_pid" 2>/dev/null || true
  fi
  child_code=0
  wait "$child_pid" || child_code=$?
  # wait returns early when the trap handles TERM.  Keep waiting until
  # cloudflared has finished its graceful shutdown, then keep its own status.
  while kill -0 "$child_pid" 2>/dev/null; do
    child_code=0
    wait "$child_pid" || child_code=$?
  done
  child_pid=''
  kill "$watch_pid" 2>/dev/null || true
  wait "$watch_pid" 2>/dev/null || true
  watch_pid=''
  if [ "$stopping" = '1' ]; then
    echo "hook-tunnel: cloudflared stopped code=$child_code"
    break
  fi
  if [ "$(token_sum)" != "$started" ]; then
    echo "hook-tunnel: token changed, cloudflared stopped code=$child_code"
    continue
  fi
  echo "hook-tunnel: cloudflared exited code=$child_code, retrying in 30s..."
  pause 30
done
