#!/usr/bin/env python3
"""Collect live registry evidence and apply Tools/oci_release_contract.py."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Tools"))
from oci_release_contract import DIGEST_RE, SPDX, SLSA_PREFIX, verify  # noqa: E402


def run(*args: str) -> str:
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "command failed")
    return result.stdout


def find_values(value, key: str):
    if isinstance(value, dict):
        if key in value:
            yield value[key]
        for child in value.values():
            yield from find_values(child, key)
    elif isinstance(value, list):
        for child in value:
            yield from find_values(child, key)


def first_json(command: list[str]):
    return json.loads(run(*command))


def main(argv: list[str]) -> int:
    if len(argv) != 8:
        return 2
    _, image, index_digest, revision, tree, version, created, source_manifest = argv
    if not DIGEST_RE.fullmatch(index_digest):
        print(json.dumps({"index_digest": index_digest, "status": "FAIL", "errors": ["index digest format"]}, sort_keys=True))
        return 1
    if not re.fullmatch(r"[^/@\s]+(?:/[^/@\s]+)+", image):
        print(json.dumps({"index_digest": index_digest, "status": "FAIL", "errors": ["image reference format"]}, sort_keys=True))
        return 1
    index = first_json(["docker", "buildx", "imagetools", "inspect", f"{image}@{index_digest}", "--raw"])
    runtimes = []
    attestations = []
    for descriptor in index.get("manifests", []):
        platform = descriptor.get("platform", {})
        os_name, arch = platform.get("os"), platform.get("architecture")
        digest = descriptor.get("digest", "")
        if os_name == "unknown" and arch == "unknown":
            continue
        name = f"{os_name}/{arch}"
        run("docker", "pull", "--platform", name, f"{image}@{digest}")
        labels = first_json(["docker", "image", "inspect", f"{image}@{digest}", "--format", "{{json .Config.Labels}}"])
        runtimes.append({"platform": name, "digest": digest, "labels": labels})

        sbom = first_json(["docker", "buildx", "imagetools", "inspect", f"{image}@{digest}", "--format", "{{json .SBOM}}"])
        if sbom:
            attestations.append({"subject_digest": digest, "predicate_type": SPDX})
        provenance = first_json(["docker", "buildx", "imagetools", "inspect", f"{image}@{digest}", "--format", "{{json .Provenance}}"])
        predicate_types = sorted({str(value) for value in find_values(provenance, "predicateType") if str(value).startswith(SLSA_PREFIX)})
        material_values = list(find_values(provenance, "materials"))
        build_arg_values = list(find_values(provenance, "build-args"))
        for predicate_type in predicate_types:
            attestations.append({
                "subject_digest": digest,
                "predicate_type": predicate_type,
                "materials": material_values[0] if len(material_values) == 1 else None,
                "build_args": build_arg_values[0] if len(build_arg_values) == 1 else None,
            })

    bundle = {
        "expected": {"revision": revision, "tree": tree, "version": version, "created": created, "source_manifest": source_manifest},
        "runtimes": runtimes,
        "attestations": attestations,
    }
    errors = verify(bundle)
    print(json.dumps({"index_digest": index_digest, "status": "PASS" if not errors else "FAIL", "errors": errors}, sort_keys=True))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
