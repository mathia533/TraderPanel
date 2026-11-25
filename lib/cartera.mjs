// Mi cartera: lectura y análisis de una cartera en el navegador.
//
// Todo corre en la página (cartera.html): la cartera vive en un repo privado del propio usuario y
// solo se lee con su token de GitHub. Ni el motor ni el dueño del panel la ven. Precios y velas
// salen de data912 (BYMA), que el navegador puede leer directo.
//
// Formato de archivo: docs/cartera-formato.md ("traderpanel-cartera", versión 1).
import {rsi, findSetups, SETUP_ES, swingTimeMachine, LEAN_ES} from './timemachine.mjs';

export const FORMAT = 'traderpanel-cartera';
export const LIVE_KINDS = {arg_cedears: 'cedear', arg_stocks: 'accion', arg_bonds: 'bono', arg_corp: 'on', arg_notes: 'letra'};
export const HIST_PATH = {cedear: 'cedears', accion: 'stocks'};          // tipos con análisis de tendencia
export const PER_100 = new Set(['bono', 'on', 'letra']);                  // BYMA cotiza la renta fija cada 100 de VN
export const TYPE_ES = {cedear: 'CEDEAR', accion: 'Acción', bono: 'Bono', on: 'Obligación negociable', letra: 'Letra', fci: 'Fondo común', otro: 'Otro'};
// ETFs de índice: no cuentan para el tope de concentración por activo
export const INDEX_ETF = new Set(['SPY', 'QQQ', 'DIA', 'IWM', 'VOO', 'VTI', 'EEM', 'EWZ', 'ACWI', 'XLE', 'XLF', 'XLK', 'XLV',
  'XLY', 'XLP', 'XLI', 'XLU', 'XLB', 'GLD', 'SLV', 'ARKK', 'EFA', 'TLT', 'IBIT', 'ETHA']);

/* ------------------------------------------------------------------ entrada */
// "1.234,56" → 1234.56 ; "4897.78" → 4897.78 ; "3.281" → 3281
export function num(t) {
  t = String(t).replace(/US\$|[$\s]/g, '');
  if (!/^-?[\d.,]+$/.test(t)) return NaN;
  if (t.includes(',')) return +t.replace(/\./g, '').replace(',', '.');
  if (/^\d{1,3}(\.\d{3})+$/.test(t)) return +t.replace(/\./g, '');
  return +t;
}

const SKIP = new Set(['ARS', 'USD', 'US', 'BCBA', 'NYSE', 'CEDEAR', 'CEDEARS', 'T0', 'T1', 'T2', 'ON', 'FCI', 'TOTAL', 'PPC']);
// Texto pegado o escrito: un activo por línea, símbolo y cantidad (y opcional, precio de compra)
export function parseText(text, live = {}) {
  const rows = [], seen = new Set();
  for (const line of String(text).split(/\r?\n/)) {
    const toks = line.trim().split(/[\t ;|]+/).filter(Boolean);
    let sym = null; const nums = [];
    for (const tk of toks) {
      const up = tk.toUpperCase();
      if (!sym && /^[A-Z][A-Z0-9.]{1,9}$/i.test(tk) && !SKIP.has(up) && (live[up] || /^[A-Z][A-Z0-9]{1,7}$/.test(tk))) { sym = up; continue; }
      if (sym) { const n = num(tk); if (isFinite(n)) nums.push(n); }
    }
    if (!sym || !nums.length || nums[0] <= 0 || seen.has(sym)) continue;
    seen.add(sym);
    rows.push({symbol: sym, qty: nums[0], ppc: nums.length === 2 ? nums[1] : null, value: null, type: null});
  }
  return rows;
}

export function fromGeneric(j) {
  if (!j || j.formato !== FORMAT) throw new Error('El archivo no tiene el formato de cartera (traderpanel-cartera).');
  const rows = (j.activos || []).map(a => ({
    symbol: String(a.simbolo || '').toUpperCase().trim(), qty: +a.cantidad || 0,
    ppc: a.precio_compra != null && a.precio_compra !== '' ? +a.precio_compra : null,
    value: a.valor != null && a.valor !== '' ? +a.valor : null, type: a.tipo || null, desc: a.descripcion || '',
  })).filter(r => r.symbol && r.qty > 0);
  const cash = (j.efectivo || []).filter(c => +c.monto).map(c => ({moneda: String(c.moneda || 'ARS').toUpperCase(), monto: +c.monto}));
  return {rows, cash, broker: j.broker || '', fecha: j.fecha || ''};
}

