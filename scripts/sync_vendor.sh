#!/usr/bin/env bash
# Actualiza las skills vendorizadas (se copian sin modificar):
#   - tradermonty/claude-trading-skills  -> vendor/tradermonty/<skill>/
#   - jakeschincariol/day-trader-skill   -> vendor/day-trader-skill/trader-*/
# Uso: scripts/sync_vendor.sh [commit-tradermonty] [commit-day-trader-skill]   (por defecto, el último de main)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
git clone -q https://github.com/tradermonty/claude-trading-skills "$TMP/cts"
[ -n "${1:-}" ] && git -C "$TMP/cts" checkout -q "$1"
for s in vcp-screener stockbee-momentum-burst-screener crypto-regime-analyzer \
         breakout-trade-planner market-breadth-analyzer uptrend-analyzer sector-analyst \
         exposure-coach drawdown-circuit-breaker trade-performance-coach; do
  rm -rf "$ROOT/vendor/tradermonty/$s"
  mkdir -p "$ROOT/vendor/tradermonty/$s"
  cp -r "$TMP/cts/skills/$s/scripts" "$ROOT/vendor/tradermonty/$s/"
  cp "$TMP/cts/skills/$s/SKILL.md" "$ROOT/vendor/tradermonty/$s/"
  rm -rf "$ROOT/vendor/tradermonty/$s/scripts/tests"
done
cp "$TMP/cts/LICENSE" "$ROOT/vendor/tradermonty/LICENSE"
echo "commit: $(git -C "$TMP/cts" rev-parse HEAD)" > "$ROOT/vendor/tradermonty/VERSION"

git clone -q https://github.com/jakeschincariol/day-trader-skill "$TMP/dts"
[ -n "${2:-}" ] && git -C "$TMP/dts" checkout -q "$2"
rm -rf "$ROOT/vendor/day-trader-skill"
mkdir -p "$ROOT/vendor/day-trader-skill"
cp -r "$TMP/dts"/trader-* "$ROOT/vendor/day-trader-skill/"
cp "$TMP/dts/LICENSE" "$ROOT/vendor/day-trader-skill/LICENSE"
echo "commit: $(git -C "$TMP/dts" rev-parse HEAD)" > "$ROOT/vendor/day-trader-skill/VERSION"
rm -rf "$TMP"
echo "Listo. Corré: python -m pytest tests && node --test tests/alerts.test.mjs"
