#!/usr/bin/env bash
# Catch up recent market data. Thin wrapper around market.update_market_data.
#
#   ./update_recent.sh
#   ./update_recent.sh 30
#   ./update_recent.sh --days 14 --skip-us
set -euo pipefail
cd "$(dirname "$0")"

PY="venv/bin/python"
[ -x "$PY" ] || PY="python3"

args=()
if [[ "${1:-}" =~ ^[0-9]+$ ]]; then
  args+=(--days "$1")
  shift
fi
# Legacy: ./update_recent.sh 14 --stocks  (stock daily is now the default)
if [[ "${1:-}" == "--stocks" ]]; then
  shift
fi
exec "$PY" -m market.update_market_data "${args[@]}" "$@"
