#!/usr/bin/env bash
# Woodpecker 各阶段的耗时记录，格式与 devbox 其他仓库的 CI_CACHE_PHASE 一致。
set -euo pipefail

ci_cache_now_ms() { date +%s%3N; }

ci_cache_emit() {
  local phase="$1" mode="$2" duration_ms="$3" status="$4" cache_key="$5"
  local cache_hit="${6:-null}" downloaded_bytes="${7:-null}"
  python3 - "$phase" "$mode" "$duration_ms" "$status" "$cache_key" "$cache_hit" "$downloaded_bytes" <<'PY'
import json
import sys

phase, mode, duration, status, key, hit, downloaded = sys.argv[1:]

def value(raw: str):
    if raw == "null":
        return None
    if raw == "true":
        return True
    if raw == "false":
        return False
    return int(raw) if raw.isdigit() else raw

print("CI_CACHE_PHASE " + json.dumps({
    "phase": phase,
    "mode": mode,
    "duration_ms": int(duration),
    "status": status,
    "cache_key": key,
    "downloaded_bytes": value(downloaded),
    "cache_hit": value(hit),
}, separators=(",", ":")))
PY
}

ci_cache_phase() {
  local phase="$1" mode="$2" cache_key="$3" cache_hit="${4:-null}"
  shift 4
  local started finished rc status
  started="$(ci_cache_now_ms)"
  set +e
  "$@"
  rc=$?
  set -e
  finished="$(ci_cache_now_ms)"
  status=ok
  [[ "$rc" -eq 0 ]] || status=failed
  ci_cache_emit "$phase" "$mode" "$((finished - started))" "$status" "$cache_key" "$cache_hit" null
  return "$rc"
}
