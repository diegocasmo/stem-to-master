#!/usr/bin/env bash
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
  step "mise (into ~/.local/bin)" bash -c "set -o pipefail; curl -fsSL https://mise.run | MISE_VERSION=v2026.11.8 sh"
fi
step "Python and uv (mise.toml)" mise install
step "Python packages (uv.lock)" mise exec -- uv sync --locked
step "plugins (plugins.lock)" mise exec -- uv run tools/plugins.py
echo "setup: done. Run commands with: $(command -v mise) exec -- uv run <command>"
