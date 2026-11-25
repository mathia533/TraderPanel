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

from . import planner, skills, sources

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CONFIG = Path(__file__).resolve().parent / "config.json"
VERSION = "1.0"
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


def bench_kind(kind):
    return kind


# ------------------------------------------------------------------ textos
SETUP_NAME = {"vcp": "VCP (Minervini)", "burst": "Momentum Burst (Stockbee)"}


def tg_entry(ev):
    c = CUR[ev["kind"]]
    lines = [
        f"🟢 <b>ENTRADA · {ev['label']}</b> ({MKT_LABEL[ev['kind']]})",
        f"Setup: {SETUP_NAME[ev['setup']]} · puntaje {ev['score']:.0f}/100",
        f"Precio: {c} {fmt(ev['price'])}",
        f"Stop: {c} {fmt(ev['stop'], ev['price'])} (−{fmt(ev['stop_pct'], 1)} %)",
        f"Objetivo: {c} {fmt(ev['target'], ev['price'])} · R:R {fmt(ev['rr'], 1)}",
    ]
    if ev.get("units"):
        lines.append(f"Tamaño sugerido: {units_txt(ev['units'])} ({c} {fmt(ev['notional'])}, riesgo {c} {fmt(ev['max_loss'])})")
    lines.append(f"<i>{ev['why']}</i>")
    return "\n".join(lines)


