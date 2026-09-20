#!/usr/bin/env bash
# BODYBOOT 60-second demo (Linux / macOS).
#   scripts/demo.sh                  real Claude CLI if installed, else deterministic
#   scripts/demo.sh deterministic
#   scripts/demo.sh claude
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONIOENCODING=utf-8

if command -v uv >/dev/null 2>&1; then UV=(uv)
elif python3 -m uv --version >/dev/null 2>&1; then UV=(python3 -m uv)
else echo "uv not found. Install it: https://docs.astral.sh/uv/" >&2; exit 1; fi

AGENT="${1:-auto}"
if [ "$AGENT" = "auto" ]; then
  if command -v claude >/dev/null 2>&1 || [ -x "$HOME/.local/bin/claude" ] || [ -n "${BODYBOOT_CLAUDE_BIN:-}" ]; then
    AGENT=claude
  else
    AGENT=deterministic
  fi
  echo "agent: $AGENT"
fi

"${UV[@]}" sync --quiet
"${UV[@]}" run bodyboot demo --agent "$AGENT"
