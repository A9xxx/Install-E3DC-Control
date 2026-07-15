#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 7 ]; then
  echo "usage: $0 IMAGE INDEX_DIGEST COMMIT_SHA TREE_ID VERSION CREATED SOURCE_MANIFEST_SHA256" >&2
  exit 2
fi

# Keep Bash as the workflow entrypoint, but delegate JSON semantics to the
# fixture-tested verifier. No lexicographic count comparison is used.
exec python3 .github/scripts/verify_oci_release_live.py "$@"
