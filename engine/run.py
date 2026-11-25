"""Motor de señales de TraderPanel.

    python -m engine.run                 # baja datos, analiza y escribe data/*.json
    python -m engine.run --offline DIR   # usa velas guardadas (DIR/<kind>_<id>.json)
    python -m engine.run --no-crypto-regime

Escribe:
  data/signals.json  lo que lee el panel: puntaje y setups por activo, régimen de
                     cada mercado, posiciones abiertas, preparadas y eventos.
  data/events.json   avisos de entrada/salida (cada uno una sola vez) con el texto
                     listo para Telegram. engine/telegram.py los envía.
  data/state.json    posiciones abiertas y avisos ya emitidos.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config_check, dtskills, patterns, pipeline, planner, review, skills, sources, tmextra

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CONFIG = Path(__file__).resolve().parent / "config.json"
VERSION = "2.0"
MAX_EVENTS = 200
MKT_LABEL = {"adr": "ADR", "byma": "BYMA", "cripto": "Cripto"}
CUR = {"adr": "US$", "byma": "$", "cripto": "US$"}


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def save_json(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False, default=_num))
    tmp.replace(p)


def _num(x):
    try:
        return float(x)
    except Exception:
        return str(x)


def rnd(v, ref=None):
    if v is None:
        return None
    ref = abs(ref if ref is not None else v)
    d = 0 if ref >= 1000 else 2 if ref >= 1 else 6
    return round(float(v), d)


MK_NAME = {"adr": "ADR", "byma": "BYMA", "cripto": "Cripto"}
esc = pipeline.esc


def units_txt(u):
    return fmt(u, 1000) if u == int(u) else f"{u:.6f}".rstrip("0").replace(".", ",")


def fmt(v, ref=None):
    """Número en formato argentino. `ref` decide los decimales; si es 1, se usa 1 decimal."""
    if v is None:
        return "—"
    if ref == 1:
        d = 1
    else:
        ref = abs(ref if ref is not None else v)
        d = 0 if ref >= 1000 else 2 if ref >= 1 else 4
    s = f"{v:,.{d}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


# ------------------------------------------------------------------ datos
def get_bars(kind: str, ident: str, offline: Path | None):
    if offline:
        f = offline / f"{kind}_{ident.replace('^', '_')}.json"
        bars = json.loads(f.read_text())
        return bars, "offline"
    return sources.fetch_history(kind, ident)


CANDLES_KEEP = 180   # 6 meses visibles + 50 ruedas para la media de 50


def candles_payload(fetched: dict, now) -> dict:
    """Velas diarias de la watchlist para la ficha del activo en el panel (data/candles.json).
    Cada vela es [fecha, apertura, máximo, mínimo, cierre, volumen], de la más vieja a la más nueva,
    e incluye la vela en curso si el mercado está abierto."""
    items = {}
    for key, (bars, src) in fetched.items():
        rows = []
        for b in reversed(bars[:CANDLES_KEEP]):
            try:
                c = float(b["close"])
            except (KeyError, TypeError, ValueError):
                continue
            ref = c
            rows.append([str(b["date"])[:10], rnd(b.get("open", c), ref), rnd(b.get("high", c), ref),
                         rnd(b.get("low", c), ref), rnd(c, ref), int(float(b.get("volume") or 0))])
        if rows:
            flat = all(r[2] == r[3] for r in rows[-20:])
            items[key] = {"src": src, "ohlc": not flat, "bars": rows}
    return {"generated_at": now.isoformat(), "items": items}


def save_compact(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=_num))
    tmp.replace(p)


def synthetic_index(series: list[list[dict]]) -> list[dict]:
    """Índice igual ponderado (base 100) con las fechas que comparten los activos."""
    series = [b for b in series if len(b) >= 200]
    if len(series) < 3:
        return []
    maps = [{x["date"]: x["close"] for x in b} for b in series]
    dates = sorted(set.intersection(*(set(m) for m in maps)))[-400:]
    if len(dates) < 200:
        return []
    out = []
    for d in dates:
        v = 100 * sum(m[d] / m[dates[0]] for m in maps) / len(maps)
        out.append({"date": d, "open": v, "high": v, "low": v, "close": v, "volume": 0})
    out.reverse()
    return out


# ------------------------------------------------------------------ textos
TAGS = {"4pct_breakout": "ruptura del 4 %", "dollar_breakout": "ruptura en dólares",
        "range_expansion": "expansión de rango", "9m_volume": "volumen de 9 M"}
SETUP_NAME = {"vcp": "VCP (Minervini)", "burst": "Momentum Burst (Stockbee)",
              "orb": "ruptura del rango de apertura 15' (day-trader-skill)"}


def tg_entry(ev):
    c = CUR[ev["kind"]]
    head = f"🟢 <b>ENTRADA · {ev['label']}</b> ({MKT_LABEL[ev['kind']]}{' · intradía' if ev.get('mode') == 'intradia' else ''})"
    setup = f"Setup: {SETUP_NAME[ev['setup']]}" + (f" · puntaje {ev['score']:.0f}/100" if ev.get("score") is not None else "")
    lines = [
        head,
        setup,
        f"Precio: {c} {fmt(ev['price'])}",
        f"Stop: {c} {fmt(ev['stop'], ev['price'])} (−{fmt(ev['stop_pct'], 1)} %)",
        f"Objetivo: {c} {fmt(ev['target'], ev['price'])} · R:R {fmt(ev['rr'], 1)}",
    ]
    if ev.get("units"):
        lines.append(f"Tamaño sugerido: {units_txt(ev['units'])} ({c} {fmt(ev['notional'])}, riesgo {c} {fmt(ev['max_loss'])})")
    if ev.get("flat_by"):
        lines.append(f"Cierre: a las {ev['flat_by']} de NY si no tocó stop ni objetivo")
    if ev.get("tm"):
        lines.append(f"⏳ Time Machine: {ev['tm']['verdict']}")
    lines.append("✅ Riesgo: PASS (trader-risk)")
    lines.append(f"<i>{ev['why']}</i>")
    return "\n".join(lines)


def tg_exit(ev):
    c = CUR[ev["kind"]]
    icon = {"target": "🎯", "stop": "🔴", "trend": "🟠", "time": "⏱", "flat": "🔔"}.get(ev["reason"], "⚪")
    pnl = ev.get("pnl_pct")
    return "\n".join([
        f"{icon} <b>SALIDA · {ev['label']}</b> ({MKT_LABEL[ev['kind']]})",
        f"{ev['why']} · {c} {fmt(ev['price'])}",
        f"Entrada {c} {fmt(ev['entry'], ev['price'])} → resultado {'+' if pnl >= 0 else '−'}{fmt(abs(pnl), 1)} % ({'+' if ev['r'] >= 0 else '−'}{fmt(abs(ev['r']), 1)}R)",
    ])


def tg_update(ev):
    c = CUR[ev["kind"]]
    return f"🔵 <b>AJUSTE · {ev['label']}</b>\n{ev['why']}: stop a {c} {fmt(ev['stop'])}"


# ------------------------------------------------------------------ motor
def analyze_all(cfg, offline=None, use_regime=True, now=None, intraday_fetch=None, extras=None):
    """Corrida completa. `intraday_fetch(sym)` y `extras` (carpetas offline de noticias,
    balances y congresistas) sirven para las pruebas sin red."""
    now = now or datetime.now(timezone.utc)
    extras = extras or {}
    state = load_json(DATA / "state.json", {"positions": {}, "rung": {}, "closed": []})
    events_log = load_json(DATA / "events.json", {"events": []})
    risk = cfg["risk"]
    new_events, errors, assets, watch = [], {}, {}, []
    regime_out = {}

    # Velas de todos los activos primero (el índice sintético las necesita)
    fetched = {}
    for kind, ids in cfg["watchlist"].items():
        for ident in ids:
            try:
                fetched[f"{kind}:{ident}"] = get_bars(kind, ident, offline)
            except Exception as exc:
                errors[f"{kind}:{ident}"] = str(exc)[:200]
            if not offline:
                time.sleep(0.3)  # no saturar Yahoo

    # Velas para la ficha del activo del panel (no cambia el análisis)
    try:
        save_compact(DATA / "candles.json", candles_payload(fetched, now))
    except Exception as exc:
        errors["candles"] = str(exc)[:200]

    # Índices de referencia y filtro de mercado
    bench, bench_src = {}, {}
    for kind, sym in cfg["benchmarks"].items():
        try:
            if offline:
                b, _ = get_bars(kind, sym, offline)
            else:
                b = sources.fetch_yahoo(sym)
            bench_src[kind] = sym
        except Exception as exc:
            # Sin índice: promedio igual ponderado de la watchlist de ese mercado
            b = synthetic_index([fetched[k][0] for k in fetched if k.startswith(kind + ":")])
            bench_src[kind] = "índice propio de la watchlist" if b else sym
            if not b:
                errors[f"bench:{sym}"] = str(exc)[:200]
        bench[kind], _ = sources.split_partial(kind, b, now) if b else ([], None)
    crypto_regime = None
    if use_regime and not offline:
        try:
            crypto_regime = skills.run_crypto_regime(DATA)
        except Exception as exc:
            errors["crypto_regime"] = str(exc)[:200]
    for kind in cfg["watchlist"]:
        if kind == "cripto":
            gate, why = planner.market_gate_from_regime(crypto_regime, bench.get("cripto"))
        else:
            gate, why = planner.market_gate_from_trend(bench.get(kind))
        regime_out[kind] = {"gate": gate, "why": why, "benchmark": bench_src.get(kind, cfg["benchmarks"][kind])}
        if kind == "cripto" and crypto_regime:
            regime_out[kind]["regime"] = crypto_regime.get("composite")
            regime_out[kind]["components"] = crypto_regime.get("components")

    # Contexto: salud del mercado de EE. UU. (exposure-coach) y cortacircuitos por pérdidas
    DATA.mkdir(parents=True, exist_ok=True)
    ctx = pipeline.market_context(cfg, state, now, offline is not None, DATA)
    errors.update(ctx["errors"])
    if "adr" in regime_out:
        g = pipeline.merge_gate((regime_out["adr"]["gate"], regime_out["adr"]["why"]),
                                tmextra.exposure_gate(ctx["us_market"]))
        regime_out["adr"]["gate"], regime_out["adr"]["why"] = g
        if ctx["us_market"]:
            regime_out["adr"]["exposure"] = (ctx["us_market"] or {}).get("exposure")
    for kind, br in ctx["breakers"].items():
        if kind in regime_out:
            regime_out[kind]["breaker"] = br

    # Noticias (trader-news) de toda la watchlist, una vez por corrida
    news_items, news_by_asset = [], {}
    if offline is None or extras.get("news"):
        news_items, news_by_asset = pipeline.run_news(cfg, DATA, now, errors, offline=extras.get("news"))
    if not news_by_asset:
        _, news_by_asset = pipeline.news_tickers(cfg)
    news_by_ticker = pipeline.group_news(news_items)
    n_watch_pipes = 0

    for kind, ids in cfg["watchlist"].items():
        flt = cfg["filters"][kind]
        gate = regime_out[kind]["gate"]
        for ident in ids:
            key = f"{kind}:{ident}"
            if key not in fetched:
                continue
            bars, src = fetched[key]
            closed, live = sources.split_partial(kind, bars, now)
            if len(closed) < 60:
                errors[key] = f"historial corto ({len(closed)} velas)"
                continue
            last = live or closed[0]
            label = ident.upper() if kind != "cripto" else sources.yahoo_symbol(kind, ident).split("-")[0]
            degraded = src in ("data912", "coingecko") and all(b["high"] == b["low"] for b in closed[:20])
            try:
                vcp = skills.run_vcp(closed, bench.get(kind) or None)
            except Exception as exc:
                vcp = {"ok": False, "error": str(exc)[:200]}
            try:
                burst = skills.run_burst(closed, gate, min_price=flt["min_price"], min_volume=flt["min_volume"], symbol=label)
            except Exception as exc:
                burst = {"ok": False, "error": str(exc)[:200]}

            try:
                sc = dtskills.scanner_setups(closed)
            except Exception as exc:
                sc = {"tags": [], "labels": [], "rsi": None, "error": str(exc)[:200]}
            news_t = news_by_asset.get(key)
            pipe = None

            c = [b["close"] for b in closed]
            s50, s200 = planner.sma(c, 50), planner.sma(c, 200)
            stage4 = s50 is not None and s200 is not None and c[0] < s50 < s200
            score = max(vcp.get("score", 0) if vcp.get("ok") else 0,
                        burst.get("score", 0) if burst.get("ok") else 0)
            if stage4:
                score = min(score, 30)

            # ---- posiciones abiertas: stops, objetivos y salidas
            pos = state["positions"].get(key)
            exited_now = False
            if pos:
                # Revisar cada vela posterior a la de entrada (por si se saltó una corrida)
                later = [i for i in range(len(closed) - 1, -1, -1) if closed[i]["date"] > pos["bar_date"]]
                steps = [(closed[i], closed[i:]) for i in later] + ([(live, closed)] if live else [])
                held = len(later)
                ex = None
                for n, (bar, hist) in enumerate(steps):
                    ex = planner.check_exit(pos, bar, hist, risk, min(n + 1, held))
                    if ex:
                        last_bar = bar
                        break
                    if planner.maybe_breakeven(pos, bar, risk):
                        new_events.append({"type": "update", "kind": kind, "id": ident, "label": label, "setup": pos["setup"],
                                           "stop": pos["stop"], "price": rnd(bar["close"]),
                                           "why": "Avanzó 1R, el stop pasa a la entrada",
                                           "id_ev": f"{key}:update:{pos['bar_date']}:breakeven", "bar_date": bar["date"]})
                if ex:
                    pnl = (ex["price"] / pos["entry"] - 1) * 100
                    r_mult = (ex["price"] - pos["entry"]) / (pos["entry"] - pos["initial_stop"])
                    pnl_money = round((ex["price"] - pos["entry"]) * (pos.get("units") or 0), 2)
                    ev = {"type": "exit", "reason": ex["reason"], "kind": kind, "id": ident, "label": label,
                          "setup": pos["setup"], "price": rnd(ex["price"]), "entry": pos["entry"],
                          "initial_stop": pos["initial_stop"],
                          "why": ex["why"], "pnl_pct": round(pnl, 2), "r": round(r_mult, 2),
                          "bar_date": last_bar["date"], "opened_at": pos["opened_at"]}
                    ev["id_ev"] = f"{key}:exit:{pos['bar_date']}:{ex['reason']}"
                    new_events.append(ev)
                    state["closed"] = ([{**pos, "exit": ev["price"], "exit_reason": ex["reason"], "pnl_pct": ev["pnl_pct"],
                                         "r": ev["r"], "pnl_money": pnl_money, "closed_at": now.isoformat()}] + state.get("closed", []))[:100]
                    del state["positions"][key]
                    state.setdefault("last_exit", {})[key] = closed[0]["date"]
                    pos, exited_now = None, True
                if pos:
                    pos["last"] = rnd(last["close"])
                    pos["r_now"] = round((last["close"] - pos["entry"]) / (pos["entry"] - pos["initial_stop"]), 2)
                    pos["sessions"] = held

            # ---- entradas nuevas (sólo con velas cerradas)
            cand = None
            if vcp.get("ok") and vcp["state"] == "Breakout" and vcp["rating"] in cfg["setups"]["vcp_min_rating"]:
                cand = {"setup": "vcp", "score": vcp["score"], "entry": closed[0]["close"], "stop": vcp.get("stop"),
                        "why": f"{vcp['rating']}: rompió el pivote {fmt(vcp['pivot'])} con volumen "
                               f"({vcp['contractions']} contracciones, tendencia {vcp['trend_score']:.0f}/100)"}
            elif burst.get("ok") and burst["state"] in cfg["setups"]["burst_entry_states"]:
                cand = {"setup": "burst", "score": burst["score"], "entry": closed[0]["close"], "stop": burst.get("stop"),
                        "why": f"Momentum Burst {burst['rating']}: +{fmt(burst['day_gain_pct'], 1)} % en el día, "
                               f"volumen {fmt(burst['volume_ratio_20d'], 1)}x el promedio ({', '.join(TAGS.get(t, t) for t in burst['tags'])})"}
            blocked = None
            entered_today = False
            if cand and not pos and not exited_now and state.get("last_exit", {}).get(key) != closed[0]["date"] \
                    and state.get("last_entry_bar", {}).get(key) != closed[0]["date"]:
                # Pipeline de day-trader-skill: plan -> Time Machine -> tamaño -> riesgo (PASS/BLOCK)
                ev_p = pipeline.evaluate_swing(cfg=cfg, ctx=ctx, kind=kind, ident=ident, label=label, closed=closed,
                                               cand=cand, vcp=vcp, gate=gate, gate_why=regime_out[kind]["why"],
                                               state=state, now=now, news_by_ticker=news_by_ticker,
                                               news_ticker=news_t, sc=sc)
                pipe = {"setup": cand["setup"], "verdict": ev_p["verdict"], "stages": ev_p["stages"]}
                if ev_p["verdict"] != "PASS":
                    blocked = "; ".join(ev_p["reasons"]) or "bloqueada"
                else:
                    plan, sz = ev_p["plan"], ev_p["size"]
                    tm = ev_p.get("tm") or {}
                    entered_today = True
                    pos = {"kind": kind, "id": ident, "label": label, "setup": cand["setup"],
                           "entry": rnd(plan["entry"]), "stop": rnd(plan["stop"], plan["entry"]),
                           "initial_stop": rnd(plan["stop"], plan["entry"]),
                           "target": rnd(plan["target"], plan["entry"]), "rr": plan["rr"],
                           "stop_pct": plan["stop_pct"], "units": sz["units"], "notional": sz["notional"],
                           "max_loss": sz["max_loss"], "limited_by": sz["bound_by"],
                           "score": cand["score"], "why": cand["why"], "bar_date": closed[0]["date"],
                           "gate": gate,
                           "timemachine": {"lean": tm.get("lean"), "verdict": tm.get("verdict")} if tm.get("ok") else None,
                           "opened_at": now.isoformat(), "last": rnd(last["close"]), "r_now": 0.0, "sessions": 0}
                    state["positions"][key] = pos
                    state.setdefault("last_entry_bar", {})[key] = closed[0]["date"]
                    pipeline.log_entry(state, kind, label, dtskills.ny_date(now), sz["max_loss"], "swing")
                    new_events.append({"type": "entry", "kind": kind, "id": ident, "label": label,
                                       "setup": cand["setup"], "score": cand["score"], "price": pos["entry"],
                                       "stop": pos["stop"], "target": pos["target"], "rr": pos["rr"],
                                       "stop_pct": pos["stop_pct"], "units": pos["units"],
                                       "notional": pos["notional"], "max_loss": pos["max_loss"],
                                       "tm": pos["timemachine"], "why": cand["why"], "bar_date": closed[0]["date"],
                                       "id_ev": f"{key}:entry:{closed[0]['date']}:{cand['setup']}"})

            # ---- preparadas (todavía no dispararon)
            if not pos:
                if vcp.get("ok") and vcp["state"] == "Pre-breakout" and vcp["rating"] in cfg["setups"]["vcp_watch_rating"] \
                        and vcp.get("distance_pct") is not None and vcp["distance_pct"] >= -8:
                    watch.append({"kind": kind, "id": ident, "label": label, "setup": "vcp", "score": vcp["score"],
                                  "rating": vcp["rating"], "trigger": vcp["pivot"], "stop": vcp.get("stop"),
                                  "distance_pct": vcp["distance_pct"],
                                  "why": f"{vcp['rating']} a {fmt(abs(vcp['distance_pct']), 1)} % del pivote. Entrada si cierra arriba de {fmt(vcp['pivot'])} con volumen."})
                elif burst.get("ok") and burst["state"] in cfg["setups"]["burst_watch_states"]:
                    watch.append({"kind": kind, "id": ident, "label": label, "setup": "burst", "score": burst["score"],
                                  "rating": burst["rating"], "trigger": burst.get("entry"), "stop": burst.get("stop"),
                                  "why": f"Momentum Burst {burst['rating']} ({burst['state'].replace('_', ' ').lower()}): revisar gráfico."})
                if blocked and cand:
                    watch.append({"kind": kind, "id": ident, "label": label, "setup": cand["setup"], "score": cand["score"],
                                  "trigger": cand["entry"], "stop": cand.get("stop"), "blocked": blocked,
                                  "why": cand["why"] + f". No se abre: {blocked}."})
                if pipe is None and watch and watch[-1]["kind"] == kind and watch[-1]["id"] == ident and n_watch_pipes < 15:
                    w = watch[-1]
                    n_watch_pipes += 1
                    try:
                        pipe = {"setup": w["setup"], "verdict": "WATCH",
                                "stages": pipeline.watch_pipeline(cfg=cfg, kind=kind, label=label, closed=closed,
                                                                  trigger=w.get("trigger"), stop=w.get("stop"),
                                                                  setup=w["setup"], news_by_ticker=news_by_ticker,
                                                                  news_ticker=news_t, sc=sc, vcp=vcp, now=now)}
                    except Exception as exc:
                        errors[f"pipeline:{key}"] = str(exc)[:200]
            if pipe is None:
                pipe = {"setup": None, "verdict": None,
                        "stages": [pipeline.scanner_stage(sc), pipeline.news_stage(news_by_ticker, news_t)]}

            if pos or entered_today or (vcp.get("ok") and vcp["rating"] in cfg["setups"]["vcp_min_rating"]) \
                    or (burst.get("ok") and burst["state"] in cfg["setups"]["burst_entry_states"]):
                lab = "COMPRA"
            elif stage4 or exited_now:
                lab = "VENTA"
            else:
                lab = "NEUTRAL"
            # ---- patrones clásicos (doble piso, HCH, triángulos): entrada condicional
            try:
                pats = patterns.scan(closed)
            except Exception as exc:  # noqa: BLE001
                pats, errors[f"{key}:patrones"] = [], str(exc)[:200]
            for pt in pats:
                if pt["state"] == "ruptura":
                    icon = "📐" if pt["bias"] == "alcista" else "⚠️"
                    verb = (f"rompió el gatillo {fmt(pt['trigger'])}. Stop {fmt(pt['stop'])} · objetivo {fmt(pt['target'])}"
                            + (f" · R:R {fmt(pt['rr'], 1)}" if pt.get("rr") else "")) if pt["bias"] == "alcista" else \
                        f"perdió {fmt(pt['trigger'])}: aviso para el que está comprado"
                    new_events.append({"type": "info", "info": "pattern", "kind": kind, "id": ident, "label": label,
                                       "why": pt["why"], "id_ev": f"{key}:pattern:{pt['type']}:{pt['points'][0][0]}",
                                       "text_override": f"{icon} <b>PATRÓN · {esc(label)}</b> ({MK_NAME.get(kind, kind)})\n"
                                                        f"{pt['name']} {verb}.\n<i>{esc(pt['why'])}</i>"
                                                        + ("\nMercado defensivo: el motor no abre compras nuevas." if gate == "restrictive" and pt["bias"] == "alcista" else "")})
            assets[key] = {
                "kind": kind, "id": ident, "label": label, "date": closed[0]["date"], "live_date": live["date"] if live else None,
                "last": rnd(last["close"]), "source": src, "degraded": degraded,
                "score": round(score), "signal": lab, "stage4": stage4,
                "sma50": rnd(s50, c[0]), "sma200": rnd(s200, c[0]),
                "vcp": {k: vcp.get(k) for k in ("rating", "score", "state", "pattern", "pivot", "stop", "distance_pct",
                                                 "contractions", "trend_score", "rs_score", "weakest", "error")} if vcp else None,
                "burst": {k: burst.get(k) for k in ("state", "rating", "score", "trigger", "tags", "day_gain_pct",
                                                     "volume_ratio_20d", "error")} if burst else None,
                "scanner": {k: sc.get(k) for k in ("tags", "labels", "rsi")},
                "news_ticker": news_t,
                "pipeline": pipe,
                "position": pos,
                "patterns": pats,
            }

    # ---- intradía: ruptura del rango de apertura en ADRs (mientras opera NY)
    prev_signals = load_json(DATA / "signals.json", {})
    intraday = prev_signals.get("intraday")
    if intraday_fetch is not None or (offline is None and pipeline.ny_session_open(now)) \
            or any(p.get("mode") == "intradia" for p in state["positions"].values()):
        if offline is None or intraday_fetch is not None:
            intraday = pipeline.run_intraday(cfg, ctx, state, now, (regime_out.get("adr", {}).get("gate", "neutral"),
                                             regime_out.get("adr", {}).get("why", "")), new_events, errors,
                                             fetch=intraday_fetch)

    # ---- programados: balances (trader-analyst), congresistas (trader-pelosi), resumen semanal
    analyst = congress = None
    if offline is None or extras.get("analyst"):
        analyst = pipeline.run_analyst(cfg, DATA, now, errors, new_events, offline=extras.get("analyst"))
    if offline is None or extras.get("congress_index"):
        congress = pipeline.run_congress(cfg, DATA, now, errors, new_events, offline_index=extras.get("congress_index"),
                                         offline_texts=extras.get("congress_texts"))
    weekly = pipeline.run_weekly(cfg, state, now, new_events)

    events, fresh = finish_events(state, events_log, new_events, now)

    watch.sort(key=lambda w: -w["score"])
    signals = {
        "generated_at": now.isoformat(),
        "engine": {"version": VERSION, "skills": SKILLS_USED},
        "risk": {k: risk[k] for k in ("min_reward_risk", "target_r", "max_stop_pct", "max_open_positions")},
        "markets": regime_out,
        "us_market": ctx["us_market"],
        "assets": assets,
        "positions": list(state["positions"].values()),
        "watch": watch[:30],
        "intraday": intraday,
        "news": news_items[:60],
        "weekly": weekly,
        "scheduled": {"analyst": bool(analyst and analyst.get("companies")),
                      "analyst_note": (analyst or {}).get("note"),
                      "congress": bool(congress)},
        "events": events[:50],
        "patterns": sorted(({"key": k, "kind": a["kind"], "id": a["id"], "label": a["label"], **p}
                            for k, a in assets.items() for p in a.get("patterns") or []),
                           key=lambda p: (p["state"] != "ruptura", p["bias"] != "alcista", p["label"])),
        "closed": state.get("closed", [])[:30],
        "review": review.build_review(state.get("closed", []), cfg),
        "errors": errors,
    }
    return signals, {"events": events}, state, fresh


SKILLS_USED = [
    "tradermonty/vcp-screener", "tradermonty/stockbee-momentum-burst-screener", "tradermonty/crypto-regime-analyzer",
    "tradermonty/breakout-trade-planner", "tradermonty/market-breadth-analyzer", "tradermonty/uptrend-analyzer",
    "tradermonty/sector-analyst", "tradermonty/exposure-coach", "tradermonty/drawdown-circuit-breaker",
    "tradermonty/trade-performance-coach",
    "day-trader-skill/trader-scanner", "day-trader-skill/trader-news", "day-trader-skill/trader-entry",
    "day-trader-skill/trader-exit", "day-trader-skill/trader-timemachine", "day-trader-skill/trader-budget",
    "day-trader-skill/trader-risk", "day-trader-skill/trader-alerts (reglas)", "day-trader-skill/trader-analyst",
    "day-trader-skill/trader-pelosi",
]


def event_text(ev):
    if ev.get("text_override"):
        return ev.pop("text_override")
    return tg_entry(ev) if ev["type"] == "entry" else tg_exit(ev) if ev["type"] == "exit" else tg_update(ev)


def finish_events(state, events_log, new_events, now):
    """trader-alerts: cada aviso suena una sola vez (id estable)."""
    rung = state.setdefault("rung", {})
    fresh = []
    for ev in new_events:
        if ev["id_ev"] in rung:
            ev.pop("text_override", None)
            continue
        rung[ev["id_ev"]] = now.isoformat()
        ev["id"] = ev.pop("id_ev")
        ev["ts"] = now.isoformat()
        ev["text"] = event_text(ev)
        fresh.append(ev)
    # limpiar avisos viejos (90 días)
    cutoff = now.timestamp() - 90 * 86400
    state["rung"] = {k: v for k, v in rung.items() if datetime.fromisoformat(v).timestamp() > cutoff}
    # el id del evento es el identificador estable; el del activo va en "asset"
    for ev in fresh:
        ev["asset"] = f"{ev['kind']}:{ev['label']}"
    events = (fresh[::-1] + events_log.get("events", []))[:MAX_EVENTS]
    return events, fresh


def analyze_intraday(cfg, now=None, fetch=None):
    """Corrida corta cada 15 minutos en la rueda de NY: sólo el ORB de los ADRs y sus alertas."""
    now = now or datetime.now(timezone.utc)
    state = load_json(DATA / "state.json", {"positions": {}, "rung": {}, "closed": []})
    events_log = load_json(DATA / "events.json", {"events": []})
    signals = load_json(DATA / "signals.json", {})
    errors, new_events = {}, []
    ctx = {"us_market": signals.get("us_market"), "breakers": {}, "errors": {}}
    cbc = pipeline.section(cfg, "circuit_breaker")
    try:
        ctx["breakers"]["adr"] = tmextra.circuit_breaker([t for t in state.get("closed", []) if t.get("kind") == "adr"],
                                                         cfg["risk"]["adr"]["budget"], now, cbc)
    except Exception as exc:
        errors["circuit_breaker:adr"] = str(exc)[:200]
    adr = (signals.get("markets") or {}).get("adr") or {"gate": "neutral", "why": "sin corrida completa todavía"}
    intraday = pipeline.run_intraday(cfg, ctx, state, now, (adr["gate"], adr["why"]), new_events, errors, fetch=fetch)
    events, fresh = finish_events(state, events_log, new_events, now)
    signals["intraday"] = intraday
    signals["intraday_at"] = now.isoformat()
    signals["positions"] = list(state["positions"].values())
    signals["events"] = events[:50]
    signals["closed"] = state.get("closed", [])[:30]
    signals.setdefault("errors", {}).update(errors)
    return signals, {"events": events}, state, fresh


def main(argv=None):
    ap = argparse.ArgumentParser(description="Motor de señales de TraderPanel")
    ap.add_argument("--offline", help="carpeta con velas guardadas")
    ap.add_argument("--no-crypto-regime", action="store_true")
    ap.add_argument("--config", default=str(CONFIG))
    ap.add_argument("--intradia", action="store_true",
                    help="sólo el ORB de los ADRs y sus alertas (corrida cada 15 minutos en la rueda de NY)")
    a = ap.parse_args(argv)
    cfg = json.loads(Path(a.config).read_text())
    problems = config_check.validate_engine(cfg)
    if problems:
        raise SystemExit("config.json inválido:\n  " + "\n  ".join(problems))
    if a.intradia:
        now = datetime.now(timezone.utc)
        has_open = any(p.get("mode") == "intradia" for p in load_json(DATA / "state.json", {}).get("positions", {}).values())
        if not pipeline.ny_session_open(now) and not has_open:
            print("NY cerrado y sin posiciones intradía: nada que hacer")
            return 0
        signals, events, state, fresh = analyze_intraday(cfg, now)
        save_json(DATA / "signals.json", signals)
        save_json(DATA / "events.json", events)
        save_json(DATA / "state.json", state)
        syms = (signals.get("intraday") or {}).get("symbols") or {}
        print(f"intradía: {len(syms)} ADRs revisados · {len(fresh)} avisos nuevos")
        for ev in fresh:
            print("  ", ev["id"])
        return 0
    signals, events, state, fresh = analyze_all(cfg, Path(a.offline) if a.offline else None,
                                                use_regime=not a.no_crypto_regime)
    save_json(DATA / "signals.json", signals)
    save_json(DATA / "events.json", events)
    save_json(DATA / "state.json", state)
    n = len(signals["assets"])
    print(f"{n} activos analizados · {len(signals['positions'])} posiciones abiertas · "
          f"{len(signals['watch'])} preparadas · {len(fresh)} avisos nuevos · {len(signals['errors'])} errores")
    for ev in fresh:
        print("  ", ev["id"])
    for k, v in signals["errors"].items():
        print("   error", k, v, file=sys.stderr)
    if n == 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
