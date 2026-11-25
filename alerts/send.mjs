#!/usr/bin/env node
// Envía a Telegram los eventos nuevos que genera el motor de señales (data/events.json).
// Cada evento se manda una sola vez: el registro queda en data/telegram_sent.json.
//
// Uso:
//   node alerts/send.mjs                corrida normal
//   node alerts/send.mjs --dry-run      muestra qué mandaría, sin enviar ni guardar
//   node alerts/send.mjs --test         manda un mensaje de prueba y termina
//   node alerts/send.mjs --send-backlog en la primera corrida manda también lo que ya había
// Variables: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (secrets del repo). Opción: --data-dir <carpeta>.

import {readFile, writeFile, mkdir} from 'node:fs/promises';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);
const opt = (name, def) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : def; };
const DRY = args.includes('--dry-run');
const TEST = args.includes('--test');
const BACKLOG = args.includes('--send-backlog');
const DATA = resolve(opt('--data-dir', join(ROOT, 'data')));
const CONFIG = resolve(opt('--config', join(ROOT, 'alerts', 'telegram.json')));
const TOKEN = (process.env.TELEGRAM_BOT_TOKEN || '').trim();
const CHAT = (process.env.TELEGRAM_CHAT_ID || '').trim();
const API = process.env.TELEGRAM_API || 'https://api.telegram.org';
const MAX_PER_RUN = 20;

