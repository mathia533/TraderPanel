import copy
import json
from pathlib import Path

from engine import config_check as cc

ROOT = Path(__file__).resolve().parent.parent
ENGINE = json.loads((ROOT / "engine/config.json").read_text())
TG = json.loads((ROOT / "alerts/telegram.json").read_text())


def test_repo_configs_are_valid():
    assert cc.validate_engine(ENGINE) == []
    assert cc.validate_telegram(TG) == []


def _bad(mutate):
    cfg = copy.deepcopy(ENGINE)
    mutate(cfg)
    return cc.validate_engine(cfg)


def test_engine_rejects_impossible_values():
    assert _bad(lambda c: c["risk"]["adr"].update(budget=-5))
    assert _bad(lambda c: c["risk"]["adr"].update(max_loss_pct="1"))
    assert _bad(lambda c: c["risk"]["byma"].update(max_loss_pct=30, max_position_pct=20))
    assert _bad(lambda c: c["risk"].update(max_open_positions=2.5))
    assert any("ninguna operación" in e for e in _bad(lambda c: c["risk"].update(target_r=1.0)))
    assert _bad(lambda c: c["watchlist"]["adr"].append("ypf; rm"))
    assert _bad(lambda c: c["watchlist"]["cripto"].append("bitcoin"))      # repetido
    assert _bad(lambda c: c["setups"].update(vcp_min_rating=["Mega VCP"]))
    assert _bad(lambda c: c["setups"].update(vcp_min_rating=[]))
    assert _bad(lambda c: c.pop("filters"))


def test_telegram_rules():
    t = copy.deepcopy(TG)
    t["only"] = ["adr:YPF", "nyse:YPF"]
    t["max_age_hours"] = 0
    errs = cc.validate_telegram(t)
    assert len(errs) == 2


USERS = json.loads((ROOT / "users/users.json").read_text())
TRADE = {"id": "t-ypf-1", "kind": "adr", "asset": "YPF", "entry": 30, "stop": 27, "target": 36,
         "opened_at": "2026-11-25T14:00:00Z", "note": "", "stop_history": []}


def test_example_profile_is_valid():
    # Solo el perfil de ejemplo: los archivos que editan los usuarios no frenan el motor
    # (alerts/send.mjs saltea operaciones mal cargadas).
    assert cc.validate_users({"users": {"mathia533": USERS["users"]["mathia533"]}}) == []


def test_users_rules():
    u = copy.deepcopy(USERS)
    me = u["users"]["mathia533"]
    me["telegram"]["chat"] = "mi canal"
    me["telegram"]["follow"] = ["no valido!"]
    me["risk"]["adr"]["budget"] = 0
    u["users"]["bad login!"] = copy.deepcopy(me)
    errs = cc.validate_users(u)
    assert any("chat" in e for e in errs) and any("follow" in e for e in errs)
    assert any("budget" in e for e in errs) and any("login" in e for e in errs)
    ok = copy.deepcopy(USERS)
    ok["users"]["mathia533"]["telegram"]["chat"] = "-1005699408578"
    assert cc.validate_users(ok) == []


def test_trades_rules():
    assert cc.validate_trades({"trades": [TRADE]}, "amigo") == []
    bad = lambda **kw: cc.validate_trades({"trades": [{**TRADE, **kw}]})
    assert bad(stop=31)                       # stop sobre la entrada
    assert bad(target=29)                     # objetivo debajo
    assert bad(kind="nyse")
    assert bad(asset="ypf; rm")
    assert bad(exit=33)                       # cierre sin motivo ni fecha
    assert not bad(exit=33, exit_reason="manual", closed_at="2026-11-26T14:00:00Z")
    assert cc.validate_trades({"trades": [TRADE, TRADE]})  # id repetido
