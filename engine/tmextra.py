"""Más skills de tradermonty/claude-trading-skills (vendorizadas sin cambios).

- market-breadth-analyzer, uptrend-analyzer, sector-analyst: salud del mercado de
  EE. UU. con los CSV públicos de TraderMonty (sin claves). Corren una vez por día.
- exposure-coach: junta esas tres y da el techo de exposición (0-100 %) y la
  recomendación (CASH_PRIORITY, REDUCE_ONLY, NEW_ENTRY_ALLOWED). El motor lo usa
  como filtro para ADRs, junto con la tendencia del SPY.
- breakout-trade-planner: convierte cada VCP en un plan Minervini (entrada señal
  y peor entrada, stop debajo de la última contracción, objetivo 2R) y decide si
  es operable, para vigilar o descartado.
- drawdown-circuit-breaker: con las operaciones cerradas por el motor decide
  TRADING_ALLOWED, COOLDOWN (racha de pérdidas) o HALTED (pérdida diaria,
  semanal o mensual). Con COOLDOWN o HALTED no se abren posiciones nuevas.
- trade-performance-coach: revisión semanal del proceso (veredicto, causa,
  reglas para la semana que viene) para el resumen de Telegram.
"""

from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TM = ROOT / "vendor" / "tradermonty"
_mods: dict = {}


def _load(name: str, rel: str):
    if name not in _mods:
        path = TM / rel
        d = str(path.parent)
        if d not in sys.path:
            sys.path.insert(0, d)
        spec = importlib.util.spec_from_file_location(f"_tp_tm_{name}", path)
        m = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = m
        spec.loader.exec_module(m)
        _mods[name] = m
    return _mods[name]


def _run(script: Path, args: list[str], timeout=240) -> tuple[bool, str]:
    try:
        p = subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True,
                           timeout=timeout, cwd=str(script.parent))
    except subprocess.TimeoutExpired:
        return False, "tiempo agotado"
    return p.returncode == 0, (p.stderr or p.stdout)[-400:]


def _newest(folder: str, pattern: str):
    files = sorted(glob.glob(str(Path(folder) / pattern)))
    if not files:
        return None
    return Path(files[-1])


# ------------------------------------------------------------- mercado de EE. UU.
REC_ES = {"CASH_PRIORITY": "priorizar liquidez", "REDUCE_ONLY": "sólo reducir",
          "NEW_ENTRY_ALLOWED": "entradas nuevas permitidas"}
PHASE_ES = {"early": "inicio de ciclo", "mid": "mitad de ciclo", "late": "fin de ciclo", "recession": "recesión"}


