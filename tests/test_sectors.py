import json
from datetime import datetime, timezone

from engine import sectors

SEC = {"bancos": {"name": "Bancos", "byma": ["GGAL", "BMA", "SUPV"]}, "energia": {"name": "Energía", "byma": ["YPFD", "PAMP"]},
       "vacio": {"name": "Sin datos", "byma": ["NADA"]}}
LIVE = [{"symbol": "GGAL", "c": 100, "pct_change": 2, "v": 3000}, {"symbol": "BMA", "c": 100, "pct_change": -1, "v": 1000},
        {"symbol": "SUPV", "c": 50, "pct_change": 0, "v": 0}, {"symbol": "YPFD", "c": 100, "pct_change": -2, "v": 10},
        {"symbol": "PAMP", "c": 100, "pct_change": -1, "v": 10}]


def test_weighted_by_traded_amount():
    d = sectors.day_rows(LIVE, SEC)
    assert d["bancos"]["ch"] == 1.25            # (2*300000 - 1*100000) / 400000
    assert (d["bancos"]["up"], d["bancos"]["down"], d["bancos"]["n"]) == (1, 1, 3)
    assert d["bancos"]["best"] == ["GGAL", 2.0] and d["energia"]["worst"] == ["YPFD", -2.0]
    assert "vacio" not in d


def test_history_ranking_and_compound():
    prev = None
    for i, day in enumerate(["2026-11-23", "2026-11-24", "2026-11-25"]):
        now = datetime.fromisoformat(day + "T19:00:00+00:00")
        live = [{**r, "pct_change": r["pct_change"] + i} for r in LIVE]
        prev = sectors.build(json.loads(json.dumps(prev)) if prev else None, live, SEC, now)
    assert prev["days"] == 3 and prev["date"] == "2026-11-25"
    assert [r["key"] for r in prev["ranking"]] == ["bancos", "energia"]
    b = prev["ranking"][0]
    assert len(b["spark"]) == 3 and b["ch5"] is not None and b["share"] > 90
    # El sábado no se agrega rueda
    sat = sectors.build(prev, LIVE, SEC, datetime(2026, 11, 28, 15, tzinfo=timezone.utc))
    assert sat["days"] == 3


def test_offline_cli(tmp_path):
    f = tmp_path / "live.json"
    f.write_text(json.dumps(LIVE))
    out = tmp_path / "s.json"
    assert sectors.main(["--offline", str(f), "--out", str(out)]) == 0
    j = json.loads(out.read_text())
    assert "ranking" in j and "history" in j