def tg_exit(ev):
    c = CUR[ev["kind"]]
    icon = {"target": "🎯", "stop": "🔴", "trend": "🟠", "time": "⏱"}.get(ev["reason"], "⚪")
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
def analyze_all(cfg, offline=None, use_regime=True, now=None):
    now = now or datetime.now(timezone.utc)
    state = load_json(DATA / "state.json", {"positions": {}, "rung": {}, "closed": []})
    events_log = load_json(DATA / "events.json", {"events": []})
    risk = cfg["risk"]
    new_events, errors, assets, watch = [], {}, {}, []
    regime_out = {}

    # Índices de referencia y filtro de mercado
    bench = {}
    for kind, sym in cfg["benchmarks"].items():
        try:
            if offline:
                b, _ = get_bars(kind, sym, offline)
            else:
                b = sources.fetch_yahoo(sym)
            b, _ = sources.split_partial(kind, b, now)
            bench[kind] = b
        except Exception as exc:
            bench[kind] = []
            errors[f"bench:{sym}"] = str(exc)[:200]
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
        regime_out[kind] = {"gate": gate, "why": why, "benchmark": cfg["benchmarks"][kind]}
        if kind == "cripto" and crypto_regime:
            regime_out[kind]["regime"] = crypto_regime.get("composite")
            regime_out[kind]["components"] = crypto_regime.get("components")

    for kind, ids in cfg["watchlist"].items():
        flt = cfg["filters"][kind]
        gate = regime_out[kind]["gate"]
        for ident in ids:
            key = f"{kind}:{ident}"
            try:
                bars, src = get_bars(kind, ident, offline)
            except Exception as exc:
                errors[key] = str(exc)[:200]
                continue
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
                    ev = {"type": "exit", "reason": ex["reason"], "kind": kind, "id": ident, "label": label,
                          "setup": pos["setup"], "price": rnd(ex["price"]), "entry": pos["entry"],
                          "why": ex["why"], "pnl_pct": round(pnl, 2), "r": round(r_mult, 2),
                          "bar_date": last_bar["date"], "opened_at": pos["opened_at"]}
                    ev["id_ev"] = f"{key}:exit:{pos['bar_date']}:{ex['reason']}"
                    new_events.append(ev)
                    state["closed"] = ([{**pos, "exit": ev["price"], "exit_reason": ex["reason"], "pnl_pct": ev["pnl_pct"],
                                         "r": ev["r"], "closed_at": now.isoformat()}] + state.get("closed", []))[:100]
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
                               f"volumen {fmt(burst['volume_ratio_20d'], 1)}x el promedio ({', '.join(burst['tags'])})"}
            blocked = None
            entered_today = False
            if cand and not pos and not exited_now and state.get("last_exit", {}).get(key) != closed[0]["date"] \
                    and state.get("last_entry_bar", {}).get(key) != closed[0]["date"]:
                plan = planner.plan_trade(cand["entry"], cand["stop"], risk)
                if not plan["ok"]:
                    blocked = plan["why"]
                elif gate == "restrictive":
                    blocked = "mercado en modo defensivo (" + regime_out[kind]["why"] + ")"
                elif len(state["positions"]) >= risk["max_open_positions"]:
                    blocked = f"ya hay {risk['max_open_positions']} posiciones abiertas"
                else:
                    sz = planner.size_position(plan, risk[kind])
                    if not sz["ok"]:
                        blocked = sz["why"]
                    else:
                        entered_today = True
                        pos = {"kind": kind, "id": ident, "label": label, "setup": cand["setup"],
                               "entry": rnd(plan["entry"]), "stop": rnd(plan["stop"], plan["entry"]),
                               "initial_stop": rnd(plan["stop"], plan["entry"]),
                               "target": rnd(plan["target"], plan["entry"]), "rr": plan["rr"],
                               "stop_pct": plan["stop_pct"], "units": sz["units"], "notional": sz["notional"],
                               "max_loss": sz["max_loss"], "limited_by": sz["limited_by"],
                               "score": cand["score"], "why": cand["why"], "bar_date": closed[0]["date"],
                               "opened_at": now.isoformat(), "last": rnd(last["close"]), "r_now": 0.0, "sessions": 0}
                        state["positions"][key] = pos
                        state.setdefault("last_entry_bar", {})[key] = closed[0]["date"]
                        new_events.append({"type": "entry", "kind": kind, "id": ident, "label": label,
                                           "setup": cand["setup"], "score": cand["score"], "price": pos["entry"],
                                           "stop": pos["stop"], "target": pos["target"], "rr": pos["rr"],
                                           "stop_pct": pos["stop_pct"], "units": pos["units"],
                                           "notional": pos["notional"], "max_loss": pos["max_loss"],
                                           "why": cand["why"], "bar_date": closed[0]["date"],
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

            if pos or entered_today or (vcp.get("ok") and vcp["rating"] in cfg["setups"]["vcp_min_rating"]) \
                    or (burst.get("ok") and burst["state"] in cfg["setups"]["burst_entry_states"]):
                lab = "COMPRA"
            elif stage4 or exited_now:
                lab = "VENTA"
            else:
                lab = "NEUTRAL"
            assets[key] = {
                "kind": kind, "id": ident, "label": label, "date": closed[0]["date"], "live_date": live["date"] if live else None,
                "last": rnd(last["close"]), "source": src, "degraded": degraded,
                "score": round(score), "signal": lab, "stage4": stage4,
                "sma50": rnd(s50, c[0]), "sma200": rnd(s200, c[0]),
                "vcp": {k: vcp.get(k) for k in ("rating", "score", "state", "pattern", "pivot", "stop", "distance_pct",
                                                 "contractions", "trend_score", "rs_score", "weakest", "error")} if vcp else None,
                "burst": {k: burst.get(k) for k in ("state", "rating", "score", "trigger", "tags", "day_gain_pct",
                                                     "volume_ratio_20d", "error")} if burst else None,
                "position": pos,
            }
            if not offline:
                time.sleep(0.4)  # no saturar Yahoo

    # ---- avisos una sola vez (trader-alerts)
    rung = state.setdefault("rung", {})
    fresh = []
    for ev in new_events:
        if ev["id_ev"] in rung:
            continue
        rung[ev["id_ev"]] = now.isoformat()
        ev["id"] = ev.pop("id_ev")
        ev["ts"] = now.isoformat()
        ev["text"] = tg_entry(ev) if ev["type"] == "entry" else tg_exit(ev) if ev["type"] == "exit" else tg_update(ev)
        fresh.append(ev)
    # limpiar avisos viejos (90 días)
    cutoff = now.timestamp() - 90 * 86400
    state["rung"] = {k: v for k, v in rung.items() if datetime.fromisoformat(v).timestamp() > cutoff}
    # el id del evento es el identificador estable; el del activo va en "asset"
    for ev in fresh:
        ev["asset"] = f"{ev['kind']}:{ev['label']}"
    events = (fresh[::-1] + events_log.get("events", []))[:MAX_EVENTS]

    watch.sort(key=lambda w: -w["score"])
    signals = {
        "generated_at": now.isoformat(),
        "engine": {"version": VERSION, "skills": [
            "tradermonty/vcp-screener", "tradermonty/stockbee-momentum-burst-screener",
            "tradermonty/crypto-regime-analyzer",
            "day-trader-skill: trader-exit, trader-budget, trader-risk, trader-alerts (reglas)"]},
        "risk": {k: risk[k] for k in ("min_reward_risk", "target_r", "max_stop_pct", "max_open_positions")},
        "markets": regime_out,
        "assets": assets,
        "positions": list(state["positions"].values()),
        "watch": watch[:30],
        "events": events[:50],
        "closed": state.get("closed", [])[:30],
        "errors": errors,
    }
    return signals, {"events": events}, state, fresh


def main(argv=None):
    ap = argparse.ArgumentParser(description="Motor de señales de TraderPanel")
    ap.add_argument("--offline", help="carpeta con velas guardadas")
    ap.add_argument("--no-crypto-regime", action="store_true")
    ap.add_argument("--config", default=str(CONFIG))
    a = ap.parse_args(argv)
    cfg = json.loads(Path(a.config).read_text())
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
