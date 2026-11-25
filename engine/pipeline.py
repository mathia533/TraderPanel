"""El pipeline de day-trader-skill aplicado a cada activo, más los programados.

    escáner -> noticias -> entrada -> plan -> Time Machine -> tamaño -> riesgo -> ticket

Cada etapa queda escrita en signals.json (assets[*].pipeline e intraday) para que
el panel la muestre en fila, y el veredicto PASS/BLOCK decide si el motor abre la
posición. El ticket nunca se envía a un broker.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import dtskills, planner, sources, tmextra

MKT = {"adr": "ADR", "byma": "BYMA", "cripto": "Cripto"}
CUR = {"adr": "US$", "byma": "$", "cripto": "US$"}

DEFAULTS = {
    "pipeline": {"timemachine_block": ["stop_strong"], "horizon": {"vcp": 20, "burst": 5},
                 "max_daily_loss_pct": 3, "max_trades_per_day": 3, "cash_buffer_pct": 0},
    "intraday": {"enabled": True, "symbols": [], "or_minutes": 15, "target_r": 1, "flat_by": "15:55",
                 "no_entries_before": "09:45", "no_entries_after": "15:30", "max_trigger_age_min": 30,
                 "market_gate": True, "replays": 10000},
    "us_market": {"enabled": True},
    "circuit_breaker": {"max_daily_loss_pct": 2, "losing_streak_n": 3, "cooldown_hours": 24,
                        "weekly_drawdown_pct": 5, "monthly_drawdown_pct": 8},
    "news": {"enabled": True, "hours": 36, "cripto": True, "adr_map": {}},
    "analyst": {"enabled": True},
    "congress": {"enabled": True},
    "weekly": {"enabled": True},
}


def section(cfg: dict, name: str) -> dict:
    out = dict(DEFAULTS[name])
    out.update(cfg.get(name) or {})
    return out


def fmt(v, ref=None):
    if v is None:
        return "—"
    ref = abs(ref if ref is not None else v)
    d = 0 if ref >= 1000 else 2 if ref >= 1 else 4
    return f"{v:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def pct(v):
    return f"{v * 100:.0f} %"


# ------------------------------------------------------------------ contexto del mercado
def market_context(cfg: dict, state: dict, now: datetime, offline: bool, data_dir: Path) -> dict:
    ctx = {"us_market": None, "breakers": {}, "errors": {}}
    if section(cfg, "us_market")["enabled"] and not offline:
        try:
            ctx["us_market"] = tmextra.us_market(data_dir, now)
        except Exception as exc:
            ctx["errors"]["us_market"] = str(exc)[:200]
    cbc = section(cfg, "circuit_breaker")
    for kind in cfg["watchlist"]:
        closed = [t for t in state.get("closed", []) if t.get("kind") == kind]
        try:
            ctx["breakers"][kind] = tmextra.circuit_breaker(closed, cfg["risk"][kind]["budget"], now, cbc)
        except Exception as exc:
            ctx["errors"][f"circuit_breaker:{kind}"] = str(exc)[:200]
    return ctx


def merge_gate(a: tuple[str, str], b: tuple[str | None, str]) -> tuple[str, str]:
    rank = {"allowed": 0, "neutral": 1, "restrictive": 2}
    if b[0] is None:
        return a
    return b if rank[b[0]] > rank[a[0]] else a


# ------------------------------------------------------------------ etapas
def stage(key, name, status, text, **extra):
    return {"key": key, "name": name, "status": status, "text": text, **extra}


def news_stage(news_by_ticker: dict, ticker: str | None) -> dict:
    items = news_by_ticker.get(ticker or "", []) if ticker else []
    if not ticker:
        return stage("news", "Noticias", "na", "No hay fuente de noticias para este activo")
    if not items:
        return stage("news", "Noticias", "ok", f"No hubo noticias de {ticker} en las últimas horas: nada en contra")
    earn = [i for i in items if "earnings" in i.get("tags", [])]
    top = items[0]
    tags = sorted({dtskills.TAG_ES.get(t, t) for i in items[:5] for t in i.get("tags", [])})
    txt = f"{len(items)} noticias recientes de {ticker}" + (f" (temas: {', '.join(tags)})" if tags else "") + f". La última: «{top['title']}»"
    return stage("news", "Noticias", "warn" if earn else "ok",
                 txt + (". Ojo: la empresa presenta su balance pronto y el precio puede saltar fuerte para cualquier lado mientras la operación está abierta." if earn else ""),
                 items=[{"title": i["title"], "link": i["link"], "time": i["time"], "tags": i.get("tags", [])} for i in items[:4]])


def scanner_stage(sc: dict) -> dict:
    if not sc["tags"]:
        return stage("scanner", "Escáner", "na", f"Hoy no aparece ninguna señal técnica (RSI {dtskills.fmt_num(sc['rsi'] or 0, 1)})")
    return stage("scanner", "Escáner", "ok", ", ".join(sc["labels"]).capitalize() + f" · RSI {dtskills.fmt_num(sc['rsi'] or 0, 1)}")


def tm_stage(tm: dict | None, block_list: list[str]) -> dict:
    if not tm:
        return stage("timemachine", "Time Machine", "na", "No se simuló")
    if not tm.get("ok"):
        return stage("timemachine", "Time Machine", "na", tm.get("error") or "No se pudo simular")
    h = tm.get("history") or {}
    hist = ""
    if h.get("fired"):
        hist = (f" En el pasado esta señal ({h.get('label', '')}) apareció {h['fired']} veces en este activo: "
                f"{h.get('target', 0)} llegaron al objetivo, {h.get('stop', 0)} al stop y {h.get('neither', 0)} a ninguno"
                + (f" (resultado medio {dtskills.fmt_num(h['avg_R'])} veces lo arriesgado)" if h.get("avg_R") is not None else "") + ".")
    st = "block" if dtskills.tm_blocks(tm, block_list) else "warn" if tm["lean"] in ("stop", "stop_strong") else "ok"
    return stage("timemachine", "Time Machine", st, tm["verdict"] + hist, data=tm)


# ------------------------------------------------------------------ una entrada de varios días
def evaluate_swing(*, cfg, ctx, kind, ident, label, closed, cand, vcp, gate, gate_why, state, now,
                   news_by_ticker, news_ticker, sc) -> dict:
    """Corre el pipeline sobre un candidato (VCP o Momentum Burst) y devuelve las etapas y el veredicto."""
    risk = cfg["risk"]
    pcfg = section(cfg, "pipeline")
    stages = [scanner_stage(sc), news_stage(news_by_ticker, news_ticker)]
    entry = cand["entry"]
    stop = cand.get("stop")
    extra_block = []

    # breakout-trade-planner para VCP: precio de entrada, stop bajo la contracción y si es operable
    bp = None
    if cand["setup"] == "vcp":
        bp = tmextra.breakout_plan(label, vcp, entry, account=risk[kind]["budget"], risk_pct=risk[kind]["max_loss_pct"],
                                   max_position_pct=risk[kind]["max_position_pct"], target_r=risk["target_r"], now=now)
        if bp and bp.get("stop"):
            stop = bp["stop"]
        if bp and bp["classification"] not in ("revalidation", "actionable"):
            extra_block.append(f"breakout-trade-planner: {tmextra.CLASS_ES.get(bp['classification'], bp['classification'])}"
                               + (f" ({bp['reason']})" if bp.get("reason") else ""))
    stages.append(stage("entry", "Entrada", "ok", cand["why"], setup=cand["setup"],
                        planner=bp and {k: bp.get(k) for k in ("classification", "signal_entry", "worst_entry", "stop", "reason")}))

    plan = planner.plan_trade(entry, stop, risk)
    if not plan["ok"]:
        stages.append(stage("plan", "Plan", "block", plan["why"]))
        return {"verdict": "BLOCK", "reasons": [plan["why"]], "stages": stages, "plan": None}
    c = CUR[kind]
    stages.append(stage("plan", "Plan", "ok",
                        f"Compra a {c} {fmt(plan['entry'])}. Vende con pérdida si baja a {c} {fmt(plan['stop'], plan['entry'])} "
                        f"(stop, −{dtskills.fmt_num(plan['stop_pct'], 1)} %) o con ganancia si sube a {c} {fmt(plan['target'], plan['entry'])} "
                        f"(objetivo, gana {dtskills.fmt_num(plan['rr'], 1)} veces lo que arriesga)", plan={k: plan[k] for k in ("entry", "stop", "target")}))

    horizon = pcfg["horizon"].get(cand["setup"], 10)
    tm = dtskills.swing_timemachine(closed, plan["entry"], plan["stop"], plan["target"], horizon=horizon)
    stages.append(tm_stage(tm, pcfg["timemachine_block"]))
    if dtskills.tm_blocks(tm, pcfg["timemachine_block"]):
        extra_block.append("la simulación (Time Machine) da " + dtskills.LEAN_ES[tm["lean"]].lower())

    open_notional = [(p["label"], p.get("notional") or 0) for p in state["positions"].values() if p.get("kind") == kind]
    today = dtskills.ny_date(now)
    entries_today = [e for e in state.get("entries_log", []) if e.get("kind") == kind and e.get("date") == today]
    rules = {**pcfg, "min_reward_risk": risk["min_reward_risk"]}
    sz = dtskills.size_and_check(label=label, entry=plan["entry"], stop=plan["stop"], target=plan["target"],
                                 setup=cand["setup"], market_cfg=risk[kind], rules=rules, open_notional=open_notional,
                                 entries_today=entries_today, now=now)
    if sz.get("units"):
        stages.append(stage("budget", "Tamaño", "ok",
                            f"Comprar {dtskills.fmt_num(sz['units'], 6)} {label} ({c} {fmt(sz['notional'])}). Si toca el stop se pierden como mucho {c} {fmt(sz['max_loss'])}; "
                            f"la cantidad la limita {sz['bound_by']}"))
    else:
        stages.append(stage("budget", "Tamaño", "block", "; ".join(sz["reasons"])))

    # filtros previos a la orden (pre-trade gate)
    if gate == "restrictive":
        extra_block.append("el mercado está flojo y el motor no compra (" + gate_why + ")")
    if len(state["positions"]) >= risk["max_open_positions"]:
        extra_block.append(f"ya hay {risk['max_open_positions']} posiciones abiertas")
    br = ctx["breakers"].get(kind)
    if br and br["recommendation"] != "TRADING_ALLOWED":
        extra_block.append("drawdown-circuit-breaker: " + tmextra.CB_ES[br["recommendation"]] + " ("
                           + ", ".join(tmextra.CB_RULE_ES.get(r["rule"], r["rule"]) for r in br["rules"]) + ")")
    reasons = list(sz.get("reasons") or []) + extra_block
    verdict = "PASS" if sz["verdict"] == "PASS" and not extra_block else "BLOCK"
    stages.append(stage("risk", "Riesgo", "ok" if verdict == "PASS" else "block",
                        "Aprobado: respeta todos los límites de riesgo" if verdict == "PASS" else "Frenado: " + "; ".join(reasons),
                        verdict=verdict, reasons=reasons))
    if sz.get("ticket"):
        stages.append(stage("ticket", "Ticket", "ok" if verdict == "PASS" else "na",
                            sz["ticket"]["text"] + ". No se envía a ningún broker.", ticket=sz["ticket"]))
    return {"verdict": verdict, "reasons": reasons, "stages": stages, "plan": plan, "size": sz, "tm": tm, "bp": bp}


def watch_pipeline(*, cfg, kind, label, closed, trigger, stop, setup, news_by_ticker, news_ticker, sc, vcp=None, now=None):
    """Para las preparadas: escáner, noticias, plan sobre el disparo y Time Machine."""
    risk = cfg["risk"]
    stages = [scanner_stage(sc), news_stage(news_by_ticker, news_ticker)]
    bp = None
    if setup == "vcp" and vcp:
        bp = tmextra.breakout_plan(label, vcp, closed[0]["close"], account=risk[kind]["budget"],
                                   risk_pct=risk[kind]["max_loss_pct"], max_position_pct=risk[kind]["max_position_pct"],
                                   target_r=risk["target_r"], now=now or datetime.now(timezone.utc))
        if bp and bp.get("stop"):
            stop = bp["stop"]
            trigger = bp.get("signal_entry") or trigger
        if bp:
            stages.append(stage("entry", "Entrada", "warn" if bp["classification"] in ("actionable", "watchlist") else "na",
                                f"breakout-trade-planner: {tmextra.CLASS_ES.get(bp['classification'], bp['classification'])}"
                                + (f". Orden de compra sobre {fmt(bp['signal_entry'])}, como mucho {fmt(bp['worst_entry'])}"
                                   if bp.get("signal_entry") else "")))
    if not trigger or not stop:
        return stages
    plan = planner.plan_trade(trigger, stop, risk)
    if not plan["ok"]:
        stages.append(stage("plan", "Plan", "block", plan["why"]))
        return stages
    c = CUR[kind]
    stages.append(stage("plan", "Plan", "ok", f"Si el precio llega a {c} {fmt(plan['entry'])}, compra. Después vende con pérdida si baja a {c} {fmt(plan['stop'], plan['entry'])} (stop) "
                                              f"o con ganancia si sube a {c} {fmt(plan['target'], plan['entry'])} (objetivo)", plan={k: plan[k] for k in ("entry", "stop", "target")}))
    tm = dtskills.swing_timemachine(closed, plan["entry"], plan["stop"], plan["target"],
                                    horizon=section(cfg, "pipeline")["horizon"].get(setup, 10), replays=5000)
    stages.append(tm_stage(tm, section(cfg, "pipeline")["timemachine_block"]))
    return stages


# ------------------------------------------------------------------ intradía ADRs
def fetch_intraday_chart(sym: str) -> str:
    return sources.fetch_text(f"https://query2.finance.yahoo.com/v8/finance/chart/{sym}?range=60d&interval=5m")


def ny_session_open(now: datetime) -> bool:
    e = dtskills.mod("entry")
    nowu = dtskills.naive_utc(now)
    local = e.to_eastern(nowu)
    ok, _ = e.trading_day(local.date())
    if not ok:
        return False
    o, c = e.session(local.date())
    return o <= local.time() <= (datetime.combine(local.date(), c) + timedelta(minutes=20)).time()


def run_intraday(cfg: dict, ctx: dict, state: dict, now: datetime, adr_gate: tuple[str, str],
                 new_events: list, errors: dict, fetch=None) -> dict:
    """ORB de 15 minutos en los ADRs: entry -> exit -> timemachine -> budget -> risk, y alertas."""
    icfg = section(cfg, "intraday")
    fetch = fetch or fetch_intraday_chart
    out = {"enabled": icfg["enabled"], "date": dtskills.ny_date(now), "time_ny": dtskills.ny_time(now), "symbols": {}}
    if not icfg["enabled"]:
        return out
    syms = icfg["symbols"] or cfg["watchlist"].get("adr", [])
    risk = cfg["risk"]
    pcfg = section(cfg, "pipeline")
    today = dtskills.ny_date(now)
    for sym in syms:
        key = f"intradia:{sym}"
        try:
            text = fetch(sym)
        except Exception as exc:
            errors[f"intradia:{sym}"] = str(exc)[:200]
            continue
        pos = state["positions"].get(key)
        if pos:
            try:
                ex = dtskills.intraday_exit(pos, text, now, icfg["flat_by"])
            except Exception as exc:
                errors[f"intradia:{sym}"] = str(exc)[:200]
                ex = None
            if ex:
                close_position(state, key, pos, ex, now, new_events)
            else:
                out["symbols"][sym] = {"status": "open", "status_es": "posición abierta", "position": pos}
                continue
        try:
            r = dtskills.intraday_orb(sym, text, now, icfg)
        except Exception as exc:
            errors[f"intradia:{sym}"] = str(exc)[:200]
            continue
        r.pop("bars", None)
        done_today = any(e.get("label") == sym and e.get("date") == today and e.get("mode") == "intradia"
                         for e in state.get("entries_log", []))
        if r["status"] == "triggered" and r.get("plan") and not done_today:
            p = r["plan"]
            age = _minutes_since(r, now)
            tm = r.get("timemachine")
            open_notional = [(x["label"], x.get("notional") or 0) for x in state["positions"].values() if x.get("kind") == "adr"]
            entries_today = [e for e in state.get("entries_log", []) if e.get("kind") == "adr" and e.get("date") == today]
            rules = {**pcfg, **{k: icfg[k] for k in ("no_entries_before", "no_entries_after", "flat_by")},
                     "min_reward_risk": min(risk["min_reward_risk"], icfg["target_r"])}
            sz = dtskills.size_and_check(label=sym, entry=p["entry"], stop=p["stop"], target=p["target"], setup="orb",
                                         market_cfg=risk["adr"], rules=rules, open_notional=open_notional,
                                         entries_today=entries_today, now=now, intraday=True)
            extra = []
            if age is not None and age > icfg["max_trigger_age_min"]:
                extra.append(f"la ruptura fue hace {age} minutos (máximo {icfg['max_trigger_age_min']})")
            if dtskills.tm_blocks(tm, pcfg["timemachine_block"]):
                extra.append("la simulación (Time Machine) da " + dtskills.LEAN_ES[tm["lean"]].lower())
            if icfg["market_gate"] and adr_gate[0] == "restrictive":
                extra.append("el mercado está flojo y el motor no compra (" + adr_gate[1] + ")")
            if len(state["positions"]) >= risk["max_open_positions"]:
                extra.append(f"ya hay {risk['max_open_positions']} posiciones abiertas")
            br = ctx["breakers"].get("adr")
            if br and br["recommendation"] != "TRADING_ALLOWED":
                extra.append("drawdown-circuit-breaker: " + tmextra.CB_ES[br["recommendation"]])
            reasons = list(sz.get("reasons") or []) + extra
            verdict = "PASS" if sz["verdict"] == "PASS" and not extra else "BLOCK"
            r["risk"] = {"verdict": verdict, "reasons": reasons, "units": sz.get("units"), "notional": sz.get("notional"),
                         "max_loss": sz.get("max_loss"), "ticket": sz.get("ticket")}
            if verdict == "PASS":
                trig = r["trigger"]
                pos = {"kind": "adr", "id": sym, "label": sym, "setup": "orb", "mode": "intradia", "date": today,
                       "entry_bar_end": trig["close_time"], "entry": p["entry"], "stop": p["stop"], "initial_stop": p["stop"],
                       "target": p["target"], "rr": p["reward_risk"], "stop_pct": round(p["risk"] / p["entry"] * 100, 2),
                       "units": sz["units"], "notional": sz["notional"], "max_loss": sz["max_loss"],
                       "limited_by": sz["bound_by"], "score": None, "flat_by": icfg["flat_by"],
                       "why": f"Ruptura del rango de apertura ({fmt(r['opening_range']['low'])}–{fmt(r['opening_range']['high'])}): "
                              f"la vela de las {trig['bar_start']} cerró en {fmt(trig['price'])}",
                       "timemachine": tm and {k: tm.get(k) for k in ("lean", "verdict")},
                       "bar_date": today, "opened_at": now.isoformat(), "last": p["entry"], "r_now": 0.0, "sessions": 0}
                state["positions"][key] = pos
                log_entry(state, "adr", sym, today, sz["max_loss"], "intradia")
                new_events.append({"type": "entry", "kind": "adr", "id": sym, "label": sym, "setup": "orb", "mode": "intradia",
                                   "score": None, "price": p["entry"], "stop": p["stop"], "target": p["target"],
                                   "rr": p["reward_risk"], "stop_pct": pos["stop_pct"], "units": sz["units"],
                                   "notional": sz["notional"], "max_loss": sz["max_loss"], "why": pos["why"],
                                   "tm": pos["timemachine"], "flat_by": icfg["flat_by"], "bar_date": today,
                                   "id_ev": f"{key}:entry:{today}"})
                r["status_es"] = "entrada abierta"
        out["symbols"][sym] = r
    return out


def _minutes_since(r, now):
    t = (r.get("trigger") or {}).get("close_time")
    if not t:
        return None
    local = dtskills.mod("entry").to_eastern(dtskills.naive_utc(now))
    h, m = map(int, t.split(":"))
    return (local.hour * 60 + local.minute) - (h * 60 + m)


def log_entry(state, kind, label, day, max_loss, mode):
    state["entries_log"] = ([{"kind": kind, "label": label, "date": day, "max_loss": max_loss, "mode": mode}]
                            + state.get("entries_log", []))[:300]


def close_position(state, key, pos, ex, now, new_events):
    pnl = (ex["price"] / pos["entry"] - 1) * 100
    r_mult = (ex["price"] - pos["entry"]) / (pos["entry"] - pos["initial_stop"])
    money = (ex["price"] - pos["entry"]) * (pos.get("units") or 0)
    ev = {"type": "exit", "reason": ex["reason"], "kind": pos["kind"], "id": pos["id"], "label": pos["label"],
          "setup": pos["setup"], "mode": pos.get("mode"), "price": round(ex["price"], 4), "entry": pos["entry"],
          "initial_stop": pos["initial_stop"], "why": ex["why"], "pnl_pct": round(pnl, 2), "r": round(r_mult, 2),
          "bar_date": pos.get("date") or pos["bar_date"], "opened_at": pos["opened_at"],
          "id_ev": f"{key}:exit:{pos['bar_date']}:{ex['reason']}"}
    new_events.append(ev)
    state["closed"] = ([{**pos, "exit": ev["price"], "exit_reason": ex["reason"], "pnl_pct": ev["pnl_pct"], "r": ev["r"],
                         "pnl_money": round(money, 2), "closed_at": now.isoformat()}] + state.get("closed", []))[:100]
    del state["positions"][key]
    return ev


# ------------------------------------------------------------------ noticias
def news_tickers(cfg: dict) -> tuple[dict, dict]:
    """{TICKER Yahoo: nombre} a consultar y {clave del activo: TICKER}."""
    ncfg = section(cfg, "news")
    amap = ncfg.get("adr_map") or {}
    tickers, by_asset = {}, {}
    for t in cfg["watchlist"].get("adr", []):
        tickers[t] = ""
        by_asset[f"adr:{t}"] = t
    for t in cfg["watchlist"].get("byma", []):
        adr = amap.get(t)
        if adr:
            tickers.setdefault(adr, "")
            by_asset[f"byma:{t}"] = adr
    if ncfg.get("cripto"):
        for c in cfg["watchlist"].get("cripto", []):
            y = sources.yahoo_symbol("cripto", c)
            tickers[y] = ""
            by_asset[f"cripto:{c}"] = y
    return tickers, by_asset


def run_news(cfg, data_dir: Path, now: datetime, errors: dict, offline=None) -> tuple[list, dict]:
    ncfg = section(cfg, "news")
    if not ncfg["enabled"]:
        return [], {}
    tickers, by_asset = news_tickers(cfg)
    st_file = data_dir / "news_state.json"
    try:
        st = json.loads(st_file.read_text())
    except Exception:
        st = {}
    ua = os.environ.get("SEC_USER_AGENT", "").strip() or None
    try:
        items, errs = dtskills.news_for(tickers, st, now, sec_ua=ua, hours=ncfg["hours"], offline=offline)
    except Exception as exc:
        errors["news"] = str(exc)[:200]
        return [], by_asset
    if errs:
        errors["news"] = "; ".join(errs[:3])[:300]
    st_file.write_text(json.dumps(st))
    return items, by_asset


def group_news(items: list) -> dict:
    out: dict = {}
    for it in items:
        for t in set([it["ticker"]] + it.get("touches", [])):
            out.setdefault(t, []).append(it)
    return out


IMPORTANT = {"earnings", "deal", "halt", "risk", "legal"}


def news_events(items: list, now: datetime, by_asset: dict, max_n: int = 5) -> list:
    """Titulares nuevos y relevantes (balance, operación corporativa, suspensión, riesgo) de las últimas 6 horas."""
    inv = {}
    for k, t in by_asset.items():
        inv.setdefault(t, []).append(k)
    out = []
    cutoff = (now - timedelta(hours=6)).isoformat()
    for it in items:
        if not it.get("new") or not (set(it.get("tags", [])) & IMPORTANT) or (it.get("time") or "") < cutoff:
            continue
        keys = inv.get(it["ticker"], [])
        kind = keys[0].split(":")[0] if keys else "adr"
        tags = ", ".join(dtskills.TAG_ES.get(t, t) for t in it["tags"])
        text = (f"📰 <b>NOTICIA · {esc(it['ticker'])}</b> ({tags})\n{esc(it['title'])}\n"
                f"<a href=\"{esc(it['link'])}\">{esc(it['source'])}</a>")
        out.append({"type": "info", "info": "news", "kind": kind, "id": it["ticker"], "label": it["ticker"],
                    "why": it["title"], "id_ev": f"news:{it['id']}", "text_override": text})
        if len(out) >= max_n:
            break
    return out


def esc(s):
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ------------------------------------------------------------------ programados: balances, congresistas, semana
def run_analyst(cfg, data_dir: Path, now: datetime, errors: dict, new_events: list, offline=None) -> dict | None:
    if not section(cfg, "analyst")["enabled"]:
        return None
    f = data_dir / "analyst.json"
    try:
        prev = json.loads(f.read_text())
    except Exception:
        prev = {}
    today = now.date().isoformat()
    if prev.get("date") == today and not offline:
        return prev
    ua = os.environ.get("SEC_USER_AGENT", "").strip() or None
    fa = dtskills.mod("facts")
    if not ua and not offline:
        prev.update({"date": today, "note": "Falta el secret SEC_USER_AGENT (nombre y mail) para leer balances de la SEC."})
        f.write_text(json.dumps(prev, ensure_ascii=False))
        return prev
    fetcher = fa.Fetcher(ua=ua, offline=offline)
    try:
        ciks = fa.ticker_ciks(fetcher)
    except Exception as exc:
        errors["analyst"] = str(exc)[:200]
        return prev or None
    tickers = section(cfg, "analyst").get("tickers") or cfg["watchlist"].get("adr", [])
    rows = {}
    for t in tickers:
        try:
            a = dtskills.analyst_for(t, fetcher, ciks)
        except Exception as exc:
            a = {"ticker": t, "ok": False, "error": str(exc)[:200]}
        rows[t] = a
        old = (prev.get("companies") or {}).get(t) or {}
        rep, orep = (a.get("filings") or {}).get("report"), (old.get("filings") or {}).get("report")
        if rep and orep and rep.get("accession") != orep.get("accession"):
            txt = f"📊 <b>BALANCE · {esc(t)}</b>\nPresentó {rep['form']} del período al {rep['period']} ({rep['filed']})."
            r0 = (a.get("rows") or [None])[0]
            if r0 and r0.get("end") == rep.get("period"):
                bits = []
                if r0.get("revenue_yoy") is not None:
                    bits.append(f"ingresos {r0['revenue_yoy']:+.1f} % interanual")
                if r0.get("eps_diluted") is not None:
                    bits.append(f"EPS {r0['eps_diluted']}")
                if bits:
                    txt += "\n" + ", ".join(bits).replace(".", ",")
            txt += f"\n<a href=\"{esc(rep['url'])}\">Documento en la SEC</a>"
            new_events.append({"type": "info", "info": "earnings", "kind": "adr", "id": t, "label": t,
                               "why": f"{rep['form']} {rep['period']}", "id_ev": f"filing:{t}:{rep['accession']}",
                               "text_override": txt})
    out = {"date": today, "generated_at": now.isoformat(), "companies": rows}
    f.write_text(json.dumps(out, ensure_ascii=False))
    return out


def run_congress(cfg, data_dir: Path, now: datetime, errors: dict, new_events: list, offline_index=None,
                 offline_texts=None) -> dict | None:
    if not section(cfg, "congress")["enabled"]:
        return None
    f = data_dir / "congress.json"
    try:
        prev = json.loads(f.read_text())
    except Exception:
        prev = {"rows": [], "state": {"seen": [], "last_check": None}}
    today = now.date()
    if prev.get("date") == today.isoformat() and offline_index is None:
        return prev
    watch = set(cfg["watchlist"].get("adr", [])) | set((section(cfg, "news").get("adr_map") or {}).values())
    watch |= set(section(cfg, "congress").get("extra_tickers") or [])
    try:
        found, errs = dtskills.congress_trades(prev.get("state") or {}, today, watch,
                                               offline_index=offline_index, offline_texts=offline_texts)
    except Exception as exc:
        errors["congress"] = str(exc)[:200]
        return prev
    if errs:
        errors["congress"] = "; ".join(sorted(set(errs))[:3])[:300]
    for r in found:
        if r["watch"]:
            tipo = dtskills.TXN_ES.get(r["type"], r["type"])
            new_events.append({"type": "info", "info": "congress", "kind": "adr", "id": r["ticker"], "label": r["ticker"],
                               "why": f"{r['member']}: {tipo} {r['ticker']}",
                               "id_ev": f"ptr:{r['doc_id']}:{r['ticker']}:{r.get('date')}:{r['type']}",
                               "text_override": (f"🏛 <b>CONGRESO EE. UU. · {esc(r['ticker'])}</b>\n{esc(r['member'])} ({esc(r['district'])}): "
                                                 f"{tipo} {esc(r['asset'])}, {esc(r['amount'])}, operó el {r.get('date')}, "
                                                 f"informó el {r['filed']}.\n<a href=\"{esc(r['url'])}\">PTR</a>")})
    rows = (found + prev.get("rows", []))[:80]
    out = {"date": today.isoformat(), "rows": rows, "state": prev.get("state") or {}}
    f.write_text(json.dumps(out, ensure_ascii=False))
    return out


def run_weekly(cfg, state: dict, now: datetime, new_events: list) -> dict | None:
    """El viernes después del cierre de NY: resumen de la semana con trade-performance-coach."""
    if not section(cfg, "weekly")["enabled"]:
        return None
    rev = tmextra.weekly_review(state.get("closed", []), now)
    local = dtskills.mod("entry").to_eastern(dtskills.naive_utc(now))
    if local.weekday() == 4 and local.hour >= 16:
        lines = [f"🗓 <b>RESUMEN SEMANAL</b> (desde el {rev['week_start']})"]
        if rev["trades"]:
            lines.append(f"{rev['trades']} operaciones cerradas: {rev['wins']} ganadas, {rev['losses']} perdidas "
                         f"({dtskills.fmt_num(rev['win_rate'], 1)} %)")
            lines.append(f"Resultado {rev['total_r']:+.2f}R · esperanza {rev['expectancy_r']:+.2f}R por operación".replace(".", ","))
            lines.append(f"Mejor: {esc(rev['best'])} · peor: {esc(rev['worst'])}")
        else:
            lines.append("Sin operaciones cerradas esta semana.")
        lines.append(f"Proceso: {rev['verdict_es']}")
        for r in rev["rules"][:2]:
            lines.append("• " + esc(r))
        new_events.append({"type": "info", "info": "weekly", "kind": "adr", "id": "semana", "label": "Semana",
                           "why": "Resumen semanal", "id_ev": f"weekly:{rev['week_start']}", "text_override": "\n".join(lines)})
    return rev
