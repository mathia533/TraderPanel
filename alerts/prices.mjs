#!/usr/bin/env node
// Alertas de precio personales: cada usuario elige en alertas.html un activo y un nivel
// ("avisame si YPF sube a 35 o más", "si BTC cae 5 % en el día") y le llega un mensaje
// a SU chat de Telegram cuando se cumple. No pasa por el motor de señales.
//
// De dónde salen:  avisos/<login>.json   (lo escribe alertas.html con el token del usuario)
// A qué chat van:  users/users.json → telegram.chat del mismo login ("default" = TELEGRAM_CHAT_ID)
// Estado:          data/avisos_estado.json (qué alerta ya saltó y cuándo; solo cambia si salta
//                  o aparece una alerta nueva, así el Action no hace un commit cada 10 minutos)
// Precios:         data912 (BYMA y ADRs), CoinGecko (cripto), DolarAPI (dólar), en vivo.
//
// Uso: node alerts/prices.mjs [--dry-run] [--avisos-dir d] [--users f] [--data-dir d]
// Variables: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID. PRICES_FILE=<json> usa precios fijos (pruebas).

import {readFile, writeFile, mkdir, readdir} from 'node:fs/promises';
import {dirname, join, resolve, basename} from 'node:path';
import {fileURLToPath} from 'node:url';
import {tg, chatOf} from './send.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);
const opt = (name, def) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : def; };
const DRY = args.includes('--dry-run');
const AVISOS = resolve(opt('--avisos-dir', join(ROOT, 'avisos')));
const USERS = resolve(opt('--users', join(ROOT, 'users', 'users.json')));
const DATA = resolve(opt('--data-dir', join(ROOT, 'data')));
const CONFIG = resolve(opt('--config', join(ROOT, 'alerts', 'telegram.json')));
const TOKEN = (process.env.TELEGRAM_BOT_TOKEN || '').trim();
const LOGIN_RE = /^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$/;
export const MAX_PER_USER = 30;
const HYST = 0.003; // una alerta repetida se vuelve a armar cuando el precio se aleja 0,3 % del nivel

export const KINDS = {adr: 'ADR', byma: 'BYMA', cripto: 'Cripto', dolar: 'Dólar'};
export const DOLARES = {oficial: 'Oficial', blue: 'Blue', bolsa: 'MEP', contadoconliqui: 'CCL', mayorista: 'Mayorista', cripto: 'Cripto', tarjeta: 'Tarjeta'};
export const CONDS = {gte: 'sube a', lte: 'baja a', chg_up: 'sube en el día', chg_dn: 'baja en el día'};
const ASSET = {adr: /^[A-Z][A-Z0-9.\-]{0,9}$/, byma: /^[A-Z][A-Z0-9.\-]{0,9}$/, cripto: /^[a-z0-9][a-z0-9\-]{0,49}$/};

