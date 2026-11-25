import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from synth import make  # noqa: E402

from engine import planner, run, skills, sources  # noqa: E402

RISK = json.loads((Path(run.CONFIG)).read_text())["risk"]


def test_vcp_skill_runs_on_own_bars():
    r = skills.run_vcp(make(vcp=True), make(seed=9, drift=0.0005))
    assert r["ok"] and 0 <= r["score"] <= 100
    assert r["state"] in {"Invalid", "Damaged", "Overextended", "Extended", "Early-post-breakout", "Breakout", "Pre-breakout"}


def test_burst_skill_flags_breakout_and_rejects_downtrend():
    assert skills.run_burst(make(burst_last=True, seed=3))["state"] == "ACTIONABLE_DAY1"
    assert skills.run_burst(make(seed=4, crash=True))["state"] == "REJECTED"


def test_plan_rules_like_trader_exit():
    assert not planner.plan_trade(100, 101, RISK)["ok"]            # stop sobre la entrada
    assert not planner.plan_trade(100, 80, RISK)["ok"]             # stop a 20 % > 12 %
    p = planner.plan_trade(100, 95, RISK)
    assert p["ok"] and p["target"] == 110 and p["rr"] == 2


def test_size_like_trader_budget():
    p = planner.plan_trade(100, 95, RISK)
    s = planner.size_position(p, {"budget": 10000, "max_loss_pct": 1, "max_position_pct": 25})
    assert s["units"] == 20 and s["limited_by"] == "pérdida máxima"   # 100 USD / 5 por acción
    s = planner.size_position(p, {"budget": 10000, "max_loss_pct": 5, "max_position_pct": 10})
    assert s["units"] == 10 and s["limited_by"] == "tope de posición"


def test_split_partial_bar():
    bars = [{"date": "2026-11-24", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}] * 2
    now = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)  # 10:00 en NY
    closed, live = sources.split_partial("adr", bars, now)
    assert live is not None and len(closed) == 1
    closed, live = sources.split_partial("adr", bars, datetime(2026, 11, 24, 22, 0, tzinfo=timezone.utc))
    assert live is None and len(closed) == 2


def _setup(tmp_path, monkeypatch, bars_by_key):
    off = tmp_path / "off"
    off.mkdir()
    for k, b in bars_by_key.items():
        (off / f"{k}.json").write_text(json.dumps(b))
    data = tmp_path / "data"
    monkeypatch.setattr(run, "DATA", data)
    cfg = json.loads(Path(run.CONFIG).read_text())
    cfg["watchlist"] = {"adr": ["TST"], "byma": [], "cripto": []}
    return off, data, cfg


def _next_day(d):
    d = date.fromisoformat(d) + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


def test_entry_then_target_exit_once(tmp_path, monkeypatch):
    bars = make(burst_last=True, seed=3)
    bench = make(seed=99, drift=0.001)
    off, data, cfg = _setup(tmp_path, monkeypatch, {"adr_TST": bars, "adr_SPY": bench, "byma__MERV": bench, "cripto_BTC-USD": bench})
    now = datetime(2027, 1, 1, tzinfo=timezone.utc)

    sig, ev, st, fresh = run.analyze_all(cfg, off, use_regime=False, now=now)
    assert [e["type"] for e in fresh] == ["entry"]
    assert "ENTRADA · TST" in fresh[0]["text"]
    pos = st["positions"]["adr:TST"]
    (data).mkdir(exist_ok=True)
    run.save_json(data / "state.json", st)
    run.save_json(data / "events.json", ev)

    # misma vela otra vez: no repite el aviso ni sale por el stop de la vela de entrada
    sig, ev, st, fresh = run.analyze_all(cfg, off, use_regime=False, now=now)
    assert fresh == [] and "adr:TST" in st["positions"]

    # nueva vela que toca el objetivo
    t = pos["target"]
    nb = {"date": _next_day(bars[0]["date"]), "open": pos["entry"], "high": t * 1.01, "low": pos["entry"] * 0.999, "close": t, "volume": 900000}
    (off / "adr_TST.json").write_text(json.dumps([nb] + bars))
    sig, ev, st, fresh = run.analyze_all(cfg, off, use_regime=False, now=now)
    assert [(e["type"], e.get("reason")) for e in fresh] == [("exit", "target")]
    assert fresh[0]["r"] >= 2 and "adr:TST" not in st["positions"]
    assert sig["closed"][0]["exit_reason"] == "target"


def test_stop_exit(tmp_path, monkeypatch):
    bars = make(burst_last=True, seed=3)
    bench = make(seed=99, drift=0.001)
    off, data, cfg = _setup(tmp_path, monkeypatch, {"adr_TST": bars, "adr_SPY": bench, "byma__MERV": bench, "cripto_BTC-USD": bench})
    now = datetime(2027, 1, 1, tzinfo=timezone.utc)
    _, ev, st, _ = run.analyze_all(cfg, off, use_regime=False, now=now)
    run.save_json(data / "state.json", st)
    pos = st["positions"]["adr:TST"]
    nb = {"date": _next_day(bars[0]["date"]), "open": pos["entry"], "high": pos["entry"], "low": pos["stop"] * 0.98, "close": pos["stop"] * 0.99, "volume": 900000}
    (off / "adr_TST.json").write_text(json.dumps([nb] + bars))
    _, _, st, fresh = run.analyze_all(cfg, off, use_regime=False, now=now)
    assert fresh[0]["reason"] == "stop" and fresh[0]["r"] == -1.0


def test_restrictive_market_blocks_entries(tmp_path, monkeypatch):
    bars = make(burst_last=True, seed=3)
    bear = make(seed=5, drift=-0.003)
    off, data, cfg = _setup(tmp_path, monkeypatch, {"adr_TST": bars, "adr_SPY": bear, "byma__MERV": bear, "cripto_BTC-USD": bear})
    sig, _, st, fresh = run.analyze_all(cfg, off, use_regime=False, now=datetime(2027, 1, 1, tzinfo=timezone.utc))
    assert sig["markets"]["adr"]["gate"] == "restrictive"
    assert fresh == [] and not st["positions"]


def test_candles_file_for_panel(tmp_path, monkeypatch):
    bars = make(burst_last=True, seed=3)
    bench = make(seed=99, drift=0.001)
    off, data, cfg = _setup(tmp_path, monkeypatch, {"adr_TST": bars, "adr_SPY": bench, "byma__MERV": bench, "cripto_BTC-USD": bench})
    sig, *_ = run.analyze_all(cfg, off, use_regime=False, now=datetime(2027, 1, 1, tzinfo=timezone.utc))
    assert "all" in sig["review"] and "curve" in sig["review"]               # revisión de resultados
    c = json.loads((data / "candles.json").read_text())
    it = c["items"]["adr:TST"]
    assert it["ohlc"] and 0 < len(it["bars"]) <= run.CANDLES_KEEP
    d, o, h, l, cl, v = it["bars"][-1]
    assert d == bars[0]["date"][:10] and l <= min(o, cl) <= max(o, cl) <= h
    assert [b[0] for b in it["bars"]] == sorted(b[0] for b in it["bars"])   # de la más vieja a la más nueva
