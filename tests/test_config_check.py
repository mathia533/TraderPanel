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
