// Time Machine de day-trader-skill (trader-timemachine) en JavaScript, para el simulador del panel.
//
// Es un puerto línea por línea de vendor/day-trader-skill/trader-timemachine/timemachine.py
// (simulate, lean) y de engine/dtskills.py (swing_timemachine, verdict_es, _daily_history),
// con el mismo generador de números al azar que Python (Mersenne Twister, random.Random(seed)
// y random.choice). Con la misma semilla y las mismas velas da exactamente las mismas
// repeticiones que el motor: tests/test_timemachine_js.py lo compara contra Python.
//
// Sin dependencias: corre en el navegador (simulador.html) y en Node (pruebas).

export const SAMPLE_PATHS = 160;
export const BAND_PATHS = 2000;
export const BAND_Q = [0.05, 0.25, 0.5, 0.75, 0.95];
export const HIST_BINS = 24;
export const COIN_FLIP = 0.05;
export const STRONG = 0.15;
export const MANY_NEITHER = 0.40;

/* ------------------------------------------------------------------ azar idéntico a Python */
// random.Random(seed) de CPython: MT19937 sembrado con init_by_array(abs(seed) en palabras de 32 bits).
export class PyRandom {
  constructor(seed = 19) {
    this.mt = new Uint32Array(624);
    this.i = 625;
    let n = BigInt(Math.abs(Math.trunc(seed)));
    const key = [];
    do { key.push(Number(n & 0xffffffffn)); n >>= 32n; } while (n > 0n);
    this.#byArray(key);
  }
  #init(s) {
    const mt = this.mt;
    mt[0] = s >>> 0;
    for (let i = 1; i < 624; i++) {
      const p = mt[i - 1] ^ (mt[i - 1] >>> 30);
      mt[i] = (Math.imul(1812433253, p) + i) >>> 0;
    }
    this.i = 624;
  }
  #byArray(key) {
    const mt = this.mt;
    this.#init(19650218);
    let i = 1, j = 0;
    for (let k = Math.max(624, key.length); k; k--) {
      const p = mt[i - 1] ^ (mt[i - 1] >>> 30);
      mt[i] = ((mt[i] ^ Math.imul(p, 1664525)) + key[j] + j) >>> 0;
      i++; j++;
      if (i >= 624) { mt[0] = mt[623]; i = 1; }
      if (j >= key.length) j = 0;
    }
    for (let k = 623; k; k--) {
      const p = mt[i - 1] ^ (mt[i - 1] >>> 30);
      mt[i] = ((mt[i] ^ Math.imul(p, 1566083941)) - i) >>> 0;
      i++;
      if (i >= 624) { mt[0] = mt[623]; i = 1; }
    }
    mt[0] = 0x80000000;
  }
  uint32() {
    const mt = this.mt;
    if (this.i >= 624) {
      for (let k = 0; k < 624; k++) {
        const y = (mt[k] & 0x80000000) | (mt[(k + 1) % 624] & 0x7fffffff);
        mt[k] = mt[(k + 397) % 624] ^ (y >>> 1) ^ (y & 1 ? 0x9908b0df : 0);
      }
      this.i = 0;
    }
    let y = mt[this.i++];
    y ^= y >>> 11;
    y ^= (y << 7) & 0x9d2c5680;
    y ^= (y << 15) & 0xefc60000;
    y ^= y >>> 18;
    return y >>> 0;
  }
  // getrandbits(k) para k <= 32 y _randbelow_with_getrandbits(n), como CPython
  bits(k) { return k <= 0 ? 0 : this.uint32() >>> (32 - k); }
  below(n) {
    if (n <= 0) return 0;
    const k = 32 - Math.clz32(n);
    let r = this.bits(k);
    while (r >= n) r = this.bits(k);
    return r;
  }
  choice(seq) { return seq[this.below(seq.length)]; }
  randint(a, b) { return a + this.below(b - a + 1); }
}

/* ------------------------------------------------------------------ utilidades */
export const pyRound = (x, n = 0) => Number(Number(x).toFixed(n));
const pct = (a, q) => a[Math.min(a.length - 1, Math.floor(q * a.length))];
const mean = a => a.reduce((s, v) => s + v, 0) / a.length;

