"""Velas sintéticas para probar el motor sin red."""
import math, random
from datetime import date, timedelta

def make(n=320, seed=1, drift=0.0015, vol=0.015, start=100.0, burst_last=False, vcp=False, crash=False):
    rnd = random.Random(seed)
    d0 = date(2025, 1, 1)
    p = start; out = []; d = d0
    for i in range(n):
        d += timedelta(days=1)
        while d.weekday() >= 5: d += timedelta(days=1)
        v = vol
        dr = drift
        if vcp and i > n - 90:
            # tres contracciones cada vez más chicas
            k = i - (n - 90)
            seg = [(0, 25, -0.006), (25, 45, 0.006), (45, 60, -0.004), (60, 72, 0.004), (72, 80, -0.002), (80, 89, 0.002)]
            for a, b, x in seg:
                if a <= k < b: dr = x
            v = vol * (1 - k / 120)
        if crash and i > n - 60: dr = -0.006
        o = p
        c = p * (1 + dr + rnd.gauss(0, v))
        h = max(o, c) * (1 + abs(rnd.gauss(0, v / 2)))
        l = min(o, c) * (1 - abs(rnd.gauss(0, v / 2)))
        vv = int(500_000 * (1 + rnd.random()))
        if vcp and i > n - 30: vv = int(vv * 0.6)
        out.append({"date": d.isoformat(), "open": o, "high": h, "low": l, "close": c, "volume": vv})
        p = c
    if burst_last or vcp:
        prev = out[-1]; d += timedelta(days=1)
        while d.weekday() >= 5: d += timedelta(days=1)
        hi = max(b["high"] for b in out[-60:])
        c = max(prev["close"] * 1.06, hi * 1.01)
        out.append({"date": d.isoformat(), "open": prev["close"] * 1.005, "high": c * 1.003, "low": prev["close"] * 1.0, "close": c, "volume": 2_500_000})
    out.reverse()
    return out
