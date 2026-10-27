#!/usr/bin/env python3
"""Replace an LPK's embedded image with the OCI layers buildx exported, verified end to end.

lzc-cli 2.0.9 can truncate a locally exported layer while keeping a valid gzip and digest: on 2026-10-25 three of
the eight runtime layers ended mid-tar, and LazyCat refused the package with "can't import embedded images ...
unexpected EOF". Adapted from Clash-Verge-For-LC scripts/repair_dashboard_image.py; unlike that image, this one is
rebuilt, so its id changes and the services' embed pins in manifest.yml are rewritten to match.

usage: repack_image.py <package.lpk> <image.oci.tar> <alias>
"""
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

INDEX_TYPES = {"application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json"}


def repack(package: Path, archive: Path, alias: str) -> None:
    with tempfile.TemporaryDirectory(prefix="lpk-oci-") as temporary:
        root = Path(temporary)
        source, target = root / "source", root / "package"
        source.mkdir()
        target.mkdir()
        for path, destination in ((archive, source), (package, target)):
            with tarfile.open(path) as handle:
                handle.extractall(destination, filter="data")

        def blob(item: dict) -> bytes:
            digest = item["digest"]
            assert digest.startswith("sha256:") and len(digest) == 71, digest
            data = (source / "blobs" / "sha256" / digest[7:]).read_bytes()
            assert len(data) == item["size"], f"OCI blob size mismatch: {digest}"
            assert hashlib.sha256(data).hexdigest() == digest[7:], f"OCI blob digest mismatch: {digest}"
            return data

        def image_manifest(descriptors: list) -> dict:
            """The linux/amd64 image manifest, through nested indexes, skipping attestation manifests."""
            for item in descriptors:
                if (item.get("annotations") or {}).get("vnd.docker.reference.type") == "attestation-manifest":
                    continue
                platform = item.get("platform") or {}
                if platform and (platform.get("os"), platform.get("architecture")) != ("linux", "amd64"):
                    continue
                if item["mediaType"] in INDEX_TYPES:
                    return image_manifest(json.loads(blob(item))["manifests"])
                return item
            raise AssertionError("no linux/amd64 image manifest in the OCI archive")

        descriptor = image_manifest(json.loads((source / "index.json").read_text())["manifests"])
        manifest = json.loads(blob(descriptor))
        config = json.loads(blob(manifest["config"]))
        assert (config["os"], config["architecture"]) == ("linux", "amd64"), "expected a linux/amd64 image"
        diff_ids = config["rootfs"]["diff_ids"]
        assert len(diff_ids) == len(manifest["layers"]), "layer count does not match the config"
        for layer, diff_id in zip(manifest["layers"], diff_ids):
            data = blob(layer)
            raw = gzip.decompress(data) if "gzip" in layer["mediaType"] else data
            assert "sha256:" + hashlib.sha256(raw).hexdigest() == diff_id, f"diff_id mismatch: {layer['digest']}"
            with tarfile.open(fileobj=io.BytesIO(raw)) as handle:
                for entry in handle:  # read every file body: a listing alone can miss a truncated one
                    if entry.isfile():
                        stream = handle.extractfile(entry)
                        while stream.read(1024 * 1024):
                            pass

        images = target / "images"
        shutil.rmtree(images)
        (images / "blobs" / "sha256").mkdir(parents=True)
        for item in (descriptor, manifest["config"], *manifest["layers"]):
            shutil.copy2(source / "blobs" / "sha256" / item["digest"][7:], images / "blobs" / "sha256")
        shutil.copy2(source / "oci-layout", images / "oci-layout")
        entry = {k: descriptor[k] for k in ("mediaType", "digest", "size")}
        entry["annotations"] = {"org.opencontainers.image.ref.name": alias}
        (images / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": [entry]}))
        (target / "images.lock").write_text(json.dumps({"version": 1, "images": {alias: {
            "image_id": manifest["config"]["digest"], "upstream": "",
            "layers": [{"digest": layer["digest"], "source": "embed"} for layer in manifest["layers"]],
        }}}))
        # lzc-cli pins every service to the image id it built; LazyCat rejects a pin that disagrees with images.lock.
        packed = target / "manifest.yml"
        text, pins = re.subn(rf"embed:{re.escape(alias)}@sha256:[0-9a-f]{{64}}",
                             f"embed:{alias}@{manifest['config']['digest']}", packed.read_text())
        assert pins, f"no embed:{alias}@sha256 pin in manifest.yml"
        packed.write_text(text)
        output = package.with_suffix(".repack.tmp")
        try:
            with tarfile.open(output, "w") as handle:
                for child in sorted(target.iterdir()):
                    handle.add(child, arcname=child.name)
            os.replace(output, package)
        finally:
            output.unlink(missing_ok=True)
    print(f"verified and packed {len(manifest['layers'])} OCI layers for '{alias}' (linux/amd64), {pins} service pins")


if __name__ == "__main__":
    repack(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])