/* ------------------------------------------------------------------ simulate() */
// pool[i] = lista de velas (o, h, l, c) / cierre anterior para el paso i. Repite los pasos k+1..last.
export function simulate(pool, entry, stop, target, k, last, {replays = 10000, seed = 19, window = 2, side = 'buy'} = {}) {
  const long = side === 'buy';
  const risk = long ? entry - stop : stop - entry;
  const rTarget = pyRound((long ? target - entry : entry - target) / risk, 4);
  const top = pool.length - 1;
  const slot = (rng, i) => window === 0 ? i : Math.min(top, Math.max(1, i + rng.randint(-window, window)));

  let rng = new PyRandom(seed);
  const outcomes = [], closes = [], paths = [];
  for (let n = 0; n < replays; n++) {
    let p = entry, outcome = null;
    const keep = n < SAMPLE_PATHS, path = [entry];
    for (let i = k + 1; i <= last; i++) {
      const [, h, l, c] = rng.choice(pool[slot(rng, i)]);
      const hi = p * h, lo = p * l, cl = p * c;
      if (outcome === null) {
        if (long) {
          if (lo <= stop) outcome = 'stop';          // las dos en la misma vela: cuenta la pérdida
          else if (hi >= target) outcome = 'target';
        } else {
          if (hi >= stop) outcome = 'stop';
          else if (lo <= target) outcome = 'target';
        }
      }
      p = cl;
      if (keep) path.push(pyRound(p, 2));
    }
    outcomes.push(outcome || 'close');
    closes.push(p);
    if (keep) paths.push(path);
  }
  const cnt = {target: 0, stop: 0, close: 0};
  for (const o of outcomes) cnt[o]++;
  let green = 0;
  const Rs = outcomes.map((o, idx) => {
    const p = closes[idx];
    if (o === 'target' || (o === 'close' && (long ? p > entry : p < entry))) green++;
    return o === 'target' ? rTarget : o === 'stop' ? -1 : (long ? p - entry : entry - p) / risk;
  });
  const srt = closes.slice().sort((a, b) => a - b);

  // bandas por paso para el abanico: una segunda corrida de 2.000 caminos con la misma semilla
  rng = new PyRandom(seed);
  const allp = [];
  for (let n = 0; n < BAND_PATHS; n++) {
    let p = entry;
    const path = [entry];
    for (let i = k + 1; i <= last; i++) {
      const [, , , c] = rng.choice(pool[slot(rng, i)]);
      p = p * c;
      path.push(p);
    }
    allp.push(path);
  }
  const bands = [];
  for (let j = 0; j <= last - k; j++) {
    const col = allp.map(pp => pp[j]).sort((a, b) => a - b);
    bands.push(BAND_Q.map(q => pyRound(pct(col, q), 2)));
  }
  const lo_ = pct(srt, 0.01), hi_ = pct(srt, 0.99), W = (hi_ - lo_) / HIST_BINS;
  const bins = new Array(HIST_BINS).fill(0);
  for (const c of srt) {
    const b = W > 0 ? Math.trunc((c - lo_) / W) : 0;
    if (b >= 0 && b < HIST_BINS) bins[b]++;
  }
  return {
    replays, seed, window, bars_left: last - k,
    p_target: pyRound(cnt.target / replays, 4), p_stop: pyRound(cnt.stop / replays, 4),
    p_neither: pyRound(cnt.close / replays, 4), p_green: pyRound(green / replays, 4),
    median_close: pyRound(pct(srt, 0.5), 2), p5_close: pyRound(pct(srt, 0.05), 2),
    p95_close: pyRound(pct(srt, 0.95), 2), avg_R: pyRound(mean(Rs), 3),
    r_at_target: rTarget, band_paths: BAND_PATHS, bands, sample_paths: paths,
    hist: {lo: pyRound(lo_, 2), hi: pyRound(hi_, 2), bins},
  };
}

/* ------------------------------------------------------------------ veredicto */
export function lean(mc) {
  const pt = mc.p_target, ps = mc.p_stop;
  if (pt + ps <= 0) return null;
  const r = mc.r_at_target || 1;
  const share = pt / (pt + ps), even = 1 / (1 + r);
  return [share - even, share, even];
}

