#!/usr/bin/env bash
# Print every release body as publish.sh would post it.
#
#   ./scripts/preview-notes.sh > /tmp/release-notes.md
set -euo pipefail
cd "$(dirname "$0")/.."
exec ./scripts/publish.sh --print-notes
