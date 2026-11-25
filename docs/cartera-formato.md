# Formato de cartera para importar (`traderpanel-cartera`, versión 1)

Un archivo JSON que se importa en **Mi cartera** (`cartera.html` → **Importar archivo**). Sirve para cualquier broker: lo completás a mano, lo exportás de una planilla o lo genera Claude con un conector (por ejemplo, el de InvertirOnline). Ejemplo completo: [`cartera-plantilla.json`](cartera-plantilla.json).

```json
{
  "formato": "traderpanel-cartera",
  "version": 1,
  "broker": "InvertirOnline",
  "fecha": "2026-11-25",
  "activos": [
    {"simbolo": "KO", "cantidad": 13, "precio_compra": 30000},
    {"simbolo": "AL30", "cantidad": 3281},
    {"simbolo": "IOLCAMA", "cantidad": 4897.78, "tipo": "fci", "valor": 62109}
  ],
  "efectivo": [{"moneda": "ARS", "monto": 19333.93}]
}
```

| Campo | Obligatorio | Qué es |
|---|---|---|
| `formato`, `version` | sí | Siempre `"traderpanel-cartera"` y `1`. |
| `activos[].simbolo` | sí | El símbolo en BYMA (`KO`, `GGAL`, `AL30`, `YMCJO`…). |
| `activos[].cantidad` | sí | Cantidad que tenés. En bonos y ONs, valor nominal (como lo muestra el broker). |
| `activos[].precio_compra` | no | Tu precio promedio de compra, en la misma unidad que cotiza BYMA (bonos y ONs, cada 100 de VN). Sirve para calcular la ganancia. |
| `activos[].tipo` | no | `cedear`, `accion`, `bono`, `on`, `letra`, `fci` u `otro`. Si el símbolo cotiza en BYMA, se detecta solo. |
| `activos[].valor` | no | Valor total en pesos, solo para lo que no cotiza en BYMA (fondos comunes, por ejemplo). |
| `activos[].descripcion` | no | Texto libre. |
| `broker`, `fecha`, `moneda` | no | Informativos. Montos en pesos. |
| `efectivo[]` | no | `{"moneda": "ARS" o "USD", "monto": número}`. |

**Precios:** no hace falta ponerlos. El motor usa el último precio de BYMA (data912) y lo actualiza cada hora.

**Privacidad:** el archivo queda en tu compu. Al importarlo, la página lo guarda en un repo **privado** de tu cuenta de GitHub (`<tu-usuario>/traderpanel-cartera`), que solo se lee con tu token. Nunca lo subas al repo del panel, que es público.
