"""Pipeline de day-trader-skill y skills extra de tradermonty, sin red.

Los datos reales vienen de los fixtures de la propia day-trader-skill: la
operación del video (TSLA, lunes 19 de octubre de 2026), titulares de Yahoo,
el índice de la Cámara y un balance de la SEC.
"""
import gzip
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from synth import make  # noqa: E402

from engine import dtskills, pipeline, run, tmextra  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "dts"
TSLA = gzip.open(FIX / "TSLA-60d5m.json.gz", "rt").read()
CFG = json.loads(Path(run.CONFIG).read_text())


def _cfg(**over):
    cfg = json.loads(json.dumps(CFG))
    cfg["watchlist"] = {"adr": ["TSLA"], "byma": [], "cripto": []}
    cfg["intraday"] = {**cfg.get("intraday", {}), "symbols": ["TSLA"]}
    cfg.update(over)
    return cfg


# ------------------------------------------------------------------ herramientas sueltas
def test_reel_trade_matches_day_trader_skill():
    """El ejemplo del README de day-trader-skill: rango 354,47-362,18, entrada 364,18, stop 358,32, objetivo 370,04."""
    r = dtskills.intraday_orb("TSLA", TSLA, datetime(2026, 10, 19, 14, 20, tzinfo=timezone.utc), {"target_r": 1})
    assert r["status"] == "triggered"
    assert r["opening_range"]["high"] == 362.18 and r["opening_range"]["low"] == 354.47
    assert r["plan"]["entry"] == 364.18 and r["plan"]["stop"] == 358.32 and r["plan"]["target"] == 370.04
    tm = r["timemachine"]
    assert tm["ok"] and tm["mc"]["replays"] == 10000 and tm["mc"]["pool_sessions"] == 59
    assert tm["history"]["fired"] > 0 and "llegó primero al objetivo" in tm["verdict"]


def test_budget_and_risk_like_the_reel():
    now = datetime(2026, 10, 19, 14, 20, tzinfo=timezone.utc)
    sz = dtskills.size_and_check(label="TSLA", entry=364.18, stop=358.32, target=370.04, setup="orb",
                                 market_cfg={"budget": 5000, "max_loss_pct": 1, "max_position_pct": 75},
                                 rules={"min_reward_risk": 1}, open_notional=[], entries_today=[], now=now, intraday=True)
    assert sz["verdict"] == "PASS" and sz["units"] == 8 and sz["max_loss"] == 46.88   # 50 / 5,86 = 8 acciones
    late = dtskills.size_and_check(label="TSLA", entry=364.18, stop=358.32, target=370.04, setup="orb",
                                   market_cfg={"budget": 5000, "max_loss_pct": 1, "max_position_pct": 75},
                                   rules={"min_reward_risk": 1}, open_notional=[], entries_today=[],
                                   now=datetime(2026, 10, 19, 19, 45, tzinfo=timezone.utc), intraday=True)
    assert late["verdict"] == "BLOCK" and any(r.startswith("horario") for r in late["reasons"])


def test_risk_blocks_daily_loss_and_trades_per_day_in_byma():
    sz = dtskills.size_and_check(label="TGSU2", entry=7000, stop=6600, target=7800, setup="vcp",
                                 market_cfg={"budget": 10_000_000, "max_loss_pct": 1, "max_position_pct": 25},
                                 rules={}, open_notional=[("GGAL", 2_000_000)],
                                 entries_today=[{"label": "GGAL", "max_loss": 100000}, {"label": "YPFD", "max_loss": 100000},
                                                {"label": "PAMP", "max_loss": 90000}],
                                 now=datetime(2027, 1, 4, 15, tzinfo=timezone.utc))
    assert sz["verdict"] == "BLOCK"
    assert any(r.startswith("pérdida diaria") for r in sz["reasons"])
    assert any(r.startswith("operaciones por día") for r in sz["reasons"])


