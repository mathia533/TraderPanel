"""Mi cartera de InvertirOnline (IOL) analizada con las reglas del motor.

    python -m engine.cartera                    # abre carteras/*.json (y los secrets del dueño) y escribe data/cartera/<login>.enc
    python -m engine.cartera --desde FILE       # usa una cartera guardada (formato de la API o del conector)
    python -m engine.cartera --desde FILE --sin-cifrar OUT.json   # prueba local, nunca dentro de data/

Solo LEE la cuenta: pide el token, la cartera y el estado de cuenta. No coloca,
cancela ni modifica órdenes ni stops.

El repo y el panel son públicos, así que la cartera nunca se guarda en claro:
data/cartera/<login>.enc va cifrado con AES-256-GCM y una clave derivada (PBKDF2-SHA256)
de la frase de cada usuario. El panel (cartera.html) la descifra en el navegador
con la misma frase. Los logs de Actions también son públicos: este script solo
imprime cantidades, nunca activos ni montos.

Dos caminos para traer una cartera:

1. Desde el navegador (cada usuario con su propia cuenta): en cartera.html cada uno
   entra a IOL con SU usuario y clave. El navegador habla directo con IOL; la clave
   no se guarda ni pasa por GitHub. La cartera se sube cifrada a carteras/<login>.json
   con una clave K que sale de la frase del usuario. K viaja envuelta con la llave
   pública del repo (engine/cartera_pub.json); el motor la abre con el secret
   CARTERA_PRIVADA, analiza y devuelve data/cartera/<login>.enc cifrado con la misma K.

2. Solo el dueño del repo, opcional: secrets IOL_USUARIO, IOL_CLAVE y CARTERA_CLAVE.

Otros: TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID para el aviso privado cuando cambia
el veredicto de un activo (al chat del perfil de cada uno en users/users.json).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import dtskills, planner, skills, sources

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CONFIG = Path(__file__).resolve().parent / "config.json"
LEGACY = DATA / "cartera.enc"   # versión de un solo usuario (PR #23)
OUT_DIR = DATA / "cartera"       # una cartera cifrada por usuario: <login>.enc
USERS = ROOT / "users" / "users.json"
UPLOADS = ROOT / "carteras"        # lo que sube cada navegador: <login>.json (cifrado)
PUBKEY = ROOT / "engine" / "cartera_pub.json"
IOL = "https://api.invertironline.com"
KDF_ITER = 250_000
MIN_KEY = 12
VERSION = 1

# CEDEAR -> símbolo en EE. UU. cuando no coinciden
CEDEAR_US = {"BRKB": "BRK-B", "DISN": "DIS", "GOOGL": "GOOGL", "BABA": "BABA", "TWTR": "TWTR",
             "BRK.B": "BRK-B", "XLE": "XLE", "AMZN": "AMZN"}

# ETFs de índice: no cuentan para el tope de concentración por activo
INDEX_ETF = {"SPY", "QQQ", "DIA", "IWM", "VOO", "VTI", "EEM", "EWZ", "ACWI", "XLE", "XLF", "XLK", "XLV",
             "XLY", "XLP", "XLI", "XLU", "XLB", "GLD", "SLV", "ARKK", "EFA", "TLT", "IBIT", "ETHA"}

TYPE_ES = {"cedear": "CEDEAR", "accion": "Acción", "bono": "Bono", "on": "Obligación negociable",
           "fci": "Fondo común", "letra": "Letra", "otro": "Otro"}


# ------------------------------------------------------------------ IOL (solo lectura)
def _req(url: str, *, data: bytes | None = None, headers: dict | None = None, timeout: int = 30):
    r = urllib.request.Request(url, data=data, headers={"Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def iol_token(user: str, password: str) -> str:
    body = urllib.parse.urlencode({"username": user, "password": password, "grant_type": "password"}).encode()
    j = _req(f"{IOL}/token", data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    tok = j.get("access_token")
    if not tok:
        raise RuntimeError("IOL no devolvió token (¿API habilitada en la cuenta?)")
    return tok


def fetch_iol(user: str, password: str) -> dict:
    tok = iol_token(user, password)
    h = {"Authorization": f"Bearer {tok}"}
    out = {"source": "api", "portafolio": [], "estadocuenta": None}
    for pais in ("argentina", "estados_Unidos"):
        try:
            out["portafolio"].append(_req(f"{IOL}/api/v2/portafolio/{pais}", headers=h))
        except Exception as exc:  # una cuenta sin EE. UU. puede dar error
            print(f"cartera: portafolio {pais} no disponible ({type(exc).__name__})")
    try:
        out["estadocuenta"] = _req(f"{IOL}/api/v2/estadocuenta", headers=h)
    except Exception as exc:
        print(f"cartera: estado de cuenta no disponible ({type(exc).__name__})")
    return out


# ------------------------------------------------------------------ normalización
def _type(raw: str, symbol: str) -> str:
    t = (raw or "").lower().replace("_", " ")
    if "cedear" in t:
        return "cedear"
    if "accion" in t or "acción" in t:
        return "accion"
    if "obligac" in t or t == "on":
        return "on"
    if "fondo" in t or "fci" in t:
        return "fci"
    if "letra" in t:
        return "letra"
    if "public" in t or "bono" in t or "titulo" in t:
        return "bono"
    return "otro"


def _cur(raw: str) -> str:
    t = (raw or "").lower()
    return "US$" if ("dolar" in t or "usd" in t or "u$s" in t) else "$"


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def normalize(src: dict) -> tuple[list[dict], list[dict]]:
    """Acepta la respuesta de la API v2 de IOL ({portafolio: [...], estadocuenta}) o la del
    conector de IOL ({positions: [...]}, o una lista de esas). Devuelve (tenencias, efectivo)."""
    holdings, cash = [], []
    ports = src.get("portafolio") if isinstance(src, dict) and "portafolio" in src else src
    if isinstance(ports, dict):
        ports = [ports]
    for p in ports or []:
        for a in p.get("activos") or []:  # API v2
            t = a.get("titulo") or {}
            qty, price = _f(a.get("cantidad")), _f(a.get("ultimoPrecio"))
            value = _f(a.get("valorizado"))
            holdings.append({
                "symbol": str(t.get("simbolo", "")).upper(), "desc": t.get("descripcion") or "",
                "type": _type(t.get("tipo"), t.get("simbolo", "")), "market": (t.get("mercado") or "").upper(),
                "currency": _cur(t.get("moneda")), "qty": qty, "price": price, "value": value,
                "ppc": _f(a.get("ppc")), "gain_pct": _f(a.get("gananciaPorcentaje")),
                "gain": _f(a.get("gananciaDinero")), "day_pct": _f(a.get("variacionDiaria")),
            })
        for a in p.get("positions") or []:  # conector
            t = a.get("asset") or {}
            qty, unit = _f(a.get("quantity")), _f(a.get("unit_price"))
            lot = _f(a.get("lot_price"))
            upl = _f(t.get("units_per_lot")) or 1
            value = qty * lot / upl if qty is not None and lot is not None else None
            ppc = _f(a.get("average_price") or a.get("ppc"))
            holdings.append({
                "symbol": str(t.get("symbol", "")).upper(), "desc": t.get("description") or "",
                "type": _type(t.get("type"), t.get("symbol", "")), "market": (t.get("market") or "").upper(),
                "currency": "US$" if (t.get("currency") or "").upper() == "USD" else "$",
                "qty": qty, "price": lot, "value": value, "ppc": ppc,
                "gain_pct": round((lot / ppc - 1) * 100, 2) if ppc and lot else None,
                "gain": None, "day_pct": _f(a.get("daily_change_pct")),
            })
    ec = src.get("estadocuenta") if isinstance(src, dict) else None
    for c in (ec or {}).get("cuentas") or []:
        disp = _f(c.get("disponible"))
        if disp:
            cash.append({"currency": _cur(c.get("moneda")), "amount": disp})
    for c in (src.get("cash") or []) if isinstance(src, dict) else []:
        if _f(c.get("amount")):
            cash.append({"currency": c.get("currency", "$"), "amount": _f(c["amount"])})
    holdings = [h for h in holdings if h["symbol"] and (h["qty"] or 0) > 0]
    return holdings, cash


# ------------------------------------------------------------------ análisis
def analyzable(h: dict) -> tuple[str, str] | None:
    """(mercado del motor, símbolo) para bajar velas, o None si es renta fija / fondo."""
    if h["type"] == "cedear":
        return "adr", CEDEAR_US.get(h["symbol"], h["symbol"])
    if h["type"] == "accion":
        return ("adr", h["symbol"]) if h["currency"] == "US$" and h["market"] in ("NYSE", "NASDAQ") else ("byma", h["symbol"])
    return None


def analyze_bars(closed: list[dict], live: dict | None, risk: dict, gate: str) -> dict:
    """Lectura de una tenencia con las reglas del motor. `closed` va de la vela más nueva a la más vieja."""
    c = [b["close"] for b in closed]
    price = (live or closed[0])["close"]
    s20, s50, s200 = planner.sma(c, 20), planner.sma(c, 50), planner.sma(c, 200)
    try:
        sc = dtskills.scanner_setups(closed)
    except Exception:
        sc = {"labels": [], "rsi": None}
    try:
        vcp = skills.run_vcp(closed, None)
    except Exception:
        vcp = {"ok": False}
    try:
        burst = skills.run_burst(closed, gate)
    except Exception:
        burst = {"ok": False}
    score = max(vcp.get("score", 0) if vcp.get("ok") else 0, burst.get("score", 0) if burst.get("ok") else 0)

    # Stop de protección: mínimo de las últimas 10 ruedas, al menos 1,5 ATR por debajo
    # (para que el ruido diario no lo toque) y con el tope de distancia del motor
    max_stop = risk.get("max_stop_pct", 12) / 100
    low10 = min(b["low"] for b in closed[:10])
    trs = [max(b["high"], p["close"]) - min(b["low"], p["close"]) for b, p in zip(closed[:14], closed[1:15])]
    atr = sum(trs) / len(trs) if trs else 0
    stop = max(min(low10, price - 1.5 * atr), price * (1 - max_stop))
    if stop >= price:
        stop = price * (1 - max_stop / 2)
    stop_pct = (price - stop) / price * 100
    target = price + risk.get("target_r", 2) * (price - stop)

    up = s50 is not None and s200 is not None and price > s50 > s200
    stage4 = s50 is not None and s200 is not None and price < s50 < s200
    rsi = sc.get("rsi")
    notes = []
    if stage4 or (s200 is not None and s50 is not None and price < s200 and price < s50):
        verdict, why = "contra", "Está debajo de sus medias de 50 y 200 ruedas: la tendencia juega en contra."
        score = min(score, 30)
    elif s50 is not None and price < s50:
        verdict, why = "ojo", "Cerró debajo de la media de 50 ruedas, que es donde el motor sale de una compra por tendencia."
    elif up:
        verdict, why = "favor", "Está sobre sus medias de 50 y 200 ruedas y la de 50 va arriba de la de 200."
    else:
        verdict, why = "ojo", "Sin tendencia clara: las medias no se ponen de acuerdo."
    if rsi is not None and rsi >= 75:
        notes.append(f"RSI {rsi:.0f}: subió muy rápido, es común que corrija.")
        if verdict == "favor":
            verdict = "ojo"
    if stop_pct < 3:
        notes.append("El precio está muy cerca del mínimo de las últimas 10 ruedas.")

    try:
        tm = dtskills.swing_timemachine(closed, price, stop, target, horizon=10, replays=4000)
    except Exception as exc:
        tm = {"ok": False, "error": str(exc)[:120]}
    tm_out = None
    if tm.get("ok"):
        mc = tm["mc"]
        tm_out = {"lean": tm["lean"], "lean_es": dtskills.LEAN_ES.get(tm["lean"], ""), "verdict": tm["verdict"],
                  "p_target": mc.get("p_target"), "p_stop": mc.get("p_stop")}

    return {
        "price": price, "date": (live or closed[0])["date"], "sma20": s20, "sma50": s50, "sma200": s200,
        "rsi": rsi, "setups": sc.get("labels") or [], "score": round(score),
        "vcp": vcp.get("score") if vcp.get("ok") else None, "burst": burst.get("score") if burst.get("ok") else None,
        "stop": stop, "stop_pct": round(stop_pct, 2), "target": target,
        "verdict": verdict, "why": why, "notes": notes, "tm": tm_out,
        "spark": [round(x, 4) for x in reversed(c[:120])],
    }


def build(holdings: list[dict], cash: list[dict], cfg: dict, signals: dict, *, fetch=None, now=None) -> dict:
    """Arma el informe completo de la cartera (lo que se cifra)."""
    now = now or datetime.now(timezone.utc)
    fetch = fetch or sources.fetch_history
    risk = cfg.get("risk", {})
    regimes = (signals or {}).get("regime") or (signals or {}).get("markets") or {}
    errors = 0
    rows = []
    for h in holdings:
        row = dict(h)
        row["type_es"] = TYPE_ES.get(h["type"], "Otro")
        target = analyzable(h)
        if target:
            kind, sym = target
            gate = (regimes.get(kind) or {}).get("gate", "neutral")
            row["ref"] = {"kind": kind, "symbol": sym, "gate": gate,
                          "underlying": h["type"] == "cedear"}
            try:
                bars, src = fetch(kind, sym)
                closed, live = sources.split_partial(kind, bars, now)
                if len(closed) < 60:
                    raise ValueError("historial corto")
                a = analyze_bars(closed, live, risk, gate)
                a["src"] = src
                # El stop se calcula sobre el subyacente; en pesos se aplica la misma distancia al CEDEAR
                if h.get("price"):
                    a["stop_local"] = h["price"] * (1 - a["stop_pct"] / 100)
                    if h.get("qty") and h.get("value"):
                        a["at_risk"] = h["value"] * a["stop_pct"] / 100
                row["analysis"] = a
            except Exception as exc:
                errors += 1
                row["analysis"] = {"verdict": "nodata", "why": f"No se pudieron bajar las velas ({type(exc).__name__})."}
            time.sleep(0.2 if fetch is sources.fetch_history else 0)
        else:
            row["analysis"] = {"verdict": "na",
                               "why": ("Renta fija: el análisis de tendencia no aplica. Lo que importa es la tasa (TIR), "
                                       "el vencimiento y el emisor.") if h["type"] in ("bono", "on", "letra") else
                               ("Fondo común: se mira el rendimiento y el plazo de rescate, no la tendencia." if h["type"] == "fci"
                                else "Sin análisis para este tipo de activo.")}
        rows.append(row)

    by_cur = {}
    for r in rows:
        if r.get("value"):
            by_cur.setdefault(r["currency"], 0.0)
            by_cur[r["currency"]] += r["value"]
    total = by_cur.get("$", 0.0)
    for r in rows:
        r["weight"] = round(r["value"] / by_cur[r["currency"]] * 100, 2) if r.get("value") and by_cur.get(r["currency"]) else None
    by_type = {}
    for r in rows:
        if r.get("value") and r["currency"] == "$":
            by_type[r["type_es"]] = by_type.get(r["type_es"], 0) + r["value"]
    max_pos = (risk.get("byma") or {}).get("max_position_pct", 25)
    alerts = []
    for r in rows:
        if r.get("weight") and r["weight"] > max_pos and r["type"] in ("cedear", "accion") and r["symbol"] not in INDEX_ETF:
            alerts.append(f"{r['symbol']} pesa {r['weight']:.0f} % de la cartera en pesos (el motor no pasa de {max_pos} % por activo).")
    at_risk = sum((r["analysis"].get("at_risk") or 0) for r in rows if r["currency"] == "$")
    counts = {}
    for r in rows:
        v = r["analysis"]["verdict"]
        counts[v] = counts.get(v, 0) + 1
    return {
        "v": VERSION, "generated_at": now.isoformat(), "holdings": rows, "cash": cash,
        "totals": {"by_currency": by_cur, "by_type": by_type, "at_risk": at_risk,
                   "at_risk_pct": round(at_risk / total * 100, 2) if total else None,
                   "gain": sum(r.get("gain") or 0 for r in rows if r["currency"] == "$") or None},
        "counts": counts, "alerts": alerts,
        "regime": {k: {"gate": (v or {}).get("gate"), "why": (v or {}).get("why")} for k, v in regimes.items()},
        "errors": errors,
    }


# ------------------------------------------------------------------ cifrado
def _key(passphrase: str, salt: bytes, iters: int = KDF_ITER) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, iters, dklen=32)


def _b64(x: bytes) -> str:
    return base64.b64encode(x).decode()


def encrypt(obj: dict, passphrase: str | None = None, *, key: bytes | None = None, salt: bytes | None = None,
            iters: int = KDF_ITER) -> dict:
    """Cifra con la frase (deriva la clave) o con una clave ya derivada (`key`, con su `salt` e `iters`,
    para que el navegador la vuelva a derivar de la misma frase)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if key is None:
        salt = os.urandom(16)
        key = _key(passphrase, salt, iters)
    iv = os.urandom(12)
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ct = AESGCM(key).encrypt(iv, data, None)
    return {"v": VERSION, "alg": "AES-256-GCM", "kdf": "PBKDF2-SHA256", "iter": iters,
            "salt": _b64(salt), "iv": _b64(iv), "data": _b64(ct), "updated": obj.get("generated_at")}


