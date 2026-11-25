import json
from datetime import datetime, timezone
from pathlib import Path

from engine import report, run
from synth import make

ASSETS = {
    "adr:YPF": {"kind": "adr", "label": "YPF", "day_ch": 3.2}, "adr:GGAL": {"kind": "adr", "label": "GGAL", "day_ch": -1.5},
    "adr:PAM": {"kind": "adr", "label": "PAM", "day_ch": 0.4}, "byma:ALUA": {"kind": "byma", "label": "ALUA", "day_ch": -2.0},
}


def _sm():
    return report.summarize(ASSETS, {"adr": {"gate": "allowed"}, "byma": {"gate": "restrictive"}},
                            [{"label": "VIST", "r_now": 0.8}],
                            [{"type": "entry", "label": "YPF", "price": 30, "setup": "vcp"}, {"type": "exit", "label": "BMA", "r": -1.0, "reason": "stop"}],
                            [{"label": "TGS", "name": "Doble piso", "bias": "alcista", "state": "ruptura"}],
                            {"date": "2026-11-25", "ranking": [{"name": "Bancos", "ch": 1.2}, {"name": "Energía", "ch": -0.8}]}, "2026-11-25")


def test_summary_and_text():
    s = _sm()
    assert s["movers"]["adr"]["up"] == [["YPF", 3.2], ["PAM", 0.4]] and s["movers"]["adr"]["down"] == [["GGAL", -1.5]]
    assert s["movers"]["adr"]["breadth"] == [2, 3]
    assert s["gates"] == {"adr": "favorable", "byma": "defensivo"}
    t = report.text(s)
    assert "INFORME DEL CIERRE</b> · 25/11" in t
    assert "compra YPF" in t and "venta BMA (−1,0R)" in t
    assert "mejor Bancos +1,2 %" in t and "TGS (doble piso)" in t
    assert len(t) <= 1024


def test_card_png(tmp_path):
    p = report.render(_sm(), tmp_path / "c.png", [["MEP", 1450.0], ["CCL", 1480.0], ["Blue", 1420.0]])
    from PIL import Image
    im = Image.open(p)
    assert im.size == (1080, 1920)


def test_engine_emits_one_report_after_ny_close(tmp_path, monkeypatch):
    off = tmp_path / "off"
    off.mkdir()
    for k, b in {"adr_TST": make(seed=3), "adr_SPY": make(seed=99), "byma__MERV": make(seed=99), "cripto_BTC-USD": make(seed=99)}.items():
        (off / f"{k}.json").write_text(json.dumps(b))
    monkeypatch.setattr(run, "DATA", tmp_path / "data")
    cfg = json.loads(Path(run.CONFIG).read_text())
    cfg["watchlist"] = {"adr": ["TST"], "byma": [], "cripto": []}
    before = datetime(2026, 11, 25, 18, tzinfo=timezone.utc)      # 13 h en NY: todavía no
    sig, _, _, fresh = run.analyze_all(cfg, off, use_regime=False, now=before)
    assert not [e for e in fresh if e.get("info") == "report"]
    assert sig["assets"]["adr:TST"]["day_ch"] is not None
    after = datetime(2026, 11, 25, 21, 35, tzinfo=timezone.utc)   # 16:35 en NY
    _, ev, st, fresh = run.analyze_all(cfg, off, use_regime=False, now=after)
    run.save_json(tmp_path / "data" / "state.json", st)
    run.save_json(tmp_path / "data" / "events.json", ev)
    reps = [e for e in fresh if e.get("info") == "report"]
    assert len(reps) == 1 and reps[0]["id"] == "report:2026-11-25" and reps[0]["text"].startswith("🌙")
    _, _, _, fresh = run.analyze_all(cfg, off, use_regime=False, now=after.replace(hour=22))
    assert not [e for e in fresh if e.get("info") == "report"]       # una sola vez por día
    out = tmp_path / "cards"
    assert report.main(["--render", str(out), "--data-dir", str(tmp_path / "data"), "--no-dolar"]) == 0
    assert (out / "report-2026-11-25.png").exists()
