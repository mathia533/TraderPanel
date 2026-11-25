#!/usr/bin/env bash
# Actualiza las skills vendorizadas desde tradermonty/claude-trading-skills.
# Uso: scripts/sync_vendor.sh [commit]   (por defecto, el último de main)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
git clone -q https://github.com/tradermonty/claude-trading-skills "$TMP/cts"
[ -n "${1:-}" ] && git -C "$TMP/cts" checkout -q "$1"
for s in vcp-screener stockbee-momentum-burst-screener crypto-regime-analyzer; do
  rm -rf "$ROOT/vendor/tradermonty/$s"
  mkdir -p "$ROOT/vendor/tradermonty/$s"
  cp -r "$TMP/cts/skills/$s/scripts" "$ROOT/vendor/tradermonty/$s/"
  cp "$TMP/cts/skills/$s/SKILL.md" "$ROOT/vendor/tradermonty/$s/"
  rm -rf "$ROOT/vendor/tradermonty/$s/scripts/tests"
done
cp "$TMP/cts/LICENSE" "$ROOT/vendor/tradermonty/LICENSE"
echo "commit: $(git -C "$TMP/cts" rev-parse HEAD)" > "$ROOT/vendor/tradermonty/VERSION"
rm -rf "$TMP"
echo "Listo. Corré: python -m pytest tests"
