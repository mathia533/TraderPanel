from datetime import date, timedelta

from engine import patterns


def path(*knots):
    """Precios por tramos rectos: path((0, 120), (20, 100), ...) -> velas, la más nueva primero."""
    closes = []
    for (i0, p0), (i1, p1) in zip(knots, knots[1:]):
        for i in range(i0, i1):
            closes.append(p0 + (p1 - p0) * (i - i0) / (i1 - i0))
    closes.append(knots[-1][1])
    d = date(2026, 6, 1)
    bars = []
    for c in closes:
        d += timedelta(days=1)
        bars.append({"date": d.isoformat(), "open": c, "high": c * 1.004, "low": c * 0.996, "close": c, "volume": 1000})
    return list(reversed(bars))


def by_type(closed):
    return {p["type"]: p for p in patterns.scan(closed)}


def test_double_bottom_forming_and_breakout():
    p = by_type(path((0, 125), (20, 100), (35, 112), (50, 100.5), (62, 110)))["double_bottom"]
    assert p["state"] == "formándose" and p["bias"] == "alcista"
    assert abs(p["trigger"] - 112 * 1.004) < 0.01
    assert p["stop"] < 100 and p["target"] > p["trigger"] and p["rr"] > 0.8
    assert len(p["points"]) == 4
    br = by_type(path((0, 125), (20, 100), (35, 112), (50, 100.5), (63, 113.5)))["double_bottom"]
    assert br["state"] == "ruptura"


def test_inverse_head_and_shoulders():
    p = by_type(path((0, 120), (15, 104), (25, 112), (40, 96), (55, 112), (68, 104.5), (78, 110)))["inv_head_shoulders"]
    assert p["bias"] == "alcista" and p["state"] == "formándose"
    assert 111 < p["trigger"] < 113 and p["target"] > p["trigger"]


def test_head_and_shoulders_is_a_warning():
    p = by_type(path((0, 90), (15, 106), (25, 98), (40, 114), (55, 98), (68, 105.5), (78, 100)))["head_shoulders"]
    assert p["bias"] == "bajista" and p["rr"] is None and p["target"] < p["trigger"]


def test_ascending_triangle():
    knots = [(0, 90), (8, 100)]
    lows = [92, 94.5, 96.5, 98]
    i = 8
    for lo in lows:
        knots += [(i + 5, lo), (i + 10, 100)]
        i += 10
    p = by_type(path(*knots, (i + 3, 99)))["asc_triangle"]
    assert p["bias"] == "alcista" and abs(p["trigger"] - 100.4) < 0.6


def test_nothing_in_a_plain_trend_and_short_history():
    assert patterns.scan(path((0, 100), (80, 130))) == []
    assert patterns.scan(path((0, 100), (20, 110))) == []
