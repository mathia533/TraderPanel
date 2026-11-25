// Service worker de TraderPanel: permite instalar el panel como app y abrirlo sin conexión.
// Siempre va primero a la red (los datos y las páginas cambian seguido); la copia guardada
// se usa solo si no hay conexión. Nunca toca pedidos a otros dominios (APIs de mercado,
// api.github.com con el token del usuario, Telegram): esos pasan directo.
const CACHE = 'tp-v3';
const SHELL = ['./', './index.html', './operaciones.html', './alertas.html', './config.html', './simulador.html', './lib/timemachine.mjs', './manifest.webmanifest',
  './icons/icon-192.png', './icons/icon-512.png', './engine/sectors.json'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {}).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== self.location.origin) return;
  e.respondWith((async () => {
    const cache = await caches.open(CACHE);
    // La copia se guarda sin el ?t= que usa el panel para saltear la caché de Pages
    const key = url.pathname.endsWith('.json') ? url.origin + url.pathname : req;
    try {
      const res = await fetch(req);
      if (res.ok && (req.mode === 'navigate' || /\.(json|html|png|webmanifest|mjs)$/.test(url.pathname) || url.pathname.endsWith('/'))) cache.put(key, res.clone());
      return res;
    } catch (err) {
      const hit = await cache.match(key, {ignoreSearch: true}) || (req.mode === 'navigate' ? await cache.match('./index.html') : null);
      if (hit) return hit;
      throw err;
    }
  })());
});
