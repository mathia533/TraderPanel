# TraderPanel

Panel web de una sola página con el dólar en Argentina, acciones (ADRs y BYMA) y cripto, con señales técnicas de entrada y salida. No necesita servidor ni build: abrí `index.html` en el navegador o publicalo con GitHub Pages.

## Qué muestra

- **Dólar**: blue, MEP, CCL y oficial con la brecha contra el oficial y la tendencia de los últimos 30 días.
- **Watchlist** con pestañas para ADRs (USD), BYMA (ARS), cripto y dólares. Podés agregar y quitar activos, y las listas quedan guardadas en el navegador.
- **Motor de señales con skills de trading** (ver abajo): cada hora, un GitHub Action analiza la watchlist con las skills de [tradermonty/claude-trading-skills](https://github.com/tradermonty/claude-trading-skills) y las reglas de riesgo de [day-trader-skill](https://github.com/jakeschincariol/day-trader-skill), y el panel muestra entradas, salidas, posiciones abiertas y setups preparados.
- **Señal técnica local** por activo (sólo para lo que el motor no cubre) (compra, venta o neutral, con un puntaje de 0 a 100) calculada sobre cierres diarios:
  - precio contra la media de 20 ruedas (±15)
  - media de 20 contra media de 50 (±15)
  - RSI de 14 ruedas (entre −10 y +10)
  - variación de las últimas 5 ruedas (±10)
- **Señales activas**: entradas y salidas del motor con stop, objetivo y R:R, posiciones abiertas con su R actual y setups preparados (cerca del gatillo o bloqueados por riesgo), más el estado de cada mercado (favorable, neutral o defensivo). Si el motor todavía no publicó nada, el panel usa el cálculo local.
- **Alertas por Telegram**: un GitHub Action manda al canal cada entrada y salida nueva que detecta el motor de señales. La tarjeta del panel muestra si está conectado y las últimas alertas enviadas.
- Estado de BYMA y NYSE (sin contemplar feriados), modo claro y oscuro, y vista para celular.

## Fuentes

[DolarAPI](https://dolarapi.com), [ArgentinaDatos](https://argentinadatos.com) (historial del dólar), [data912](https://data912.com) (acciones e historial) y [CoinGecko](https://www.coingecko.com). Los historiales se guardan 6 horas en el navegador para no saturar las APIs.

Los datos tienen demora y las señales son indicadores técnicos simples, no asesoramiento financiero.

## Motor de señales

```
GitHub Action (cada hora con mercado abierto, cada 4 h el fin de semana)
  └─ python -m engine.run
       ├─ engine/sources.py    velas diarias OHLCV (Yahoo; respaldo data912 / CoinGecko)
       ├─ engine/skills.py     skills de tradermonty, código original en vendor/
       │    ├─ vcp-screener                     Trend Template + VCP de Minervini, pivote, volumen, fuerza relativa
       │    ├─ stockbee-momentum-burst-screener ruptura del 4 % con volumen y base previa
       │    └─ crypto-regime-analyzer           régimen del mercado cripto 0-100 (filtro de entradas cripto)
       ├─ engine/planner.py    reglas de day-trader-skill: stop antes de entrar, objetivo 2R,
       │                       tamaño por pérdida máxima, tope de posiciones, stop a la entrada tras 1R
       └─ data/signals.json  → lo lee el panel
          data/events.json   → avisos de entrada/salida, cada uno una sola vez, con el texto para Telegram
          data/state.json    → posiciones abiertas y avisos emitidos
```

**Entradas.** VCP con calificación *Good* o mejor que rompe el pivote con volumen, o Momentum Burst *ACTIONABLE_DAY1*. Se calculan sólo con velas cerradas. No se abre nada si el stop queda a más del 12 %, si ya hay 8 posiciones abiertas o si el mercado de referencia está en modo defensivo (SPY para ADRs, Merval para BYMA o un índice propio de la watchlist si Yahoo no lo da, régimen cripto para cripto).

**Salidas.** Stop o objetivo tocados (también con la vela en curso), cierre debajo de la media de 50 ruedas para VCP, y salida por tiempo a las 5 ruedas para Momentum Burst.

**Configuración.** Watchlist, presupuesto por mercado, pérdida máxima por operación y límites en `engine/config.json`. Para correrlo a mano: `pip install -r engine/requirements.txt && python -m engine.run`. Pruebas: `python -m pytest tests`.

**Skills vendorizadas.** `vendor/tradermonty/` tiene los scripts originales (licencia MIT) sin modificar. Para actualizarlos: `scripts/sync_vendor.sh`.

Las señales son resultados de reglas técnicas sobre datos con demora. No son asesoramiento financiero ni órdenes: nada en este repo opera por vos.

## Alertas por Telegram

El envío corre en GitHub, no en el navegador, así el token del bot nunca queda expuesto.

1. El workflow **Motor de señales** calcula las señales y escribe `data/events.json`.
2. Al terminar, el workflow **Alertas Telegram** (`.github/workflows/telegram.yml`) corre `alerts/send.mjs`: manda solo los eventos que todavía no salieron y los anota en `data/telegram_sent.json`. Cada evento se manda una única vez.
3. El panel lee `data/telegram_sent.json` para mostrar el estado y qué se envió.

### Conectar el canal (una sola vez)

1. En Telegram, abrí un chat con **@BotFather**, mandá `/newbot`, elegí nombre y usuario (tiene que terminar en `bot`). Te devuelve un token tipo `123456789:AA…`. No lo compartas.
2. Creá un canal (puede ser privado) y en *Administradores* agregá a tu bot con permiso para **publicar mensajes**.
3. En este repo: **Settings → Secrets and variables → Actions → New repository secret**:
   - `TELEGRAM_BOT_TOKEN`: el token del paso 1.
   - `TELEGRAM_CHAT_ID`: el `@usuario` del canal si es público. Si es privado, el id numérico que empieza con `-100` (lo ves reenviando un mensaje del canal a @userinfobot, o abriendo el canal en web.telegram.org: es el número de la URL con `-100` adelante).
4. **Actions → Alertas Telegram → Run workflow**, marcá *Mandar solo un mensaje de prueba*. Si llega el ✅ al canal, quedó listo.

La primera corrida normal marca como vistas las señales que ya existían, para no llenar el canal de alertas viejas.

### Elegir qué se avisa

Editá `alerts/telegram.json`:

- `types`: `entry` (entradas), `exit` (salidas, incluye stop u objetivo tocado), `update` (ajustes).
- `kinds`: mercados `adr`, `byma`, `cripto`.
- `only`: lista opcional de activos, por ejemplo `["adr:YPF", "cripto:BTC"]`. Vacía avisa todos. El botón *Copiar activos seguidos* del panel arma esta lista con los activos que marcaste con la campanita.
- `max_age_hours`: eventos más viejos que esto no se mandan (evita avalanchas después de una caída).

Para probar localmente sin enviar: `node alerts/send.mjs --dry-run`.
