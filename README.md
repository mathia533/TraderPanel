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
GitHub Action
  ├─ cada hora con mercado abierto (cada 4 h el fin de semana): python -m engine.run
  └─ cada 15 minutos con Wall Street abierto:                   python -m engine.run --intradia
       ├─ engine/sources.py    velas diarias OHLCV (Yahoo; respaldo data912 / CoinGecko)
       ├─ engine/skills.py     skills de tradermonty (vendor/tradermonty/)
       │    ├─ vcp-screener, stockbee-momentum-burst-screener, crypto-regime-analyzer
       ├─ engine/tmextra.py    más skills de tradermonty
       │    ├─ market-breadth-analyzer, uptrend-analyzer, sector-analyst → exposure-coach
       │    │                     techo de exposición en EE. UU. (frena compras de ADRs si es bajo)
       │    ├─ breakout-trade-planner   valida la ruptura VCP antes de entrar
       │    ├─ drawdown-circuit-breaker frena entradas tras pérdidas seguidas o del día
       │    └─ trade-performance-coach  resumen semanal del proceso (viernes al cierre)
       ├─ engine/dtskills.py   day-trader-skill (vendor/day-trader-skill/)
       │    ├─ trader-scanner        setups del día (escáner)
       │    ├─ trader-news           titulares de Yahoo y hechos relevantes de la SEC
       │    ├─ trader-entry/exit     ruptura del rango de 15′ (intradía ADRs), stop y objetivo
       │    ├─ trader-timemachine    10.000 repeticiones con historia real: ¿llega antes al stop o al objetivo?
       │    ├─ trader-budget/risk    tamaño y control PASS/BLOCK (pérdida del día, operaciones por día, tope)
       │    ├─ trader-analyst        balances de la SEC (necesita el secret SEC_USER_AGENT)
       │    └─ trader-pelosi         operaciones declaradas por congresistas de EE. UU.
       ├─ engine/pipeline.py   une todo: escáner → noticias → entrada → plan → Time Machine → tamaño → riesgo → ticket
       └─ data/signals.json  → lo lee el panel (pestaña Pipeline)
          data/events.json   → avisos de entrada/salida/stop/objetivo/cierre 15:55 y noticias, una sola vez
          data/state.json    → posiciones abiertas y avisos emitidos
          data/us_market.json, news_state.json, analyst.json, congress.json, indices.json
