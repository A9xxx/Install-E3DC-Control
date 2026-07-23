#!/usr/bin/env python3
"""Semantic verification for a two-platform OCI release evidence bundle."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

PLATFORMS = {"linux/amd64", "linux/arm64"}
SPDX = "https://spdx.dev/Document"
SLSA_PREFIX = "https://slsa.dev/provenance/"
SOURCE = "https://github.com/A9xxx/Install-E3DC-Control"
TITLE = "E3DC-Control"
DESCRIPTION = "Lokales Energie- und Installationssystem für E3DC-Anlagen"
LICENSE = "AGPL-3.0-or-later"
RELEASE_VERSIONS = {"5.3.2b", "5.4.0", "5.4.0a", "5.4.0b"}
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


def expected_labels(expected: dict) -> dict[str, str]:
    """Return the complete, closed label set emitted by the release Dockerfile."""

    return {
        "org.opencontainers.image.title": TITLE,
        "org.opencontainers.image.description": DESCRIPTION,
        "org.opencontainers.image.source": SOURCE,
        "org.opencontainers.image.licenses": LICENSE,
        "org.opencontainers.image.version": str(expected.get("version", "")),
        "org.opencontainers.image.revision": str(expected.get("revision", "")),
        "org.opencontainers.image.created": str(expected.get("created", "")),
        "io.e3dc.git.tree": str(expected.get("tree", "")),
        "io.e3dc.source.manifest": str(expected.get("source_manifest", "")),
    }


def expected_build_args(expected: dict) -> dict[str, str]:
    """Return the exact provenance build-argument binding for a release."""

    return {
        "E3DC_VERSION": str(expected.get("version", "")),
        "E3DC_REVISION": str(expected.get("revision", "")),
        "E3DC_TREE": str(expected.get("tree", "")),
        "E3DC_CREATED": str(expected.get("created", "")),
        "E3DC_SOURCE_MANIFEST": str(expected.get("source_manifest", "")),
    }


def verify(bundle: dict) -> list[str]:
    errors: list[str] = []
    expected = bundle.get("expected", {})
    if not isinstance(expected, dict):
        return ["expected object"]
    revision = str(expected.get("revision", ""))
    tree = str(expected.get("tree", ""))
    version = str(expected.get("version", ""))
    created = str(expected.get("created", ""))
    source_manifest = str(expected.get("source_manifest", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", revision): errors.append("expected revision")
    if not re.fullmatch(r"[0-9a-f]{40}", tree): errors.append("expected tree")
    if not re.fullmatch(r"[0-9a-f]{64}", source_manifest): errors.append("expected source manifest")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", created): errors.append("expected created")
    if version not in RELEASE_VERSIONS: errors.append("expected release version")

    runtimes = bundle.get("runtimes", [])
    if not isinstance(runtimes, list):
        return errors + ["runtimes list"]
    if any(not isinstance(item, dict) for item in runtimes):
        return errors + ["runtime object"]
    platforms = [item.get("platform") for item in runtimes]
    if set(platforms) != PLATFORMS or len(platforms) != len(PLATFORMS):
        errors.append("runtime platform allowlist")
    runtime_digests = [item.get("digest") for item in runtimes]
    if any(not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest) for digest in runtime_digests):
        errors.append("runtime digest format")
    string_digests = [digest for digest in runtime_digests if isinstance(digest, str)]
    if len(string_digests) != len(set(string_digests)):
        errors.append("runtime digest uniqueness")
    required_labels = expected_labels(expected)
    for item in runtimes:
        labels = item.get("labels", {})
        if labels != required_labels:
            errors.append(f"exact labels {item.get('platform')}")

    attestations = bundle.get("attestations", [])
    if not isinstance(attestations, list):
        return errors + ["attestations list"]
    runtime_digest_set = set(string_digests)
    for attestation in attestations:
        if not isinstance(attestation, dict):
            errors.append("attestation object")
            continue
        subject_digest = attestation.get("subject_digest")
        predicate_type = str(attestation.get("predicate_type", ""))
        if subject_digest not in runtime_digest_set:
            errors.append(f"unexpected attestation subject {subject_digest}")
        if predicate_type != SPDX and not predicate_type.startswith(SLSA_PREFIX):
            errors.append(f"unexpected predicate type {predicate_type}")

    for digest in runtime_digests:
        relevant = [item for item in attestations if isinstance(item, dict) and item.get("subject_digest") == digest]
        sboms = [item for item in relevant if item.get("predicate_type") == SPDX]
        provenances = [item for item in relevant if str(item.get("predicate_type", "")).startswith(SLSA_PREFIX)]
        if len(sboms) != 1:
            errors.append(f"one SPDX subject {digest}")
        if len(provenances) != 1:
            errors.append(f"one provenance subject {digest}")
            continue
        provenance = provenances[0]
        if provenance.get("subject_digest") != digest:
            errors.append(f"provenance subject {digest}")
        materials = provenance.get("materials", [])
        if not isinstance(materials, list) or any(not isinstance(material, dict) for material in materials):
            errors.append(f"materials structure {digest}")
            continue
        source_matches = [
            material
            for material in materials
            if material.get("uri") in {SOURCE, SOURCE + ".git"}
            and material.get("digest") == {"sha1": revision}
        ]
        build_args = provenance.get("build_args", {})
        if len(source_matches) != 1 or build_args != expected_build_args(expected):
            errors.append(f"exact materials {digest}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    errors = verify(json.loads(args.bundle.read_text(encoding="utf-8")))
    print(json.dumps({"status": "PASS" if not errors else "FAIL", "errors": errors}, sort_keys=True))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
