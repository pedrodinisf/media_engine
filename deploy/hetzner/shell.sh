#!/usr/bin/env bash
# media_engine — drop into a shell inside the engine container.
# With args, run them and exit.
#
#   bash deploy/hetzner/shell.sh
#   bash deploy/hetzner/shell.sh -c 'med ops | head'

set -euo pipefail
# shellcheck source=_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"
require_env

if [[ $# -eq 0 ]]; then
    dc exec engine bash
else
    dc exec -T engine bash "$@"
fi