def test_crypto_is_sized_in_lots():
    sz = dtskills.size_and_check(label="BTC", entry=95000, stop=92000, target=101000, setup="vcp",
                                 market_cfg={"budget": 5000, "max_loss_pct": 1, "max_position_pct": 25, "fractional": True},
                                 rules={}, open_notional=[], entries_today=[], now=datetime(2027, 1, 4, tzinfo=timezone.utc))
    assert sz["verdict"] == "PASS" and 0 < sz["units"] < 0.02 and sz["max_loss"] <= 50


def test_swing_timemachine_and_scanner():
    bars = make(seed=3, burst_last=True)
    sc = dtskills.scanner_setups(bars)
    assert "breakout_20d" in sc["tags"]
    e = bars[0]["close"]
    tm = dtskills.swing_timemachine(bars, e, e * 0.95, e * 1.10, horizon=10)
    assert tm["ok"] and tm["mc"]["replays"] == 10000 and tm["history"]["fired"] > 0
    assert tm["lean"] in dtskills.LEAN_ES


def test_breakout_planner_and_circuit_breaker():
    from engine import skills
    bars = make(vcp=True)
    v = skills.run_vcp(bars, make(seed=9, drift=0.0005))
    bp = tmextra.breakout_plan("X", v, bars[0]["close"], account=10000, risk_pct=1, max_position_pct=25, target_r=2,
                               now=datetime(2027, 1, 4, tzinfo=timezone.utc))
    assert bp["classification"] in tmextra.CLASS_ES and bp["stop"] < bp["signal_entry"]
    now = datetime(2027, 1, 4, 20, tzinfo=timezone.utc)
    losses = [{"label": x, "r": -1, "pnl_money": -100, "opened_at": (now - timedelta(hours=h + 20)).isoformat(),
               "closed_at": (now - timedelta(hours=h)).isoformat()} for x, h in (("A", 1), ("B", 2), ("C", 3))]
    assert tmextra.circuit_breaker(losses, 10000, now)["recommendation"] != "TRADING_ALLOWED"
    assert tmextra.circuit_breaker([], 10000, now)["recommendation"] == "TRADING_ALLOWED"


# ------------------------------------------------------------------ motor
def _env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(run, "DATA", data)
    return data


def test_intraday_entry_then_flat_by_close(tmp_path, monkeypatch):
    data = _env(tmp_path, monkeypatch)
    cfg = _cfg()
    fetch = lambda sym: TSLA  # noqa: E731
    sig, ev, st, fresh = run.analyze_intraday(cfg, datetime(2026, 10, 19, 14, 20, tzinfo=timezone.utc), fetch=fetch)
    assert [(e["type"], e["setup"]) for e in fresh] == [("entry", "orb")]
    assert "Time Machine" in fresh[0]["text"] and "PASS" in fresh[0]["text"] and "15:55" in fresh[0]["text"]
    assert sig["intraday"]["symbols"]["TSLA"]["risk"]["verdict"] == "PASS"
    pos = st["positions"]["intradia:TSLA"]
    assert pos["stop"] == 358.32 and pos["mode"] == "intradia"
    for name, obj in (("signals", sig), ("events", ev), ("state", st)):
        run.save_json(data / f"{name}.json", obj)

    # otra corrida 15 minutos después: no repite la entrada
    sig, ev, st, fresh = run.analyze_intraday(cfg, datetime(2026, 10, 19, 14, 35, tzinfo=timezone.utc), fetch=fetch)
    assert all(e["type"] != "entry" for e in fresh)
    for name, obj in (("signals", sig), ("events", ev), ("state", st)):
        run.save_json(data / f"{name}.json", obj)

    # al cierre: sale (stop, objetivo o 15:55) y queda en cerradas con su resultado en plata
    sig, ev, st, fresh = run.analyze_intraday(cfg, datetime(2026, 10, 19, 20, 30, tzinfo=timezone.utc), fetch=fetch)
    exits = [e for e in fresh if e["type"] == "exit"]
    assert len(exits) == 1 and exits[0]["reason"] in ("stop", "target", "flat")
    assert "intradia:TSLA" not in st["positions"] and st["closed"][0]["pnl_money"] is not None


