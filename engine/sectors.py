"""Ranking de sectores locales (BYMA) con registro diario.

    python -m engine.sectors              # baja data912 y actualiza data/sectors.json
    python -m engine.sectors --offline F  # usa F (respuesta de /live/arg_stocks guardada)

Para cada sector de engine/sectors.json calcula la variación del día ponderada por el
monto operado (volumen × precio), cuántas suben y cuántas bajan, la que más sube y la
que más baja. Guarda una fila por rueda en "history" (la de hoy se pisa en cada corrida,
hasta 250 ruedas), y con eso arma la variación acumulada de 5 y 20 ruedas.
Corre en el Action del motor como paso aparte: si data912 falla, queda el archivo anterior.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import sources

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "sectors.json"
MAP = ROOT / "engine" / "sectors.json"
URL = "https://data912.com/live/arg_stocks"
KEEP = 250
AR = ZoneInfo("America/Argentina/Buenos_Aires")


def day_rows(live: list[dict], sectors: dict) -> dict:
    """Una rueda: {sector: {ch, n, up, down, vol, best, worst}} con el cambio ponderado por monto."""
    by = {}
    for r in live or []:
        s = str(r.get("symbol") or "").upper()
        if s and s not in by:
            by[s] = r
    out = {}
    for key, sc in sectors.items():
        items = []
        for t in sc.get("byma", []):
            r = by.get(t)
            try:
                c, ch = float(r["c"]), float(r["pct_change"])
            except (TypeError, KeyError, ValueError):
                continue
            if c > 0:
                items.append((t, ch, max(0.0, float(r.get("v") or 0)) * c))
        if not items:
            continue
        vol = sum(w for _, _, w in items)
        # Sin volumen (antes de la apertura) se pondera igual a todas
        ch = sum(c * w for _, c, w in items) / vol if vol > 0 else sum(c for _, c, _ in items) / len(items)
        best, worst = max(items, key=lambda x: x[1]), min(items, key=lambda x: x[1])
        out[key] = {"ch": round(ch, 3), "n": len(items), "up": sum(1 for _, c, _ in items if c > 0),
                    "down": sum(1 for _, c, _ in items if c < 0), "vol": round(vol),
                    "best": [best[0], round(best[1], 2)], "worst": [worst[0], round(worst[1], 2)]}
    return out


def compound(history: dict, key: str, n: int) -> float | None:
    days = sorted(history)[-n:]
    vals = [history[d][key]["ch"] for d in days if key in history[d]]
    if len(vals) < min(n, 2):
        return None
    acc = 1.0
    for v in vals:
        acc *= 1 + v / 100
    return round((acc - 1) * 100, 2)


def build(prev: dict | None, live: list[dict], sectors: dict, now: datetime) -> dict:
    history = dict((prev or {}).get("history") or {})
    today = now.astimezone(AR).date()
    rows = day_rows(live, sectors)
    # Sólo días hábiles y con datos: el fin de semana data912 repite el viernes
    if rows and today.weekday() < 5:
        history[today.isoformat()] = rows
    history = {d: history[d] for d in sorted(history)[-KEEP:]}
    last = sorted(history)[-1] if history else None
    total = sum(v["vol"] for v in (history.get(last) or {}).values()) or 1
    ranking = []
    for key, sc in sectors.items():
        r = (history.get(last) or {}).get(key)
        if not r:
            continue
        ranking.append({"key": key, "name": sc.get("name", key), **r, "share": round(100 * r["vol"] / total, 1),
                        "ch5": compound(history, key, 5), "ch20": compound(history, key, 20),
                        "spark": [history[d][key]["ch"] for d in sorted(history)[-20:] if key in history[d]]})
    ranking.sort(key=lambda x: -x["ch"])
    return {"generated_at": now.isoformat(), "date": last, "ranking": ranking, "days": len(history), "history": history}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--offline", type=Path)
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args(argv)
    sectors = json.loads(MAP.read_text(encoding="utf-8"))["sectors"]
    prev = json.loads(a.out.read_text(encoding="utf-8")) if a.out.exists() else None
    try:
        live = json.loads(a.offline.read_text(encoding="utf-8")) if a.offline else sources._get_json(URL)
    except Exception as exc:  # noqa: BLE001
        print(f"data912 no respondió: {exc}", file=sys.stderr)
        return 1
    out = build(prev, live, sectors, datetime.now(timezone.utc))
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    top = ", ".join(f"{r['name']} {r['ch']:+.2f}%" for r in out["ranking"][:3])
    print(f"Sectores: {len(out['ranking'])} · ruedas guardadas: {out['days']} · arriba: {top or '—'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
