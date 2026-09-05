#!/usr/bin/env bash
# Create the sandboxed workspaces root and install the demo project as a real git repo.
#   scripts/setup_workspaces.sh            -> ~/MuseWorkspaces/demo-project
#   MUSEGLASS_WORKSPACES=/tmp/ws scripts/setup_workspaces.sh
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
root="${MUSEGLASS_WORKSPACES:-$HOME/MuseWorkspaces}"
project="${1:-demo-project}"
target="$root/$project"

mkdir -p "$root"
if [[ -e "$target" ]]; then
  echo "refusing to overwrite existing $target" >&2
  echo "delete it first if you want a fresh copy" >&2
  exit 1
fi

cp -R "$here/examples/demo-fastapi" "$target"
(
  cd "$target"
  git init -q -b main
  git add -A
  git -c user.name="MuseGlass" -c user.email="museglass@localhost" commit -q -m "Initial demo service"
)

# register it
python3 - "$root" "$project" "$target" <<'PY'
import json, sys, pathlib
root, project, target = sys.argv[1:]
reg = pathlib.Path(root) / ".museglass-projects.json"
data = json.loads(reg.read_text()) if reg.exists() else {}
data[project] = {"path": target, "display_name": project.replace("-", " ")}
reg.write_text(json.dumps(data, indent=2, sort_keys=True))
PY

echo "workspace ready: $target"
echo "registered as '$project' in $root/.museglass-projects.json"
