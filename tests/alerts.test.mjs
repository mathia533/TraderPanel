// Pruebas del envío de alertas: node --test tests/
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {mkdtemp, mkdir, writeFile, readFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, resolve, dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {execFile} from 'node:child_process';
import {sizeFor, tradeEvents, wants, textFor} from '../alerts/send.mjs';

const SEND = resolve(dirname(fileURLToPath(import.meta.url)), '../alerts/send.mjs');
const risk = (b) => ({adr: {budget: b, max_loss_pct: 1, max_position_pct: 25}, byma: {budget: b * 1000, max_loss_pct: 1, max_position_pct: 25}, cripto: {budget: b, max_loss_pct: 1, max_position_pct: 25, fractional: true}});
const tgPrefs = (extra) => ({chat: 'default', engine: true, follow: [], types: {entry: true, exit: true, update: false}, kinds: {adr: true, byma: true, cripto: true}, only: [], ...extra});

test('el tamaño sigue las reglas del motor (pérdida máxima y tope de posición)', () => {
  assert.equal(sizeFor({budget: 10000, max_loss_pct: 1, max_position_pct: 25}, 100, 95).units, 20);
  assert.equal(sizeFor({budget: 10000, max_loss_pct: 5, max_position_pct: 10}, 100, 95).units, 10);
  assert.equal(sizeFor({budget: 1000, max_loss_pct: 1, max_position_pct: 25, fractional: true}, 50000, 48000).units, 0.005);
  assert.equal(sizeFor({budget: 10000, max_loss_pct: 1, max_position_pct: 25}, 100, 101), null);
});

test('una operación registrada genera entrada, ajustes de stop y salida', () => {
  const ev = tradeEvents('amigo', 'Juan', [{id: 't-ypf-1', kind: 'adr', asset: 'YPF', entry: 30, stop: 27, target: 36, opened_at: '2026-11-25T14:00:00Z',
    stop_history: [{at: '2026-11-26T14:00:00Z', stop: 30}], exit: 36, exit_reason: 'target', closed_at: '2026-11-27T14:00:00Z'}]);
  assert.deepEqual(ev.map(e => e.id), ['trade:amigo:t-ypf-1:entry', 'trade:amigo:t-ypf-1:stop:0', 'trade:amigo:t-ypf-1:exit']);
  const yo = {login: 'yo', risk: risk(10000), max_open_positions: 1, telegram: tgPrefs({follow: ['amigo']})};
  const otro = {login: 'otro', risk: risk(2000), max_open_positions: 8, telegram: tgPrefs()};
  assert.ok(wants(yo, ev[0]) && wants(yo, ev[1]));         // los ajustes del que sigue llegan aunque update=false
  assert.ok(!wants(otro, ev[0]));                           // no lo sigue
  const t = textFor(ev[0], yo, {panel_url: ''}, {open: 2});
  assert.match(t, /Juan<\/b> <b>COMPRÓ YPF/);
  assert.match(t, /Para vos:<\/b> 33 acciones/);            // 100 USD de riesgo / 3 por acción
  assert.match(t, /tu tope es 1/);
  assert.match(textFor(ev[2], yo, {}), /\+US\$ 198,00 con 33 acciones/);
  assert.match(textFor(ev[0], otro, {}), /6 acciones/);     // 20 USD / 3
});

test('noticias, balances y resúmenes (info) llegan salvo que se apaguen', () => {
  const info = {src: 'engine', id: 'weekly:2027-01-04', type: 'info', kind: 'adr', label: 'Semana', text: '🗓 <b>RESUMEN SEMANAL</b>'};
  const flat = {src: 'engine', id: 'x', type: 'exit', reason: 'flat', kind: 'adr', label: 'YPF', price: 31, entry: 30, initial_stop: 29.5, text: '🔔 <b>SALIDA · YPF</b>'};
  const u = {login: 'yo', risk: risk(10000), telegram: tgPrefs()};
  assert.ok(wants(u, info) && wants(u, flat));
  assert.ok(!wants({...u, telegram: tgPrefs({types: {entry: true, exit: true, update: false, info: false}})}, info));
  assert.match(textFor(info, u, {}), /RESUMEN SEMANAL/);
});

test('envía a cada usuario con su tamaño y no repite', async () => {
  const got = [];
  const srv = createServer((req, res) => { let b = ''; req.on('data', c => b += c); req.on('end', () => { got.push(JSON.parse(b)); res.setHeader('content-type', 'application/json'); res.end('{"ok":true}'); }); });
  await new Promise(r => srv.listen(0, r));
  const dir = await mkdtemp(join(tmpdir(), 'tp-'));
  for (const d of ['data', 'users', 'trades', 'alerts']) await mkdir(join(dir, d));
  const now = new Date().toISOString();
  await writeFile(join(dir, 'alerts/telegram.json'), JSON.stringify({max_age_hours: 24, panel_url: ''}));
  await writeFile(join(dir, 'users/users.json'), JSON.stringify({users: {
    yo: {name: 'Mathias', risk: risk(10000), max_open_positions: 8, telegram: tgPrefs({follow: ['amigo']})},
    amigo: {name: 'Juan', risk: risk(2000), max_open_positions: 8, telegram: tgPrefs({chat: '-100222'})}}}));
  // Registro previo (formato viejo, sin perfiles): el usuario principal no es nuevo, el amigo sí
  await writeFile(join(dir, 'data/telegram_sent.json'), JSON.stringify({sent: {'viejo': {at: now, status: 'enviado'}}, recent: []}));
  await writeFile(join(dir, 'data/events.json'), JSON.stringify({events: [{id: 'adr:GGAL:entry:x', type: 'entry', kind: 'adr', label: 'GGAL', price: 50, stop: 45, ts: now,
    text: '🟢 <b>ENTRADA · GGAL</b>\nTamaño sugerido: 20 (US$ 1.000)\n<i>VCP</i>'}]}));
  await writeFile(join(dir, 'trades/amigo.json'), JSON.stringify({trades: [{id: 't-ypf-1', kind: 'adr', asset: 'YPF', entry: 30, stop: 27, opened_at: now}]}));
  const run = (...a) => new Promise((ok, ko) => execFile('node', [SEND, '--data-dir', join(dir, 'data'), '--config', join(dir, 'alerts/telegram.json'),
    '--users', join(dir, 'users/users.json'), '--trades-dir', join(dir, 'trades'), ...a],
    {env: {...process.env, TELEGRAM_BOT_TOKEN: 'x', TELEGRAM_CHAT_ID: '-100111', TELEGRAM_API: `http://127.0.0.1:${srv.address().port}`, GITHUB_REPOSITORY_OWNER: 'yo'}},
    (err, out, errOut) => err ? ko(new Error(errOut || err.message)) : ok(out)));
  await run();
  assert.equal(got.length, 2, JSON.stringify(got));         // a mí: señal del motor + operación del amigo; al amigo nada (es nuevo)
  assert.ok(got.every(m => m.chat_id === '-100111'));
  const sig = got.find(m => m.text.includes('GGAL')).text;
  assert.ok(!sig.includes('Tamaño sugerido') && sig.includes('20 acciones'));
  assert.match(got.find(m => m.text.includes('YPF')).text, /Juan.*COMPRÓ YPF[\s\S]*33 acciones/);
  const reg = JSON.parse(await readFile(join(dir, 'data/telegram_sent.json'), 'utf8'));
  assert.ok(reg.sent['yo|viejo'] && reg.users.amigo);

  // El amigo cierra: me llega la salida, una sola vez
  await writeFile(join(dir, 'trades/amigo.json'), JSON.stringify({trades: [{id: 't-ypf-1', kind: 'adr', asset: 'YPF', entry: 30, stop: 27, opened_at: now, exit: 33, exit_reason: 'manual', closed_at: now}]}));
  got.length = 0; await run(); await run();
  assert.equal(got.length, 1);
  assert.match(got[0].text, /VENDIÓ YPF[\s\S]*\+US\$ 99,00 con 33 acciones/);
  srv.close();
});

test('el informe del cierre sale como foto con el texto de pie (y como texto si no hay tarjeta)', async () => {
  const hits = [];
  const srv = createServer((req, res) => { let b = Buffer.alloc(0); req.on('data', c => b = Buffer.concat([b, c])); req.on('end', () => {
    hits.push({path: req.url, type: req.headers['content-type'] || '', body: b.toString('latin1')}); res.setHeader('content-type', 'application/json'); res.end('{"ok":true}'); }); });
  await new Promise(r => srv.listen(0, r));
  const dir = await mkdtemp(join(tmpdir(), 'tp-card-'));
  for (const d of ['data', 'users', 'trades', 'alerts', 'cards']) await mkdir(join(dir, d));
  const now = new Date().toISOString();
  await writeFile(join(dir, 'alerts/telegram.json'), JSON.stringify({panel_url: ''}));
  await writeFile(join(dir, 'users/users.json'), JSON.stringify({users: {yo: {name: 'Yo', risk: risk(10000), telegram: tgPrefs()}}}));
  await writeFile(join(dir, 'data/telegram_sent.json'), JSON.stringify({sent: {}, recent: [], users: {yo: now}}));
  await writeFile(join(dir, 'data/events.json'), JSON.stringify({events: [
    {id: 'report:2026-11-25', type: 'info', info: 'report', kind: 'adr', label: 'Cierre', ts: now, text: '🌙 <b>INFORME DEL CIERRE</b>'},
    {id: 'report:2026-11-24', type: 'info', info: 'report', kind: 'adr', label: 'Cierre', ts: now, text: '🌙 <b>INFORME</b> sin tarjeta'}]}));
  await writeFile(join(dir, 'cards/report-2026-11-25.png'), Buffer.from([0x89, 0x50, 0x4e, 0x47]));
  await new Promise((ok, ko) => execFile('node', [SEND, '--data-dir', join(dir, 'data'), '--config', join(dir, 'alerts/telegram.json'),
    '--users', join(dir, 'users/users.json'), '--trades-dir', join(dir, 'trades'), '--cards-dir', join(dir, 'cards')],
    {env: {...process.env, TELEGRAM_BOT_TOKEN: 'x', TELEGRAM_CHAT_ID: '-100111', TELEGRAM_API: `http://127.0.0.1:${srv.address().port}`}},
    (err, out, errOut) => err ? ko(new Error(errOut || err.message)) : ok(out)));
  srv.close();
  const photo = hits.find(h => h.path.endsWith('/sendPhoto')), msg = hits.find(h => h.path.endsWith('/sendMessage'));
  assert.ok(photo && photo.type.startsWith('multipart/form-data'));
  assert.match(photo.body, /name="caption"/);
  assert.match(photo.body, /report-2026-11-25\.png/);
  assert.ok(msg && msg.body.includes('sin tarjeta'));
});
