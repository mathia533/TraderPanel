"""Escáner de patrones clásicos sobre velas diarias cerradas.

Busca en las últimas ~90 ruedas:
  - Doble piso (alcista): dos mínimos parecidos con un rebote en el medio. Entrada si
    cierra arriba del máximo del medio (la "línea de cuello").
  - Hombro-cabeza-hombro invertido (alcista) y HCH (bajista, aviso para el que está comprado).
  - Triángulos: ascendente (alcista), simétrico (neutral, se opera la ruptura hacia arriba)
    y descendente (bajista).
Cada patrón trae una entrada condicional (gatillo), stop, objetivo por "proyección de la
altura" del patrón, la relación R:R y los puntos para dibujarlo en la ficha.
Es una detección geométrica simple: sirve para mirar, no reemplaza el criterio.
"""

from __future__ import annotations

LOOK = 90       # ruedas que mira
WIN = 4         # un pivote es el máximo/mínimo de ±WIN ruedas
MAX_EXT = 0.04  # si ya se alejó más de 4 % del gatillo, la ruptura es vieja


def _n(v: float) -> str:
    d = 0 if abs(v) >= 1000 else 2 if abs(v) >= 1 else 4
    return f"{v:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _pivots(vals: list[float], kind: str, win: int = WIN) -> list[int]:
    out = []
    for i in range(win, len(vals) - win):
        seg = vals[i - win:i + win + 1]
        v = vals[i]
        if (kind == "low" and v == min(seg)) or (kind == "high" and v == max(seg)):
            if not out or i - out[-1] > win:
                out.append(i)
    return out


def _state(close: float, trigger: float, bullish: bool, recent: list[float]) -> str | None:
    """'formándose' si todavía no rompió, 'ruptura' si rompió hace poco, None si ya pasó de largo."""
    if bullish:
        if close <= trigger:
            return "formándose" if close >= trigger * 0.9 else None
        return "ruptura" if close <= trigger * (1 + MAX_EXT) and min(recent) <= trigger * 1.005 else None
    if close >= trigger:
        return "formándose" if close <= trigger * 1.1 else None
    return "ruptura" if close >= trigger * (1 - MAX_EXT) and max(recent) >= trigger * 0.995 else None


def _pack(ptype, name, bias, state, trigger, stop, target, pts, dates, why):
    rr = None
    if bias == "alcista" and trigger > stop > 0:
        rr = round((target - trigger) / (trigger - stop), 2)
    return {"type": ptype, "name": name, "bias": bias, "state": state, "trigger": round(trigger, 4),
            "stop": round(stop, 4), "target": round(target, 4), "rr": rr,
            "points": [[dates[i], round(p, 4)] for i, p in pts], "why": why}


def _double_bottom(lo, hi, cl, dates, lows):
    for a in range(len(lows) - 1, 0, -1):
        i2 = lows[a]
        for b in range(a - 1, -1, -1):
            i1 = lows[b]
            if i2 - i1 < 10:
                continue
            if i2 - i1 > 60:
                break
            l1, l2 = lo[i1], lo[i2]
            if abs(l1 - l2) / min(l1, l2) > 0.03:
                continue
            mid = max(range(i1, i2 + 1), key=lambda k: hi[k])
            neck = hi[mid]
            if neck < min(l1, l2) * 1.05 or min(lo[i2:]) < min(l1, l2) * 0.985:
                continue
            st = _state(cl[-1], neck, True, cl[-3:])
            if not st:
                continue
            low = min(l1, l2)
            return _pack("double_bottom", "Doble piso", "alcista", st, neck, low * 0.99, neck + (neck - low),
                         [(i1, l1), (mid, neck), (i2, l2), (len(cl) - 1, cl[-1])], dates,
                         f"Dos mínimos en {_n(l1)} y {_n(l2)} con rebote a {_n(neck)}. Entrada si cierra arriba de {_n(neck)}.")
    return None


def _hs(lo, hi, cl, dates, lows, highs, inverse: bool):
    piv, src = (lows, lo) if inverse else (highs, hi)
    other = hi if inverse else lo
    for k in range(len(piv) - 1, 1, -1):
        s1, h, s2 = piv[k - 2], piv[k - 1], piv[k]
        if s2 - s1 < 12 or s2 - s1 > 80:
            continue
        a, c, b = src[s1], src[h], src[s2]
        if inverse:
            ok = c < min(a, b) * 0.97 and abs(a - b) / min(a, b) <= 0.05
        else:
            ok = c > max(a, b) * 1.03 and abs(a - b) / min(a, b) <= 0.05
        if not ok:
            continue
        m1 = (max if inverse else min)(range(s1, h + 1), key=lambda i: other[i])
        m2 = (max if inverse else min)(range(h, s2 + 1), key=lambda i: other[i])
        neck = (other[m1] + other[m2]) / 2
        if inverse and min(lo[s2:]) < b * 0.985:
            continue
        if not inverse and max(hi[s2:]) > b * 1.015:
            continue
        st = _state(cl[-1], neck, inverse, cl[-3:])
        if not st:
            continue
        pts = [(s1, a), (m1, other[m1]), (h, c), (m2, other[m2]), (s2, b), (len(cl) - 1, cl[-1])]
        if inverse:
            return _pack("inv_head_shoulders", "Hombro-cabeza-hombro invertido", "alcista", st, neck, b * 0.99,
                         neck + (neck - c), pts, dates,
                         f"Cabeza en {_n(c)} entre hombros en {_n(a)} y {_n(b)}; cuello en {_n(neck)}. Entrada si cierra arriba del cuello.")
        return _pack("head_shoulders", "Hombro-cabeza-hombro", "bajista", st, neck, b * 1.01,
                     neck - (c - neck), pts, dates,
                     f"Cabeza en {_n(c)} entre hombros en {_n(a)} y {_n(b)}; cuello en {_n(neck)}. "
                     f"Si cierra abajo del cuello, aviso de salida para el que está comprado.")
    return None


