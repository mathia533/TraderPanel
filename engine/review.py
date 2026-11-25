"""Revisión de resultados del motor: cómo le fue a cada señal ya cerrada, neto de costos.

Agrupa las operaciones cerradas (state["closed"]) por mercado, por setup y por el
régimen de mercado que había al entrar (favorable / neutral / defensivo), y arma la
curva de resultados acumulados en R. Los costos son ida y vuelta, en % del monto
(comisiones + derechos + spread); se configuran en engine/config.json → "costs".
"""
from __future__ import annotations

DEFAULT_COSTS = {"adr": 0.3, "byma": 1.2, "cripto": 0.4}
GATE_ES = {"allowed": "favorable", "neutral": "neutral", "restrictive": "defensivo", None: "sin dato"}


def costs_of(cfg: dict) -> dict:
    c = {**DEFAULT_COSTS, **((cfg or {}).get("costs") or {})}
    return {k: float(v) for k, v in c.items() if isinstance(v, (int, float)) and 0 <= v <= 20}


def net_trade(t: dict, costs: dict) -> dict | None:
    """R y % de una operación cerrada, antes y después de costos."""
    try:
        entry, stop0, exit_ = float(t["entry"]), float(t.get("initial_stop") or t["stop"]), float(t["exit"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (entry > 0 and entry > stop0 > 0 and exit_ > 0):
        return None
    risk_pct = (entry - stop0) / entry * 100
    pnl = (exit_ / entry - 1) * 100
    cost = costs.get(t.get("kind"), 0.0)
    return {"r": (exit_ - entry) / (entry - stop0), "r_net": (pnl - cost) / risk_pct, "pnl_pct": pnl,
            "pnl_net": pnl - cost, "cost_pct": cost}


def _stats(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0}
    rs = [x["r_net"] for x in rows]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    eq, peak, dd = 0.0, 0.0, 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {
        "n": n,
        "win_rate": round(100 * len(wins) / n, 1),
        "avg_r": round(sum(x["r"] for x in rows) / n, 2),
        "avg_r_net": round(sum(rs) / n, 2),
        "total_r_net": round(sum(rs), 2),
        "avg_win_r": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss_r": round(sum(losses) / len(losses), 2) if losses else None,
        "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
        "max_dd_r": round(dd, 2),
        "avg_pnl_net": round(sum(x["pnl_net"] for x in rows) / n, 2),
    }


def build_review(closed: list[dict], cfg: dict | None = None) -> dict:
    costs = costs_of(cfg or {})
    rows = []
    for t in closed or []:
        nt = net_trade(t, costs)
        if nt:
            rows.append({**nt, "kind": t.get("kind"), "setup": t.get("setup"), "label": t.get("label"),
                         "gate": t.get("gate"), "closed_at": t.get("closed_at"), "reason": t.get("exit_reason")})
    rows.sort(key=lambda x: str(x["closed_at"] or ""))  # más viejo primero, para la curva

    def group(field, names=None):
        out = {}
        for x in rows:
            out.setdefault(x[field], []).append(x)
        return [{"key": k if k is not None else "none", "name": (names or {}).get(k, k or "sin dato"), **_stats(v)}
                for k, v in sorted(out.items(), key=lambda kv: -len(kv[1]))]

    eq, curve = 0.0, []
    for x in rows:
        eq += x["r_net"]
        curve.append({"t": str(x["closed_at"] or "")[:10], "r": round(x["r_net"], 2), "eq": round(eq, 2),
                      "label": x["label"], "kind": x["kind"]})
    return {
        "costs": costs,
        "all": _stats(rows),
        "by_kind": group("kind"),
        "by_setup": group("setup"),
        "by_gate": group("gate", GATE_ES),
        "curve": curve[-100:],
    }
