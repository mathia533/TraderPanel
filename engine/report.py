"""Informe post-cierre: resumen del día en texto y en una tarjeta vertical (1080×1920) para Telegram.

El motor arma el resumen (`summarize`) en la corrida de después del cierre de Nueva York
(BYMA ya cerró) y lo deja como evento "info" con id report:<fecha> en data/events.json.
El workflow "Alertas Telegram" dibuja la tarjeta antes de enviar:

    python -m engine.report --render DIR     # una PNG por informe pendiente en DIR/

y alerts/send.mjs la manda como foto con el texto de pie. Si no hay tarjeta, va el texto solo.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MK = {"adr": "ADRs", "byma": "BYMA", "cripto": "Cripto"}
GATE = {"allowed": "favorable", "neutral": "neutral", "restrictive": "defensivo"}
REASON = {"stop": "stop", "target": "objetivo", "trend": "tendencia", "time": "tiempo", "flat": "cierre del día", "manual": "manual"}


def _n(v, d=2):
    if v is None:
        return "—"
    return f"{v:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _pct(v):
    return "—" if v is None else f"{'+' if v >= 0 else '−'}{_n(abs(v), 1)} %"


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def summarize(assets: dict, markets: dict, positions: list, day_events: list, patterns: list,
              sectors: dict | None, date: str) -> dict:
    """Junta lo del día. assets: signals.assets (con day_ch); day_events: los eventos de hoy."""
    movers = {}
    for kind in ("adr", "byma", "cripto"):
        xs = [a for a in assets.values() if a.get("kind") == kind and a.get("day_ch") is not None]
        xs.sort(key=lambda a: -a["day_ch"])
        if xs:
            movers[kind] = {"up": [[a["label"], round(a["day_ch"], 2)] for a in xs[:3] if a["day_ch"] > 0],
                            "down": [[a["label"], round(a["day_ch"], 2)] for a in reversed(xs[-3:]) if a["day_ch"] < 0],
                            "breadth": [sum(1 for a in xs if a["day_ch"] > 0), len(xs)]}
    sec = None
    if sectors and sectors.get("date") == date and sectors.get("ranking"):
        r = sectors["ranking"]
        sec = {"best": [r[0]["name"], r[0]["ch"]], "worst": [r[-1]["name"], r[-1]["ch"]]}
    return {
        "date": date,
        "gates": {k: GATE.get(m.get("gate"), m.get("gate")) for k, m in (markets or {}).items()},
        "movers": movers,
        "entries": [[e["label"], e.get("price"), e.get("setup")] for e in day_events if e.get("type") == "entry"],
        "exits": [[e["label"], e.get("r"), REASON.get(e.get("reason"), e.get("reason"))] for e in day_events if e.get("type") == "exit"],
        "positions": [[p["label"], p.get("r_now")] for p in positions or []],
        "patterns": [[p["label"], p["name"], p["bias"]] for p in patterns or [] if p.get("state") == "ruptura"][:4],
        "sectors": sec,
    }


def text(s: dict) -> str:
    """Pie de la foto (Telegram admite hasta 1024 caracteres) o mensaje si no hay tarjeta."""
    d = s["date"]
    lines = [f"🌙 <b>INFORME DEL CIERRE</b> · {d[8:10]}/{d[5:7]}"]
    for kind, m in s["movers"].items():
        up = ", ".join(f"{_esc(t)} {_pct(v)}" for t, v in m["up"][:2]) or "—"
        dn = ", ".join(f"{_esc(t)} {_pct(v)}" for t, v in m["down"][:2]) or "—"
        lines.append(f"<b>{MK[kind]}</b> ({m['breadth'][0]}/{m['breadth'][1]} suben): ▲ {up} · ▼ {dn}")
    if s.get("sectors"):
        b, w = s["sectors"]["best"], s["sectors"]["worst"]
        lines.append(f"Sectores BYMA: mejor {_esc(b[0])} {_pct(b[1])}, peor {_esc(w[0])} {_pct(w[1])}")
    if s["entries"] or s["exits"]:
        lines.append("Motor hoy: " + "; ".join([f"compra {_esc(t)}" for t, _, _ in s["entries"]] +
                                               [f"venta {_esc(t)} ({'+' if (r or 0) >= 0 else '−'}{_n(abs(r or 0), 1)}R)" for t, r, _ in s["exits"]]))
    else:
        lines.append("Motor hoy: sin entradas ni salidas.")
    lines.append(f"Posiciones abiertas: {len(s['positions'])}" + (
        " (" + ", ".join(f"{_esc(t)} {'+' if (r or 0) >= 0 else '−'}{_n(abs(r or 0), 1)}R" for t, r in s["positions"][:5]) + ")" if s["positions"] else ""))
    if s["patterns"]:
        lines.append("Patrones que rompieron: " + ", ".join(f"{_esc(t)} ({_esc(n.lower())})" for t, n, _ in s["patterns"]))
    if s["gates"]:
        lines.append("Mercado: " + " · ".join(f"{MK.get(k, k)} {v}" for k, v in s["gates"].items()))
    while len("\n".join(lines)) > 900 and len(lines) > 2:   # pie de foto: Telegram corta en 1024
        lines.pop(-2)
    return "\n".join(lines)


# ------------------------------------------------------------------ tarjeta vertical
W, H = 1080, 1920
COL = {"bg": (2, 6, 23), "card": (14, 18, 35), "line": (30, 41, 59), "ink": (248, 250, 252), "ink2": (203, 213, 225),
       "mute": (148, 163, 184), "up": (74, 222, 128), "dn": (248, 113, 113), "accent": (34, 197, 94), "info": (147, 197, 253)}
FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/usr/share/fonts/truetype/liberation"]


def _font(size: int, bold=False, mono=False):
    from PIL import ImageFont
    names = (["DejaVuSansMono-Bold.ttf", "LiberationMono-Bold.ttf"] if bold else ["DejaVuSansMono.ttf", "LiberationMono-Regular.ttf"]) if mono else \
        (["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"] if bold else ["DejaVuSans.ttf", "LiberationSans-Regular.ttf"])
    for d in FONT_DIRS:
        for n in names:
            p = Path(d) / n
            if p.exists():
                return ImageFont.truetype(str(p), size)
    return ImageFont.load_default(size=size)


def render(s: dict, path: Path, dolar: list | None = None) -> Path:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, H), COL["bg"])
    dr = ImageDraw.Draw(img)
    M = 64
    f_h, f_b, f_t = _font(64, True), _font(38, True), _font(34)
    f_s, f_num, f_numb = _font(28), _font(34, mono=True), _font(44, True, mono=True)
    y = 80
    dr.line([(M, y + 30), (M + 40, y + 6), (M + 62, y + 22), (M + 100, y - 10)], fill=COL["accent"], width=9, joint="curve")
    dr.text((M + 124, y - 14), "TraderPanel", font=f_b, fill=COL["ink"])
    y += 80
    d = s["date"]
    dr.text((M, y), "Informe del cierre", font=f_h, fill=COL["ink"])
    y += 84
    dr.text((M, y), f"{d[8:10]}/{d[5:7]}/{d[:4]} · BYMA y Wall Street", font=f_t, fill=COL["mute"])
    y += 76

    def box(h):
        nonlocal y
        dr.rounded_rectangle([M, y, W - M, y + h], radius=28, fill=COL["card"], outline=COL["line"], width=2)
        top = y
        y += h + 28
        return top

    if dolar:
        t = box(150)
        cols = [x for x in dolar if x[0] in ("MEP", "CCL", "Blue")][:3]
        cw = (W - 2 * M) / max(1, len(cols))
        for i, (name, v) in enumerate(cols):
            x = M + 36 + i * cw
            dr.text((x, t + 26), f"Dólar {name}", font=f_s, fill=COL["mute"])
            dr.text((x, t + 66), f"$ {_n(v, 0)}", font=f_numb, fill=COL["ink"])

    for kind, m in s["movers"].items():
        rows = max(len(m["up"][:2]), len(m["down"][:2]), 1)
        t = box(104 + rows * 50)
        dr.text((M + 36, t + 26), MK[kind], font=f_b, fill=COL["ink"])
        br = f"{m['breadth'][0]} de {m['breadth'][1]} suben"
        dr.text((W - M - 36 - dr.textlength(br, font=f_s), t + 34), br, font=f_s, fill=COL["mute"])
        half = (W - 2 * M) / 2
        for col, key, color in ((0, "up", COL["up"]), (1, "down", COL["dn"])):
            for i, (tk, v) in enumerate(m[key][:2]):
                x, yy = M + 36 + col * half, t + 88 + i * 50
                dr.text((x, yy), tk, font=f_t, fill=COL["ink2"])
                dr.text((x + half - 72 - dr.textlength(_pct(v), font=f_num), yy), _pct(v), font=f_num, fill=color)

    if s.get("sectors"):
        t = box(196)
        b, w = s["sectors"]["best"], s["sectors"]["worst"]
        dr.text((M + 36, t + 26), "Sectores BYMA", font=f_s, fill=COL["mute"])
        for i, (lab, (name, v), c) in enumerate((("▲", b, COL["up"]), ("▼", w, COL["dn"]))):
            dr.text((M + 36, t + 70 + i * 52), f"{lab} {name}", font=f_t, fill=c)
            dr.text((W - M - 36 - dr.textlength(_pct(v), font=f_num), t + 70 + i * 52), _pct(v), font=f_num, fill=c)

    lines = [(f"Compra {t}", COL["up"]) for t, _, _ in s["entries"]] + \
            [(f"Venta {t} {'+' if (r or 0) >= 0 else '−'}{_n(abs(r or 0), 1)}R ({why})", COL["dn"] if (r or 0) < 0 else COL["up"]) for t, r, why in s["exits"]] + \
            [(f"Patrón: {t} · {n.lower()}", COL["info"]) for t, n, _ in s["patterns"]]
    lines = lines or [("Sin entradas ni salidas hoy", COL["mute"])]
    pos = s["positions"]
    lines.append((f"Abiertas ({len(pos)}): " + (", ".join(f"{tk} {'+' if (r or 0) >= 0 else '−'}{_n(abs(r or 0), 1)}R" for tk, r in pos[:3])
                                                 + (" …" if len(pos) > 3 else "") if pos else "ninguna"), COL["ink2"]))
    room = int((H - 190 - y - 100) // 48)
    if len(lines) > room:
        extra = len(lines) - room + 1
        lines = lines[:room - 2] + [(f"… y {extra} más en el panel", COL["mute"]), lines[-1]]
    t = box(96 + len(lines) * 48)
    dr.text((M + 36, t + 26), "Motor de señales", font=f_s, fill=COL["mute"])
    for i, (ln, c) in enumerate(lines):
        dr.text((M + 36, t + 68 + i * 48), ln, font=f_t, fill=c)

    foot = "Datos con demora. No es asesoramiento financiero."
    gates = " · ".join(f"{MK.get(k, k)} {v}" for k, v in s["gates"].items())
    if gates:
        dr.text((M, H - 150), "Mercado: " + gates, font=f_s, fill=COL["ink2"])
    dr.text((M, H - 100), foot, font=f_s, fill=COL["mute"])
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG", optimize=True)
    return path


def fetch_dolar() -> list:
    try:
        req = urllib.request.Request("https://dolarapi.com/v1/dolares", headers={"User-Agent": "TraderPanel"})
        with urllib.request.urlopen(req, timeout=15) as r:
            js = json.loads(r.read().decode())
        names = {"bolsa": "MEP", "contadoconliqui": "CCL", "blue": "Blue"}
        return [[names[d["casa"]], d["venta"]] for d in js if d.get("casa") in names]
    except Exception as exc:  # noqa: BLE001
        print(f"Sin dólar para la tarjeta: {exc}", file=sys.stderr)
        return []


def card_name(ev_id: str) -> str:
    return ev_id.replace(":", "-") + ".png"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--render", type=Path, required=True, help="carpeta donde dejar las tarjetas")
    ap.add_argument("--data-dir", type=Path, default=DATA)
    ap.add_argument("--no-dolar", action="store_true")
    a = ap.parse_args(argv)
    ev = json.loads((a.data_dir / "events.json").read_text(encoding="utf-8")) if (a.data_dir / "events.json").exists() else {}
    reps = [e for e in ev.get("events", []) if e.get("info") == "report" and e.get("report")][:2]
    if not reps:
        print("No hay informes para dibujar.")
        return 0
    dolar = [] if a.no_dolar else fetch_dolar()
    for e in reps:
        p = render(e["report"], a.render / card_name(e["id"]), dolar)
        print(f"Tarjeta: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