def us_market(data_dir: Path, now: datetime | None = None, force: bool = False) -> dict | None:
    """Corre amplitud, tendencia por sectores, rotación y exposición (una vez por día)."""
    now = now or datetime.now(timezone.utc)
    out_file = data_dir / "us_market.json"
    today = now.date().isoformat()
    prev = None
    if out_file.exists():
        try:
            prev = json.loads(out_file.read_text())
            if not force and prev.get("date") == today and prev.get("exposure"):
                return prev
        except Exception:
            prev = None
    errors = {}
    with tempfile.TemporaryDirectory() as tmp:
        ok, err = _run(TM / "market-breadth-analyzer/scripts/market_breadth_analyzer.py", ["--output-dir", tmp])
        if not ok:
            errors["breadth"] = err
        ok, err = _run(TM / "uptrend-analyzer/scripts/uptrend_analyzer.py", ["--output-dir", tmp])
        if not ok:
            errors["uptrend"] = err
        ok, err = _run(TM / "sector-analyst/scripts/analyze_sector_rotation.py",
                       ["--json", "--save", "--output-dir", tmp])
        if not ok:
            errors["sector"] = err
        breadth = _newest(tmp, "market_breadth_2*.json")
        uptrend = _newest(tmp, "uptrend_analysis_*.json")
        sector = _newest(tmp, "sector_rotation_*.json")
        args = ["--output-dir", tmp, "--json-only"]
        for flag, f in (("--breadth", breadth), ("--uptrend", uptrend), ("--sector", sector)):
            if f:
                args += [flag, str(f)]
        expo = None
        if breadth or uptrend or sector:
            ok, err = _run(TM / "exposure-coach/scripts/calculate_exposure.py", args)
            f = _newest(tmp, "exposure_posture_*.json")
            if ok and f:
                expo = json.loads(f.read_text())
            else:
                errors["exposure"] = err
        res = {"date": today, "generated_at": now.isoformat(), "errors": errors}
        if breadth:
            b = json.loads(breadth.read_text())
            c = b.get("composite") or {}
            res["breadth"] = {"score": c.get("composite_score"), "zone": c.get("zone"),
                              "guidance": c.get("exposure_guidance"), "as_of": (b.get("metadata") or {}).get("data_date")}
        if uptrend:
            u = json.loads(uptrend.read_text())
            c = u.get("composite") or {}
            res["uptrend"] = {"score": c.get("composite_score"), "zone": c.get("zone"),
                              "guidance": c.get("exposure_guidance"),
                              "warnings": [w.get("label") for w in c.get("active_warnings") or []]}
        if sector:
            s = json.loads(sector.read_text())
            cp = s.get("cycle_phase") or {}
            res["sector"] = {"phase": cp.get("phase"), "phase_es": PHASE_ES.get(cp.get("phase"), cp.get("phase")),
                             "confidence": cp.get("confidence"),
                             "leaders": [r["sector"] for r in (s.get("ranking") or [])[:3]],
                             "laggards": [r["sector"] for r in (s.get("ranking") or [])[-3:]],
                             "rows": [{"sector": r["sector"], "pct": r.get("ratio_pct"), "trend": r.get("trend")}
                                      for r in s.get("ranking") or []]}
        if expo:
            res["exposure"] = {"ceiling_pct": expo.get("exposure_ceiling_pct"), "recommendation": expo.get("recommendation"),
                               "recommendation_es": REC_ES.get(expo.get("recommendation"), expo.get("recommendation")),
                               "bias": expo.get("bias"), "participation": expo.get("participation"),
                               "confidence": expo.get("confidence"), "inputs": expo.get("inputs_provided"),
                               "rationale": expo.get("rationale")}
    if not res.get("exposure") and prev and prev.get("exposure"):
        prev["errors"] = errors
        prev["stale"] = True
        res = prev
    data_dir.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(res, indent=1, ensure_ascii=False))
    return res


def exposure_gate(usm: dict | None) -> tuple[str | None, str]:
    """allowed / neutral / restrictive según exposure-coach (None si no hay dato)."""
    ex = (usm or {}).get("exposure") or {}
    if not ex:
        return None, "sin dato de exposición de EE. UU."
    ceil = ex.get("ceiling_pct") or 0
    rec = ex.get("recommendation")
    why = f"exposure-coach: techo {ceil:.0f} %, {REC_ES.get(rec, rec)}"
    if rec == "CASH_PRIORITY" or ceil < 30:
        return "restrictive", why
    if rec == "NEW_ENTRY_ALLOWED" and ceil >= 60:
        return "allowed", why
    return "neutral", why


