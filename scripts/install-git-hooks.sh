#!/usr/bin/env bash
# Point this repository at the version-controlled hooks in .githooks/.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
chmod +x "$here/.githooks/"*
git -C "$here" config core.hooksPath .githooks
echo "core.hooksPath set to .githooks"