const esc = s => String(s ?? '').replace(/[&<>]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;'}[c]));
const fmt = (v, d) => (v == null || !isFinite(v)) ? '—' : Number(v).toLocaleString('es-AR', {minimumFractionDigits: d, maximumFractionDigits: d});
const px = v => fmt(v, Math.abs(v) >= 1000 ? 0 : Math.abs(v) >= 1 ? 2 : 4);
const cur = kind => kind === 'byma' || kind === 'dolar' ? '$' : 'US$';
const sleep = ms => new Promise(r => setTimeout(r, ms));
const dayAR = t => new Date(t).toLocaleDateString('en-CA', {timeZone: 'America/Argentina/Buenos_Aires'});

async function readJSON(file, def) {
  try { return JSON.parse(await readFile(file, 'utf8')); }
  catch (e) { if (e.code !== 'ENOENT') console.warn(`No pude leer ${file}: ${e.message}`); return def; }
}

/** Valida una alerta tal como la guarda alertas.html. Devuelve null si está bien o el motivo. */
export function invalid(a) {
  if (!a || typeof a !== 'object' || typeof a.id !== 'string' || !a.id) return 'sin id';
  if (!KINDS[a.kind]) return 'mercado desconocido';
  if (a.kind === 'dolar' ? !DOLARES[a.asset] : !ASSET[a.kind].test(String(a.asset || ''))) return 'activo inválido';
  if (!CONDS[a.cond]) return 'condición desconocida';
  if (a.kind === 'dolar' && a.cond.startsWith('chg')) return 'el dólar solo admite niveles de precio';
  if (!(Number(a.value) > 0) || (a.cond.startsWith('chg') && a.value > 100)) return 'valor inválido';
  return null;
}
export const keyOf = (login, a) => `${login}|${a.id}|${a.cond}|${a.value}`; // si se edita el nivel, se vuelve a armar
const met = (a, p) => a.cond === 'gte' ? p.last >= a.value : a.cond === 'lte' ? p.last <= a.value
  : a.cond === 'chg_up' ? p.ch != null && p.ch >= a.value : p.ch != null && p.ch <= -a.value;
const away = (a, p) => a.cond === 'gte' ? p.last < a.value * (1 - HYST) : p.last > a.value * (1 + HYST);

/**
 * Revisa todas las alertas contra los precios. Puro: no envía ni escribe nada.
 * byUser: {login: [alerta]}, prices: {"kind:asset": {last, ch}}, state: {items: {...}}.
 * Devuelve {fire: [{login, alert, price, key}], state}.
 *  - Una vez (repeat=false): salta la primera vez que se cumple y queda cumplida.
 *  - Repetir: un nivel de precio se vuelve a armar cuando el precio se aleja un 0,3 %;
 *    una variación diaria, al día siguiente (hora de Buenos Aires).
 */
export function evaluate(byUser, prices, state, now = Date.now()) {
  const items = {...(state?.items || {})};
  const seen = new Set(), fire = [];
  const today = dayAR(now);
  for (const [login, list] of Object.entries(byUser)) {
    for (const a of list) {
      if (invalid(a)) continue;
      const key = keyOf(login, a); seen.add(key);
      const st = {...(items[key] || {armed: true, count: 0})};
      items[key] = st;
      if (a.active === false || st.done) continue;
      const p = prices[`${a.kind}:${a.asset}`];
      if (!p || !(p.last > 0)) continue;
      const daily = a.cond.startsWith('chg');
      if (!st.armed && a.repeat) {
        if (daily ? st.day !== today : away(a, p)) st.armed = true;
      }
      if (st.armed && met(a, p)) {
        fire.push({login, alert: a, price: p, key});
        Object.assign(st, {armed: false, fired_at: new Date(now).toISOString(), fired_price: p.last, day: today, count: (st.count || 0) + 1});
        if (!a.repeat) st.done = true;
      }
    }
  }
  for (const k of Object.keys(items)) if (!seen.has(k)) delete items[k]; // alertas borradas
  return {fire, state: {items}};
}

export function textFor(f, name, cfg = {}) {
  const a = f.alert, p = f.price, c = cur(a.kind);
  const label = a.kind === 'dolar' ? `Dólar ${DOLARES[a.asset]}` : a.kind === 'cripto' ? (a.label || a.asset).toUpperCase() : a.asset;
  const ch = p.ch == null ? '' : ` · ${p.ch >= 0 ? '+' : '−'}${fmt(Math.abs(p.ch), 1)} % en el día`;
  const what = a.cond === 'gte' ? `Subió a <b>${c} ${px(p.last)}</b> (tu nivel: ${c} ${px(a.value)})`
    : a.cond === 'lte' ? `Bajó a <b>${c} ${px(p.last)}</b> (tu nivel: ${c} ${px(a.value)})`
    : `${a.cond === 'chg_up' ? 'Sube' : 'Baja'} más de ${fmt(a.value, 1)} % en el día: <b>${c} ${px(p.last)}</b>`;
  const lines = [`🔔 <b>Alerta de precio · ${esc(label)}</b> <i>${KINDS[a.kind]}</i>`, what + (a.cond.startsWith('chg') ? '' : ch)];
  if (a.note) lines.push(`📝 ${esc(String(a.note).slice(0, 200))}`);
  lines.push(a.repeat ? `<i>Te vuelvo a avisar si lo cruza de nuevo${a.cond.startsWith('chg') ? ' otro día' : ''}.</i>` : '<i>Era de una sola vez: ya quedó cumplida.</i>');
  if (name) lines.push(`<i>Alerta de ${esc(name)}</i>`);
  if (cfg.panel_url && a.kind !== 'dolar') lines.push(`<a href="${esc(cfg.panel_url)}#ficha=${encodeURIComponent(a.kind + ':' + a.asset)}">Ver la ficha</a>`);
  return lines.join('\n');
}

/* ---------- Precios en vivo ---------- */
async function getJSON(url) {
  const r = await fetch(url, {headers: {accept: 'application/json', 'user-agent': 'TraderPanel'}, signal: AbortSignal.timeout(20000)});
  if (!r.ok) throw new Error(`HTTP ${r.status} en ${new URL(url).host}`);
  return r.json();
}
export async function fetchPrices(need) {
  const out = {}, kinds = new Set(need.map(k => k.split(':')[0]));
  const add = (k, last, ch) => { if (Number(last) > 0) out[k] = {last: Number(last), ch: ch == null || !isFinite(ch) ? null : Number(ch)}; };
  const jobs = [];
  for (const kind of ['byma', 'adr']) if (kinds.has(kind)) jobs.push((async () => {
    const urls = kind === 'byma' ? ['https://data912.com/live/arg_stocks'] : ['https://data912.com/live/usa_adrs', 'https://data912.com/live/usa_stocks'];
    for (const u of urls) {
      try { for (const r of await getJSON(u)) { const k = `${kind}:${String(r.symbol || '').toUpperCase()}`; if (!out[k]) add(k, r.c, r.pct_change); } }
      catch (e) { console.warn(`Precios ${kind}: ${e.message}`); }
    }
  })());
  if (kinds.has('cripto')) jobs.push((async () => {
    const ids = [...new Set(need.filter(k => k.startsWith('cripto:')).map(k => k.slice(7)))];
    try {
      const j = await getJSON(`https://api.coingecko.com/api/v3/simple/price?ids=${ids.map(encodeURIComponent).join(',')}&vs_currencies=usd&include_24hr_change=true`);
      for (const id of ids) if (j[id]) add(`cripto:${id}`, j[id].usd, j[id].usd_24h_change);
    } catch (e) { console.warn(`Precios cripto: ${e.message}`); }
  })());
  if (kinds.has('dolar')) jobs.push((async () => {
    try { for (const d of await getJSON('https://dolarapi.com/v1/dolares')) add(`dolar:${d.casa}`, d.venta, null); }
    catch (e) { console.warn(`Precios dólar: ${e.message}`); }
  })());
  await Promise.all(jobs);
  return out;
}

async function loadAvisos() {
  const out = {};
  let files = [];
  try { files = await readdir(AVISOS); } catch (e) { if (e.code !== 'ENOENT') throw e; }
  for (const f of files.filter(f => f.endsWith('.json')).sort()) {
    const login = basename(f, '.json');
    if (!LOGIN_RE.test(login)) { console.warn(`Ignoro avisos/${f}: el nombre tiene que ser un login de GitHub.`); continue; }
    const j = await readJSON(join(AVISOS, f), null);
    const list = Array.isArray(j?.avisos) ? j.avisos : [];
    const ok = list.filter(a => { const why = invalid(a); if (why) console.warn(`avisos/${f} · ${a?.id || '?'}: ${why}`); return !why; });
    out[login] = ok.slice(0, MAX_PER_USER);
  }
  return out;
}

async function main() {
  const byUser = await loadAvisos();
  const active = Object.values(byUser).flat().filter(a => a.active !== false);
  const stateFile = join(DATA, 'avisos_estado.json');
  const prev = await readJSON(stateFile, {items: {}});
  const need = [...new Set(active.map(a => `${a.kind}:${a.asset}`))];
  const prices = !need.length ? {} : process.env.PRICES_FILE ? await readJSON(process.env.PRICES_FILE, {}) : await fetchPrices(need);
  const {fire, state} = evaluate(byUser, prices, prev);

  const cfg = await readJSON(CONFIG, {});
  const users = (await readJSON(USERS, {}))?.users || {};
  let sent = 0, failed = 0;
  for (const f of fire) {
    const u = {login: f.login, ...(users[f.login] || {})}, chat = chatOf(u), st = state.items[f.key];
    if (!TOKEN || !chat) { st.status = 'sin_telegram'; continue; }
    const text = textFor(f, u.name || f.login, cfg);
    if (DRY) { console.log(`\n--- ${f.login} · ${f.alert.id} ---\n${text}`); st.status = 'dry'; continue; }
    const r = await tg(chat, text);
    if (r.ok) { st.status = 'enviado'; sent++; }
    else { failed++; Object.assign(st, {status: 'fallo', armed: true, done: false}); } // se reintenta en la próxima corrida
    await sleep(1100);
  }
  const missing = need.filter(k => !prices[k]);
  console.log(`Alertas activas: ${active.length} · activos: ${need.length} (sin precio: ${missing.join(', ') || 'ninguno'}) · saltaron: ${fire.length} · enviadas: ${sent} · fallidas: ${failed}`);
  if (!DRY && JSON.stringify(state) !== JSON.stringify({items: prev.items || {}})) {
    await mkdir(DATA, {recursive: true});
    await writeFile(stateFile, JSON.stringify(state, null, 1) + '\n');
  }
  if (failed) process.exitCode = 1;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url))
  main().catch(e => { console.error(e.message); process.exit(1); });
