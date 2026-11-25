"""Índices bursátiles para la pestaña "Índices" del panel.

    python -m engine.indices             # baja Yahoo y escribe data/indices.json
    python -m engine.indices --offline DIR   # usa DIR/<símbolo>.json (respuestas de Yahoo guardadas)

El navegador no puede pedirle datos a Yahoo directamente (no permite CORS), así que
este script corre en el mismo Action que el motor de señales y deja un JSON chico
con el último valor, la variación del día y unos 6 meses de cierres diarios de cada índice.
Si un índice falla, se conserva el dato anterior (marcado con "stale") en vez de
borrarlo, así el panel nunca queda vacío por un error pasajero.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import sources

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "indices.json"
KEEP = 130  # ~6 meses de ruedas: alcanza para la media de 50 y el gráfico chico

# (símbolo de Yahoo, nombre, grupo, descripción corta, unidad)
INDICES = [
    ("^GSPC", "S&P 500", "usa", "500 empresas grandes de EE. UU.", "pts"),
    ("^NDX", "Nasdaq 100", "usa", "100 mayores no financieras del Nasdaq", "pts"),
    ("^IXIC", "Nasdaq Composite", "usa", "Todo el Nasdaq", "pts"),
    ("^DJI", "Dow Jones", "usa", "30 industriales", "pts"),
    ("^RUT", "Russell 2000", "usa", "Empresas chicas de EE. UU.", "pts"),
    ("^VIX", "VIX", "usa", "Volatilidad esperada del S&P 500 (miedo)", "pts"),
    ("^TNX", "Bono 10 años EE. UU.", "usa", "Tasa del Tesoro a 10 años", "%"),
    ("^MERV", "Merval", "arg", "Panel líder de BYMA, en pesos", "pts"),
    ("^BVSP", "Bovespa", "mundo", "Bolsa de Brasil", "pts"),
    ("^STOXX50E", "Euro Stoxx 50", "mundo", "50 grandes de la zona euro", "pts"),
    ("^N225", "Nikkei 225", "mundo", "Bolsa de Japón", "pts"),
    ("DX-Y.NYB", "Índice dólar (DXY)", "mundo", "Dólar contra 6 monedas fuertes", "pts"),
]


def parse_chart(j: dict) -> dict:
    """Convierte la respuesta de /v8/finance/chart en {last, prev, ch, at, d, c}."""
    res = ((j or {}).get("chart") or {}).get("result") or []
    if not res:
        raise sources.SourceError("Yahoo sin datos")
    r = res[0]
    meta = r.get("meta") or {}
    tz = ZoneInfo(meta.get("exchangeTimezoneName") or "UTC")
    closes = (((r.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
    d, c = [], []
    for ts, v in zip(r.get("timestamp") or [], closes):
        if v is None or v <= 0:
            continue
        day = datetime.fromtimestamp(ts, tz).date().isoformat()
        if d and d[-1] == day:
            c[-1] = float(v)
        else:
            d.append(day)
            c.append(float(v))
    if len(c) < 2:
        raise sources.SourceError("historial corto")
    last = meta.get("regularMarketPrice")
    last = float(last) if isinstance(last, (int, float)) and last > 0 else c[-1]
    mt = meta.get("regularMarketTime")
    at = datetime.fromtimestamp(mt, timezone.utc) if isinstance(mt, (int, float)) else None
    # Si la última vela es de hoy, la variación es contra la anterior
    if at and d[-1] == at.astimezone(tz).date().isoformat():
        c[-1] = last
        prev = c[-2]
    else:
        prev = c[-1]
        if at:
            d.append(at.astimezone(tz).date().isoformat())
            c.append(last)
    return {
        "last": round(last, 4),
        "prev": round(prev, 4),
        "ch": round((last / prev - 1) * 100, 3) if prev else None,
        "at": at.isoformat(timespec="seconds") if at else None,
        "d": d[-KEEP:],
        "c": [round(x, 4) for x in c[-KEEP:]],
    }


def fetch(symbol: str, offline: Path | None = None) -> dict:
    if offline:
        f = offline / f"{symbol}.json"
        return parse_chart(json.loads(f.read_text()))
    q = urllib.parse.quote(symbol)
    return parse_chart(sources._get_json(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{q}?range=1y&interval=1d&includePrePost=false"))


def build(offline: Path | None = None, previous: dict | None = None) -> dict:
    prev_items = {x["id"]: x for x in (previous or {}).get("items", [])}
    items, errors = [], {}
    for sym, name, group, desc, unit in INDICES:
        base = {"id": sym, "name": name, "group": group, "desc": desc, "unit": unit}
        try:
            items.append({**base, **fetch(sym, offline)})
        except Exception as exc:  # red, 429, símbolo dado de baja
            errors[sym] = str(exc)[:200]
            old = prev_items.get(sym)
            if old:
                items.append({**old, **base, "stale": True})
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "Yahoo Finance",
        "items": items,
        "errors": errors,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Índices bursátiles para el panel")
    ap.add_argument("--offline", help="carpeta con respuestas de Yahoo guardadas")
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args(argv)
    out = Path(a.out)
    try:
        previous = json.loads(out.read_text())
    except Exception:
        previous = None
    data = build(Path(a.offline) if a.offline else None, previous)
    ok = sum(1 for x in data["items"] if not x.get("stale"))
    if not data["items"]:
        print("Ningún índice disponible:", data["errors"], file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"{ok}/{len(INDICES)} índices actualizados")
    for k, v in data["errors"].items():
        print("   error", k, v, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
