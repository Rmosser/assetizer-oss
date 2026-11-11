#!/usr/bin/env bash
# Woodpecker 后端检查：装 ffmpeg 和 Python 依赖，然后跑 AGENTS.md 里的 `.venv/bin/pytest -q`。
# apt 和 pip 都先直连清华镜像，失败再经 devbox 的下载代理连官方源。从 devbox 连官方源很慢且忽快忽慢：
# deb.debian.org 60 秒只拿到 0.9 MB、pypi.org 约 8 KB/s（经代理约 90 KB/s，依赖装一次 12 分钟）；
# 清华镜像 9 MB 的 Packages.xz、19 MB 的 jieba 都约 1 秒。CI_CACHE_MODE=off 不用代理兜底。
# apt 照常校验 Debian 的签名；Python 依赖的版本按生产用的 docker/requirements.lock 约束，镜像只是同一批文件的另一个来源。
set -euo pipefail

source scripts/ci/ci-cache-phase.sh

readonly PROXY='http://172.18.0.1:17896'
readonly MIRROR='https://pypi.tuna.tsinghua.edu.cn/simple'
readonly DEBIAN_MIRROR='https://mirrors.tuna.tsinghua.edu.cn/debian'
readonly LOCK='docker/requirements.lock'

case "${CI_CACHE_MODE:-auto}" in
  off) mode=off ;;
  auto|hot) mode=auto ;;
  *) printf 'unsupported CI_CACHE_MODE=%s\n' "$CI_CACHE_MODE" >&2; exit 2 ;;
esac
cache_key="assetizer-backend-$(cat pyproject.toml "$LOCK" | sha256sum | cut -c1-48)"

# 有个测试要求剩余空间不少于 15 GB（DISK_LOW），先把容器看到的空间打出来。
df -h "${TMPDIR:-/tmp}" .

with_proxy() {
  env HTTP_PROXY="$PROXY" HTTPS_PROXY="$PROXY" http_proxy="$PROXY" https_proxy="$PROXY" \
    NO_PROXY=127.0.0.1,localhost,172.18.0.1 no_proxy=127.0.0.1,localhost,172.18.0.1 "$@"
}

direct() {
  env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy -u ALL_PROXY -u all_proxy "$@"
}

# 清华镜像同时有 deb.debian.org 的 /debian 和 /debian-security，一次替换就把两条源都换掉。
point_apt_at() {
  local f
  for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do
    [[ -f "$f" ]] || continue
    [[ -f "$f.orig" ]] || cp "$f" "$f.orig"
    sed -E "s#https?://deb\.debian\.org/debian#$1#g" "$f.orig" >"$f"
  done
}

install_ffmpeg() {
  if [[ "$1" == direct ]]; then point_apt_at "$DEBIAN_MIRROR"; else point_apt_at http://deb.debian.org/debian; fi
  "$1" DEBIAN_FRONTEND=noninteractive timeout 180 apt-get update -qq &&
    "$1" DEBIAN_FRONTEND=noninteractive timeout 300 \
      apt-get install -y -qq --no-install-recommends ffmpeg >/dev/null
}

install_python() {
  local index timeout
  if [[ "$1" == direct ]]; then index="$MIRROR" timeout=300; else index='https://pypi.org/simple' timeout=1200; fi
  rm -rf .venv &&
    python3 -m venv .venv &&
    "$1" timeout "$timeout" .venv/bin/pip install --disable-pip-version-check --no-input -q \
      --index-url "$index" --timeout 30 --retries 2 --log /tmp/pip-install.log -c "$LOCK" -e '.[dev]' ||
    { tail -n 40 /tmp/pip-install.log; return 1; }
}

install() {
  local phase="$1" step="$2"
  ci_cache_phase "$phase" cold "$cache_key" null "$step" direct && return 0
  [[ "$mode" == auto ]] || return 1
  echo "$phase：直连安装失败，改走代理重试"
  ci_cache_phase "$phase" fallback "$cache_key" null "$step" with_proxy
}

install runtime install_ffmpeg
install package_install install_python

python3 --version
ffmpeg -hide_banner -version | head -1
ci_cache_phase checks cold "$cache_key" null .venv/bin/pytest -q
