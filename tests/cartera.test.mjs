// Pruebas de Mi cartera (lib/cartera.mjs): node --test tests/
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {num, parseText, fromGeneric, toGeneric, analyzeBars, buildReport, barsFromData912, changesSince} from '../lib/cartera.mjs';

const plantilla = JSON.parse(await readFile(new URL('../docs/cartera-plantilla.json', import.meta.url), 'utf8'));

// Serie que sube: cierres de 100 a ~160 en 260 ruedas
function bars(n = 260, from = 100, step = 0.25) {
  const out = [];
  for (let i = 0; i < n; i++) { const c = from + i * step + Math.sin(i / 3); out.push([`2026-01-${i}`, c - 0.3, c + 1, c - 1, c, 1000]); }
  return out;
}

test('num entiende formatos argentinos', () => {
  assert.equal(num('1.234,56'), 1234.56);
  assert.equal(num('3.281'), 3281);
  assert.equal(num('4897.78'), 4897.78);
  assert.equal(num('$ 15.000'), 15000);
  assert.ok(Number.isNaN(num('abc')));
});

test('parseText: un activo por línea, ignora repetidos y palabras sueltas', () => {
  const rows = parseText('KO 13\nAL30 3.281 75.000\nKO 5\nTOTAL 999\nhola', {KO: {}, AL30: {}});
  assert.deepEqual(rows.map(r => [r.symbol, r.qty, r.ppc]), [['KO', 13, null], ['AL30', 3281, 75000]]);
});

test('la plantilla se importa completa, con efectivo', () => {
  const g = fromGeneric(plantilla);
  assert.ok(g.rows.length >= 4);
  assert.ok(g.rows.every(r => r.symbol && r.qty > 0));
  assert.ok(g.cash.some(c => c.moneda === 'ARS' && c.monto > 0));
  const back = fromGeneric(toGeneric(g.rows, g.cash));
  assert.deepEqual(back.rows.map(r => [r.symbol, r.qty]), g.rows.map(r => [r.symbol, r.qty]));
});

test('fromGeneric rechaza otro formato', () => {
  assert.throws(() => fromGeneric({activos: []}));
});

test('analyzeBars: tendencia a favor y stop razonable', () => {
  const b = bars(), price = b.at(-1)[4];
  const a = analyzeBars(b, price, {replays: 200});
  assert.ok(['favor', 'ojo'].includes(a.verdict));
  assert.ok(a.stop < price && a.stop_pct >= 1 && a.stop_pct <= 12, `stop ${a.stop_pct}`);
  assert.equal(a.spark.length, 120);
});

test('analyzeBars: pocas ruedas → sin datos', () => {
  assert.equal(analyzeBars(bars(30), 100).verdict, 'nodata');
});

test('buildReport: valores, renta fija cada 100 VN, fondos con valor y efectivo', () => {
  const rows = [{symbol: 'KO', qty: 10, ppc: 20000}, {symbol: 'AL30', qty: 1000}, {symbol: 'IOLCAMA', qty: 5, value: 50000}];
  const live = {KO: {type: 'cedear', c: 25000, pct: 1}, AL30: {type: 'bono', c: 80000, pct: -0.5}};
  const r = buildReport(rows, [{moneda: 'ARS', monto: 1000}], {live, bars: {KO: bars(260, 18000, 30)}, gates: {adr: {gate: 'restrictive', why: 'x'}}});
  const by = Object.fromEntries(r.holdings.map(h => [h.symbol, h]));
  assert.equal(by.KO.value, 250000);
  assert.equal(by.AL30.value, 800000);
  assert.equal(by.IOLCAMA.type, 'fci');
  assert.equal(by.IOLCAMA.value, 50000);
  assert.equal(by.AL30.analysis.verdict, 'na');
  assert.equal(by.KO.gain, 50000);
  assert.equal(r.totals.by_currency['$'], 1100000);
  assert.equal(r.cash[0].amount, 1000);
  assert.ok(r.alerts.some(a => a.startsWith('AL30')) === false, 'la renta fija no cuenta para concentración');
});

test('barsFromData912 y changesSince', () => {
  assert.deepEqual(barsFromData912([{date: 'd', o: 1, h: 2, l: 0.5, c: 1.5, v: 9}, {date: 'x', c: 0}]), [['d', 1, 2, 0.5, 1.5, 9]]);
  const rep = {holdings: [{symbol: 'KO', analysis: {verdict: 'contra'}}, {symbol: 'AL30', analysis: {verdict: 'na'}}]};
  assert.deepEqual(changesSince({KO: 'favor', AL30: 'na'}, rep), [{symbol: 'KO', from: 'favor', to: 'contra'}]);
  assert.deepEqual(changesSince(null, rep), []);
});

/* ---------- avisos (lib/avisos.mjs) y sealed box (lib/sealbox.mjs) ---------- */
import {messagesFor, stateFor} from '../lib/avisos.mjs';
import {blake2b, seal} from '../lib/sealbox.mjs';
import {createHash} from 'node:crypto';
import {createRequire} from 'node:module';

const rep = (v, price = 100, stop = 90) => ({generated_at: 't', counts: {favor: 1}, holdings: [{symbol: 'KO', price, analysis: {verdict: v, why: 'porque sí', stop}}]});

test('avisos: la primera vez solo da la bienvenida', () => {
  const m = messagesFor(rep('favor'), null);
  assert.equal(m.length, 1);
  assert.match(m[0], /activados/);
});

test('avisos: cambio de veredicto y stop tocado', () => {
  const prev = stateFor(rep('favor'));
  assert.deepEqual(messagesFor(rep('favor', 95), prev), []);
  const m = messagesFor(rep('contra', 89), prev);
  assert.equal(m.length, 2);
  assert.match(m[0], /KO: pasó de a favor ✓ a en contra ✕/);
  assert.match(m[1], /tocó su stop/);
});

test('BLAKE2b coincide con el de Node', () => {
  for (const n of [0, 1, 64, 128, 129, 1000]) {
    const x = new Uint8Array(n).map((_, i) => i * 31);
    assert.equal(Buffer.from(blake2b(x, 64)).toString('hex'), createHash('blake2b512').update(x).digest('hex'));
  }
});

test('sealed box: el destinatario la abre', () => {
  const nacl = createRequire(import.meta.url)('../lib/vendor/nacl-fast.min.js');
  const kp = nacl.box.keyPair(), msg = new TextEncoder().encode('123:token');
  const c = seal(nacl, msg, kp.publicKey);
  const epk = c.subarray(0, 32), nin = new Uint8Array(64); nin.set(epk); nin.set(kp.publicKey, 32);
  const open = nacl.box.open(c.subarray(32), blake2b(nin, 24), epk, kp.secretKey);
  assert.equal(new TextDecoder().decode(open), '123:token');
});