export function leanCode(mc) {
  const lv = lean(mc);
  if (!lv) return 'none';
  const d = lv[0];
  if (Math.abs(d) < COIN_FLIP) return 'even';
  if (Math.abs(d) < STRONG) return d > 0 ? 'target' : 'stop';
  return d > 0 ? 'target_strong' : 'stop_strong';
}

export const LEAN_ES = {
  target_strong: 'Fuerte hacia el objetivo',
  target: 'Se inclina al objetivo',
  even: 'Parejo',
  stop: 'Se inclina al stop',
  stop_strong: 'Fuerte hacia el stop',
  none: 'Sin definición',
};

export function fmtNum(v, d = 2) {
  return v.toFixed(d).replace(/\.?0+$/, '').replace('.', ',');
}

const pc1 = v => (v * 100).toFixed(1).replace('.', ',');

// El mismo texto que verdict_es() del motor
export function verdictEs(mc, horizonTxt) {
  const code = leanCode(mc);
  const pt = mc.p_target, ps = mc.p_stop;
  if (code === 'none') return `Ninguna repetición tocó el objetivo ni el stop en ${horizonTxt}: decide el precio al final.`;
  const [, share, even] = lean(mc);
  const r = mc.r_at_target || 1;
  let head = LEAN_ES[code];
  if (code === 'even' && Math.abs(r - 1) < 0.005) head = 'Casi una moneda al aire';
  const odds = `objetivo primero ${pc1(pt)} % y stop primero ${pc1(ps)} %`;
  let txt = Math.abs(r - 1) < 0.005 ? `${head}: ${odds}.`
    : `${head}: a ${fmtNum(r)}R el objetivo tiene que llegar primero en el ${(even * 100).toFixed(0)} % de las ` +
      `repeticiones que tocan un lado, y llegó en el ${(share * 100).toFixed(0)} % (${odds}).`;
  const pn = mc.p_neither || 0;
  if (pn > MANY_NEITHER) {
    txt += ` El ${(pn * 100).toFixed(0)} % no toca ninguno en ${horizonTxt}: ahí decide el precio final ` +
      `(promedio de todas ${fmtNum(mc.avg_R || 0)}R).`;
  }
  return txt;
}

/* ------------------------------------------------------------------ escáner (trader-scanner) */
export function rsi(closes, n = 14) {
  if (closes.length < n + 1) return null;
  const g = [], lo = [];
  for (let i = 1; i < closes.length; i++) {
    g.push(Math.max(0, closes[i] - closes[i - 1]));
    lo.push(Math.max(0, closes[i - 1] - closes[i]));
  }
  let ag = g.slice(0, n).reduce((s, v) => s + v, 0) / n, al = lo.slice(0, n).reduce((s, v) => s + v, 0) / n;
  for (let i = n; i < g.length; i++) {
    ag = (ag * (n - 1) + g[i]) / n;
    al = (al * (n - 1) + lo[i]) / n;
  }
  return al ? 100 - 100 / (1 + ag / al) : 100;
}

export function findSetups(c) {
  const last = c.at(-1);
  const hi20 = Math.max(...c.slice(-21, -1));
  const ma20 = c.slice(-20).reduce((s, v) => s + v, 0) / 20;
  const ma20Prev = c.slice(-21, -1).reduce((s, v) => s + v, 0) / 20;
  const run = c.at(-6) / c.at(-16) - 1;
  const rng5 = (Math.max(...c.slice(-5)) - Math.min(...c.slice(-5))) / last;
  const r = rsi(c), rPrev = rsi(c.slice(0, -1));
  const tags = [];
  if (last > hi20) tags.push('breakout_20d');
  if (run > 0.10 && rng5 < 0.05) tags.push('bull_flag');
  if (c.at(-2) < ma20Prev && last > ma20) tags.push('reclaim_ma20');
  if (r !== null && r < 30) tags.push('oversold');
  if (rPrev !== null && rPrev < 30 && last > c.at(-2)) tags.push('oversold_bounce');
  return tags;
}

