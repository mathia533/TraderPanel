#!/usr/bin/env node
// Envía a Telegram los avisos nuevos, a cada usuario con su propio tamaño de posición.
//
// De dónde salen los avisos:
//   - data/events.json   entradas y salidas del motor de señales (comunes a todos)
//   - trades/<login>.json operaciones que registra cada usuario desde operaciones.html
// A quién van: users/users.json (un perfil por login de GitHub, con presupuesto, tope,
// qué avisos quiere, a qué chat y a quién sigue). Cada aviso se manda una sola vez por
// usuario: el registro queda en data/telegram_sent.json con claves "login|id".
//
// Uso:
//   node alerts/send.mjs                corrida normal
//   node alerts/send.mjs --dry-run      muestra qué mandaría, sin enviar ni guardar
//   node alerts/send.mjs --test         manda un mensaje de prueba a cada chat y termina
//   node alerts/send.mjs --send-backlog a un usuario nuevo le manda también lo que ya había
//   node alerts/send.mjs --report       si no hay alertas nuevas, manda un resumen al chat principal
// Variables: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (chat "default"). Opciones: --data-dir,
// --config, --users, --trades-dir, --engine-config <archivo>.

import {readFile, writeFile, mkdir, readdir} from 'node:fs/promises';
import {dirname, join, resolve, basename} from 'node:path';
import {fileURLToPath} from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);
const opt = (name, def) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : def; };
const DRY = args.includes('--dry-run');
const TEST = args.includes('--test');
const BACKLOG = args.includes('--send-backlog');
const REPORT = args.includes('--report');
const DATA = resolve(opt('--data-dir', join(ROOT, 'data')));
const CONFIG = resolve(opt('--config', join(ROOT, 'alerts', 'telegram.json')));
const USERS = resolve(opt('--users', join(ROOT, 'users', 'users.json')));
const TRADES = resolve(opt('--trades-dir', join(ROOT, 'trades')));
// Tarjetas (PNG) que dibuja engine/report.py antes de enviar: se mandan como foto con el texto de pie
const CARDS = opt('--cards-dir', process.env.CARDS_DIR || '');
const ENGINE_CFG = resolve(opt('--engine-config', join(ROOT, 'engine', 'config.json')));
const TOKEN = (process.env.TELEGRAM_BOT_TOKEN || '').trim();
const DEFAULT_CHAT = (process.env.TELEGRAM_CHAT_ID || '').trim();
const API = process.env.TELEGRAM_API || 'https://api.telegram.org';
const MAX_PER_RUN = 25;
const LOGIN_RE = /^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$/;