```

**Broker.** El último paso arma el ticket (compra, stop y objetivo) pero **no se envía a ningún broker**. `trader-broker` está en vendor/ sin usar. Conectar Alpaca en modo paper sería un cambio aparte y sólo con claves cargadas por vos.

**Secret opcional `SEC_USER_AGENT`.** La SEC pide identificarse: cargalo en *Settings → Secrets and variables → Actions* con el formato `Nombre mail@ejemplo.com`. Sin él no se leen balances ni hechos relevantes de la SEC; el resto funciona igual.

**Intradía.** Sólo ADRs/acciones de EE. UU. listados en `intraday.symbols` de `engine/config.json` (vacío = ninguno). Entrada por ruptura del rango de los primeros 15 minutos entre 9:45 y 15:30, objetivo 1R y cierre obligatorio a las 15:55 de Nueva York. BYMA y cripto siguen en velas diarias, con el mismo Time Machine, tamaño (cripto en lotes) y control de riesgo.

**Entradas.** VCP con calificación *Good* o mejor que rompe el pivote con volumen, o Momentum Burst *ACTIONABLE_DAY1*. Se calculan sólo con velas cerradas. No se abre nada si el stop queda a más del 12 %, si ya hay 8 posiciones abiertas o si el mercado de referencia está en modo defensivo (SPY para ADRs, Merval para BYMA o un índice propio de la watchlist si Yahoo no lo da, régimen cripto para cripto).

**Salidas.** Stop o objetivo tocados (también con la vela en curso), cierre debajo de la media de 50 ruedas para VCP, y salida por tiempo a las 5 ruedas para Momentum Burst.

**Configuración.** Watchlist, presupuesto por mercado, pérdida máxima por operación y límites en `engine/config.json`. Se puede editar desde el panel (ver *Configurar desde el panel*). Para correrlo a mano: `pip install -r engine/requirements.txt && python -m engine.run`. Pruebas: `python -m pytest tests`.

**Skills vendorizadas.** `vendor/tradermonty/` y `vendor/day-trader-skill/` tienen los scripts originales (licencia MIT) sin modificar. Para actualizarlos: `scripts/sync_vendor.sh`.

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

### Varios usuarios: perfil propio y seguir las operaciones de otro

Cada usuario entra a **Configuración** con su token de GitHub y queda identificado por su login.

| Común (lo comparten todos) | Por usuario (`users/users.json`, cada uno el suyo) |
|---|---|
| Watchlist del motor, setups, reglas técnicas (stop máx., objetivo en R, R:R mínimo) — `engine/config.json` | Presupuesto, pérdida máx. y posición máx. por mercado |
| Antigüedad máx. de los avisos y link al panel — `alerts/telegram.json` | Tope de operaciones abiertas |
| Cartera de referencia del motor (solo para los resultados del panel) | Chat de Telegram, qué señales del motor recibe y a quién sigue |

**Registrar operaciones.** En **Mis operaciones** (`operaciones.html`) cada uno anota sus compras (precio, stop obligatorio, objetivo opcional y nota), mueve el stop y registra la venta. Se guarda en `trades/<login>.json` y el push dispara **Alertas Telegram** al instante.

**Seguir a alguien.** En *Mis avisos* marcás a quién seguís. Te llega cada compra, cambio de stop y venta suya, con la cantidad calculada para **tu** presupuesto (mismas reglas que el motor: el stop decide el tamaño). Las señales del motor también llegan con el tamaño de cada uno. Si ya seguís más operaciones abiertas que tu tope, la alerta de entrada te lo marca.

**Chat.** `default` usa el secret `TELEGRAM_CHAT_ID`. Cualquier otro usuario pone el id de su canal (`-100…`) o `@canal`, con el bot como administrador. El repo es público: lo que va en `users/` y `trades/` se ve.

**Token de un colaborador.** GitHub no deja usar tokens *Fine-grained* en repos personales de otra persona: el colaborador usa un token clásico con `public_repo` (y `workflow` si quiere correr el motor o la prueba de Telegram desde el panel).

Un usuario nuevo no recibe lo que ya había antes de crear su perfil. Pruebas del envío: `node --test tests/alerts.test.mjs`.

Para probar localmente sin enviar: `node alerts/send.mjs --dry-run`.

## Configurar desde el panel

El ícono de engranaje del panel abre `config.html`: un formulario para cambiar el presupuesto por mercado, la pérdida máxima, las reglas de operación, la watchlist del motor, los setups y qué avisos manda Telegram. Al guardar, la página hace un commit en `engine/config.json` y/o `alerts/telegram.json` con la API de GitHub, y el motor usa los valores nuevos en la próxima corrida (o enseguida, si marcás *Correr el motor apenas se guarde*).

**Cómo se entra.** GitHub Pages es estático, así que no hay servidor que guarde una contraseña. Se entra con un token *fine-grained* de GitHub:

1. [Crear token](https://github.com/settings/personal-access-tokens/new): *Repository access → Only select repositories →* este repo.
2. *Permissions → Repository*: **Contents: Read and write**. Opcional, **Actions: Read and write** para disparar el motor al guardar.
3. Vencimiento de 90 días y pegarlo en la página.

El token queda solo en tu navegador (en la sesión, o en `localStorage` si marcás *Recordarme*) y la página solo puede conectarse a `api.github.com` (tiene una Content-Security-Policy que bloquea cualquier otro destino). Sólo quien tiene permiso de escritura en el repo puede guardar. Para revocarlo: *GitHub → Settings → Developer settings → Fine-grained tokens*.

**Validación.** El formulario no deja guardar valores fuera de rango (pérdida máxima entre 0,05 % y 10 %, objetivo mayor o igual al R:R mínimo, tickers con formato válido, etc.). Las mismas reglas están en `engine/config_check.py`: el motor las vuelve a chequear al arrancar y falla con un mensaje claro si alguien edita el JSON a mano con un valor imposible. Para chequear a mano: `python -m engine.config_check`.
