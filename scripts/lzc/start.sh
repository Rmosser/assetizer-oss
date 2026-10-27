#!/bin/sh
# Container entry for the LazyCat package. tini is PID 1: it reaps ffmpeg/agy leftovers and forwards SIGTERM,
# so the worker's handler can kill in-flight agy calls and leave its clean-exit mark.
set -eu
mkdir -p "$HOME/.local/bin" "$ASSETIZER_HOME" "$ASSETIZER_WHISPER_ROOT"
export PATH="$HOME/.local/bin:$PATH"  # agy installs and updates itself here, in the persistent volume
cd /lzcapp/pkg/content
case "${1:-}" in
  web) exec tini -- python -m assetizer.cli web --host 0.0.0.0 --port "${ASSETIZER_PORT:-8770}" ;;
  worker) exec tini -- python -m assetizer.cli worker ;;
  *) echo "usage: start.sh web|worker" >&2; exit 2 ;;
esac