const MK = {adr: 'ADR', byma: 'BYMA', cripto: 'Cripto'};
const REASON = {stop: 'tocó el stop', target: 'llegó al objetivo', trend: 'perdió la tendencia', time: 'cierre por tiempo', flat: 'cierre del día 15:55 NY', manual: 'cierre manual'};
const sleep = ms => new Promise(r => setTimeout(r, ms));
const esc = s => String(s ?? '').replace(/[&<>]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;'}[c]));
const fmt = (v, d) => (v == null || !isFinite(v)) ? '—' : Number(v).toLocaleString('es-AR', {minimumFractionDigits: d, maximumFractionDigits: d});
const px = v => fmt(v, Math.abs(v) >= 1000 ? 0 : Math.abs(v) >= 1 ? 2 : 4);
const money = v => fmt(v, Math.abs(v) >= 1000 ? 0 : 2);
const cur = kind => kind === 'byma' ? '$' : 'US$';
const signed = (v, s) => `${v >= 0 ? '+' : '−'}${s(Math.abs(v))}`;

async function readJSON(file, def) {
  try { return JSON.parse(await readFile(file, 'utf8')); }
  catch (e) { if (e.code !== 'ENOENT') console.warn(`No pude leer ${file}: ${e.message}`); return def; }
}

/* ---------- Tamaño de posición (mismas reglas que engine/planner.py size_position) ---------- */
export function sizeFor(r, entry, stop) {
  if (!r || !(entry > 0) || !(stop > 0) || !(entry > stop)) return null;
  const perUnit = entry - stop;
  const byLoss = r.budget * r.max_loss_pct / 100 / perUnit;
  const byCap = r.budget * r.max_position_pct / 100 / entry;
  let units = Math.min(byLoss, byCap);
  units = r.fractional ? Math.floor(units * 1e6) / 1e6 : Math.floor(units);
  if (!(units > 0)) return {units: 0};
  return {units, notional: units * entry, risk: units * perUnit, limit: byLoss <= byCap ? 'pérdida máxima' : 'tope de posición'};
}
const unitsTxt = (u, kind, label) => kind === 'cripto'
  ? `${Number(u).toLocaleString('es-AR', {maximumFractionDigits: 6})} ${esc(label)}`
  : `${fmt(u, 0)} ${u === 1 ? 'acción' : 'acciones'}`;

function sizeLine(u, e) {
  const s = sizeFor(u.risk?.[e.kind], e.price, e.stop);
  const c = cur(e.kind);
  if (!s) return '';
  if (!s.units) return `Para vos: el presupuesto de ${MK[e.kind]} no alcanza para una unidad con ese stop.`;
  return `💼 <b>Para vos:</b> ${unitsTxt(s.units, e.kind, e.label)} (${c} ${money(s.notional)}, riesgo ${c} ${money(s.risk)})`;
}
function resultLine(u, e) {
  const s = sizeFor(u.risk?.[e.kind], e.entry, e.initial_stop);
  if (!s?.units || !(e.price > 0)) return '';
  const c = cur(e.kind);
  return `💼 <b>Para vos:</b> ${signed((e.price - e.entry) * s.units, v => `${c} ${money(v)}`)} con ${unitsTxt(s.units, e.kind, e.label)}`;
}

/* ---------- Telegram ---------- */
export async function tg(chat, text) {
  for (let i = 1; i <= 3; i++) {
    let status = 0, j = {};
    try {
      const r = await fetch(`${API}/bot${TOKEN}/sendMessage`, {
        method: 'POST', headers: {'content-type': 'application/json'},
        body: JSON.stringify({chat_id: chat, text, parse_mode: 'HTML', disable_web_page_preview: true}),
        signal: AbortSignal.timeout(20000)
      });
      status = r.status; j = await r.json().catch(() => ({}));
      if (r.ok && j.ok) return {ok: true};
    } catch (e) { j = {description: e.message}; }
    if ((status === 429 || status >= 500 || status === 0) && i < 3) { await sleep(((j.parameters?.retry_after) || 2 * i) * 1000); continue; }
    // Nunca se imprime el token: solo lo que responde Telegram
    console.error(`Telegram respondió ${status}: ${j.description || 'sin detalle'}`);
    if (status === 401 || status === 404) console.error('→ El token no es válido. Revisá el secret TELEGRAM_BOT_TOKEN.');
    if (status === 400) console.error('→ Revisá el chat (secret TELEGRAM_CHAT_ID o "chat" del perfil en users/users.json).');
    if (status === 403) console.error('→ El bot no puede escribir ahí: agregalo como administrador del canal o mandale /start.');
    return {ok: false, fatal: [400, 401, 403, 404].includes(status) && /chat|token|bot|unauthorized|not found|forbidden/i.test(j.description || '')};
  }
  return {ok: false};
}

export async function tgPhoto(chat, file, caption) {
  const buf = await readFile(file);
  for (let i = 1; i <= 3; i++) {
    let status = 0, j = {};
    try {
      const fd = new FormData();
      fd.append('chat_id', chat); fd.append('caption', caption); fd.append('parse_mode', 'HTML');
      fd.append('photo', new Blob([buf], {type: 'image/png'}), basename(file));
      const r = await fetch(`${API}/bot${TOKEN}/sendPhoto`, {method: 'POST', body: fd, signal: AbortSignal.timeout(30000)});
      status = r.status; j = await r.json().catch(() => ({}));
      if (r.ok && j.ok) return {ok: true};
    } catch (e) { j = {description: e.message}; }
    if ((status === 429 || status >= 500 || status === 0) && i < 3) { await sleep(((j.parameters?.retry_after) || 2 * i) * 1000); continue; }
    console.error(`Telegram (foto) respondió ${status}: ${j.description || 'sin detalle'}`);
    return {ok: false, status};
  }
  return {ok: false};
}
async function cardFor(e) {
  if (!CARDS || e.info !== 'report') return null;
  const f = join(resolve(CARDS), String(e.id).replace(/:/g, '-') + '.png');
  try { await readFile(f); return f; } catch { return null; }
}

/* ---------- Mensajes ---------- */
const footer = (cfg, e) => cfg.panel_url ? `\n<a href="${esc(cfg.panel_url)}#${e?.src === 'trade' ? 'operaciones' : 'senales'}">Ver en el panel</a>` : '';

function engineFallback(e) {
  const head = e.type === 'entry' ? `🟢 <b>ENTRADA · ${esc(e.label)}</b>`
    : e.type === 'exit' ? `${e.reason === 'target' ? '🎯' : e.reason === 'stop' ? '🛑' : '🔴'} <b>SALIDA · ${esc(e.label)}</b>${e.reason ? ` (${REASON[e.reason] || esc(e.reason)})` : ''}`
    : `🔄 <b>ACTUALIZACIÓN · ${esc(e.label)}</b>`;
  const lines = [`${head} · ${MK[e.kind] || esc(e.kind)}${e.setup ? ` · ${esc(String(e.setup).toUpperCase())}` : ''}`];
  if (e.why) lines.push(esc(e.why));
  lines.push('');
  if (e.price != null) lines.push(`Precio: <b>${cur(e.kind)} ${px(e.price)}</b>`);
  if (e.stop != null || e.target != null) lines.push(`Stop: ${px(e.stop)} · Objetivo: ${px(e.target)}${e.rr ? ` (R:R ${fmt(e.rr, 1)})` : ''}`);
  return lines.join('\n');
}

function engineText(e, u) {
  let lines = ((typeof e.text === 'string' && e.text.trim()) ? e.text : engineFallback(e)).split('\n');
  if (e.type === 'entry') {
    // El tamaño del motor es el de su cartera de referencia: se reemplaza por el de cada usuario
    lines = lines.filter(l => !/^Tamaño sugerido:/.test(l));
    const sl = sizeLine(u, e);
    if (sl) { const i = lines.findIndex(l => l.startsWith('<i>')); lines.splice(i >= 0 ? i : lines.length, 0, sl); }
  } else if (e.type === 'exit') {
    const rl = resultLine(u, e);
    if (rl) lines.push(rl);
  }
  return lines.join('\n');
}

function tradeText(e, u) {
  const c = cur(e.kind), who = `👤 <b>${esc(e.who_name)}</b>`;
  if (e.type === 'entry') {
    const stopPct = (e.price - e.stop) / e.price * 100;
    const rr = e.target ? (e.target - e.price) / (e.price - e.stop) : null;
    const lines = [`${who} <b>COMPRÓ ${esc(e.label)}</b> (${MK[e.kind]})`,
      `Precio: <b>${c} ${px(e.price)}</b>`,
      `Stop: ${c} ${px(e.stop)} (−${fmt(stopPct, 1)} %)${e.target ? ` · Objetivo: ${c} ${px(e.target)} · R:R ${fmt(rr, 1)}` : ''}`];
    const sl = sizeLine(u, e);
    if (sl) lines.push(sl);
    if (e.note) lines.push(`<i>${esc(e.note)}</i>`);
    return lines.join('\n');
  }
  if (e.type === 'update') return `${who} movió el stop de <b>${esc(e.label)}</b> a ${c} ${px(e.stop)}` + (e.note ? `\n<i>${esc(e.note)}</i>` : '');
  const pnl = (e.price / e.entry - 1) * 100;
  const r = (e.price - e.entry) / (e.entry - e.initial_stop);
  const icon = e.reason === 'target' ? '🎯' : e.reason === 'stop' ? '🛑' : '🔴';
  const lines = [`${icon} ${who} <b>VENDIÓ ${esc(e.label)}</b> (${MK[e.kind]}) · ${REASON[e.reason] || 'cierre'}`,
    `Precio: <b>${c} ${px(e.price)}</b> · entrada ${c} ${px(e.entry)} → ${signed(pnl, v => fmt(v, 1) + ' %')} (${signed(r, v => fmt(v, 1))}R)`];
  const rl = resultLine(u, e);
  if (rl) lines.push(rl);
  if (e.note) lines.push(`<i>${esc(e.note)}</i>`);
  return lines.join('\n');
}

export function textFor(e, u, cfg, ctx = {}) {
  let t = e.src === 'trade' ? tradeText(e, u) : engineText(e, u);
  if (e.type === 'entry' && ctx.open != null && u.max_open_positions && ctx.open > u.max_open_positions)
    t += `\n⚠️ Ya seguís ${ctx.open} operaciones abiertas y tu tope es ${u.max_open_positions}: evaluá si entrar.`;
  return t + footer(cfg, e);
}

/* ---------- Operaciones registradas → eventos ---------- */
export function tradeEvents(login, name, trades) {
  const out = [];
  for (const t of Array.isArray(trades) ? trades : []) {
    if (!t || !t.id || !MK[t.kind] || !(t.entry > 0) || !(t.stop > 0)) continue;
    const base = {src: 'trade', who: login, who_name: name || login, kind: t.kind, label: t.asset, trade: t.id};
    const pre = `trade:${login}:${t.id}`;
    out.push({...base, id: `${pre}:entry`, type: 'entry', ts: t.opened_at, price: t.entry, stop: t.stop, target: t.target ?? null, note: t.note || ''});
    (t.stop_history || []).forEach((h, i) => out.push({...base, id: `${pre}:stop:${i}`, type: 'update', ts: h.at, stop: h.stop, note: h.note || ''}));
    if (t.exit != null) out.push({...base, id: `${pre}:exit`, type: 'exit', ts: t.closed_at, price: t.exit, entry: t.entry,
      initial_stop: t.stop, reason: t.exit_reason || 'manual', note: t.exit_note || ''});
  }
  return out;
}

async function loadTrades() {
  let files = [];
  try { files = (await readdir(TRADES)).filter(f => f.endsWith('.json')); } catch {}
  const out = {};
  for (const f of files) {
    const login = basename(f, '.json');
    if (!LOGIN_RE.test(login)) continue;
    const j = await readJSON(join(TRADES, f), null);
    out[login] = Array.isArray(j?.trades) ? j.trades : [];
  }
  return out;
}

/* ---------- Usuarios ---------- */
const OWNER = process.env.GITHUB_REPOSITORY_OWNER || '';

async function loadUsers(cfg) {
  const j = await readJSON(USERS, null);
  const list = Object.entries(j?.users || {}).map(([login, u]) => ({login, ...u}));
  if (list.length) return list;
  // Sin perfiles: como antes, un solo destinatario con la config del motor y alerts/telegram.json
  const eng = await readJSON(ENGINE_CFG, {});
  return [{login: OWNER || 'default', name: '', risk: eng.risk || {}, max_open_positions: eng.risk?.max_open_positions,
    telegram: {chat: 'default', engine: true, follow: [], types: cfg.types, kinds: cfg.kinds, only: cfg.only || []}}];
}
export const chatOf = u => { const c = (u.telegram?.chat || '').trim(); return c === 'default' ? DEFAULT_CHAT : c; };

export function wants(u, e) {
  const t = u.telegram || {};
  if (e.src === 'trade') return e.who !== u.login && (t.follow || []).includes(e.who); // seguir a alguien = todos sus movimientos
  if (!t.engine) return false;
  const on = (t.types || {})[e.type];
  if (on === false || (on === undefined && e.type !== 'info')) return false; // 'info' (noticias, balances, resumen) llega salvo que se apague
  if ((t.kinds || {})[e.kind] === false) return false;
  const only = Array.isArray(t.only) && t.only.length ? t.only : null;
  return !only || only.includes(`${e.kind}:${e.label}`);
}

/* ---------- Principal ---------- */
async function main() {
  const cfg = {types: {entry: true, exit: true, update: false, info: true}, kinds: {adr: true, byma: true, cripto: true}, max_age_hours: 24, ...(await readJSON(CONFIG, {}))};
  const users = await loadUsers(cfg);
  const mainUser = users.find(u => u.login === OWNER) || users.find(u => u.telegram?.chat === 'default') || users[0];

  if (TEST) {
    if (!TOKEN) { console.error('Falta el secret TELEGRAM_BOT_TOKEN.'); process.exit(1); }
    const chats = new Map();
    for (const u of users) { const c = chatOf(u); if (c && !chats.has(c)) chats.set(c, u); }
    if (!chats.size) { console.error('Ningún perfil tiene chat. Cargá el secret TELEGRAM_CHAT_ID.'); process.exit(1); }
    let bad = 0;
    for (const [chat, u] of chats) {
      const r = await tg(chat, `✅ <b>TraderPanel conectado</b>\nHola${u.name ? ' ' + esc(u.name) : ''}: acá vas a recibir las alertas de entrada y salida, con el tamaño calculado para tu presupuesto.${footer(cfg)}`);
      if (!r.ok) bad++; else console.log(`Prueba enviada al chat de ${u.login}.`);
      await sleep(1100);
    }
    const regFile = join(DATA, 'telegram_sent.json');
    const reg = await readJSON(regFile, null);
    if (reg && !DRY && !bad) { reg.telegram = true; reg.last_test = new Date().toISOString(); await writeFile(regFile, JSON.stringify(reg, null, 1) + '\n'); }
    if (bad) process.exit(1);
    return;
  }

  const evData = await readJSON(join(DATA, 'events.json'), null);
  const engineEvents = (Array.isArray(evData?.events) ? evData.events : Array.isArray(evData) ? evData : []).map(e => ({src: 'engine', ...e}));
  const trades = await loadTrades();
  const names = Object.fromEntries(users.map(u => [u.login, u.name || u.login]));
  const tradeEv = Object.entries(trades).flatMap(([login, list]) => tradeEvents(login, names[login], list));
  const events = [...engineEvents, ...tradeEv].filter(e => e && e.id).sort((a, b) => (Date.parse(a.ts) || 0) - (Date.parse(b.ts) || 0));

  // Posiciones abiertas que sigue cada usuario (para el aviso de tope)
  const sig = await readJSON(join(DATA, 'signals.json'), null);
  const engineOpen = Array.isArray(sig?.positions) ? sig.positions.length : 0;
  const openTrades = login => (trades[login] || []).filter(t => t && t.exit == null).length;
  const openFor = u => (u.telegram?.engine ? engineOpen : 0) + (u.telegram?.follow || []).reduce((n, l) => n + openTrades(l), 0);

  const regFile = join(DATA, 'telegram_sent.json');
  const prev = await readJSON(regFile, null);
  const reg = prev || {sent: {}, recent: []};
  reg.sent ||= {}; reg.recent ||= [];
  if (prev && !reg.users) {
    // Registro de antes de los perfiles: lo enviado era del usuario principal
    for (const id of Object.keys(reg.sent)) if (!id.includes('|')) { reg.sent[`${mainUser.login}|${id}`] = reg.sent[id]; delete reg.sent[id]; }
    reg.users = {[mainUser.login]: new Date().toISOString()};
  }
  reg.users ||= {};

  const now = Date.now();
  const maxAge = (cfg.max_age_hours ?? 24) * 3600e3;
  const mark = (u, e, status) => {
    const at = new Date().toISOString();
    reg.sent[`${u.login}|${e.id}`] = {at, status};
    reg.recent.unshift({id: e.id, to: u.login, src: e.src, who: e.who || null, ts: e.ts || null, kind: e.kind, label: e.label, type: e.type,
      reason: e.reason || null, setup: e.setup || null, price: e.price ?? null, status, at});
  };

  let sent = 0, skipped = 0, failed = 0, n = 0, fresh = 0;
  const fatalChats = new Set();
  for (const u of users) {
    const isNew = !reg.users[u.login];
    const chat = chatOf(u);
    for (const e of events) {
      if (reg.sent[`${u.login}|${e.id}`] || !wants(u, e)) continue;
      fresh++;
      const age = e.ts ? now - Date.parse(e.ts) : 0;
      const why = (isNew && !BACKLOG) ? 'backlog' : age > maxAge ? 'viejo' : !(TOKEN && chat) ? 'sin_telegram' : null;
      if (why) { mark(u, e, why); skipped++; continue; }
      if (fatalChats.has(chat) || n >= MAX_PER_RUN) continue; // quedan para la próxima corrida
      n++;
      const text = textFor(e, u, cfg, {open: openFor(u)});
      if (DRY) { console.log(`\n--- ${u.login} · ${e.id} ---\n${text}`); mark(u, e, 'dry'); continue; }
      const card = await cardFor(e);
      let r = card ? await tgPhoto(chat, card, text) : await tg(chat, text);
      if (card && !r.ok && r.status !== 403) r = await tg(chat, text); // sin foto, por lo menos el texto
      if (r.ok) { mark(u, e, 'enviado'); sent++; }
      else { failed++; if (r.fatal) fatalChats.add(chat); }
      await sleep(1100); // Telegram admite ~1 mensaje por segundo al mismo chat
    }
    reg.users[u.login] ||= new Date().toISOString();
  }

  if (REPORT && TOKEN && chatOf(mainUser) && !sent && !failed && !DRY) {
    const nOpen = Object.keys(trades).reduce((a, l) => a + openTrades(l), 0);
    const lines = [`ℹ️ <b>Revisión manual</b>: no hay avisos nuevos.`,
      `El motor tiene ${engineEvents.length} ${engineEvents.length === 1 ? 'evento' : 'eventos'}${evData ? '' : ' (todavía no corrió)'} y ${engineOpen} ${engineOpen === 1 ? 'posición abierta' : 'posiciones abiertas'}. Operaciones registradas abiertas: ${nOpen}.`];
    if (sig?.generated_at) lines.push(`Última corrida del motor: ${new Date(sig.generated_at).toLocaleString('es-AR', {timeZone: 'America/Argentina/Buenos_Aires', day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'})}`);
    const r = await tg(chatOf(mainUser), lines.join('\n') + footer(cfg));
    if (!r.ok) failed++;
  }

  // Registro acotado: ids que siguen vigentes o de los últimos 30 días
  const keep = new Set(events.map(e => e.id));
  const cut = now - 30 * 864e5;
  for (const k in reg.sent) if (!keep.has(k.slice(k.indexOf('|') + 1)) && Date.parse(reg.sent[k].at) < cut) delete reg.sent[k];
  reg.recent = reg.recent.slice(0, 40);
  reg.last_run = new Date(now).toISOString();
  reg.telegram = !!TOKEN && !!chatOf(mainUser) && !fatalChats.has(chatOf(mainUser));
  reg.last_error = failed ? `${failed} mensaje(s) no salieron` : null;

  console.log(`Usuarios: ${users.length} · eventos: ${events.length} (motor ${engineEvents.length}, operaciones ${tradeEv.length}) · nuevos: ${fresh} · enviados: ${sent} · salteados: ${skipped} · fallidos: ${failed}`);
  if (!TOKEN) console.warn('Telegram sin configurar: cargá el secret TELEGRAM_BOT_TOKEN.');
  if (!DRY) { await mkdir(DATA, {recursive: true}); await writeFile(regFile, JSON.stringify(reg, null, 1) + '\n'); }
  if (failed) process.exitCode = 1;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url))
  main().catch(e => { console.error(e.message); process.exit(1); });
