"""Plan de salida, tamaño de posición y control de riesgo.

Las reglas siguen a jakeschincariol/day-trader-skill, llevadas de intradiario a
velas diarias:

- trader-exit: el stop se fija ANTES de entrar, por debajo de la entrada; el
  objetivo va a 2R; se rechaza un stop por encima de la entrada, un objetivo por
  debajo y una relación beneficio/riesgo menor al mínimo.
- trader-budget: el stop decide el tamaño. Unidades enteras (fracciones en
  cripto) que entran en la pérdida máxima por operación y en el tope de
  posición. Un stop demasiado lejos es un rechazo, no una orden más chica.
- trader-risk: máximo de posiciones abiertas y de entradas nuevas por corrida;
  salir nunca se bloquea.
- trader-alerts: cada aviso suena una sola vez (id estable por evento).

Y a tradermonty para el filtro de mercado (exposure-coach / pre-trade gate):
con el mercado en modo defensivo no se abren posiciones nuevas.
"""

from __future__ import annotations

import math


def sma(closes_new_first: list[float], k: int, offset: int = 0):
    s = closes_new_first[offset:offset + k]
    return sum(s) / k if len(s) == k else None


def market_gate_from_trend(bars: list[dict]) -> tuple[str, str]:
    """allowed / neutral / restrictive según la tendencia del índice de referencia."""
    if not bars or len(bars) < 200:
        return "neutral", "sin historial suficiente del índice"
    c = [b["close"] for b in bars]
    s50, s200 = sma(c, 50), sma(c, 200)
    last = c[0]
    if last > s50 > s200:
        return "allowed", "índice sobre su media de 50 y la de 50 sobre la de 200"
    if last < s200 and s50 < s200:
        return "restrictive", "índice debajo de su media de 200 con la de 50 bajista"
    return "neutral", "índice sin tendencia clara"


def market_gate_from_regime(regime: dict | None, btc_bars: list[dict]) -> tuple[str, str]:
    comp = (regime or {}).get("composite") or {}
    score = comp.get("score")
    if score is None:
        g, why = market_gate_from_trend(btc_bars)
        return g, "régimen cripto no disponible; " + why.replace("índice", "BTC")
    if score >= 60:
        return "allowed", f"régimen cripto {score:.0f}/100 ({comp.get('zone')})"
    if score >= 40:
        return "neutral", f"régimen cripto {score:.0f}/100 ({comp.get('zone')})"
    return "restrictive", f"régimen cripto {score:.0f}/100 ({comp.get('zone')})"


def plan_trade(entry: float, stop: float | None, risk_cfg: dict) -> dict:
    """Valida stop y objetivo como trader-exit. Devuelve {ok, ...} o {ok: False, why}."""
    if stop is None or not math.isfinite(stop):
        return {"ok": False, "why": "sin stop definido"}
    if stop >= entry:
        return {"ok": False, "why": "el stop queda en o sobre la entrada"}
    risk = entry - stop
    stop_pct = risk / entry * 100
    if stop_pct > risk_cfg["max_stop_pct"]:
        return {"ok": False, "why": f"stop demasiado lejos ({stop_pct:.1f} % > {risk_cfg['max_stop_pct']} %)"}
    if stop_pct < 0.3:
        return {"ok": False, "why": "stop demasiado ajustado para el spread"}
    r_mult = risk_cfg["target_r"]
    target = entry + r_mult * risk
    if r_mult < risk_cfg["min_reward_risk"]:
        return {"ok": False, "why": f"R:R {r_mult:.1f} menor al mínimo {risk_cfg['min_reward_risk']}"}
    return {"ok": True, "entry": entry, "stop": stop, "target": target, "rr": r_mult,
            "risk_per_unit": risk, "stop_pct": round(stop_pct, 2)}


def size_position(plan: dict, market_cfg: dict) -> dict:
    """Tamaño como trader-budget: lo decide la pérdida máxima y el tope de posición."""
    budget = market_cfg["budget"]
    max_loss = budget * market_cfg["max_loss_pct"] / 100
    max_pos = budget * market_cfg["max_position_pct"] / 100
    by_loss = max_loss / plan["risk_per_unit"]
    by_cap = max_pos / plan["entry"]
    units = min(by_loss, by_cap)
    limit = "pérdida máxima" if by_loss <= by_cap else "tope de posición"
    if not market_cfg.get("fractional"):
        units = math.floor(units)
    else:
        units = math.floor(units * 1e6) / 1e6
    if units <= 0:
        return {"ok": False, "why": "el presupuesto no alcanza para una unidad con ese stop"}
    return {"ok": True, "units": units, "notional": round(units * plan["entry"], 2),
            "max_loss": round(units * plan["risk_per_unit"], 2), "limited_by": limit}


def check_exit(pos: dict, bar: dict, closed_bars: list[dict], risk_cfg: dict, sessions_held: int) -> dict | None:
    """Mira si una posición abierta tocó stop u objetivo, o si se rompió la idea.

    `bar` es la vela más reciente (puede estar en curso); `closed_bars` son las
    cerradas, de la más nueva a la más vieja.
    """
    stop, target = pos["stop"], pos["target"]
    if bar["low"] <= stop:
        px = min(stop, bar["open"]) if bar["open"] < stop else stop
        return {"reason": "stop", "price": px, "why": "Tocó el stop"}
    if bar["high"] >= target:
        px = max(target, bar["open"]) if bar["open"] > target else target
        return {"reason": "target", "price": px, "why": f"Alcanzó el objetivo ({pos.get('rr', 2):.1f}R)"}
    if closed_bars and bar is closed_bars[0]:
        c = [b["close"] for b in closed_bars]
        if pos["setup"] == "vcp":
            s50 = sma(c, 50)
            if s50 and c[0] < s50:
                return {"reason": "trend", "price": c[0], "why": "Cerró debajo de la media de 50 ruedas"}
        if pos["setup"] == "burst" and sessions_held >= risk_cfg["burst_max_sessions"]:
            return {"reason": "time", "price": c[0],
                    "why": f"Salida por tiempo: {sessions_held} ruedas (Momentum Burst es de 3 a 5 días)"}
    return None


def maybe_breakeven(pos: dict, bar: dict, risk_cfg: dict) -> bool:
    """Sube el stop a la entrada cuando el precio avanzó 1R (como trailing simple)."""
    if pos.get("breakeven"):
        return False
    one_r = pos["entry"] + risk_cfg["breakeven_after_r"] * (pos["entry"] - pos["initial_stop"])
    if bar["high"] >= one_r and pos["stop"] < pos["entry"]:
        pos["stop"] = pos["entry"]
        pos["breakeven"] = True
        return True
    return False
