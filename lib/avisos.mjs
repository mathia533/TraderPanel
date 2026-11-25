// Avisos de Mi cartera por Telegram. Corre en el repo PRIVADO de cada usuario (<login>/traderpanel-cartera),
// con su propio workflow y sus propios secrets: nadie más ve la cartera ni el chat.
// La página Mi cartera copia este archivo, cartera.mjs y timemachine.mjs a .tp/ de ese repo.
//
//   node .tp/avisos.mjs            (lee cartera.json y estado.json de la carpeta actual)
//
// Avisa cuando un activo cambia de veredicto (a favor / ojo / en contra) o cuando el precio toca el stop
// de protección de la corrida anterior. La primera vez solo manda un mensaje de bienvenida.
import {readFile, writeFile} from 'node:fs/promises';
import {analyzePortfolio, changesSince} from './cartera.mjs';

const TOKEN = (process.env.TELEGRAM_BOT_TOKEN || '').trim();
const CHAT = (process.env.TELEGRAM_CHAT_ID || '').trim();
const TG = process.env.TELEGRAM_API || 'https://api.telegram.org';
const DATA = process.env.DATA912 || 'https://data912.com';
const SIGNALS = process.env.PANEL_SIGNALS || 'https://raw.githubusercontent.com/mathia533/TraderPanel/main/data/signals.json';
const PANEL = process.env.PANEL_URL || 'https://mathia533.github.io/TraderPanel/cartera.html';
const ES = {favor: 'a favor ✓', ojo: 'ojo !', contra: 'en contra ✕'};
const fmt = v => v == null ? '—' : Number(v).toLocaleString('es-AR', {maximumFractionDigits: Math.abs(v) >= 1000 ? 0 : 2});

const getJSON = async u => { const r = await fetch(u, {headers: {'User-Agent': 'traderpanel-cartera'}}); if (!r.ok) throw new Error(`${r.status} ${u}`); return r.json(); };
const readJSON = async (f, d) => { try { return JSON.parse(await readFile(f, 'utf8')); } catch { return d; } };

// Arma los mensajes a partir del informe nuevo y el estado anterior (sin red: se prueba aparte)
export function messagesFor(report, prev) {
  if (!prev) {
    const c = report.counts || {};
    return [`Avisos de tu cartera activados. Te escribo acá cuando un activo cambie de veredicto o toque su stop de protección.\n\nHoy: ${report.holdings.length} activos · ${c.favor || 0} a favor, ${c.ojo || 0} ojo, ${c.contra || 0} en contra.`];
  }
  const out = [];
  for (const ch of changesSince(prev.verdicts, report)) {
    const h = report.holdings.find(x => x.symbol === ch.symbol);
    out.push(`${ch.symbol}: pasó de ${ES[ch.from]} a ${ES[ch.to]}.\n${h.analysis.why}`);
  }
  for (const h of report.holdings) {
    const s = prev.stops?.[h.symbol];
    if (s && h.price != null && h.price <= s) out.push(`${h.symbol} tocó su stop de protección: $ ${fmt(h.price)} (el stop estaba en $ ${fmt(s)}).`);
  }
  return out;
}

export function stateFor(report) {
  return {actualizado: report.generated_at,
    verdicts: Object.fromEntries(report.holdings.map(h => [h.symbol, h.analysis.verdict])),
    stops: Object.fromEntries(report.holdings.filter(h => h.analysis.stop).map(h => [h.symbol, Math.round(h.analysis.stop * 100) / 100]))};
}

async function send(text) {
  const r = await fetch(`${TG}/bot${TOKEN}/sendMessage`, {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({chat_id: CHAT, text: `${text}\n\n${PANEL}`, disable_web_page_preview: true})});
  if (!r.ok) throw new Error(`Telegram respondió ${r.status}`);
}

async function main() {
  if (!TOKEN || !CHAT) { console.log('Faltan los secrets TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID: no se manda nada.'); return; }
  const src = await readJSON('cartera.json', null);
  if (!src) { console.log('Todavía no hay cartera.json.'); return; }
  const prev = await readJSON('estado.json', null);
  const report = await analyzePortfolio(src, {getJSON, base: DATA, signalsUrl: SIGNALS});
  if (!report.holdings.some(h => h.price != null)) { console.log('Sin precios de BYMA ahora: no comparo.'); return; }
  const msgs = messagesFor(report, prev);
  for (const m of msgs.slice(0, 10)) await send(m);
  await writeFile('estado.json', JSON.stringify(stateFor(report), null, 2) + '\n');
  console.log(`Listo: ${msgs.length} aviso(s).`);  // sin activos ni montos en el log
}

if (import.meta.url === `file://${process.argv[1]}`) main().catch(e => { console.error(e.message); process.exit(1); });
