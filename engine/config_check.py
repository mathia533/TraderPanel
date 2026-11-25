"""Validación de engine/config.json, alerts/telegram.json, users/users.json y trades/*.json.

Las mismas reglas están en config.html (el formulario del panel) para avisar antes de
guardar. Acá se vuelven a chequear al arrancar el motor: si alguien edita el JSON a mano
y deja un valor imposible, el Action falla con un mensaje claro en vez de operar con él.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

MARKETS = ("adr", "byma", "cripto")
VCP_RATINGS = ["Textbook VCP", "Strong VCP", "Good VCP", "Developing VCP", "Weak VCP"]
BURST_STATES = ["ACTIONABLE_DAY1", "MANUAL_REVIEW", "MANUAL_REVIEW_ONLY", "WATCH_ONLY"]
TICKER = {
    "adr": re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$"),
    "byma": re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$"),
    "cripto": re.compile(r"^[a-z0-9][a-z0-9\-]{0,49}$"),
}
BENCH = re.compile(r"^[\^A-Za-z0-9.\-=]{1,20}$")
MAX_WATCHLIST = 60

# (clave, mínimo, máximo, entero)
RISK_MARKET = [("budget", 1, 1e12, False), ("max_loss_pct", 0.05, 10, False), ("max_position_pct", 1, 100, False)]
RISK_GLOBAL = [
    ("min_reward_risk", 0.5, 10, False),
    ("target_r", 0.5, 20, False),
    ("max_stop_pct", 0.5, 50, False),
    ("max_open_positions", 1, 50, True),
    ("burst_max_sessions", 1, 60, True),
    ("breakeven_after_r", 0.1, 10, False),
]
FILTERS = [("min_price", 0, 1e9, False), ("min_volume", 0, 1e13, False)]


def _num(errs, path, v, lo, hi, integer):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        errs.append(f"{path}: tiene que ser un número")
    elif integer and int(v) != v:
        errs.append(f"{path}: tiene que ser un número entero")
    elif not lo <= v <= hi:
        errs.append(f"{path}: tiene que estar entre {lo:g} y {hi:g}")


def _obj(errs, path, v):
    if not isinstance(v, dict):
        errs.append(f"{path}: falta o no es un objeto")
        return {}
    return v


def _subset(errs, path, v, allowed, non_empty=True):
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        errs.append(f"{path}: tiene que ser una lista de textos")
        return
    bad = [x for x in v if x not in allowed]
    if bad:
        errs.append(f"{path}: valores desconocidos {bad} (válidos: {allowed})")
    if non_empty and not v:
        errs.append(f"{path}: elegí al menos uno")


def validate_engine(cfg) -> list[str]:
    errs: list[str] = []
    cfg = _obj(errs, "config", cfg)

    wl = _obj(errs, "watchlist", cfg.get("watchlist"))
    for m in MARKETS:
        ids = wl.get(m)
        if not isinstance(ids, list):
            errs.append(f"watchlist.{m}: tiene que ser una lista")
            continue
        if len(ids) > MAX_WATCHLIST:
            errs.append(f"watchlist.{m}: máximo {MAX_WATCHLIST} activos (tiene {len(ids)})")
        for x in ids:
            if not isinstance(x, str) or not TICKER[m].match(x):
                errs.append(f"watchlist.{m}: «{x}» no es un {'id de CoinGecko' if m == 'cripto' else 'ticker'} válido")
        if len(set(ids)) != len(ids):
            errs.append(f"watchlist.{m}: hay activos repetidos")
    extra = set(wl) - set(MARKETS)
    if extra:
        errs.append(f"watchlist: mercados desconocidos {sorted(extra)}")

    bm = _obj(errs, "benchmarks", cfg.get("benchmarks"))
    for m in MARKETS:
        v = bm.get(m)
        if not isinstance(v, str) or not BENCH.match(v):
            errs.append(f"benchmarks.{m}: símbolo de Yahoo inválido")

    fl = _obj(errs, "filters", cfg.get("filters"))
    for m in MARKETS:
        f = _obj(errs, f"filters.{m}", fl.get(m))
        for k, lo, hi, integer in FILTERS:
            _num(errs, f"filters.{m}.{k}", f.get(k), lo, hi, integer)

    risk = _obj(errs, "risk", cfg.get("risk"))
    for m in MARKETS:
        r = _obj(errs, f"risk.{m}", risk.get(m))
        for k, lo, hi, integer in RISK_MARKET:
            _num(errs, f"risk.{m}.{k}", r.get(k), lo, hi, integer)
        if "fractional" in r and not isinstance(r["fractional"], bool):
            errs.append(f"risk.{m}.fractional: tiene que ser verdadero o falso")
        lp, pp = r.get("max_loss_pct"), r.get("max_position_pct")
        if isinstance(lp, (int, float)) and isinstance(pp, (int, float)) and lp > pp:
            errs.append(f"risk.{m}: la pérdida máxima ({lp} %) no puede superar el tamaño máximo de posición ({pp} %)")
    for k, lo, hi, integer in RISK_GLOBAL:
        _num(errs, f"risk.{k}", risk.get(k), lo, hi, integer)
    tr, mr = risk.get("target_r"), risk.get("min_reward_risk")
    if isinstance(tr, (int, float)) and isinstance(mr, (int, float)) and tr < mr:
        errs.append(f"risk.target_r ({tr}) es menor que risk.min_reward_risk ({mr}): el motor no abriría ninguna operación")

    st = _obj(errs, "setups", cfg.get("setups"))
    _subset(errs, "setups.vcp_min_rating", st.get("vcp_min_rating"), VCP_RATINGS)
    _subset(errs, "setups.vcp_watch_rating", st.get("vcp_watch_rating"), VCP_RATINGS, non_empty=False)
    _subset(errs, "setups.burst_entry_states", st.get("burst_entry_states"), BURST_STATES, non_empty=False)
    _subset(errs, "setups.burst_watch_states", st.get("burst_watch_states"), BURST_STATES, non_empty=False)
    _validate_pipeline(errs, cfg)
    return errs


LEANS = ["stop_strong", "stop", "even", "none"]
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _validate_pipeline(errs, cfg):
    """Secciones opcionales del pipeline de day-trader-skill (si faltan, el motor usa los valores por defecto)."""
    p = cfg.get("pipeline")
    if p is not None:
        p = _obj(errs, "pipeline", p)
        if "timemachine_block" in p:
            _subset(errs, "pipeline.timemachine_block", p["timemachine_block"], LEANS, non_empty=False)
        for k, lo, hi, integer in (("max_daily_loss_pct", 0.1, 50, False), ("max_trades_per_day", 1, 100, True),
                                   ("cash_buffer_pct", 0, 90, False)):
            if k in p:
                _num(errs, f"pipeline.{k}", p[k], lo, hi, integer)
        for k, v in (p.get("horizon") or {}).items():
            _num(errs, f"pipeline.horizon.{k}", v, 1, 77, True)
    i = cfg.get("intraday")
    if i is not None:
        i = _obj(errs, "intraday", i)
        for k in ("enabled", "market_gate"):
            if k in i and not isinstance(i[k], bool):
                errs.append(f"intraday.{k}: tiene que ser verdadero o falso")
        for k in ("flat_by", "no_entries_before", "no_entries_after"):
            if k in i and not (isinstance(i[k], str) and HHMM.match(i[k])):
                errs.append(f"intraday.{k}: hora de NY tipo 15:55")
        if "or_minutes" in i:
            _num(errs, "intraday.or_minutes", i["or_minutes"], 5, 120, True)
            if isinstance(i["or_minutes"], int) and i["or_minutes"] % 5:
                errs.append("intraday.or_minutes: tiene que ser múltiplo de 5")
        for k, lo, hi, integer in (("target_r", 0.5, 10, False), ("max_trigger_age_min", 5, 390, True),
                                   ("replays", 100, 100000, True)):
            if k in i:
                _num(errs, f"intraday.{k}", i[k], lo, hi, integer)
        syms = i.get("symbols", [])
        if not isinstance(syms, list) or not all(isinstance(x, str) and TICKER["adr"].match(x) for x in syms):
            errs.append("intraday.symbols: lista de tickers de EE. UU. (vacía = todos los ADRs)")
    cb = cfg.get("circuit_breaker")
    if cb is not None:
        cb = _obj(errs, "circuit_breaker", cb)
        for k, lo, hi, integer in (("max_daily_loss_pct", 0.1, 50, False), ("losing_streak_n", 1, 20, True),
                                   ("cooldown_hours", 1, 720, False), ("weekly_drawdown_pct", 0.1, 90, False),
                                   ("monthly_drawdown_pct", 0.1, 90, False)):
            if k in cb:
                _num(errs, f"circuit_breaker.{k}", cb[k], lo, hi, integer)
    n = cfg.get("news")
    if n is not None:
        n = _obj(errs, "news", n)
        if "hours" in n:
            _num(errs, "news.hours", n["hours"], 1, 240, True)
        am = n.get("adr_map", {})
        if not isinstance(am, dict) or not all(isinstance(v, str) and TICKER["adr"].match(v) for v in am.values()):
            errs.append("news.adr_map: tiene que ser {\"TICKER BYMA\": \"ADR\"}")
    for sec in ("us_market", "analyst", "congress", "weekly"):
        s = cfg.get(sec)
        if s is not None and not isinstance(_obj(errs, sec, s).get("enabled", True), bool):
            errs.append(f"{sec}.enabled: tiene que ser verdadero o falso")


ONLY = re.compile(r"^(adr|byma|cripto):[A-Za-z0-9.\-^]{1,50}$")


def validate_telegram(cfg) -> list[str]:
    errs: list[str] = []
    cfg = _obj(errs, "telegram", cfg)
    for group, keys in (("types", ("entry", "exit", "update")), ("kinds", MARKETS)):
        g = _obj(errs, group, cfg.get(group))
        for k in keys:
            if not isinstance(g.get(k), bool):
                errs.append(f"{group}.{k}: tiene que ser verdadero o falso")
        if group == "types" and "info" in g and not isinstance(g["info"], bool):
            errs.append("types.info: tiene que ser verdadero o falso")
    only = cfg.get("only", [])
    if not isinstance(only, list) or not all(isinstance(x, str) and ONLY.match(x) for x in only):
        errs.append("only: tiene que ser una lista tipo [\"adr:YPF\", \"cripto:BTC\"]")
    _num(errs, "max_age_hours", cfg.get("max_age_hours"), 1, 720, True)
    url = cfg.get("panel_url", "")
    if url and (not isinstance(url, str) or not url.startswith("https://")):
        errs.append("panel_url: tiene que empezar con https://")
    return errs


LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
CHAT = re.compile(r"^(default|-?\d{5,20}|@[A-Za-z0-9_]{5,32})?$")
ASSET = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")
TRADE_ID = re.compile(r"^[a-z0-9\-]{4,60}$")
EXIT_REASONS = ("target", "stop", "manual")


def validate_users(cfg) -> list[str]:
    """users/users.json: perfil de cada usuario (presupuesto, tope y avisos)."""
    errs: list[str] = []
    cfg = _obj(errs, "users.json", cfg)
    users = _obj(errs, "users", cfg.get("users"))
    for login, u in users.items():
        p = f"users.{login}"
        if not LOGIN.match(login):
            errs.append(f"{p}: «{login}» no es un login de GitHub válido")
        u = _obj(errs, p, u)
        name = u.get("name", "")
        if not isinstance(name, str) or len(name) > 40:
            errs.append(f"{p}.name: texto de hasta 40 caracteres")
        risk = _obj(errs, f"{p}.risk", u.get("risk"))
        for m in MARKETS:
            r = _obj(errs, f"{p}.risk.{m}", risk.get(m))
            for k, lo, hi, integer in RISK_MARKET:
                _num(errs, f"{p}.risk.{m}.{k}", r.get(k), lo, hi, integer)
            if "fractional" in r and not isinstance(r["fractional"], bool):
                errs.append(f"{p}.risk.{m}.fractional: tiene que ser verdadero o falso")
            lp, pp = r.get("max_loss_pct"), r.get("max_position_pct")
            if isinstance(lp, (int, float)) and isinstance(pp, (int, float)) and lp > pp:
                errs.append(f"{p}.risk.{m}: la pérdida máxima ({lp} %) no puede superar el tamaño máximo de posición ({pp} %)")
        _num(errs, f"{p}.max_open_positions", u.get("max_open_positions"), 1, 50, True)
        tg = _obj(errs, f"{p}.telegram", u.get("telegram"))
        chat = tg.get("chat", "")
        if not isinstance(chat, str) or not CHAT.match(chat):
            errs.append(f"{p}.telegram.chat: «default», un id tipo -100… o un @canal (vacío = sin Telegram)")
        if not isinstance(tg.get("engine"), bool):
            errs.append(f"{p}.telegram.engine: tiene que ser verdadero o falso")
        follow = tg.get("follow", [])
        if not isinstance(follow, list) or not all(isinstance(x, str) and LOGIN.match(x) for x in follow):
            errs.append(f"{p}.telegram.follow: lista de logins de GitHub")
        errs += [f"{p}.telegram.{e}" for e in validate_telegram({**tg, "max_age_hours": 24, "panel_url": ""})]
    return errs


def validate_trades(cfg, login: str = "") -> list[str]:
    """trades/<login>.json: operaciones que registra cada usuario (solo compras)."""
    errs: list[str] = []
    cfg = _obj(errs, "trades.json", cfg)
    trades = cfg.get("trades")
    if not isinstance(trades, list):
        return errs + ["trades: tiene que ser una lista"]
    ids = set()
    for i, t in enumerate(trades):
        t = _obj(errs, f"trades[{i}]", t)
        p = f"trades[{i}] ({t.get('asset', '?')})"
        tid = t.get("id")
        if not isinstance(tid, str) or not TRADE_ID.match(tid):
            errs.append(f"{p}.id: inválido")
        elif tid in ids:
            errs.append(f"{p}.id: repetido")
        ids.add(tid)
        if t.get("kind") not in MARKETS:
            errs.append(f"{p}.kind: tiene que ser {', '.join(MARKETS)}")
        if not isinstance(t.get("asset"), str) or not ASSET.match(t.get("asset", "")):
            errs.append(f"{p}.asset: ticker en mayúsculas (ej. YPF, GGAL, BTC)")
        for k in ("entry", "stop"):
            _num(errs, f"{p}.{k}", t.get(k), 1e-9, 1e12, False)
        e, s, tg = t.get("entry"), t.get("stop"), t.get("target")
        num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
        if num(e) and num(s) and s >= e:
            errs.append(f"{p}.stop: tiene que quedar debajo de la entrada")
        if tg is not None:
            _num(errs, f"{p}.target", tg, 1e-9, 1e12, False)
            if num(e) and num(tg) and tg <= e:
                errs.append(f"{p}.target: tiene que quedar arriba de la entrada")
        if t.get("units") is not None:
            _num(errs, f"{p}.units", t.get("units"), 1e-9, 1e12, False)
        if not isinstance(t.get("opened_at"), str):
            errs.append(f"{p}.opened_at: falta la fecha")
        if not isinstance(t.get("note", ""), str) or len(t.get("note", "")) > 280:
            errs.append(f"{p}.note: texto de hasta 280 caracteres")
        hist = t.get("stop_history", [])
        if not isinstance(hist, list) or not all(isinstance(h, dict) and isinstance(h.get("at"), str) and num(h.get("stop")) for h in hist):
            errs.append(f"{p}.stop_history: lista de {{at, stop}}")
        if t.get("exit") is not None:
            _num(errs, f"{p}.exit", t.get("exit"), 1e-9, 1e12, False)
            if t.get("exit_reason") not in EXIT_REASONS:
                errs.append(f"{p}.exit_reason: tiene que ser {', '.join(EXIT_REASONS)}")
            if not isinstance(t.get("closed_at"), str):
                errs.append(f"{p}.closed_at: falta la fecha de cierre")
    if login and not LOGIN.match(login):
        errs.append(f"trades/{login}.json: el nombre del archivo tiene que ser un login de GitHub")
    return errs


def main(argv=None) -> int:
    root = Path(__file__).resolve().parent.parent
    files = [(root / "engine/config.json", validate_engine), (root / "alerts/telegram.json", validate_telegram)]
    if (root / "users/users.json").exists():
        files.append((root / "users/users.json", validate_users))
    for f in sorted((root / "trades").glob("*.json")):
        files.append((f, lambda c, _l=f.stem: validate_trades(c, _l)))
    bad = 0
    for path, fn in files:
        try:
            errs = fn(json.loads(path.read_text()))
        except json.JSONDecodeError as exc:
            errs = [f"JSON inválido: {exc}"]
        for e in errs:
            print(f"{path.relative_to(root)}: {e}", file=sys.stderr)
        bad += len(errs)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
