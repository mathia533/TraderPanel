"""Historiales diarios OHLCV para el motor de señales.

Fuente principal: el gráfico JSON de Yahoo Finance (gratis, sin clave), el mismo
que usan las herramientas de day-trader-skill. Sirve para ADRs (YPF), BYMA
(GGAL.BA), índices (^MERV, SPY) y cripto (BTC-USD).

Respaldo: data912 (BYMA y acciones de EE. UU.) y CoinGecko (cripto). Esos dan
sólo cierres, así que se arma la vela con apertura = máximo = mínimo = cierre;
el VCP y el Momentum Burst quedan degradados y el motor lo informa.

Las barras se devuelven como dicts {date, open, high, low, close, volume},
de la más reciente a la más vieja, que es el formato que esperan las skills
vendorizadas de tradermonty.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

UA = "Mozilla/5.0 (X11; Linux x86_64) TraderPanel-engine"

# Hora de cierre por mercado (para saber si la última vela está incompleta)
MARKETS = {
    "adr": {"tz": "America/New_York", "close": (16, 0)},
    "byma": {"tz": "America/Argentina/Buenos_Aires", "close": (17, 0)},
    "cripto": {"tz": "UTC", "close": (24, 0)},
}

COINGECKO_TO_YAHOO = {
    "bitcoin": "BTC-USD",
    "ethereum": "ETH-USD",
    "solana": "SOL-USD",
    "binancecoin": "BNB-USD",
    "ripple": "XRP-USD",
    "cardano": "ADA-USD",
    "dogecoin": "DOGE-USD",
    "tron": "TRX-USD",
    "avalanche-2": "AVAX-USD",
    "chainlink": "LINK-USD",
    "polkadot": "DOT-USD",
    "litecoin": "LTC-USD",
}


class SourceError(RuntimeError):
    pass


def _get_json(url: str, timeout: int = 25, retries: int = 3):
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as exc:  # red, 429, JSON roto
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise SourceError(f"{url}: {last}")


def yahoo_symbol(kind: str, ident: str) -> str:
    if kind == "byma":
        return ident if "." in ident or ident.startswith("^") else f"{ident}.BA"
    if kind == "cripto":
        return COINGECKO_TO_YAHOO.get(ident, f"{ident.upper()}-USD")
    return ident


def fetch_yahoo(symbol: str, range_: str = "2y") -> list[dict]:
    q = urllib.parse.quote(symbol)
    j = _get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{q}?range={range_}&interval=1d&includePrePost=false")
    res = (j.get("chart") or {}).get("result") or []
    if not res:
        raise SourceError(f"Yahoo sin datos para {symbol}")
    r = res[0]
    tz = (r.get("meta") or {}).get("exchangeTimezoneName") or "UTC"
    qd = (r.get("indicators") or {}).get("quote") or [{}]
    qd = qd[0]
    bars = []
    for i, ts in enumerate(r.get("timestamp") or []):
        try:
            o, h, l, c = qd["open"][i], qd["high"][i], qd["low"][i], qd["close"][i]
            v = qd.get("volume", [0] * (i + 1))[i] or 0
        except (KeyError, IndexError):
            continue
        if None in (o, h, l, c) or min(o, h, l, c) <= 0:
            continue
        d = datetime.fromtimestamp(ts, ZoneInfo(tz)).date().isoformat()
        bar = {"date": d, "open": float(o), "high": float(h), "low": float(l), "close": float(c), "volume": int(v)}
        if bars and bars[-1]["date"] == d:
            bars[-1] = bar
        else:
            bars.append(bar)
    if len(bars) < 30:
        raise SourceError(f"Yahoo: historial corto para {symbol} ({len(bars)} velas)")
    bars.reverse()
    return bars


def _close_only(rows: list[tuple[str, float]]) -> list[dict]:
    out = [{"date": d, "open": c, "high": c, "low": c, "close": c, "volume": 0} for d, c in rows if c and c > 0]
    out.reverse()
    return out


def fetch_fallback(kind: str, ident: str) -> list[dict]:
    if kind == "byma":
        j = _get_json(f"https://data912.com/historical/stocks/{urllib.parse.quote(ident)}")
        bars = []
        for r in j:
            c = float(r.get("c") or 0)
            if c <= 0:
                continue
            bars.append({
                "date": str(r.get("date"))[:10],
                "open": float(r.get("o") or c), "high": float(r.get("h") or c),
                "low": float(r.get("l") or c), "close": c, "volume": int(float(r.get("v") or 0)),
            })
        bars.reverse()
        return bars
    if kind == "adr":
        j = _get_json(f"https://data912.com/historical/usa_stocks/{urllib.parse.quote(ident)}")
        return _close_only([(str(d)[:10], float(p)) for d, p in zip(j.get("dates", []), j.get("prices", []))])
    if kind == "cripto":
        j = _get_json(f"https://api.coingecko.com/api/v3/coins/{urllib.parse.quote(ident)}/market_chart?vs_currency=usd&days=365&interval=daily")
        rows = {}
        for t, p in j.get("prices", []):
            rows[datetime.fromtimestamp(t / 1000, timezone.utc).date().isoformat()] = float(p)
        return _close_only(sorted(rows.items()))
    raise SourceError(f"Sin respaldo para {kind}")


def fetch_history(kind: str, ident: str) -> tuple[list[dict], str]:
    """Devuelve (barras, fuente). Prueba Yahoo y después el respaldo."""
    errors = []
    try:
        return fetch_yahoo(yahoo_symbol(kind, ident)), "yahoo"
    except Exception as exc:
        errors.append(str(exc))
    try:
        bars = fetch_fallback(kind, ident)
        if len(bars) >= 30:
            return bars, "data912" if kind != "cripto" else "coingecko"
        errors.append("respaldo con historial corto")
    except Exception as exc:
        errors.append(str(exc))
    raise SourceError(" | ".join(errors))


def split_partial(kind: str, bars: list[dict], now: datetime | None = None) -> tuple[list[dict], dict | None]:
    """Separa la vela de hoy si el mercado todavía no cerró.

    Devuelve (velas completas, vela en curso o None). Las entradas se calculan
    sólo con velas cerradas; la vela en curso se usa para vigilar stops y
    objetivos de las posiciones abiertas.
    """
    if not bars:
        return bars, None
    m = MARKETS[kind]
    now = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(m["tz"]))
    today = now.date().isoformat()
    if bars[0]["date"] != today:
        return bars, None
    h, mi = m["close"]
    # 15 minutos de margen después del cierre para que la vela quede firme
    if h >= 24 or now.hour * 60 + now.minute < h * 60 + mi + 15:
        return bars[1:], bars[0]
    return bars, None
