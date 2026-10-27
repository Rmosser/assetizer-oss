#!/bin/sh
# lzc-build buildscript: build the UI, then lay out the package content the containers mount at /lzcapp/pkg/content.
# The layout keeps src/ next to assets/, because subtitles find the bundled CJK font relative to the source tree.
set -eu
cd "$(dirname "$0")/../.."
[ -d web/node_modules ] || npm --prefix web ci --no-audit --no-fund
(cd web && npx tsc -b && npx vite build)  # writes src/assetizer/web/static
rm -rf dist
mkdir -p dist/scripts .pkgout  # lzc-cli requires pkgout to exist
rsync -a --exclude __pycache__ --exclude '*.egg-info' src assets pyproject.toml dist/
cp -R scripts/lzc dist/scripts/lzc
chmod 0755 dist/scripts/lzc/*.sh
test -f dist/src/assetizer/web/static/index.html
test -f dist/assets/subtitle/NotoSansCJKsc-Regular.otf