def decrypt(blob: dict, passphrase: str | None = None, *, key: bytes | None = None) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    d = lambda k: base64.b64decode(blob[k])
    if key is None:
        key = _key(passphrase, d("salt"), blob.get("iter", KDF_ITER))
    pt = AESGCM(key).decrypt(d("iv"), d("data"), None)
    return json.loads(pt.decode("utf-8"))


# ------------------------------------------------------------------ carteras subidas desde el navegador
def open_upload(upload: dict, private_pem: str) -> tuple[bytes, dict]:
    """carteras/<login>.json: la cartera que el navegador bajó de IOL, cifrada con la clave K que sale
    de la frase del usuario; K viaja envuelta con la llave pública del repo (RSA-OAEP SHA-256).
    Devuelve (K, cartera)."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    priv = serialization.load_pem_private_key(private_pem.encode(), password=None)
    k = priv.decrypt(base64.b64decode(upload["key"]),
                     padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    if len(k) != 32:
        raise ValueError("clave envuelta inválida")
    return k, decrypt(upload, key=k)


# ------------------------------------------------------------------ avisos privados
VERDICT_ES = {"favor": "✓ A FAVOR", "ojo": "! OJO", "contra": "✕ EN CONTRA"}


def verdict_changes(prev: dict | None, cur: dict) -> list[str]:
    """Mensajes cuando cambia el veredicto de una tenencia respecto de la corrida anterior."""
    if not prev:
        return []
    old = {r["symbol"]: (r.get("analysis") or {}).get("verdict") for r in prev.get("holdings", [])}
    msgs = []
    for r in cur["holdings"]:
        a = r.get("analysis") or {}
        v, o = a.get("verdict"), old.get(r["symbol"])
        if v in VERDICT_ES and o in VERDICT_ES and v != o:
            msgs.append(f"<b>Mi cartera · {r['symbol']}</b>: {VERDICT_ES[o]} → <b>{VERDICT_ES[v]}</b>\n{a.get('why', '')}")
    return msgs


def send_telegram(msgs: list[str], chat: str | None = None) -> int:
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not (tok and chat and msgs):
        return 0
    sent = 0
    for m in msgs[:10]:
        body = urllib.parse.urlencode({"chat_id": chat, "text": m, "parse_mode": "HTML",
                                       "disable_web_page_preview": "true"}).encode()
        try:
            _req(f"https://api.telegram.org/bot{tok}/sendMessage", data=body,
                 headers={"Content-Type": "application/x-www-form-urlencoded"})
            sent += 1
        except Exception as exc:
            print(f"cartera: Telegram falló ({type(exc).__name__})")
    return sent


# ------------------------------------------------------------------ main
def chat_for(profile: dict, env: dict) -> str | None:
    """Chat privado del usuario según su perfil: "default" es el secret TELEGRAM_CHAT_ID."""
    chat = str(((profile or {}).get("telegram") or {}).get("chat") or "").strip()
    if chat == "default":
        return env.get("TELEGRAM_CHAT_ID")
    return chat if chat.startswith(("-", "@")) or chat.isdigit() else None


def run_user(login: str, src: dict, *, passphrase: str | None = None, key: bytes | None = None,
             salt: bytes | None = None, iters: int = KDF_ITER, cfg: dict, signals: dict, chat: str | None,
             fetch=None, out_dir: Path = OUT_DIR) -> dict:
    holdings, cash = normalize(src)
    report = build(holdings, cash, cfg, signals, fetch=fetch)
    report["source"] = src.get("source", "conector") if isinstance(src, dict) else "conector"
    report["synced_at"] = src.get("synced_at") if isinstance(src, dict) else None
    report["login"] = login
    out = out_dir / f"{login}.enc"
    prev = None
    for f in (out, LEGACY):
        if f.exists():
            try:
                prev = decrypt(json.loads(f.read_text()), passphrase, key=key)
                break
            except Exception:
                pass
    if os.environ.get("CARTERA_AVISOS", "1") != "0":
        n = send_telegram(verdict_changes(prev, report), chat)
        if n:
            print(f"cartera {login}: {n} avisos privados enviados")
    out.parent.mkdir(parents=True, exist_ok=True)
    blob = encrypt(report, key=key, salt=salt, iters=iters) if key else encrypt(report, passphrase)
    out.write_text(json.dumps(blob))
    print(f"cartera {login}: {len(holdings)} tenencias, {report['errors']} sin velas, cifrada")
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--desde", type=Path, help="cartera guardada (JSON de la API de IOL o del conector)")
    ap.add_argument("--sin-cifrar", type=Path, help="escribe el informe en claro en este archivo (solo pruebas, fuera de data/)")
    ap.add_argument("--offline", type=Path, help="carpeta con velas guardadas (<kind>_<símbolo>.json)")
    ap.add_argument("--subidas", type=Path, default=UPLOADS, help="carpeta de carteras subidas desde el navegador")
    args = ap.parse_args(argv)

    env = os.environ
    try:
        profiles = json.loads(USERS.read_text()).get("users") or {}
    except Exception:
        profiles = {}
    owner = env.get("GITHUB_REPOSITORY_OWNER") or next(iter(profiles), None)
    cfg = json.loads(CONFIG.read_text())
    try:
        signals = json.loads((DATA / "signals.json").read_text())
    except Exception:
        signals = {}
    fetch = None
    if args.offline:
        def fetch(kind, sym):
            return json.loads((args.offline / f"{kind}_{sym}.json").read_text()), "offline"

    if args.sin_cifrar:
        if DATA.resolve() in args.sin_cifrar.resolve().parents:
            print("cartera: --sin-cifrar no puede escribir dentro de data/ (es público)")
            return 2
        src = json.loads(args.desde.read_text())
        h, cash = normalize(src)
        report = build(h, cash, cfg, signals, fetch=fetch)
        report["source"] = src.get("source", "conector") if isinstance(src, dict) else "conector"
        args.sin_cifrar.write_text(json.dumps(report, ensure_ascii=False, indent=1))
        print(f"cartera: {len(h)} tenencias, informe en claro escrito")
        return 0

    done, failed = set(), 0
    # 1) Carteras que cada uno sincronizó desde su navegador (su clave de IOL nunca sale de ahí)
    priv = env.get("CARTERA_PRIVADA", "")
    uploads = sorted(args.subidas.glob("*.json")) if args.subidas.exists() else []
    if uploads and not priv:
        print("cartera: hay carteras subidas pero falta el secret CARTERA_PRIVADA")
    for f in uploads if priv else []:
        login = f.stem
        try:
            up = json.loads(f.read_text())
            if up.get("login") != login:
                raise ValueError("el login no coincide con el archivo")
            k, src = open_upload(up, priv)
        except Exception as exc:
            print(f"cartera {login}: no se pudo abrir lo subido ({type(exc).__name__})")
            failed += 1
            continue
        run_user(login, src, key=k, salt=base64.b64decode(up["salt"]), iters=int(up.get("iter", KDF_ITER)),
                 cfg=cfg, signals=signals, chat=chat_for(profiles.get(login), env), fetch=fetch)
        done.add(login)

    # 2) Opcional, solo el dueño del repo: sus propios secrets de IOL (PR #23)
    if args.desde or (env.get("IOL_USUARIO") and env.get("IOL_CLAVE")):
        login = owner or "local"
        key = env.get("CARTERA_CLAVE", "")
        if login in done:
            pass
        elif len(key) < MIN_KEY:
            print(f"cartera: falta CARTERA_CLAVE (mínimo {MIN_KEY} caracteres); no se publica la del dueño")
        else:
            try:
                src = json.loads(args.desde.read_text()) if args.desde else fetch_iol(env["IOL_USUARIO"], env["IOL_CLAVE"])
                run_user(login, src, passphrase=key, cfg=cfg, signals=signals,
                         chat=chat_for(profiles.get(login), env), fetch=fetch)
                done.add(login)
            except Exception as exc:
                # No mostrar el detalle: puede traer datos de la cuenta
                print(f"cartera {login}: no se pudo entrar a IOL ({type(exc).__name__})")
                failed += 1

    if done and LEGACY.exists():
        LEGACY.unlink()  # el archivo de un solo usuario pasó a data/cartera/<login>.enc
    if not done and not failed:
        print("cartera: nadie sincronizó su cartera todavía; se saltea")
    return 1 if failed and not done else 0


if __name__ == "__main__":
    sys.exit(main())
