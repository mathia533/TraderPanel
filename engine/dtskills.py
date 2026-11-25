"""Adaptadores a jakeschincariol/day-trader-skill (vendorizada sin cambios).

El pipeline de la skill es

    scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker
    alerts: stop, objetivo y cierre 15:55 (mientras la operación está abierta)
    news / analyst / pelosi (programados)

y acá se usa así, con el código de cada herramienta tal cual:

- trader-scanner  find_setups(): ruptura de 20 días, bandera alcista, recupera la
                  media de 20, sobreventa y rebote de sobreventa, en velas diarias
                  de todos los mercados.
- trader-entry    evaluate(): ruptura del rango de apertura de 15 minutos, vela a
                  vela de 5 minutos (sólo ADRs y acciones de EE. UU.).
- trader-exit     compute(): stop en el medio del rango, objetivo en R, cierre 15:55.
- trader-timemachine  time_machine(): 10.000 repeticiones del resto de la rueda con
                  velas de 5 minutos de las últimas ~59 sesiones, más la regla sobre
                  esas sesiones reales. Para las operaciones de varios días (VCP,
                  Momentum Burst, BYMA, cripto) se usa su mismo simulate() con un
                  pozo armado con las velas DIARIAS del activo.
- trader-budget   size_trade() y bracket_order(): el tamaño lo decide la pérdida
                  máxima, el tope de posición y la caja.
- trader-risk     check(): PASS o BLOCK con el motivo (pérdida máxima, pérdida
                  diaria, operaciones por día, R:R, horario, caja, duplicadas).
- trader-broker   NO se usa: el panel sólo muestra el ticket. No se envían órdenes.
- trader-news     collect(): titulares de Yahoo y, con SEC_USER_AGENT, 8-K/6-K.
- trader-analyst  analyse() y latest_filings(): balances desde la SEC (XBRL).
- trader-pelosi   índice de la Cámara de Representantes y lectura de los PTR.
"""

from __future__ import annotations

import importlib.util
import math
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DTS = ROOT / "vendor" / "day-trader-skill"
_mods: dict = {}

FILES = {
    "scan": "trader-scanner/scan.py",
    "entry": "trader-entry/entry.py",
    "exit": "trader-exit/exit.py",
    "tm": "trader-timemachine/timemachine.py",
    "size": "trader-budget/size.py",
    "risk": "trader-risk/check.py",
    "news": "trader-news/news.py",
    "facts": "trader-analyst/facts.py",
    "ptr": "trader-pelosi/ptr.py",
}


def mod(name: str):
    """Carga una herramienta de la skill como módulo (nombres únicos, sin pisar otros)."""
    if name not in _mods:
        spec = importlib.util.spec_from_file_location(f"_tp_dts_{name}", DTS / FILES[name])
        m = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = m
        spec.loader.exec_module(m)
        _mods[name] = m
    return _mods[name]


def naive_utc(now: datetime) -> datetime:
    """Las herramientas trabajan con UTC sin zona."""
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    return now.replace(microsecond=0)


def ny_date(now: datetime) -> str:
    return mod("entry").to_eastern(naive_utc(now)).date().isoformat()


def ny_time(now: datetime) -> str:
    return mod("entry").to_eastern(naive_utc(now)).strftime("%H:%M")


# ------------------------------------------------------------------ scanner
SETUP_ES = {
    "breakout_20d": "ruptura del máximo de 20 días",
    "bull_flag": "bandera alcista",
    "reclaim_ma20": "recuperó la media de 20",
    "oversold": "sobreventa (RSI < 30)",
    "oversold_bounce": "rebote de sobreventa",
}


def scanner_setups(closed: list[dict]) -> dict:
    """trader-scanner find_setups() sobre cierres diarios (las velas vienen de la más nueva a la más vieja)."""
    closes = [b["close"] for b in reversed(closed[:260])]
    if len(closes) < 30:
        return {"tags": [], "labels": [], "rsi": None}
    tags, nums = mod("scan").find_setups(closes)
    return {"tags": tags, "labels": [SETUP_ES.get(t, t) for t in tags],
            "rsi": round(nums["rsi"], 1) if nums.get("rsi") is not None else None,
            "high20": nums.get("high20"), "ma20": nums.get("ma20")}


