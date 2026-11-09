#!/usr/bin/env bash
# Download the campaign data and trained models (GitHub release asset), check
# the SHA-256 and unpack into experiments/results/ and models/.
#
# Usage:
#   bash scripts/fetch_data.sh
#   DATA_ARCHIVE=/path/to/ids-data-v1.tar.gz bash scripts/fetch_data.sh   # local copy

set -euo pipefail

GITHUB_REPO="${GITHUB_REPO:-dimatortik/k8s-lifecycle-ids}"
DATA_TAG="${DATA_TAG:-data-v1}"
ARCHIVE_NAME="ids-data-v1.tar.gz"
SHA256="15cdd808e89bea6ec2e142f433f97460868c7838aaf9e3cf65e77d107d71dc83"
DATA_URL="${DATA_URL:-https://github.com/${GITHUB_REPO}/releases/download/${DATA_TAG}/${ARCHIVE_NAME}}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CACHE_DIR="${REPO_ROOT}/.cache"
ARCHIVE="${DATA_ARCHIVE:-${CACHE_DIR}/${ARCHIVE_NAME}}"

if [[ -z "${DATA_ARCHIVE:-}" && ! -f "$ARCHIVE" ]]; then
    mkdir -p "$CACHE_DIR"
    echo "downloading ${DATA_URL}"
    curl -fL --retry 3 -o "${ARCHIVE}.part" "$DATA_URL"
    mv "${ARCHIVE}.part" "$ARCHIVE"
fi

if command -v sha256sum >/dev/null; then
    actual="$(sha256sum "$ARCHIVE" | cut -d' ' -f1)"
else
    actual="$(shasum -a 256 "$ARCHIVE" | cut -d' ' -f1)"
fi
if [[ "$actual" != "$SHA256" ]]; then
    echo "ERROR: checksum mismatch for $ARCHIVE" >&2
    echo "  expected $SHA256" >&2
    echo "  actual   $actual" >&2
    exit 1
fi

tar -xzf "$ARCHIVE" -C "$REPO_ROOT"
echo "unpacked into ${REPO_ROOT}/experiments/results and ${REPO_ROOT}/models"
