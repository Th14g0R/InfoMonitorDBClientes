#!/usr/bin/env bash
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
printf '\nDiretorio do instalador: %s\n' "$ROOT"
exec "$ROOT/scripts/servico_unix.sh" linux "${1:-}"
