#!/usr/bin/env python3
"""Collect live registry evidence and apply Tools/oci_release_contract.py."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Tools"))
from oci_release_contract import DIGEST_RE, SOURCE, SPDX, SLSA_PREFIX, verify  # noqa: E402


ATTESTATION_REFERENCE_DIGEST = "vnd.docker.reference.digest"
ATTESTATION_REFERENCE_TYPE = "vnd.docker.reference.type"
ATTESTATION_MANIFEST = "attestation-manifest"
IN_TOTO_JSON = "application/vnd.in-toto+json"


def run(*args: str) -> str:
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "command failed")
    return result.stdout


def first_json(command: list[str]):
    return json.loads(run(*command))


def _portable_owner_errors(
    members,
    platform: str,
    *,
    maximum_uid_gid: int = 65535,
    maximum_details: int = 12,
) -> list[str]:
    """Lehnt Rootfs-Eigentümer außerhalb des portablen 16-Bit-Bereichs ab."""

    violations: list[tuple[str, int, int]] = []
    for member in members:
        uid = int(member.uid)
        gid = int(member.gid)
        if 0 <= uid <= maximum_uid_gid and 0 <= gid <= maximum_uid_gid:
            continue
        path = str(member.name).replace("\n", "?").replace("\r", "?")[:160]
        violations.append((path, uid, gid))

    errors = [
        f"Rootfs UID/GID {platform}: {path} uid={uid} gid={gid}"
        for path, uid, gid in violations[:maximum_details]
    ]
    hidden = len(violations) - len(errors)
    if hidden > 0:
        errors.append(f"Rootfs UID/GID {platform}: {hidden} weitere Einträge")
    return errors


def _runtime_layer_owner_errors(image_reference: str, platform: str) -> list[str]:
    """Prüft jedes gespeicherte Image-Layer ohne Container-Ausführung."""

    try:
        with tempfile.TemporaryDirectory(prefix="e3dc-oci-layer-check-") as temp_dir:
            archive_path = Path(temp_dir) / "image.tar"
            run(
                "docker",
                "image",
                "save",
                "--output",
                str(archive_path),
                image_reference,
            )
            with tarfile.open(archive_path, mode="r:*") as image_archive:
                manifest_stream = image_archive.extractfile("manifest.json")
                if manifest_stream is None:
                    raise ValueError("manifest.json ist keine reguläre Datei")
                with manifest_stream:
                    manifest = json.load(manifest_stream)
                if not isinstance(manifest, list) or len(manifest) != 1:
                    raise ValueError("manifest.json muss genau ein Image enthalten")
                layers = manifest[0].get("Layers") if isinstance(manifest[0], dict) else None
                if (
                    not isinstance(layers, list)
                    or not layers
                    or any(not isinstance(layer, str) or not layer for layer in layers)
                    or len(layers) != len(set(layers))
                ):
                    raise ValueError("manifest.json enthält keine eindeutige Layer-Liste")

                errors: list[str] = []
                for index, layer_name in enumerate(layers, start=1):
                    layer_stream = image_archive.extractfile(layer_name)
                    if layer_stream is None:
                        raise ValueError(f"Layer {index} ist keine reguläre Datei")
                    with layer_stream, tarfile.open(fileobj=layer_stream, mode="r:*") as layer:
                        errors.extend(
                            _portable_owner_errors(
                                layer,
                                f"{platform} layer {index}/{len(layers)}",
                            )
                        )
                return errors
    except (
        json.JSONDecodeError,
        KeyError,
        OSError,
        RuntimeError,
        tarfile.TarError,
        TypeError,
        ValueError,
    ) as exc:
        return [f"Image layer owner check {platform}: {exc}"]


def _sbom_count(value) -> int:
    if not isinstance(value, dict):
        return 0
    count = 1 if value.get("SPDX") is not None else 0
    additional = value.get("AdditionalSPDXs", [])
    if isinstance(additional, list):
        count += len(additional)
    return count


def _normalize_slsa_v1(value, revision: str, platform: str, errors: list[str]):
    if not isinstance(value, dict) or not isinstance(value.get("SLSA"), dict):
        errors.append(f"SLSA payload {platform}")
        return None, None
    predicate = value["SLSA"]
    definition = predicate.get("buildDefinition")
    if not isinstance(definition, dict):
        errors.append(f"SLSA buildDefinition {platform}")
        return None, None
    external = definition.get("externalParameters")
    resolved = definition.get("resolvedDependencies")
    if not isinstance(external, dict):
        errors.append(f"SLSA externalParameters {platform}")
        return None, None
    if not isinstance(resolved, list) or any(not isinstance(item, dict) for item in resolved):
        errors.append(f"SLSA resolvedDependencies {platform}")
        return None, None
    config_source = external.get("configSource")
    request = external.get("request")
    if not isinstance(config_source, dict) or not isinstance(request, dict):
        errors.append(f"SLSA source/request {platform}")
        return None, None

    source_uri = config_source.get("uri")
    source_digest = config_source.get("digest")
    allowed_uris = {f"{SOURCE}#{revision}", f"{SOURCE}.git#{revision}"}
    if source_uri not in allowed_uris or source_digest != {"sha1": revision}:
        errors.append(f"SLSA configSource {platform}")
        return None, None

    source_dependencies = [
        item
        for item in resolved
        if item.get("uri") in allowed_uris and item.get("digest") == {"sha1": revision}
    ]
    if len(source_dependencies) != 1:
        errors.append(f"SLSA source dependency {platform}")
        return None, None
    materials = []
    for item in resolved:
        material = dict(item)
        if item is source_dependencies[0]:
            material["uri"] = source_uri.split("#", 1)[0]
        materials.append(material)

    args = request.get("args")
    if not isinstance(args, dict):
        errors.append(f"SLSA request args {platform}")
        return None, None
    build_args = {
        key.removeprefix("build-arg:"): value
        for key, value in args.items()
        if isinstance(key, str) and key.startswith("build-arg:")
    }
    return materials, build_args


def _fail(index_digest: str, errors: list[str]) -> int:
    print(json.dumps({"index_digest": index_digest, "status": "FAIL", "errors": errors}, sort_keys=True))
    return 1


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
    try:
        index = first_json(["docker", "buildx", "imagetools", "inspect", f"{image}@{index_digest}", "--raw"])
    except (RuntimeError, json.JSONDecodeError) as exc:
        return _fail(index_digest, [f"index read: {exc}"])
    descriptors = index.get("manifests", []) if isinstance(index, dict) else []
    if not isinstance(descriptors, list) or any(not isinstance(item, dict) for item in descriptors):
        return _fail(index_digest, ["index manifests list"])

    runtime_descriptors = []
    attestation_descriptors = []
    collection_errors: list[str] = []
    for descriptor in descriptors:
        platform = descriptor.get("platform", {})
        os_name = platform.get("os") if isinstance(platform, dict) else None
        arch = platform.get("architecture") if isinstance(platform, dict) else None
        if os_name == "unknown" and arch == "unknown":
            annotations = descriptor.get("annotations", {})
            if not isinstance(annotations, dict) or annotations.get(ATTESTATION_REFERENCE_TYPE) != ATTESTATION_MANIFEST:
                collection_errors.append("unexpected unknown/unknown descriptor")
                continue
            attestation_descriptors.append(descriptor)
        else:
            runtime_descriptors.append(descriptor)

    runtimes = []
    runtime_by_digest = {}
    for descriptor in runtime_descriptors:
        platform = descriptor.get("platform", {})
        os_name = platform.get("os") if isinstance(platform, dict) else None
        arch = platform.get("architecture") if isinstance(platform, dict) else None
        digest = descriptor.get("digest", "")
        name = f"{os_name}/{arch}"
        if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
            collection_errors.append(f"runtime descriptor digest {name}")
            continue
        try:
            run("docker", "pull", "--platform", name, f"{image}@{digest}")
            labels = first_json(["docker", "image", "inspect", f"{image}@{digest}", "--format", "{{json .Config.Labels}}"])
        except (RuntimeError, json.JSONDecodeError) as exc:
            collection_errors.append(f"runtime inspect {name}: {exc}")
            continue
        collection_errors.extend(
            _runtime_layer_owner_errors(f"{image}@{digest}", name)
        )
        runtimes.append({"platform": name, "digest": digest, "labels": labels})
        runtime_by_digest[digest] = name

    attachments_by_runtime: dict[str, list[dict]] = {digest: [] for digest in runtime_by_digest}
    for descriptor in attestation_descriptors:
        annotations = descriptor.get("annotations", {})
        subject = annotations.get(ATTESTATION_REFERENCE_DIGEST)
        digest = descriptor.get("digest", "")
        if subject not in attachments_by_runtime:
            collection_errors.append(f"unexpected attestation subject {subject}")
            continue
        if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
            collection_errors.append(f"attestation descriptor digest {subject}")
            continue
        attachments_by_runtime[subject].append(descriptor)

    root_reference = f"{image}@{index_digest}"
    try:
        sboms = first_json(["docker", "buildx", "imagetools", "inspect", root_reference, "--format", "{{json .SBOM}}"])
        provenances = first_json(["docker", "buildx", "imagetools", "inspect", root_reference, "--format", "{{json .Provenance}}"])
    except (RuntimeError, json.JSONDecodeError) as exc:
        collection_errors.append(f"attestation payload read: {exc}")
        sboms, provenances = {}, {}
    if not isinstance(sboms, dict):
        collection_errors.append("SBOM platform map")
        sboms = {}
    if not isinstance(provenances, dict):
        collection_errors.append("provenance platform map")
        provenances = {}

    attestations = []
    for subject, platform in runtime_by_digest.items():
        linked = attachments_by_runtime.get(subject, [])
        if len(linked) != 1:
            collection_errors.append(f"one attestation manifest subject {subject}")
            continue
        attestation_digest = linked[0]["digest"]
        try:
            manifest = first_json(["docker", "buildx", "imagetools", "inspect", f"{image}@{attestation_digest}", "--raw"])
        except (RuntimeError, json.JSONDecodeError) as exc:
            collection_errors.append(f"attestation manifest read {subject}: {exc}")
            continue
        layers = manifest.get("layers", []) if isinstance(manifest, dict) else []
        if not isinstance(layers, list) or any(not isinstance(layer, dict) for layer in layers):
            collection_errors.append(f"attestation layers {subject}")
            continue

        spdx_layers = 0
        slsa_layers = 0
        for layer in layers:
            annotations = layer.get("annotations", {})
            predicate_type = annotations.get("in-toto.io/predicate-type") if isinstance(annotations, dict) else None
            if layer.get("mediaType") != IN_TOTO_JSON or not isinstance(predicate_type, str):
                collection_errors.append(f"in-toto layer descriptor {subject}")
                continue
            layer_digest = layer.get("digest", "")
            if not isinstance(layer_digest, str) or not DIGEST_RE.fullmatch(layer_digest):
                collection_errors.append(f"in-toto layer digest {subject}")
                continue
            attestation = {"subject_digest": subject, "predicate_type": predicate_type}
            if predicate_type == SPDX:
                spdx_layers += 1
            elif predicate_type.startswith(SLSA_PREFIX):
                slsa_layers += 1
                materials, build_args = _normalize_slsa_v1(provenances.get(platform), revision, platform, collection_errors)
                attestation["materials"] = materials
                attestation["build_args"] = build_args
            attestations.append(attestation)

        if _sbom_count(sboms.get(platform)) != spdx_layers:
            collection_errors.append(f"SPDX payload count {platform}")
        if slsa_layers != 1:
            collection_errors.append(f"SLSA layer count {platform}")

    bundle = {
        "expected": {"revision": revision, "tree": tree, "version": version, "created": created, "source_manifest": source_manifest},
        "runtimes": runtimes,
        "attestations": attestations,
    }
    errors = collection_errors + verify(bundle)
    print(json.dumps({"index_digest": index_digest, "status": "PASS" if not errors else "FAIL", "errors": errors}, sort_keys=True))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
