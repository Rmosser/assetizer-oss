#!/bin/sh
# Build the release LPK with an intact embedded runtime image. lzc-cli 2.0.9 can truncate locally exported layers,
# so after `lzc-cli project release` the image is exported again as OCI by buildx and swapped in, verified per file.
set -eu
cd "$(dirname "$0")/../.."
VERSION=$(sed -n 's/^version: //p' package.yml)
LPK=".pkgout/cloud.lazycat.app.assetizer-v$VERSION.lpk"
lzc-cli project release
docker buildx inspect assetizer-oci >/dev/null 2>&1 \
  || docker buildx create --name assetizer-oci --driver docker-container >/dev/null  # the docker driver cannot export OCI
WORK=$(mktemp -d)
trap 'rm -rf -- "$WORK"' EXIT
docker buildx build --builder assetizer-oci --platform linux/amd64 --provenance=false \
  -f docker/runtime.Dockerfile --output "type=oci,dest=$WORK/runtime.oci.tar,compression=gzip,force-compression=true" docker
python3 scripts/lzc/repack_image.py "$LPK" "$WORK/runtime.oci.tar" runtime
lzc-cli lpk lint "$LPK"
shasum -a 256 "$LPK"