def test_full_run_writes_pipeline_news_congress_and_filings(tmp_path, monkeypatch):
    data = _env(tmp_path, monkeypatch)
    off = tmp_path / "off"
    off.mkdir()
    bars = make(burst_last=True, seed=3)
    bench = make(seed=99, drift=0.001)
    for k, b in {"adr_TSLA": bars, "adr_SPY": bench, "byma__MERV": bench, "cripto_BTC-USD": bench}.items():
        (off / f"{k}.json").write_text(json.dumps(b))
    cfg = _cfg(congress={"enabled": True, "extra_tickers": ["AMAT"]}, analyst={"enabled": True, "tickers": ["AAPL"]})
    # balance anterior registrado: el nuevo 10-Q tiene que avisar
    (data / "analyst.json").write_text(json.dumps({"date": "2026-10-01", "companies": {"AAPL": {"filings": {"report": {"accession": "viejo"}}}}}))
    now = datetime(2026, 10, 19, 15, 0, tzinfo=timezone.utc)
    sig, ev, st, fresh = run.analyze_all(cfg, off, use_regime=False, now=now, intraday_fetch=lambda s: TSLA,
                                         extras={"news": str(FIX), "analyst": str(FIX),
                                                 "congress_index": str(FIX / "2026FD.zip"),
                                                 "congress_texts": {"20035590": (FIX / "ptr-20035590.txt").read_text()}})
    a = sig["assets"]["adr:TSLA"]
    keys = [s["key"] for s in a["pipeline"]["stages"]]
    assert keys[:2] == ["scanner", "news"]
    assert a["pipeline"]["stages"][1]["status"] in ("ok", "warn") and "noticias recientes" in a["pipeline"]["stages"][1]["text"]
    assert sig["news"] and sig["intraday"]["symbols"]["TSLA"]["status"] in ("triggered", "open")
    infos = [e for e in fresh if e["type"] == "info"]
    assert any(e["info"] == "earnings" and e["label"] == "AAPL" for e in infos)
    assert any(e["info"] == "congress" and e["label"] == "AMAT" for e in infos)
    assert all(e.get("text") for e in fresh)
    cong = json.loads((data / "congress.json").read_text())
    assert any(r["ticker"] == "MCD" for r in cong["rows"])


def test_breaker_blocks_new_entries(tmp_path, monkeypatch):
    data = _env(tmp_path, monkeypatch)
    off = tmp_path / "off"
    off.mkdir()
    bars = make(burst_last=True, seed=3)
    bench = make(seed=99, drift=0.001)
    for k, b in {"adr_TSLA": bars, "adr_SPY": bench, "byma__MERV": bench, "cripto_BTC-USD": bench}.items():
        (off / f"{k}.json").write_text(json.dumps(b))
    now = datetime(2027, 1, 1, 18, tzinfo=timezone.utc)
    closed = [{"kind": "adr", "label": x, "r": -1, "pnl_money": -100, "opened_at": (now - timedelta(days=2)).isoformat(),
               "closed_at": (now - timedelta(hours=h)).isoformat()} for x, h in (("A", 1), ("B", 2), ("C", 3))]
    run.save_json(data / "state.json", {"positions": {}, "rung": {}, "closed": closed})
    sig, _, st, fresh = run.analyze_all(_cfg(), off, use_regime=False, now=now)
    assert not [e for e in fresh if e["type"] == "entry"]
    assert sig["markets"]["adr"]["breaker"]["recommendation"] != "TRADING_ALLOWED"


def test_weekly_summary_on_friday_after_close(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    now = datetime(2027, 1, 8, 22, tzinfo=timezone.utc)   # viernes 17:00 NY
    st = {"closed": [{"label": "YPF", "r": 2.0, "closed_at": "2027-01-06T20:00:00+00:00"},
                     {"label": "GGAL", "r": -1.0, "closed_at": "2027-01-07T20:00:00+00:00"}]}
    evs = []
    rev = pipeline.run_weekly(_cfg(), st, now, evs)
    assert rev["trades"] == 2 and rev["total_r"] == 1.0
    assert evs and evs[0]["info"] == "weekly" and "RESUMEN SEMANAL" in evs[0]["text_override"]
