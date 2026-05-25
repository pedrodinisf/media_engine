#!/usr/bin/env bash
# media_engine — tail container logs. Defaults to all services; pass
# service names to narrow.
#
#   bash deploy/hetzner/logs.sh                  # all services
#   bash deploy/hetzner/logs.sh engine           # engine only
#   bash deploy/hetzner/logs.sh caddy postgres   # multi

set -euo pipefail
# shellcheck source=_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"
require_env

dc logs -f --tail=200 "$@"