const MK = {adr: 'ADR', byma: 'BYMA', cripto: 'Cripto'};
const REASON = {stop: 'tocó el stop', target: 'llegó al objetivo', trend: 'perdió la tendencia', time: 'cierre por tiempo'};
const sleep = ms => new Promise(r => setTimeout(r, ms));
const esc = s => String(s ?? '').replace(/[&<>]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;'}[c]));
const fmt = (v, d) => (v == null || !isFinite(v)) ? '—' : Number(v).toLocaleString('es-AR', {minimumFractionDigits: d, maximumFractionDigits: d});
const px = v => fmt(v, Math.abs(v) >= 1000 ? 0 : Math.abs(v) >= 1 ? 2 : 4);
const cur = kind => kind === 'byma' ? '$' : 'US$';

async function readJSON(file, def) {
  try { return JSON.parse(await readFile(file, 'utf8')); }
  catch (e) { if (e.code !== 'ENOENT') console.warn(`No pude leer ${file}: ${e.message}`); return def; }
}

/* ---------- Telegram ---------- */
async function tg(text) {
  for (let i = 1; i <= 3; i++) {
    let status = 0, j = {};
    try {
      const r = await fetch(`${API}/bot${TOKEN}/sendMessage`, {
        method: 'POST', headers: {'content-type': 'application/json'},
        body: JSON.stringify({chat_id: CHAT, text, parse_mode: 'HTML', disable_web_page_preview: true}),
        signal: AbortSignal.timeout(20000)
      });
      status = r.status; j = await r.json().catch(() => ({}));
      if (r.ok && j.ok) return {ok: true};
    } catch (e) { j = {description: e.message}; }
    if ((status === 429 || status >= 500 || status === 0) && i < 3) { await sleep(((j.parameters?.retry_after) || 2 * i) * 1000); continue; }
    // Nunca se imprime el token: solo lo que responde Telegram
    console.error(`Telegram respondió ${status}: ${j.description || 'sin detalle'}`);
    if (status === 401 || status === 404) console.error('→ El token no es válido. Revisá el secret TELEGRAM_BOT_TOKEN.');
    if (status === 400) console.error('→ Revisá el secret TELEGRAM_CHAT_ID (ej. @mi_canal o -100…) o el formato del mensaje.');
    if (status === 403) console.error('→ El bot no puede escribir ahí: agregalo como administrador del canal.');
    return {ok: false, fatal: [400, 401, 403, 404].includes(status) && /chat|token|bot|unauthorized|not found|forbidden/i.test(j.description || '')};
  }
  return {ok: false};
}

/* ---------- Mensajes ---------- */
function fallbackText(e, cfg) {
  const head = e.type === 'entry' ? `🟢 <b>ENTRADA · ${esc(e.label)}</b>`
    : e.type === 'exit' ? `${e.reason === 'target' ? '🎯' : e.reason === 'stop' ? '🛑' : '🔴'} <b>SALIDA · ${esc(e.label)}</b>${e.reason ? ` (${REASON[e.reason] || esc(e.reason)})` : ''}`
    : `🔄 <b>ACTUALIZACIÓN · ${esc(e.label)}</b>`;
  const lines = [`${head} · ${MK[e.kind] || esc(e.kind)}${e.setup ? ` · ${esc(String(e.setup).toUpperCase())}` : ''}`];
  if (e.why) lines.push(esc(e.why));
  lines.push('');
  if (e.price != null) lines.push(`Precio: <b>${cur(e.kind)} ${px(e.price)}</b>`);
  if (e.stop != null || e.target != null) lines.push(`Stop: ${px(e.stop)} · Objetivo: ${px(e.target)}${e.rr ? ` (R:R ${fmt(e.rr, 1)})` : ''}`);
  return lines.join('\n') + footer(cfg);
}
const footer = cfg => cfg.panel_url ? `\n<a href="${esc(cfg.panel_url)}#senales">Ver en el panel</a>` : '';
const textOf = (e, cfg) => (typeof e.text === 'string' && e.text.trim()) ? e.text : fallbackText(e, cfg);

/* ---------- Principal ---------- */
async function main() {
  const cfg = {types: {entry: true, exit: true, update: false}, kinds: {adr: true, byma: true, cripto: true}, max_age_hours: 24, ...(await readJSON(CONFIG, {}))};
  const ready = !!(TOKEN && CHAT);

  if (TEST) {
    if (!ready) { console.error('Faltan los secrets TELEGRAM_BOT_TOKEN y/o TELEGRAM_CHAT_ID.'); process.exit(1); }
    const r = await tg(`✅ <b>TraderPanel conectado</b>\nAcá vas a recibir las alertas de entrada y salida.${footer(cfg)}`);
    if (!r.ok) process.exit(1);
    console.log('Mensaje de prueba enviado.');
    const regFile = join(DATA, 'telegram_sent.json');
    const reg = await readJSON(regFile, null);
    if (reg && !DRY) { reg.telegram = true; reg.last_test = new Date().toISOString(); await writeFile(regFile, JSON.stringify(reg, null, 1) + '\n'); }
    return;
  }

  const evFile = join(DATA, 'events.json');
  const regFile = join(DATA, 'telegram_sent.json');
  const evData = await readJSON(evFile, null);
  const events = Array.isArray(evData?.events) ? evData.events : Array.isArray(evData) ? evData : [];
  if (!evData) console.warn(`Todavía no hay ${evFile}: el motor de señales no corrió.`);

  const first = !(await readJSON(regFile, null));
  const reg = (await readJSON(regFile, null)) || {sent: {}, recent: []};
  reg.sent ||= {}; reg.recent ||= [];
  const now = Date.now();
  const maxAge = (cfg.max_age_hours ?? 24) * 3600e3;
  // "only": lista opcional de activos a avisar, ej. ["adr:YPF", "cripto:BTC"]. Vacía = todos.
  const only = Array.isArray(cfg.only) && cfg.only.length ? new Set(cfg.only.map(String)) : null;

  // De más viejo a más nuevo, para que el canal quede en orden
  const fresh = events.filter(e => e && e.id && !reg.sent[e.id]).reverse();
  const mark = (e, status) => {
    reg.sent[e.id] = {at: new Date().toISOString(), status};
    reg.recent.unshift({id: e.id, ts: e.ts || null, kind: e.kind, label: e.label, type: e.type, reason: e.reason || null, setup: e.setup || null, price: e.price ?? null, status, at: reg.sent[e.id].at});
  };

  let sent = 0, skipped = 0, failed = 0, fatal = false, n = 0;
  for (const e of fresh) {
    const age = e.ts ? now - Date.parse(e.ts) : 0;
    const why = (first && !BACKLOG) ? 'backlog'
      : !cfg.types[e.type] ? 'filtrado'
      : cfg.kinds[e.kind] === false ? 'filtrado'
      : only && !only.has(`${e.kind}:${e.label}`) ? 'filtrado'
      : age > maxAge ? 'viejo'
      : !ready ? 'sin_telegram'
      : null;
    if (why) { mark(e, why); skipped++; continue; }
    if (fatal || n >= MAX_PER_RUN) continue; // quedan para la próxima corrida
    n++;
    if (DRY) { console.log(`\n--- ${e.id} ---\n${textOf(e, cfg)}`); mark(e, 'dry'); continue; }
    const r = await tg(textOf(e, cfg));
    if (r.ok) { mark(e, 'enviado'); sent++; }
    else { failed++; if (r.fatal) fatal = true; }
    await sleep(1100); // Telegram admite ~1 mensaje por segundo al mismo chat
  }

  // Registro acotado: ids que siguen en events.json o de los últimos 30 días
  const keep = new Set(events.map(e => e?.id));
  const cut = now - 30 * 864e5;
  for (const id in reg.sent) if (!keep.has(id) && Date.parse(reg.sent[id].at) < cut) delete reg.sent[id];
  reg.recent = reg.recent.slice(0, 30);
  reg.last_run = new Date(now).toISOString();
  reg.telegram = ready && !fatal;
  reg.last_error = failed ? `${failed} mensaje(s) no salieron` : null;

  console.log(`Eventos: ${events.length} · nuevos: ${fresh.length} · enviados: ${sent} · salteados: ${skipped} · fallidos: ${failed}${first ? ' · primera corrida (el histórico se marcó como visto)' : ''}`);
  if (!ready) console.warn('Telegram sin configurar: cargá los secrets TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID.');
  if (!DRY) { await mkdir(DATA, {recursive: true}); await writeFile(regFile, JSON.stringify(reg, null, 1) + '\n'); }
  if (failed) process.exitCode = 1;
}

main().catch(e => { console.error(e.message); process.exit(1); });
