import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import indices  # noqa: E402


def chart(closes, last=None, today=True):
    t0 = datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc)
    ts = [int((t0 + timedelta(days=i)).timestamp()) for i in range(len(closes))]
    mt = ts[-1] + 3600 if today else ts[-1] + 3 * 86400
    return {"chart": {"result": [{
        "meta": {"exchangeTimezoneName": "America/New_York", "regularMarketPrice": last or closes[-1], "regularMarketTime": mt},
        "timestamp": ts,
        "indicators": {"quote": [{"close": closes}]},
    }]}}


def test_parse_chart_intraday_uses_live_price():
    r = indices.parse_chart(chart([100, None, 102, 104], last=106))
    assert r["c"] == [100, 102, 106] and r["prev"] == 102
    assert round(r["ch"], 2) == 3.92


def test_parse_chart_new_session_appends_point():
    r = indices.parse_chart(chart([100, 102], last=101, today=False))
    assert r["c"][-1] == 101 and r["prev"] == 102 and len(r["d"]) == 3


def test_build_keeps_previous_when_symbol_fails(tmp_path):
    for sym, *_ in indices.INDICES:
        if sym != "^VIX":
            (tmp_path / f"{sym}.json").write_text(json.dumps(chart([10, 11, 12])))
    prev = {"items": [{"id": "^VIX", "last": 15, "c": [14, 15]}]}
    data = indices.build(tmp_path, prev)
    vix = next(x for x in data["items"] if x["id"] == "^VIX")
    assert vix["stale"] and vix["last"] == 15 and "^VIX" in data["errors"]
    assert len(data["items"]) == len(indices.INDICES)
