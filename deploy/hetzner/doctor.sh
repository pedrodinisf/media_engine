#!/usr/bin/env bash
# media_engine — run `med doctor` inside the engine container.
# Extra args pass through:
#
#   bash deploy/hetzner/doctor.sh
#   bash deploy/hetzner/doctor.sh --op intelligence.extract
#   bash deploy/hetzner/doctor.sh --json | jq .

set -euo pipefail
# shellcheck source=_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"
require_env

dc exec -T engine med doctor "$@"
