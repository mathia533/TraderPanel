// Pruebas de las alertas de precio personales: node --test tests/
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {mkdtemp, mkdir, writeFile, readFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, resolve, dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {execFile} from 'node:child_process';
import {evaluate, invalid, textFor, keyOf} from '../alerts/prices.mjs';

const PRICES = resolve(dirname(fileURLToPath(import.meta.url)), '../alerts/prices.mjs');
const A = (x) => ({id: 'a1', kind: 'byma', asset: 'YPF', cond: 'gte', value: 100, repeat: false, ...x});
const T0 = Date.parse('2026-11-25T15:00:00Z');

test('valida lo que guarda alertas.html', () => {
  assert.equal(invalid(A()), null);
  assert.equal(invalid(A({kind: 'cripto', asset: 'bitcoin', cond: 'chg_dn', value: 5})), null);
  assert.equal(invalid(A({kind: 'dolar', asset: 'blue', cond: 'lte', value: 1200})), null);
  assert.ok(invalid(A({kind: 'dolar', asset: 'blue', cond: 'chg_up'})));     // el dólar no trae variación
  assert.ok(invalid(A({asset: 'ypf'})));
  assert.ok(invalid(A({value: -1})));
  assert.ok(invalid(A({cond: 'chg_up', value: 400})));
  assert.ok(invalid(A({kind: 'acciones'})));
});

test('una alerta de una vez salta una sola vez', () => {
  const u = {yo: [A()]};
  let r = evaluate(u, {'byma:YPF': {last: 99, ch: 1}}, {}, T0);
  assert.equal(r.fire.length, 0);
  r = evaluate(u, {'byma:YPF': {last: 101, ch: 3}}, r.state, T0 + 6e5);
  assert.equal(r.fire.length, 1);
  assert.equal(r.state.items[keyOf('yo', A())].fired_price, 101);
  r = evaluate(u, {'byma:YPF': {last: 90}}, r.state, T0 + 12e5);
  r = evaluate(u, {'byma:YPF': {last: 105}}, r.state, T0 + 18e5);
  assert.equal(r.fire.length, 0);
});

test('repetir: el nivel se vuelve a armar cuando el precio se aleja; la variación, al otro día', () => {
  const u = {yo: [A({repeat: true})]};
  let r = evaluate(u, {'byma:YPF': {last: 101}}, {}, T0);
  assert.equal(r.fire.length, 1);
  r = evaluate(u, {'byma:YPF': {last: 99.9}}, r.state, T0 + 6e5);  // a menos de 0,3 %: sigue desarmada
  r = evaluate(u, {'byma:YPF': {last: 100.2}}, r.state, T0 + 12e5);
  assert.equal(r.fire.length, 0);
  r = evaluate(u, {'byma:YPF': {last: 99}}, r.state, T0 + 18e5);
  r = evaluate(u, {'byma:YPF': {last: 100.5}}, r.state, T0 + 24e5);
  assert.equal(r.fire.length, 1);

  const d = {yo: [A({cond: 'chg_dn', value: 5, repeat: true, kind: 'cripto', asset: 'bitcoin'})]};
  r = evaluate(d, {'cripto:bitcoin': {last: 90000, ch: -6}}, {}, T0);
  assert.equal(r.fire.length, 1);
  r = evaluate(d, {'cripto:bitcoin': {last: 89000, ch: -7}}, r.state, T0 + 36e5);
  assert.equal(r.fire.length, 0);
  r = evaluate(d, {'cripto:bitcoin': {last: 88000, ch: -5.5}}, r.state, T0 + 864e5);
  assert.equal(r.fire.length, 1);
});

test('pausadas no saltan, editar el nivel la rearma y borrar limpia el estado', () => {
  let r = evaluate({yo: [A({active: false})]}, {'byma:YPF': {last: 120}}, {}, T0);
  assert.equal(r.fire.length, 0);
  r = evaluate({yo: [A()]}, {'byma:YPF': {last: 120}}, r.state, T0);
  assert.equal(r.fire.length, 1);
  r = evaluate({yo: [A({value: 110})]}, {'byma:YPF': {last: 120}}, r.state, T0);
  assert.equal(r.fire.length, 1);
  assert.deepEqual(Object.keys(r.state.items), [keyOf('yo', A({value: 110}))]);
  r = evaluate({yo: []}, {}, r.state, T0);
  assert.deepEqual(r.state.items, {});
});

test('el mensaje dice el nivel, la variación y el link a la ficha', () => {
  const t = textFor({alert: A({note: 'zona de compra'}), price: {last: 101.5, ch: 2.34}}, 'Mathias', {panel_url: 'https://x.github.io/TP/'});
  assert.match(t, /Alerta de precio · YPF/);
  assert.match(t, /Subió a <b>\$ 101,50<\/b> \(tu nivel: \$ 100,00\) · \+2,3 % en el día/);
  assert.match(t, /zona de compra/);
  assert.match(t, /#ficha=byma%3AYPF/);
  const d = textFor({alert: A({kind: 'dolar', asset: 'blue', cond: 'lte', value: 1200}), price: {last: 1190, ch: null}}, '', {panel_url: 'u'});
  assert.match(d, /Dólar Blue/);
  assert.ok(!d.includes('ficha'));
});

test('de punta a punta: cada alerta va al chat de su dueño', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'tp-precios-'));
  for (const d of ['data', 'users', 'avisos', 'alerts']) await mkdir(join(dir, d), {recursive: true});
  const got = [];
  const srv = createServer((req, res) => { let b = ''; req.on('data', c => b += c); req.on('end', () => { got.push(JSON.parse(b)); res.setHeader('content-type', 'application/json'); res.end('{"ok":true}'); }); });
  await new Promise(r => srv.listen(0, '127.0.0.1', r));
  await writeFile(join(dir, 'alerts/telegram.json'), JSON.stringify({panel_url: ''}));
  await writeFile(join(dir, 'users/users.json'), JSON.stringify({users: {yo: {name: 'Mathias', telegram: {chat: 'default'}}, amigo: {name: 'Juan', telegram: {chat: '-100222'}}, mudo: {telegram: {chat: ''}}}}));
  await writeFile(join(dir, 'avisos/yo.json'), JSON.stringify({avisos: [A(), A({id: 'a2', asset: 'GGAL', value: 9999})]}));
  await writeFile(join(dir, 'avisos/amigo.json'), JSON.stringify({avisos: [A({id: 'b1', kind: 'cripto', asset: 'bitcoin', cond: 'chg_up', value: 3, label: 'btc'})]}));
  await writeFile(join(dir, 'avisos/mudo.json'), JSON.stringify({avisos: [A({id: 'c1'})]}));
  await writeFile(join(dir, 'precios.json'), JSON.stringify({'byma:YPF': {last: 104, ch: 4}, 'byma:GGAL': {last: 5000, ch: 1}, 'cripto:bitcoin': {last: 95000, ch: 3.5}}));
  const run = () => new Promise((ok, ko) => execFile('node', [PRICES, '--data-dir', join(dir, 'data'), '--users', join(dir, 'users/users.json'),
    '--avisos-dir', join(dir, 'avisos'), '--config', join(dir, 'alerts/telegram.json')],
    {env: {...process.env, TELEGRAM_BOT_TOKEN: 'x', TELEGRAM_CHAT_ID: '-100111', TELEGRAM_API: `http://127.0.0.1:${srv.address().port}`, PRICES_FILE: join(dir, 'precios.json')}},
    (err, out, errOut) => err ? ko(new Error(errOut || err.message)) : ok(out)));
  await run();
  assert.deepEqual(got.map(m => m.chat_id).sort(), ['-100111', '-100222']);
  assert.match(got.find(m => m.chat_id === '-100222').text, /BTC[\s\S]*Sube más de 3,0 % en el día/);
  const st = JSON.parse(await readFile(join(dir, 'data/avisos_estado.json'), 'utf8'));
  assert.equal(st.items[keyOf('mudo', A({id: 'c1'}))].status, 'sin_telegram');
  got.length = 0; await run();
  assert.equal(got.length, 0);                                 // no se repite
  srv.close();
});