# ------------------------------------------------------------------ time machine
def lean_code(mc: dict) -> str:
    tm = mod("tm")
    lv = tm.lean(mc)
    if lv is None:
        return "none"
    d = lv[0]
    if abs(d) < tm.COIN_FLIP:
        return "even"
    if abs(d) < tm.STRONG:
        return "target" if d > 0 else "stop"
    return "target_strong" if d > 0 else "stop_strong"


LEAN_ES = {
    "target_strong": "Muy a favor",
    "target": "A favor",
    "even": "Parejo",
    "stop": "En contra",
    "stop_strong": "Muy en contra",
    "none": "Sin definición",
}


def verdict_es(mc: dict, horizon_txt: str) -> str:
    """El veredicto de trader-timemachine en lenguaje llano, con sus mismos umbrales."""
    tm = mod("tm")
    code = lean_code(mc)
    pt, ps = mc["p_target"], mc["p_stop"]
    if code == "none":
        return (f"En ninguna simulación el precio llegó al objetivo ni al stop {horizon_txt}: "
                "el resultado depende de dónde termine.")
    _, share, even = tm.lean(mc)
    r = mc.get("r_at_target") or 1.0
    head = LEAN_ES[code]
    if code == "even" and abs(r - 1) < 0.005:
        head = "Casi una moneda al aire"
    odds = (f"el precio llegó primero al objetivo en el {pt * 100:.1f} % de las simulaciones "
            f"y al stop en el {ps * 100:.1f} %").replace(".", ",")
    if abs(r - 1) < 0.005:
        txt = f"{head}: {odds}."
    else:
        txt = (f"{head}: {odds}. Como la ganancia es {fmt_num(r)} veces lo que se arriesga, alcanza con ganar "
               f"{even * 100:.0f} de cada 100 para empatar, y ganó {share * 100:.0f} de cada 100 "
               "(contando solo las que tocaron uno de los dos).")
    pn = mc.get("p_neither", 0)
    if pn > tm.MANY_NEITHER:
        txt += (f" El {pn * 100:.0f} % no tocó ninguno {horizon_txt}: ahí cuenta dónde termina el precio "
                f"(resultado medio de todas: {fmt_num(mc.get('avg_R', 0))} veces lo arriesgado).")
    return txt


def fmt_num(v, d=2):
    s = f"{v:.{d}f}".rstrip("0").rstrip(".")
    return s.replace(".", ",")


def fmt_px(v):
    return fmt_num(v, 2 if abs(v) >= 1 else 6)


def tm_blocks(tm: dict, block_list: list) -> bool:
    """Bloquea si el veredicto está en la lista y además no lo compensan las que no tocan ningún lado
    (con más de 40 % sin definir y promedio positivo, trader-timemachine avisa que el precio final decide)."""
    if not tm or not tm.get("ok") or tm.get("lean") not in block_list:
        return False
    mc = tm.get("mc") or {}
    return (mc.get("avg_R") or 0) <= 0 or (mc.get("p_neither") or 0) <= mod("tm").MANY_NEITHER


