# TraderPanel

Panel web de una sola página con el dólar en Argentina, acciones (ADRs y BYMA) y cripto, con señales técnicas de entrada y salida. No necesita servidor ni build: abrí `index.html` en el navegador o publicalo con GitHub Pages.

## Qué muestra

- **Dólar**: blue, MEP, CCL y oficial con la brecha contra el oficial y la tendencia de los últimos 30 días.
- **Watchlist** con pestañas para ADRs (USD), BYMA (ARS), cripto y dólares. Podés agregar y quitar activos, y las listas quedan guardadas en el navegador.
- **Señal técnica** por activo (compra, venta o neutral, con un puntaje de 0 a 100) calculada sobre cierres diarios:
  - precio contra la media de 20 ruedas (±15)
  - media de 20 contra media de 50 (±15)
  - RSI de 14 ruedas (entre −10 y +10)
  - variación de las últimas 5 ruedas (±10)
- **Señales activas**: entradas (ruptura del máximo de 20 ruedas o cruce hacia arriba de la media de 20) y salidas (pérdida de la media de 20), con stop, objetivo a 2R y la hora en que aparecieron.
- **Alertas por Telegram**: por ahora guarda el canal y qué eventos avisar. El envío real queda para el próximo paso.
- Estado de BYMA y NYSE (sin contemplar feriados), modo claro y oscuro, y vista para celular.

## Fuentes

[DolarAPI](https://dolarapi.com), [ArgentinaDatos](https://argentinadatos.com) (historial del dólar), [data912](https://data912.com) (acciones e historial) y [CoinGecko](https://www.coingecko.com). Los historiales se guardan 6 horas en el navegador para no saturar las APIs.

Los datos tienen demora y las señales son indicadores técnicos simples, no asesoramiento financiero.
