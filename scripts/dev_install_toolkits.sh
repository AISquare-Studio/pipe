#!/usr/bin/env bash
# Editable-install aisquare-pipe and every toolkit package from this checkout
# into the active Python environment, in ONE pip resolve (so the toolkits'
# `aisquare-pipe>=…` and a host's `aisquare-pipe[toolkits]` are met by the
# local copies, not PyPI).
#
# The toolkit list is not typed here: scripts/check_toolkits_extra.py --dirs
# reads it from connectors/*/pyproject.toml (every package registering an
# `aisquare_pipe.toolkits` entry point), the same set CI holds the [toolkits]
# extra to.
#
# Usage, from any venv (e.g. pipe-as-service's), with a pipe worktree at $P:
#   $P/scripts/dev_install_toolkits.sh                          # framework + every toolkit
#   $P/scripts/dev_install_toolkits.sh -e $P/connectors/composio  # extra pip args are passed on
#   PYTHON=.venv/bin/python $P/scripts/dev_install_toolkits.sh    # a venv that is not active
set -euo pipefail

PYTHON="${PYTHON:-python}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

args=(-e "$ROOT")
while IFS= read -r dir; do
  args+=(-e "$dir")
done < <("$PYTHON" "$ROOT/scripts/check_toolkits_extra.py" --root "$ROOT" --dirs)

if [ "${#args[@]}" -le 2 ]; then
  echo "dev_install_toolkits: no toolkit packages found under $ROOT/connectors" >&2
  exit 1
fi

echo "dev_install_toolkits: $PYTHON -m pip install ${args[*]} $*"
"$PYTHON" -m pip install "${args[@]}" "$@"