def _fit(idx: list[int], vals: list[float]):
    n = len(idx)
    mx, my = sum(idx) / n, sum(vals) / n
    den = sum((x - mx) ** 2 for x in idx) or 1
    m = sum((x - mx) * (y - my) for x, y in zip(idx, vals)) / den
    return m, my - m * mx


def _triangle(lo, hi, cl, dates, lows, highs):
    n = len(cl)
    start = n - 45
    hs = [i for i in highs if i >= start]
    ls = [i for i in lows if i >= start]
    if len(hs) < 2 or len(ls) < 2:
        return None
    mh, bh = _fit(hs, [hi[i] for i in hs])
    ml, bl = _fit(ls, [lo[i] for i in ls])
    ref = cl[-1]
    sh, sl = mh / ref * 100, ml / ref * 100    # pendiente en % por rueda
    flat = 0.06
    x0, x1 = min(hs[0], ls[0]), n - 1
    up0, dn0 = mh * x0 + bh, ml * x0 + bl
    up1, dn1 = mh * x1 + bh, ml * x1 + bl
    height = up0 - dn0
    if height <= 0 or up1 <= dn1 or (up1 - dn1) > height * 0.75 or height / ref < 0.04:
        return None  # no converge o es muy chato
    # Los precios tienen que haber respetado las líneas (con 1,5 % de tolerancia)
    for i in range(x0, n - 3):
        if hi[i] > (mh * i + bh) * 1.015 or lo[i] < (ml * i + bl) * 0.985:
            return None
    if abs(sh) <= flat and sl > flat:
        t, name, bias = "asc_triangle", "Triángulo ascendente", "alcista"
    elif sh < -flat and sl > flat:
        t, name, bias = "sym_triangle", "Triángulo simétrico", "alcista"
    elif sh < -flat and abs(sl) <= flat:
        t, name, bias = "desc_triangle", "Triángulo descendente", "bajista"
    else:
        return None
    if bias == "alcista":
        st = _state(cl[-1], up1, True, cl[-3:])
        if not st:
            return None
        return _pack(t, name, bias, st, up1, dn1 * 0.99, up1 + height, [(x0, up0), (x1, up1), (x0, dn0), (x1, dn1)], dates,
                     f"Techo en {_n(up1)} y piso en {_n(dn1)} que se van cerrando. Entrada si cierra arriba de {_n(up1)}.")
    st = _state(cl[-1], dn1, False, cl[-3:])
    if not st:
        return None
    return _pack(t, name, bias, st, dn1, up1 * 1.01, dn1 - height, [(x0, up0), (x1, up1), (x0, dn0), (x1, dn1)], dates,
                 f"Piso en {_n(dn1)} con máximos cada vez más bajos. Si pierde {_n(dn1)}, aviso de salida.")


def scan(closed: list[dict]) -> list[dict]:
    """closed: velas cerradas, la más nueva primero (como las usa el motor)."""
    bars = list(reversed(closed[:LOOK]))
    if len(bars) < 40:
        return []
    try:
        lo = [float(b["low"]) for b in bars]
        hi = [float(b["high"]) for b in bars]
        cl = [float(b["close"]) for b in bars]
    except (KeyError, TypeError, ValueError):
        return []
    dates = [str(b["date"])[:10] for b in bars]
    # Sin máximos y mínimos reales (sólo cierres) se usa el cierre
    if all(h == l for h, l in zip(hi[-20:], lo[-20:])):
        lo, hi = cl, cl
    lows, highs = _pivots(lo, "low"), _pivots(hi, "high")
    out = []
    for f in (lambda: _double_bottom(lo, hi, cl, dates, lows),
              lambda: _hs(lo, hi, cl, dates, lows, highs, inverse=True),
              lambda: _hs(lo, hi, cl, dates, lows, highs, inverse=False),
              lambda: _triangle(lo, hi, cl, dates, lows, highs)):
        try:
            p = f()
        except (ValueError, ZeroDivisionError):
            p = None
        if p:
            out.append(p)
    return out