def _mc_slim(mc: dict) -> dict:
    keep = ("replays", "seed", "bars_left", "p_target", "p_stop", "p_neither", "p_green",
            "median_close", "p5_close", "p95_close", "avg_R", "r_at_target", "pool_sessions", "until")
    out = {k: mc.get(k) for k in keep if k in mc}
    # bandas 5-50-95 % para dibujar el abanico en el panel (pocas columnas)
    bands = mc.get("bands") or []
    if bands:
        step = max(1, len(bands) // 24)
        out["fan"] = [[b[0], b[2], b[4]] for b in bands[::step]] + ([[bands[-1][0], bands[-1][2], bands[-1][4]]]
                                                                   if (len(bands) - 1) % step else [])
    return out


def swing_timemachine(closed: list[dict], entry: float, stop: float, target: float, *, horizon: int = 10,
                      lookback: int = 250, replays: int = 10000, seed: int = 19, history_tag: str = "breakout_20d") -> dict:
    """Time Machine para operaciones de varios días.

    Usa simulate() de trader-timemachine tal cual (mismas tiradas, stop antes que
    objetivo en cada vela) con un pozo de velas DIARIAS del propio activo: cada
    vela como (apertura, máximo, mínimo, cierre) / cierre anterior, de las últimas
    `lookback` ruedas. Repite `horizon` ruedas desde la entrada.

    Historia: la misma regla (stop y objetivo a la misma distancia) en cada día
    del último año en que trader-scanner marcó `history_tag`.
    """
    tm = mod("tm")
    bars = list(reversed(closed[:lookback + 1]))
    if len(bars) < 60 or not (stop < entry < target):
        return {"ok": False, "error": "historial corto o plan inválido"}
    ratios = []
    for p, b in zip(bars, bars[1:]):
        pc = p["close"]
        if pc > 0 and min(b["open"], b["high"], b["low"], b["close"]) > 0:
            ratios.append((b["open"] / pc, b["high"] / pc, b["low"] / pc, b["close"] / pc))
    if len(ratios) < 50:
        return {"ok": False, "error": "pocas velas válidas"}
    pool = [ratios] * (horizon + 1)
    mc = tm.simulate(pool, entry, stop, target, 0, horizon, replays=replays, seed=seed, window=0, side="buy")
    mc["pool_sessions"] = len(ratios)
    mc["until"] = f"{horizon} ruedas"
    hist = _daily_history(closed, (entry - stop) / entry, (target - entry) / (entry - stop), horizon,
                          lookback, history_tag)
    code = lean_code(mc)
    return {"ok": True, "kind": "diario", "mc": _mc_slim(mc), "lean": code,
            "verdict": verdict_es(mc, f"en {horizon} ruedas"), "history": hist,
            "note": (f"Simulación con las {len(ratios)} velas diarias de este activo. No es una predicción: "
                     "balances, noticias y feriados pueden hacer lo que el último año no hizo.")}


def _daily_history(closed, stop_frac, r_mult, horizon, lookback, tag):
    scan = mod("scan")
    bars = list(reversed(closed[:lookback + 60]))
    closes = [b["close"] for b in bars]
    out = {"tag": tag, "label": SETUP_ES.get(tag, tag), "fired": 0, "target": 0, "stop": 0, "neither": 0, "avg_R": None}
    Rs = []
    for i in range(max(30, len(bars) - lookback), len(bars) - 1):
        tags, _ = scan.find_setups(closes[:i + 1])
        if tag not in tags:
            continue
        e = closes[i]
        s = e * (1 - stop_frac)
        t = e + r_mult * (e - s)
        oc = None
        for b in bars[i + 1:i + 1 + horizon]:
            if b["low"] <= s:
                oc = "stop"
                break
            if b["high"] >= t:
                oc = "target"
                break
        last = bars[min(i + horizon, len(bars) - 1)]["close"]
        R = r_mult if oc == "target" else -1.0 if oc == "stop" else (last - e) / (e - s)
        out["fired"] += 1
        out[oc or "neither"] += 1
        Rs.append(R)
    if Rs:
        out["avg_R"] = round(sum(Rs) / len(Rs), 2)
    return out


# ------------------------------------------------------------------ budget + risk
RISK_ES = {
    "format": "formato de la orden", "asset": "activo", "price": "precio", "blocked": "lista bloqueada",
    "size": "tamaño", "cash": "caja", "position": "tope de posición", "short": "venta en corto",
    "stop": "stop", "loss": "pérdida máxima", "target": "objetivo", "reward:risk": "R:R",
    "daily loss": "pérdida diaria", "trades": "operaciones por día", "window": "horario",
    "pdt": "regla PDT", "duplicate": "duplicada", "daily limit": "órdenes por día",
}


def _alias(label: str) -> str:
    """trader-risk sólo acepta tickers de EE. UU. (letras): TGSU2 -> TGSU para el chequeo interno."""
    a = re.sub(r"[^A-Z]", "", label.upper())[:5]
    return a or "X"


def lot_factor(price: float, fractional: bool) -> float:
    """Para cripto, trader-budget dimensiona en lotes (0,001 BTC, 1.000 DOGE...) de 10 a 100 de precio."""
    if not fractional or price <= 0:
        return 1.0
    return 10.0 ** (1 - math.floor(math.log10(price)))


def risk_cfg_for(market_cfg: dict, rules: dict, intraday: bool) -> dict:
    budget = float(market_cfg["budget"])
    return {
        "budget": budget,
        "cash_buffer_pct": rules.get("cash_buffer_pct", 0),
        "max_position_pct": market_cfg["max_position_pct"],
        "max_order_usd": 0,
        "min_order_usd": 0,
        "max_loss_per_trade_usd": round(budget * market_cfg["max_loss_pct"] / 100, 2),
        "max_daily_loss_usd": round(budget * rules.get("max_daily_loss_pct", 3) / 100, 2),
        "max_trades_per_day": rules.get("max_trades_per_day", 3),
        "max_orders_per_day": 0,
        "min_reward_risk": rules.get("min_reward_risk", 1.0),
        "require_stop": True,
        "no_entries_before": rules.get("no_entries_before", "09:45") if intraday else None,
        "no_entries_after": rules.get("no_entries_after", "15:30") if intraday else None,
        "paper": True,
    }


def size_and_check(*, label: str, entry: float, stop: float, target: float, setup: str, market_cfg: dict,
                   rules: dict, open_notional: list[tuple[str, float]], entries_today: list[dict],
                   now: datetime, intraday: bool = False) -> dict:
    """trader-budget size_trade() + bracket_order() y trader-risk check() para una entrada.

    open_notional: [(etiqueta, monto)] de las posiciones abiertas en ese mercado.
    entries_today: [{"label", "max_loss"}] entradas de hoy en ese mercado.
    Devuelve {verdict, reasons (castellano), units, notional, max_loss, ticket, detail}.
    """
    size, check = mod("size"), mod("risk")
    fractional = bool(market_cfg.get("fractional"))
    f = lot_factor(entry, fractional)
    cfg = risk_cfg_for(market_cfg, rules, intraday)
    alias = _alias(label)
    invested = sum(v for _, v in open_notional)
    cash = max(cfg["budget"] - invested, 0.0)
    plan = {"symbol": alias, "side": "buy", "entry": entry * f, "stop": stop * f, "target": target * f, "setup": setup}
    try:
        s = size.size_trade(plan, cfg, cash=cash)
    except size.SizeError as exc:
        return {"verdict": "BLOCK", "reasons": [f"tamaño: {exc}"], "units": 0, "detail": str(exc)}
    order = size.bracket_order(s)
    units = round(s["qty"] * f, 8)
    base = {"units": units, "notional": round(s["cost"], 2), "max_loss": round(s["max_loss"], 2),
            "bound_by": {"max loss": "pérdida máxima", "position cap": "tope de posición", "max order": "orden máxima",
                         "cash": "caja"}.get(s["bound_by"], s["bound_by"]),
            "lot": f if fractional else None}
    if order is None:
        why = s.get("refusal") or "sin orden"
        if s["qty"] <= 0 and s["bound_by"] == "max loss":
            why = "una unidad arriesga más que la pérdida máxima por operación"
        elif s["qty"] <= 0:
            why = f"el {base['bound_by']} no alcanza para una unidad"
        elif "reward:risk" in why:
            why = f"R:R {fmt_num(s['reward_risk'])} menor al mínimo {fmt_num(cfg['min_reward_risk'])}"
        return {**base, "verdict": "BLOCK", "reasons": [why], "detail": s.get("refusal")}
    today = ny_date(now)
    portfolio = {"cash": cash, "positions": [{"ticker": _alias(l), "shares": 1, "lots": [{"cost": v}]}
                                             for l, v in open_notional]}
    state = {"orders": [{"status": "placed", "date": today, "symbol": _alias(e["label"]),
                         "side": "buy", "kind": "entry", "max_loss": e.get("max_loss") or 0}
                        for e in entries_today]}
    res = check.check([order], cfg, portfolio=portfolio, prices={alias: entry * f}, state=state,
                      now=naive_utc(now))
    r = res["results"][0]
    reasons = [_risk_es(x, cfg, entries_today, base) for x in r["reasons"]]
    ticket = {"symbol": label, "side": "compra", "units": units, "type": "bracket",
              "entry": round(entry, 6), "stop": round(stop, 6), "target": round(target, 6),
              "flat_by": rules.get("flat_by", "15:55") if intraday else None,
              "text": (f"Comprar {fmt_num(units, 6)} {label} a mercado, stop {fmt_px(stop)}, "
                       f"objetivo {fmt_px(target)}" + (" y cerrar a las 15:55 NY" if intraday else "")),
              "sent": False}
    return {**base, "verdict": r["verdict"], "reasons": reasons, "ticket": ticket,
            "rr": r.get("reward_risk"), "warnings": r.get("warnings", [])[:3]}


def _risk_es(text: str, cfg: dict, entries_today: list, base: dict) -> str:
    key = text.split(":", 1)[0].strip()
    if key == "loss":
        return f"pérdida máxima: {fmt_num(base['max_loss'])} al stop supera el máximo {fmt_num(cfg['max_loss_per_trade_usd'])}"
    if key == "daily loss":
        used = sum(e.get("max_loss") or 0 for e in entries_today)
        return (f"pérdida diaria: hoy ya hay {fmt_num(used)} en riesgo; con esta serían "
                f"{fmt_num(used + base['max_loss'])}, sobre el límite de {fmt_num(cfg['max_daily_loss_usd'])}")
    if key == "trades":
        return f"operaciones por día: ya hubo {len(entries_today)} entradas hoy (tope {cfg['max_trades_per_day']})"
    if key == "window":
        if "closed" in text:
            return "horario: el mercado de NY está cerrado"
        return f"horario: entradas sólo de {cfg['no_entries_before']} a {cfg['no_entries_after']} (hora de NY)"
    if key == "reward:risk":
        return f"R:R menor al mínimo {fmt_num(cfg['min_reward_risk'])}"
    if key == "cash":
        return "caja: no alcanza con lo que queda libre del presupuesto"
    if key == "position":
        return f"tope de posición: superaría el {fmt_num(cfg['max_position_pct'])} % del presupuesto"
    if key == "duplicate":
        return "duplicada: ya hubo una entrada en este activo hoy"
    return f"{RISK_ES.get(key, key)}: {text.split(':', 1)[-1].strip()}"


# ------------------------------------------------------------------ intradía (ADRs)
STATUS_ES = {"triggered": "disparó", "waiting": "esperando", "expired": "venció", "no-data": "sin datos"}
PHASE_ES = {"pre-open": "antes de la apertura", "forming": "armando el rango", "open": "rueda abierta",
            "after-cutoff": "pasó el horario de entradas", "after-close": "rueda cerrada", "closed": "mercado cerrado"}


def intraday_orb(sym: str, chart_text: str, now: datetime, icfg: dict) -> dict:
    """trader-entry -> trader-exit -> trader-timemachine para un ADR, con un solo gráfico de 60 días a 5 minutos."""
    entry, ex, tm = mod("entry"), mod("exit"), mod("tm")
    nowu = naive_utc(now)
    chart = entry.parse_chart(chart_text, sym)
    res = entry.evaluate(chart, nowu, setup="orb", or_minutes=icfg.get("or_minutes", 15), interval="5m",
                         min_volume_x=icfg.get("min_volume_x"),
                         cfg={"no_entries_before": icfg.get("no_entries_before", "09:45"),
                              "no_entries_after": icfg.get("no_entries_after", "15:30")})
    out = {"symbol": sym, "date": res["date"], "status": res["status"], "status_es": STATUS_ES.get(res["status"], res["status"]),
           "phase": PHASE_ES.get(res.get("phase"), res.get("phase")), "opening_range": res.get("opening_range"),
           "level": res.get("level"), "last": res.get("last"), "trigger": res.get("trigger"),
           "reason": res.get("reason")}
    bars = [b for b in res.get("_bars", [])]
    out["bars"] = [{"t": b["et"].strftime("%H:%M"), "o": round(b["o"], 4), "h": round(b["h"], 4),
                    "l": round(b["l"], 4), "c": round(b["c"], 4)} for b in bars]
    if res["status"] != "triggered":
        return out
    trig, orr = res["trigger"], res["opening_range"]
    try:
        plan = ex.compute(Decimal(str(trig["price"])), "or_mid", None, "r", Decimal(str(icfg.get("target_r", 1))),
                          or_high=Decimal(str(orr["high"])), or_low=Decimal(str(orr["low"])))
    except ex.PlanError as exc:
        out["plan_error"] = str(exc)
        return out
    p = {k: float(plan[k]) for k in ("entry", "stop", "target", "risk", "reward_risk")}
    p["flat_by"] = icfg.get("flat_by", "15:55")
    p["stop_label"] = "medio del rango de apertura"
    out["plan"] = p
    try:
        tplan = tm.normalize_plan({"symbol": sym, "side": "buy", "date": res["date"], "entry": p["entry"],
                                   "stop": p["stop"], "target": p["target"], "entry_time": trig["close_time"],
                                   "setup": "orb", "opening_range": {"minutes": icfg.get("or_minutes", 15)},
                                   "stop_rule": "or_mid", "interval": "5m"})
        doc = tm.time_machine(chart_text, tplan, nowu, replays=icfg.get("replays", 10000),
                              until=p["flat_by"])
        h = doc["history"]
        out["timemachine"] = {
            "ok": True, "kind": "intradía", "mc": _mc_slim(doc["mc"]), "lean": lean_code(doc["mc"]),
            "verdict": verdict_es(doc["mc"], "antes de las " + p["flat_by"]),
            "history": {"label": "la misma ruptura en las sesiones reales", "fired": h.get("triggered", 0),
                        "sessions": h.get("sessions"), "target": h.get("target", 0), "stop": h.get("stop", 0),
                        "neither": h.get("close", 0), "avg_R": h.get("avg_R")},
            "note": doc.get("note"),
        }
    except tm.TMError as exc:
        out["timemachine"] = {"ok": False, "error": str(exc)}
    return out


def intraday_exit(pos: dict, chart_text: str, now: datetime, flat_by: str = "15:55") -> dict | None:
    """trader-alerts para una posición intradía: stop, objetivo y cierre a las 15:55.

    Revisa cada vela de 5 minutos cerrada desde la entrada (stop antes que
    objetivo, como trader-timemachine) y, a la hora de cierre, sale al último precio.
    """
    entry = mod("entry")
    nowu = naive_utc(now)
    chart = entry.parse_chart(chart_text, pos["label"])
    local = entry.to_eastern(nowu)
    day = pos["date"]
    for b in chart["bars"]:
        if b["t"] % 300:
            continue
        et = entry.to_eastern(entry.utc_of(b["t"]))
        if et.date().isoformat() != day or et.strftime("%H:%M") < pos["entry_bar_end"]:
            continue
        if entry.utc_of(b["t"] + 300) > nowu:
            break
        if b["l"] <= pos["stop"]:
            return {"reason": "stop", "price": min(pos["stop"], b["o"]), "why": f"Tocó el stop ({et.strftime('%H:%M')} NY)"}
        if b["h"] >= pos["target"]:
            return {"reason": "target", "price": max(pos["target"], b["o"]) if b["o"] > pos["target"] else pos["target"],
                    "why": f"Alcanzó el objetivo ({et.strftime('%H:%M')} NY)"}
    if local.date().isoformat() > day or local.strftime("%H:%M") >= flat_by:
        last = chart["bars"][-1]["c"] if chart["bars"] else pos["entry"]
        return {"reason": "flat", "price": last, "why": f"Cierre del día ({flat_by} NY): fuera antes de la campana"}
    return None


# ------------------------------------------------------------------ noticias
def news_for(tickers: dict, state: dict, now: datetime, *, sec_ua: str | None, hours: int = 36,
             offline: str | None = None) -> tuple[list[dict], list[str]]:
    """trader-news collect() + tag() + dedupe(). tickers: {TICKER: nombre}."""
    nw = mod("news")
    fetcher = nw.Fetcher(sec_ua=sec_ua, offline=offline)
    errors: list[str] = []
    items = nw.collect(fetcher, tickers, state, sec=bool(sec_ua), forms=("8-K", "6-K"), count=8, errors=errors)
    items = nw.dedupe(nw.tag(items, tickers))
    nowu = naive_utc(now).replace(tzinfo=timezone.utc)
    sel = nw.select(items, state, nowu, hours=hours, show_all=True)
    nw.remember(sel, state, nowu)
    out = []
    for it in sel:
        out.append({"ticker": it["ticker"], "touches": it.get("touches", []), "title": it["title"],
                    "link": it["link"], "source": it["source"], "kind": it["kind"],
                    "time": it["time"].isoformat() if it["time"] else None, "tags": it.get("tags", []),
                    "new": it.get("new", False), "id": nw.fingerprint(it), "form": it.get("form")})
    return out, errors


TAG_ES = {"earnings": "balance", "analyst": "analistas", "legal": "legal", "deal": "operación corporativa",
          "capital": "dividendos/recompra", "people": "directivos", "risk": "riesgo", "halt": "suspensión",
          "regulatory": "regulatorio", "macro": "macro"}


# ------------------------------------------------------------------ balances (SEC)
def analyst_for(ticker: str, fetcher, ciks: dict) -> dict:
    fa = mod("facts")
    cik, name = ciks.get(ticker.upper(), (None, None))
    if not cik:
        return {"ticker": ticker, "ok": False, "error": "sin CIK en la SEC"}
    out = {"ticker": ticker, "name": name, "cik": cik}
    try:
        subs = fetcher.json(fa.SUBMISSIONS_URL % cik, "submissions-CIK%s.json" % cik)
        out["filings"] = fa.latest_filings(subs, cik)
    except fa.FactsError as exc:
        out["filings_error"] = str(exc)
    try:
        cf = fetcher.json(fa.FACTS_URL % cik, "companyfacts-CIK%s.json" % cik)
        res = fa.analyse(cf, quarters=6, name=name)
        out["rows"] = [{k: r.get(k) for k in ("end", "fiscal", "revenue", "net_income", "eps_diluted",
                                              "gross_margin", "operating_margin", "net_margin", "revenue_yoy",
                                              "net_income_yoy", "eps_yoy", "form", "filed")} for r in res["rows"]]
        out["ttm"] = res.get("ttm")
        out["ok"] = True
    except fa.FactsError as exc:
        out["ok"] = False
        out["error"] = ("sin balances trimestrales en XBRL (los emisores extranjeros informan por 20-F/6-K)"
                        if "no quarterly" in str(exc) else str(exc))
    return out


# ------------------------------------------------------------------ congresistas (PTR)
def congress_trades(state: dict, today: date, watch: set[str], *, max_docs: int = 25,
                    offline_index: str | None = None, offline_texts: dict | None = None) -> tuple[list[dict], list[str]]:
    """trader-pelosi: PTR nuevos de la Cámara, leídos con pdftotext; marca los tickers de la watchlist."""
    ptr = mod("ptr")
    errors: list[str] = []
    rows = ptr.select(ptr.load_index(today.year, offline=offline_index), kind="P")
    fresh = ptr.new_since(rows, state, today, first_days=14)[:max_docs]
    found = []
    with tempfile.TemporaryDirectory() as tmp:
        for r in fresh:
            try:
                if offline_texts is not None:
                    text = offline_texts.get(r["doc_id"])
                    report = ptr.parse_ptr_text(text, filed=r["filed"]) if text else None
                else:
                    _, report = ptr._fetch_and_read(r["doc_id"], r["year"], tmp, filed=r["filed"])
            except ptr.PtrError as exc:
                errors.append(f"PTR {r['doc_id']}: {exc}")
                continue
            state.setdefault("seen", []).append(r["doc_id"])
            if not report:
                errors.append("falta pdftotext para leer los PTR")
                continue
            for row in report["rows"]:
                if not row.get("ticker"):
                    continue
                found.append({"member": r["name"], "district": r["district"], "filed": r["filed"],
                              "doc_id": r["doc_id"], "url": r["url"], "ticker": row["ticker"],
                              "asset": row["asset"], "type": row.get("type_name") or row.get("type"),
                              "date": row.get("date"), "amount": row.get("amount"),
                              "owner": row.get("owner_name"), "late": row.get("late"),
                              "watch": row["ticker"] in watch})
    state["seen"] = state.get("seen", [])[-2000:]
    state["last_check"] = today.isoformat()
    return found, errors


TXN_ES = {"purchase": "compra", "sale": "venta", "partial sale": "venta parcial", "exchange": "canje"}
