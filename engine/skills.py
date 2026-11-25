"""Adaptadores a las skills de tradermonty/claude-trading-skills (vendorizadas).

El código de cálculo de cada skill se usa tal cual, sin reescribirlo:

- vcp-screener: Trend Template de Minervini, contracciones de volatilidad (VCP),
  volumen, proximidad al pivote, fuerza relativa, estado de ejecución y puntaje
  compuesto con sus topes.
- stockbee-momentum-burst-screener: ruptura del 4 % / ruptura en dólares con
  expansión de rango y volumen, base previa, filtros de falla y puntaje.
- crypto-regime-analyzer: régimen del mercado cripto (0-100) con CoinGecko y
  funding de Binance; corre como proceso aparte porque comparte nombres de
  módulos (calculators, scorer) con vcp-screener.

Lo que estas skills piden de FMP (universo, cotización) se reemplaza por las
velas que trae engine/sources.py.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor" / "tradermonty"
VCP_DIR = VENDOR / "vcp-screener" / "scripts"
BURST_FILE = VENDOR / "stockbee-momentum-burst-screener" / "scripts" / "screen_momentum_burst.py"
REGIME_DIR = VENDOR / "crypto-regime-analyzer" / "scripts"

if str(VCP_DIR) not in sys.path:
    sys.path.insert(0, str(VCP_DIR))

from calculators.execution_state import compute_execution_state  # noqa: E402
from calculators.pattern_classifier import classify_pattern  # noqa: E402
from calculators.pivot_proximity_calculator import calculate_pivot_proximity  # noqa: E402
from calculators.relative_strength_calculator import calculate_relative_strength  # noqa: E402
from calculators.trend_template_calculator import calculate_trend_template  # noqa: E402
from calculators.vcp_pattern_calculator import calculate_vcp_pattern  # noqa: E402
from calculators.volume_pattern_calculator import calculate_volume_pattern  # noqa: E402
from scorer import calculate_composite_score  # noqa: E402


# ---------------------------------------------------------------- VCP (Minervini)
def run_vcp(bars: list[dict], benchmark: list[dict] | None, *, lookback_days: int = 120,
            breakout_volume_ratio: float = 1.5, max_sma200_extension: float = 50.0) -> dict:
    """Réplica de screen_vcp.analyze_stock para un activo, con velas propias.

    Parámetros por defecto iguales a los del screener (t1_depth_min 10,
    contraction_ratio 0.70, etc.).
    """
    if len(bars) < 60:
        return {"ok": False, "error": "historial corto para VCP"}
    last = bars[0]
    year = bars[:252]
    quote = {
        "price": last["close"],
        "yearHigh": max(b["high"] for b in year),
        "yearLow": min(b["low"] for b in year),
    }
    vcp = calculate_vcp_pattern(bars, lookback_days=lookback_days, atr_multiplier=1.5,
                                min_contraction_days=5, min_contractions=2, t1_depth_min=10.0,
                                contraction_ratio=0.70, wide_and_loose_threshold=15.0)
    if vcp.get("data_available") is False:
        return {"ok": False, "error": vcp.get("error")}
    rs = calculate_relative_strength(bars, benchmark or []) if benchmark else {"score": 50, "rs_rank_estimate": 50}
    if rs.get("data_available") is False or rs.get("error"):
        rs = {"score": 50, "rs_rank_estimate": 50, "note": rs.get("error")}
    tt = calculate_trend_template(bars, quote, rs_rank=rs.get("rs_rank_estimate", 0),
                                  ext_threshold=8.0, max_sma200_extension=max_sma200_extension)
    pivot = vcp.get("pivot_price")
    vol = calculate_volume_pattern(bars[:lookback_days], pivot_price=pivot,
                                   contractions=vcp.get("contractions"),
                                   breakout_volume_ratio=breakout_volume_ratio)
    contractions = vcp.get("contractions") or []
    last_low = contractions[-1].get("low_price") if contractions else None
    piv = calculate_pivot_proximity(current_price=last["close"], pivot_price=pivot,
                                    last_contraction_low=last_low,
                                    breakout_volume=vol.get("breakout_volume_detected", False))
    sma200 = tt.get("sma200")
    sma200_dist = (last["close"] - sma200) / sma200 * 100 if sma200 else None
    ex = compute_execution_state(distance_from_pivot_pct=piv.get("distance_from_pivot_pct"),
                                 price=last["close"], sma50=tt.get("sma50"), sma200=sma200,
                                 sma200_distance_pct=sma200_dist, last_contraction_low=last_low,
                                 breakout_volume=vol.get("breakout_volume_detected", False),
                                 max_sma200_extension=max_sma200_extension)
    state = ex["state"]
    final_depth = contractions[-1].get("depth_pct") if contractions else None
    pattern = classify_pattern(valid_vcp=vcp.get("valid_vcp", False),
                               num_contractions=vcp.get("num_contractions", 0),
                               final_contraction_depth=final_depth, execution_state=state,
                               dry_up_ratio=vol.get("dry_up_ratio"),
                               wide_and_loose=vcp.get("wide_and_loose", False))
    comp = calculate_composite_score(trend_score=tt.get("score", 0), contraction_score=vcp.get("score", 0),
                                     volume_score=vol.get("score", 0), pivot_score=piv.get("score", 0),
                                     rs_score=rs.get("score", 0), valid_vcp=vcp.get("valid_vcp", False),
                                     execution_state=state, pattern_type=pattern,
                                     wide_and_loose=vcp.get("wide_and_loose", False),
                                     sma200_extension_pct=sma200_dist)
    return {
        "ok": True,
        "score": comp["composite_score"],
        "rating": comp["rating"],
        "guidance": comp["guidance"],
        "valid_vcp": vcp.get("valid_vcp", False),
        "contractions": vcp.get("num_contractions", 0),
        "pivot": pivot,
        "stop": piv.get("stop_loss_price"),
        "risk_pct": piv.get("risk_pct"),
        "distance_pct": piv.get("distance_from_pivot_pct"),
        "state": state,
        "pattern": pattern,
        "dry_up": vol.get("dry_up_ratio"),
        "breakout_volume": vol.get("breakout_volume_detected", False),
        "trend_score": tt.get("score"),
        "trend_passed": tt.get("passed"),
        "sma50": tt.get("sma50"),
        "sma150": tt.get("sma150"),
        "sma200": sma200,
        "rs_score": rs.get("score"),
        "weakest": comp["weakest_component"],
    }


# ------------------------------------------------------- Momentum Burst (Stockbee)
_burst_mod = None


def _burst():
    global _burst_mod
    if _burst_mod is None:
        spec = importlib.util.spec_from_file_location("_tp_momentum_burst", BURST_FILE)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        # La skill mide la antigüedad de la vela con el calendario de NYSE. BYMA
        # y cripto tienen otros días hábiles, así que la frescura la controla el
        # motor (sólo se analizan velas cerradas) y acá se toma como al día.
        mod._bar_age = lambda day, expected: 0 if day == expected else max(0, (expected - day).days)
        _burst_mod = mod
    return _burst_mod


def burst_args(market_gate: str, *, min_price: float, min_volume: int, latest: str):
    mod = _burst()
    argv = sys.argv
    try:
        sys.argv = ["screen_momentum_burst.py"]
        args = mod.parse_arguments()
    finally:
        sys.argv = argv
    args.market_gate = market_gate
    args.min_price = min_price
    args.min_volume = min_volume
    args.max_bar_age_sessions = 0
    args._expected_session = date.fromisoformat(latest)
    return args


def run_burst(bars: list[dict], market_gate: str = "neutral", *, min_price: float = 1.0,
              min_volume: int = 20_000, symbol: str = "") -> dict:
    mod = _burst()
    if not bars:
        return {"ok": False, "error": "sin velas"}
    args = burst_args(market_gate, min_price=min_price, min_volume=min_volume, latest=bars[0]["date"])
    nb = mod.normalize_bars(bars, limit=args.history_days)
    r = mod.analyze_symbol(symbol or "X", nb, args)
    return {
        "ok": True,
        "state": r["state"],
        "rating": r["rating"],
        "score": r["setup_score"],
        "raw_score": r.get("raw_setup_score", r["setup_score"]),
        "trigger": r.get("primary_trigger"),
        "tags": r.get("trigger_tags", []),
        "day_gain_pct": r.get("day_gain_pct"),
        "volume_ratio_20d": r.get("volume_ratio_20d"),
        "entry": r.get("entry_reference"),
        "stop": r.get("stop_reference"),
        "risk_pct": r.get("risk_pct_to_stop"),
        "reject": r.get("reject_reasons", []),
        "soft_fail": r.get("soft_failure_tags", []),
        "action": r.get("downstream_action"),
    }


# --------------------------------------------------------- Régimen cripto
def run_crypto_regime(data_dir: Path, *, top_n: int = 20, timeout: int = 900) -> dict | None:
    """Corre crypto_regime_analyzer.py (una vez por día) y devuelve el compuesto.

    Guarda el historial de dominancia de BTC en data/ para que el componente de
    dominancia tenga sus 31 días con el tiempo.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    out_file = data_dir / "crypto_regime.json"
    today = datetime.now(timezone.utc).date().isoformat()
    if out_file.exists():
        try:
            prev = json.loads(out_file.read_text())
            if str(prev.get("metadata", {}).get("as_of", ""))[:10] == today:
                return prev
        except Exception:
            pass
    dom_hist = data_dir / "crypto_dominance_history.json"
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp) / "cache"
        cache.mkdir()
        if dom_hist.exists():
            shutil.copy(dom_hist, cache / "dominance_history.json")
        cmd = [sys.executable, str(REGIME_DIR / "crypto_regime_analyzer.py"), "--output-dir", tmp,
               "--cache-dir", str(cache), "--top-n", str(top_n), "--quiet"]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(REGIME_DIR))
        except subprocess.TimeoutExpired:
            print("crypto-regime: tiempo agotado", file=sys.stderr)
            return _load(out_file)
        if p.returncode != 0:
            print(f"crypto-regime: falló ({p.returncode}) {p.stderr.strip()[-400:]}", file=sys.stderr)
            return _load(out_file)
        if (cache / "dominance_history.json").exists():
            shutil.copy(cache / "dominance_history.json", dom_hist)
        res = json.loads((Path(tmp) / "crypto_regime.json").read_text())
    # Sólo el resumen: el reporte completo es grande y no hace falta en el panel
    slim = {
        "metadata": res.get("metadata", {}),
        "composite": res.get("composite", {}),
        "components": {k: {"score": v.get("score"), "data_available": v.get("data_available")}
                       for k, v in (res.get("components") or {}).items()},
    }
    out_file.write_text(json.dumps(slim, indent=1, ensure_ascii=False))
    return slim


def _load(p: Path):
    try:
        return json.loads(p.read_text())
    except Exception:
        return None