export function toGeneric(rows, cash = [], meta = {}) {
  return {formato: FORMAT, version: 1, ...meta, activos: rows.map(r => ({
    simbolo: r.symbol, cantidad: r.qty, ...(r.ppc ? {precio_compra: r.ppc} : {}), ...(r.value ? {valor: r.value} : {}),
    ...(r.type ? {tipo: r.type} : {}), ...(r.desc ? {descripcion: r.desc} : {}),
  })), efectivo: cash};
}

/* ------------------------------------------------------------------ análisis de un activo */
const sma = (c, k) => c.length >= k ? c.slice(-k).reduce((s, v) => s + v, 0) / k : null;

// bars: [fecha, apertura, máximo, mínimo, cierre, volumen] de la más vieja a la más nueva.
// Mismas reglas que el motor: medias de 50 y 200, stop = mínimo de 10 ruedas (al menos 1,5 ATR,
// tope 12 %), objetivo a 2 veces lo arriesgado y la Time Machine de day-trader-skill a 10 ruedas.
export function analyzeBars(bars, price, {maxStopPct = 12, targetR = 2, gate = 'neutral', replays = 4000} = {}) {
  if (!bars || bars.length < 60) return {verdict: 'nodata', why: 'Sin historial suficiente para analizar la tendencia.'};
  price = price || bars.at(-1)[4];
  const c = bars.map(b => b[4]);
  const s20 = sma(c, 20), s50 = sma(c, 50), s200 = sma(c, 200);
  const r = rsi(c.slice(-260));
  const tags = findSetups(c.slice(-260));
  const last10 = bars.slice(-10), low10 = Math.min(...last10.map(b => b[3]));
  const trs = [];
  for (let i = bars.length - 14; i < bars.length; i++) {
    const b = bars[i], pc = bars[i - 1][4];
    trs.push(Math.max(b[2], pc) - Math.min(b[3], pc));
  }
  const atr = trs.reduce((s, v) => s + v, 0) / trs.length;
  const maxStop = maxStopPct / 100;
  let stop = Math.max(Math.min(low10, price - 1.5 * atr), price * (1 - maxStop));
  if (stop >= price) stop = price * (1 - maxStop / 2);
  const stopPct = (price - stop) / price * 100, target = price + targetR * (price - stop);

  const up = s50 != null && s200 != null && price > s50 && s50 > s200;
  const notes = [];
  let verdict, why;
  if (s50 != null && s200 != null && ((price < s50 && s50 < s200) || (price < s200 && price < s50))) {
    verdict = 'contra'; why = 'Está debajo de sus medias de 50 y 200 ruedas: la tendencia juega en contra.';
  } else if (s50 != null && price < s50) {
    verdict = 'ojo'; why = 'Está debajo de la media de 50 ruedas, que es donde el motor sale de una compra por tendencia.';
  } else if (up) {
    verdict = 'favor'; why = 'Está sobre sus medias de 50 y 200 ruedas y la de 50 va arriba de la de 200.';
  } else {
    verdict = 'ojo'; why = s200 == null ? 'Menos de 200 ruedas de historia: la tendencia larga no se puede medir.' : 'Sin tendencia clara: las medias no se ponen de acuerdo.';
  }
  if (r != null && r >= 75) { notes.push(`RSI ${Math.round(r)}: subió muy rápido, es común que corrija.`); if (verdict === 'favor') verdict = 'ojo'; }
  if (stopPct < 3) notes.push('El precio está muy cerca del mínimo de las últimas 10 ruedas.');

  let tm = null;
  try {
    const t = swingTimeMachine(bars, price, stop, target, {horizon: 10, replays});
    if (t.ok) tm = {lean: t.lean, lean_es: LEAN_ES[t.lean] || '', verdict: t.verdict, p_target: t.mc.p_target, p_stop: t.mc.p_stop};
  } catch { /* sin Time Machine */ }
  return {price, date: bars.at(-1)[0], sma20: s20, sma50: s50, sma200: s200, rsi: r, setups: tags.map(t => SETUP_ES[t] || t),
    stop, stop_pct: Math.round(stopPct * 100) / 100, target, verdict, why, notes, tm, gate, spark: c.slice(-120)};
}

