#!/usr/bin/env bash
# Installs everything the pipeline needs: mise (if missing), Python and uv (mise.toml), the Python packages (uv.lock)
# and the plugins (plugins.lock, into plugins/).
set -euo pipefail
cd "$(dirname "$0")"

step() {
  local name=$1
  shift
  echo "setup: $name"
  "$@" || { echo "setup failed: $name" >&2; exit 1; }
}

export PATH="$PATH:$HOME/.local/bin"
if ! command -v mise >/dev/null; then
  step "mise (into ~/.local/bin)" sh -c "curl -fsSL https://mise.run | sh"
fi
step "Python and uv (mise.toml)" mise install
step "Python packages (uv.lock)" mise exec -- uv sync --locked
step "plugins (plugins.lock)" mise exec -- uv run tools/plugins.py
