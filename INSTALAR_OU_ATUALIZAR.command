#!/usr/bin/env bash
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
exec "$ROOT/scripts/servico_unix.sh" macos "${1:-}"