/* ------------------------------------------------------------------ cartera completa */
// rows: [{symbol, qty, ppc, value, type}] ; live: {SYM: {type, c, pct}} ; bars: {SYM: [[...], ...]}
// gates: {adr|byma: {gate, why}} del motor (data/signals.json, público)
export function buildReport(rows, cash, {live = {}, bars = {}, gates = {}, maxPositionPct = 25, now = new Date()} = {}) {
  const holdings = rows.map(r => {
    const l = live[r.symbol];
    const type = l?.type || r.type || (r.symbol.startsWith('IOL') ? 'fci' : 'otro');
    const price = l?.c ?? null;
    const value = price != null ? r.qty * price / (PER_100.has(type) ? 100 : 1) : (r.value ?? null);
    const h = {symbol: r.symbol, desc: r.desc || '', type, type_es: TYPE_ES[type] || 'Otro', currency: '$', qty: r.qty, price, value,
      ppc: r.ppc ?? null, day_pct: l?.pct ?? null, gain_pct: null, gain: null};
    if (h.ppc && price) {
      h.gain_pct = Math.round((price / h.ppc - 1) * 10000) / 100;
      h.gain = value - r.qty * h.ppc / (PER_100.has(type) ? 100 : 1);
    }
    if (HIST_PATH[type]) {
      const g = gates[type === 'cedear' ? 'adr' : 'byma'] || {};
      h.analysis = analyzeBars(bars[r.symbol], price, {gate: g.gate || 'neutral'});
      if (h.analysis.stop_pct != null && value) h.analysis.at_risk = value * h.analysis.stop_pct / 100;
      h.ref = {kind: type === 'cedear' ? 'adr' : 'byma'};
    } else {
      h.analysis = {verdict: 'na', why: ['bono', 'on', 'letra'].includes(type)
        ? 'Renta fija: el análisis de tendencia no aplica. Lo que importa es la tasa (TIR), el vencimiento y el emisor.'
        : type === 'fci' ? 'Fondo común: se mira el rendimiento y el plazo de rescate, no la tendencia.' : 'Sin análisis para este tipo de activo.'};
    }
    return h;
  });
  const total = holdings.reduce((s, h) => s + (h.value || 0), 0);
  for (const h of holdings) h.weight = h.value && total ? Math.round(h.value / total * 10000) / 100 : null;
  const byType = {};
  for (const h of holdings) if (h.value) byType[h.type_es] = (byType[h.type_es] || 0) + h.value;
  const alerts = holdings.filter(h => h.weight > maxPositionPct && HIST_PATH[h.type] && !INDEX_ETF.has(h.symbol))
    .map(h => `${h.symbol} pesa ${Math.round(h.weight)} % de la cartera (el motor no pasa de ${maxPositionPct} % por activo).`);
  const atRisk = holdings.reduce((s, h) => s + (h.analysis.at_risk || 0), 0);
  const gains = holdings.filter(h => h.gain != null);
  const counts = {};
  for (const h of holdings) counts[h.analysis.verdict] = (counts[h.analysis.verdict] || 0) + 1;
  return {v: 1, generated_at: now.toISOString(), holdings,
    cash: cash.map(c => ({currency: c.moneda === 'USD' ? 'US$' : '$', amount: c.monto})),
    totals: {by_currency: {'$': total}, by_type: byType, at_risk: atRisk, at_risk_pct: total ? Math.round(atRisk / total * 10000) / 100 : null,
      gain: gains.length ? gains.reduce((s, h) => s + h.gain, 0) : null},
    counts, alerts, regime: gates, errors: holdings.filter(h => h.analysis.verdict === 'nodata').length};
}

// data912 → [fecha, o, h, l, c, v] de la más vieja a la más nueva
export function barsFromData912(arr) {
  return (Array.isArray(arr) ? arr : []).filter(r => r && r.date && +r.c > 0)
    .map(r => [r.date, +r.o || +r.c, +r.h || +r.c, +r.l || +r.c, +r.c, +r.v || 0]);
}

// Qué cambió desde la última vez que la abriste (se guarda solo en este navegador)
export function changesSince(prev, report) {
  if (!prev) return [];
  const out = [];
  for (const h of report.holdings) {
    const o = prev[h.symbol], v = h.analysis.verdict;
    if (o && o !== v && ['favor', 'ojo', 'contra'].includes(o) && ['favor', 'ojo', 'contra'].includes(v)) out.push({symbol: h.symbol, from: o, to: v});
  }
  return out;
}