# ------------------------------------------------------------- breakout-trade-planner
def breakout_plan(label: str, vcp: dict, price: float, *, account: float, risk_pct: float,
                  max_position_pct: float, target_r: float, now: datetime) -> dict | None:
    """process_candidate() del planner con el VCP de un activo."""
    if not vcp or not vcp.get("ok") or vcp.get("pivot") is None or vcp.get("last_low") is None:
        return None
    bp = _load("planner", "breakout-trade-planner/scripts/plan_breakout_trades.py")
    result = {"symbol": label, "price": price, "composite_score": vcp["score"], "execution_state": vcp["state"],
              "valid_vcp": vcp.get("valid_vcp", False), "sector": "N/A",
              "vcp_pattern": {"pivot_price": vcp["pivot"], "contractions": [{"low_price": vcp["last_low"]}]},
              "volume_pattern": {"breakout_volume_detected": vcp.get("breakout_volume", False)},
              "pivot_proximity": {"distance_from_pivot_pct": vcp.get("distance_pct")}}
    args = argparse.Namespace(account_size=account, risk_pct=risk_pct, max_position_pct=max_position_pct,
                              max_sector_pct=100.0, max_portfolio_heat_pct=100.0, target_r_multiple=target_r,
                              stop_buffer_pct=1.0, max_chase_pct=2.0, pivot_buffer_pct=0.1)
    try:
        r = bp.process_candidate(result, args, 0.0, {}, {}, as_of=now)
    except Exception as exc:  # el planner valida y puede rechazar precios raros
        return {"classification": "rejected", "reason": str(exc)[:200]}
    d = r.get("data") or {}
    signal, worst, stop = None, None, None
    try:
        signal, worst, stop = bp.derive_trade_prices(vcp["pivot"], vcp["last_low"], pivot_buffer_pct=0.1,
                                                     max_chase_pct=2.0, stop_buffer_pct=1.0)
    except ValueError:
        pass
    tp = d.get("trade_plan") or {}
    return {"classification": r["classification"], "code": d.get("decision_code"),
            "reason": d.get("reason") or d.get("decision_reason"),
            "rating_band": d.get("rating_band"), "signal_entry": signal, "worst_entry": worst, "stop": stop,
            "target": tp.get("target_price") or (round(worst + target_r * (worst - stop), 4) if worst and stop else None),
            "risk_pct_worst": d.get("risk_pct_worst") or tp.get("risk_pct_worst")}


CLASS_ES = {"actionable": "operable (orden de compra sobre el pivote)", "revalidation": "operable: ya rompió, revalidar precio",
            "watchlist": "para vigilar", "rejected": "descartado", "constrained": "sin tamaño", "deferred": "postergado"}


# ------------------------------------------------------------- drawdown-circuit-breaker
def circuit_breaker(closed: list[dict], account: float, now: datetime, cfg: dict | None = None) -> dict:
    """Decide si se pueden abrir posiciones nuevas según las pérdidas realizadas."""
    cb = _load("circuit", "drawdown-circuit-breaker/scripts/check_circuit_breaker.py")
    theses = []
    for i, t in enumerate(closed):
        pnl = t.get("pnl_money")
        if pnl is None or not t.get("closed_at"):
            continue
        theses.append({"thesis_id": f"tp-{i}", "ticker": t.get("label", ""), "status": "CLOSED",
                       "outcome": {"pnl_dollars": pnl},
                       "status_history": [{"status": "ACTIVE", "at": t.get("opened_at") or t["closed_at"]},
                                          {"status": "CLOSED", "at": t["closed_at"], "realized_pnl": pnl}]})
    c = cfg or {}
    config = cb.CircuitConfig(max_daily_loss_pct=c.get("max_daily_loss_pct", 2.0),
                              losing_streak_n=c.get("losing_streak_n", 3),
                              cooldown_hours=c.get("cooldown_hours", 24.0),
                              weekly_drawdown_pct=c.get("weekly_drawdown_pct", 5.0),
                              monthly_drawdown_pct=c.get("monthly_drawdown_pct", 8.0))
    r = cb.evaluate_circuit_breaker(theses, account, now, config)
    return {"recommendation": r["recommendation"],
            "rules": [{"rule": x["rule"], "until": x.get("active_until"), "detail": x.get("detail")}
                      for x in r["triggered_rules"]],
            "metrics": r["metrics"]}