export const SETUP_ES = {
  breakout_20d: 'ruptura del máximo de 20 días',
  bull_flag: 'bandera alcista',
  reclaim_ma20: 'recuperó la media de 20',
  oversold: 'sobreventa (RSI < 30)',
  oversold_bounce: 'rebote de sobreventa',
  any: 'cualquier rueda',
};

/* ------------------------------------------------------------------ Time Machine de varios días */
// bars: velas diarias de la más vieja a la más nueva, cada una [fecha, apertura, máximo, mínimo, cierre, ...]
// (el formato de data/candles.json). Igual que swing_timemachine() del motor, más venta en corto.
export function swingTimeMachine(bars, entry, stop, target, {horizon = 10, lookback = 250, replays = 10000, seed = 19,
  historyTag = 'breakout_20d', side = 'buy'} = {}) {
  const long = side === 'buy';
  const b = bars.slice(-(lookback + 1));
  if (b.length < 60 || !(long ? stop < entry && entry < target : target < entry && entry < stop)) {
    return {ok: false, error: b.length < 60 ? 'Historial corto: hacen falta 60 ruedas.' : 'Plan inválido: revisá entrada, stop y objetivo.'};
  }
  const ratios = [];
  for (let i = 1; i < b.length; i++) {
    const pc = b[i - 1][4], [, o, h, l, c] = b[i];
    if (pc > 0 && Math.min(o, h, l, c) > 0) ratios.push([o / pc, h / pc, l / pc, c / pc]);
  }
  if (ratios.length < 50) return {ok: false, error: 'Pocas velas válidas.'};
  const pool = new Array(horizon + 1).fill(ratios);
  const mc = simulate(pool, entry, stop, target, 0, horizon, {replays, seed, window: 0, side});
  mc.pool_sessions = ratios.length;
  mc.until = `${horizon} ruedas`;
  const risk = long ? entry - stop : stop - entry;
  const history = dailyHistory(bars, risk / entry, Math.abs(target - entry) / risk, horizon, lookback, historyTag, long);
  const code = leanCode(mc);
  return {ok: true, kind: 'diario', mc, lean: code, verdict: verdictEs(mc, `${horizon} ruedas`), history,
    note: `Simulación con las ${ratios.length} velas diarias de este activo. No es una predicción: ` +
      'balances, noticias y feriados pueden hacer lo que el último año no hizo.'};
}

// La misma regla (stop y objetivo a la misma distancia) en cada rueda pasada en que el escáner marcó `tag`.
// tag 'any' = todas las ruedas (la tasa base, sin filtro de patrón).
export function dailyHistory(bars, stopFrac, rMult, horizon, lookback, tag, long = true) {
  const b = bars.slice(-(lookback + 60));
  const closes = b.map(x => x[4]);
  const out = {tag, label: SETUP_ES[tag] || tag, fired: 0, target: 0, stop: 0, neither: 0, avg_R: null, trades: []};
  const Rs = [];
  for (let i = Math.max(30, b.length - lookback); i < b.length - 1; i++) {
    if (tag !== 'any' && !findSetups(closes.slice(0, i + 1)).includes(tag)) continue;
    const e = closes[i];
    const s = long ? e * (1 - stopFrac) : e * (1 + stopFrac);
    const t = long ? e + rMult * (e - s) : e - rMult * (s - e);
    let oc = null, j = i + 1;
    for (; j < Math.min(b.length, i + 1 + horizon); j++) {
      const [, , h, l] = b[j];
      if (long ? l <= s : h >= s) { oc = 'stop'; break; }
      if (long ? h >= t : l <= t) { oc = 'target'; break; }
    }
    const lastC = b[Math.min(i + horizon, b.length - 1)][4];
    const R = oc === 'target' ? rMult : oc === 'stop' ? -1 : (long ? lastC - e : e - lastC) / Math.abs(e - s);
    out.fired++;
    out[oc || 'neither']++;
    Rs.push(R);
    out.trades.push({date: b[i][0], entry: e, outcome: oc || 'neither', exit_date: b[Math.min(oc ? j : i + horizon, b.length - 1)][0], R});
  }
  if (Rs.length) out.avg_R = pyRound(mean(Rs), 2);
  return out;
}
