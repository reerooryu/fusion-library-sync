#!/usr/bin/env bash
# Print every release body exactly as publish.sh would post it, to stdout.
# Copy-paste fodder, and a way to read the lot before anything is published.
#
#   ./scripts/preview-notes.sh > /tmp/release-notes.md
set -euo pipefail
cd "$(dirname "$0")/.."
exec ./scripts/publish.sh --print-notes
