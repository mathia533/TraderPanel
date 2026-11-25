"""El simulador del panel (lib/timemachine.mjs) da exactamente lo mismo que trader-timemachine en Python.

Corre el puerto de JavaScript con Node sobre las mismas velas, la misma semilla y el
mismo plan, y compara probabilidades, percentiles, abanico, histograma, veredicto e
historia. Si Node no está instalado, se saltea.
"""
import gzip
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from synth import make  # noqa: E402

from engine import dtskills  # noqa: E402

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="hace falta Node para correr el simulador del panel")

RUNNER = r"""
import * as tm from './lib/timemachine.mjs';
const job = JSON.parse(await new Promise(r => { let s = ''; process.stdin.on('data', d => s += d); process.stdin.on('end', () => r(s)); }));
const out = job.cases.map(c => c.pool
  ? tm.simulate(c.pool, c.entry, c.stop, c.target, c.k, c.last, {replays: c.replays, seed: c.seed, window: c.window, side: c.side})
  : tm.swingTimeMachine(c.bars, c.entry, c.stop, c.target, {horizon: c.horizon, replays: c.replays, seed: c.seed, historyTag: c.tag}));
process.stdout.write(JSON.stringify(out));
"""

MC_KEYS = ("p_target", "p_stop", "p_neither", "p_green", "median_close", "p5_close", "p95_close", "avg_R",
           "r_at_target", "bars_left", "bands", "sample_paths", "hist")


def run_js(cases):
    res = subprocess.run([NODE, "--input-type=module", "-e", RUNNER], input=json.dumps({"cases": cases}),
                         capture_output=True, text=True, cwd=ROOT, timeout=120, check=True)
    return json.loads(res.stdout)


def as_rows(closed):
    """Velas del motor (de la más nueva a la más vieja, dicts) -> formato de data/candles.json."""
    return [[b["date"], b["open"], b["high"], b["low"], b["close"], b.get("volume", 0)] for b in reversed(closed)]


def full_mc(closed, entry, stop, target, horizon, replays):
    """simulate() de Python con el mismo pozo que swing_timemachine (sin recortar el resultado)."""
    tm = dtskills.mod("tm")
    bars = list(reversed(closed[:251]))
    ratios = [(b["open"] / p["close"], b["high"] / p["close"], b["low"] / p["close"], b["close"] / p["close"])
              for p, b in zip(bars, bars[1:])]
    return tm.simulate([ratios] * (horizon + 1), entry, stop, target, 0, horizon, replays=replays, seed=19, window=0)


def test_swing_igual_a_python():
    cases, expect = [], []
    for seed, drift, (sl, tg, hz, tag) in [(3, 0.002, (0.95, 1.10, 10, "breakout_20d")),
                                            (11, -0.001, (0.97, 1.03, 5, "reclaim_ma20")),
                                            (42, 0.0, (0.92, 1.24, 20, "oversold_bounce"))]:
        closed = make(n=300, seed=seed, drift=drift)
        e = closed[0]["close"]
        entry, stop, target = round(e, 2), round(e * sl, 2), round(e * tg, 2)
        py = dtskills.swing_timemachine(closed, entry, stop, target, horizon=hz, replays=4000, history_tag=tag)
        mc = full_mc(closed, entry, stop, target, hz, 4000)
        cases.append({"bars": as_rows(closed), "entry": entry, "stop": stop, "target": target, "horizon": hz,
                      "replays": 4000, "seed": 19, "tag": tag})
        expect.append((py, mc))
    for js, (py, mc) in zip(run_js(cases), expect):
        assert js["ok"] and py["ok"]
        for k in MC_KEYS:
            assert js["mc"][k] == json.loads(json.dumps(mc[k])), k
        assert js["mc"]["pool_sessions"] == py["mc"]["pool_sessions"]
        assert js["lean"] == py["lean"]
        assert js["verdict"] == py["verdict"]
        for k in ("fired", "target", "stop", "neither", "avg_R"):
            assert js["history"][k] == py["history"][k], k


def test_intradia_con_ventana_igual_a_python():
    """El caso del video de day-trader-skill (TSLA, 19-10-2026): pozo de 5 minutos y ventana de ±2 velas."""
    tm = dtskills.mod("tm")
    text = gzip.open(ROOT / "tests" / "fixtures" / "dts" / "TSLA-60d5m.json.gz", "rt").read()
    bars = tm.parse_bars(tm.chart_result(text, "TSLA"), datetime(2026, 10, 19, 14, 20))[1]
    days = tm.split_sessions(bars)
    sess, _ = tm.full_sessions(days, "2026-10-19")
    pool = tm.build_pool(sess)
    py = tm.simulate(pool, 364.18, 358.32, 370.04, 8, 77, replays=3000, seed=19, window=2)
    js = run_js([{"pool": pool, "entry": 364.18, "stop": 358.32, "target": 370.04, "k": 8, "last": 77,
                  "replays": 3000, "seed": 19, "window": 2, "side": "buy"}])[0]
    for k in MC_KEYS:
        assert js[k] == json.loads(json.dumps(py[k])), k
