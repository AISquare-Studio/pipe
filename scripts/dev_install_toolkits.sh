#!/usr/bin/env bash
# Editable-install aisquare-pipe from this checkout, with every toolkit's deps
# ([toolkits]), into the active Python environment.
#
# Every connector and toolkit ships inside the one aisquare-pipe distribution
# (D8), so this is a single `pip install -e "<checkout>[toolkits]"`: a host's
# `aisquare-pipe[toolkits]` is then met by the local copy, not PyPI.
# scripts/check_toolkits_extra.py keeps [toolkits] complete in CI.
#
# Usage, from any venv (e.g. pipe-as-service's), with a pipe worktree at $P:
#   $P/scripts/dev_install_toolkits.sh                         # framework + every toolkit's deps
#   $P/scripts/dev_install_toolkits.sh --no-deps               # extra pip args are passed on
#   EXTRAS=full,toolkits $P/scripts/dev_install_toolkits.sh    # other extras ([full]: every connector)
#   PYTHON=.venv/bin/python $P/scripts/dev_install_toolkits.sh   # a venv that is not active
set -euo pipefail

PYTHON="${PYTHON:-python}"
EXTRAS="${EXTRAS:-toolkits}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "dev_install_toolkits: $PYTHON -m pip install -e $ROOT[$EXTRAS] $*"
"$PYTHON" -m pip install -e "$ROOT[$EXTRAS]" "$@"