CB_ES = {"TRADING_ALLOWED": "operar permitido", "COOLDOWN": "pausa por racha de pérdidas", "HALTED": "frenado por pérdidas"}
CB_RULE_ES = {"max_daily_loss": "pérdida diaria", "losing_streak_cooldown": "racha de pérdidas",
              "weekly_drawdown_halt": "pérdida semanal", "monthly_drawdown_halt": "pérdida mensual",
              "incomplete_state_data": "datos incompletos"}


# ------------------------------------------------------------- trade-performance-coach
VERDICT_ES = {"OK": "proceso en orden", "REVIEW_REQUIRED": "revisar el proceso",
              "RULE_VIOLATION": "se rompió una regla", "COOL_DOWN": "conviene una pausa",
              "insufficient_data": "datos insuficientes"}
RULE_ES = [
    ("Cap risk at 0.5R", "Bajar el riesgo a 0,5R en las próximas dos operaciones."),
    ("record both planned risk_r", "Registrar el riesgo planeado y el real en cada operación."),
    ("No new entry without a pre-entry thesis", "Ninguna entrada sin plan previo, invalidación y confirmación."),
    ("Switch to review-only mode", "Próxima rueda sólo de revisión: sin riesgo nuevo."),
    ("Any stop adjustment must be written", "Todo cambio de stop se define antes de entrar."),
    ("Keep the current process rules", "Mantener las reglas actuales y seguir registrando cada resultado."),
]


def _rule_es(text: str) -> str:
    return next((es for en, es in RULE_ES if text.startswith(en)), text)


def weekly_review(closed: list[dict], now: datetime, max_loss_r: float = 1.0) -> dict:
    """Resumen de la semana (lunes a hoy) con trade-performance-coach build_review()."""
    tpc = _load("coach", "trade-performance-coach/scripts/review_trade_performance.py")
    start = (now - timedelta(days=now.weekday())).date().isoformat()
    week = [t for t in closed if str(t.get("closed_at", ""))[:10] >= start]
    rs = [t.get("r") or 0 for t in week]
    wins = [r for r in rs if r > 0]
    streak = 0
    for t in closed:  # más nuevo primero
        if (t.get("r") or 0) < 0:
            streak += 1
        else:
            break
    worst = max((-r for r in rs), default=0)
    record = {
        "review_type": "weekly", "review_id": f"semana-{start}",
        "outcome": "win" if sum(rs) > 0 else "loss" if sum(rs) < 0 else "flat",
        "planned": {"thesis_recorded_before_entry": True, "setup_confirmed": True, "risk_r": 1.0,
                    "market_regime": "allowed"},
        "actual": {"risk_r": round(worst, 2) if week else 1.0, "stop_moved": any(t.get("breakeven") for t in week),
                   "stop_move_planned": True},
        "risk_plan": {"max_risk_per_trade_r": max_loss_r},
        "monthly": {"consecutive_losses": streak},
        "summary": {"trades": len(week)},
    }
    try:
        rev = tpc.build_review(record, ["engine:state.closed"])
    except Exception as exc:
        rev = {"overall_verdict": "insufficient_data", "error": str(exc)[:200], "next_session_operating_rules": []}
    return {
        "week_start": start, "trades": len(week), "wins": len(wins), "losses": len(rs) - len(wins),
        "win_rate": round(100 * len(wins) / len(rs), 1) if rs else None,
        "total_r": round(sum(rs), 2), "expectancy_r": round(sum(rs) / len(rs), 2) if rs else None,
        "best": max(week, key=lambda t: t.get("r") or 0)["label"] if week else None,
        "worst": min(week, key=lambda t: t.get("r") or 0)["label"] if week else None,
        "losing_streak": streak,
        "verdict": rev.get("overall_verdict"), "verdict_es": VERDICT_ES.get(rev.get("overall_verdict"), rev.get("overall_verdict")),
        "rules": [_rule_es(r.get("rule", "")) for r in rev.get("next_session_operating_rules", [])][:3],
    }
