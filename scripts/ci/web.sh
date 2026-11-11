#!/usr/bin/env bash
# Woodpecker 前端检查：npm ci 后跑 `npm run build`（tsc -b 加 vite build）。
# 包先从 devbox 的只读 npm 代理取；代理不通或安装失败，就回到 registry.npmjs.org 重装一次。
set -euo pipefail

source scripts/ci/ci-cache-phase.sh

readonly CACHE_ENDPOINT='http://172.18.0.1:17893'
readonly ORIGINAL_REGISTRY='https://registry.npmjs.org/'

case "${CI_CACHE_MODE:-auto}" in
  off) mode=cold ;;
  auto|hot) mode=hot ;;
  *) printf 'unsupported CI_CACHE_MODE=%s\n' "$CI_CACHE_MODE" >&2; exit 2 ;;
esac
cache_key="assetizer-web-$(sha256sum web/package-lock.json | cut -c1-48)"

cd web
node --version
npm --version

npm_ci() {
  rm -rf node_modules &&
    NPM_CONFIG_REGISTRY="$1" NPM_CONFIG_REPLACE_REGISTRY_HOST=always \
      npm ci --no-audit --no-fund --fetch-retries=1 --fetch-timeout=30000
}

if [[ "$mode" == hot ]] && curl --fail --silent --show-error --max-time 5 "$CACHE_ENDPOINT/-/ping" >/dev/null; then
  if ! ci_cache_phase package_install hot "$cache_key" null npm_ci "$CACHE_ENDPOINT"; then
    echo "经 CI npm 代理安装失败，改用 registry.npmjs.org 重试"
    mode=fallback
    ci_cache_phase package_install fallback "$cache_key" null npm_ci "$ORIGINAL_REGISTRY"
  fi
else
  [[ "$mode" == hot ]] && mode=fallback
  ci_cache_phase package_install "$mode" "$cache_key" null npm_ci "$ORIGINAL_REGISTRY"
fi

ci_cache_phase checks "$mode" "$cache_key" null npm run build
