from engine import config_check, review


def _t(kind, entry, stop, exit_, setup="vcp", gate="allowed", at="2026-11-01"):
    return {"kind": kind, "label": "X", "entry": entry, "initial_stop": stop, "stop": stop, "exit": exit_,
            "setup": setup, "gate": gate, "closed_at": at + "T20:00:00+00:00"}


def test_net_of_costs():
    nt = review.net_trade(_t("byma", 100, 95, 110), {"byma": 1.0})
    assert round(nt["r"], 2) == 2.0
    assert round(nt["r_net"], 2) == 1.8   # (10 % - 1 %) / 5 %
    assert review.net_trade({"entry": 10, "initial_stop": 11, "exit": 12}, {}) is None


def test_review_groups_and_curve():
    closed = [
        _t("adr", 100, 90, 120, at="2026-11-03"),                         # +2R
        _t("adr", 100, 90, 90, gate="restrictive", at="2026-11-01"),      # -1R
        _t("byma", 100, 95, 105, setup="burst", gate="neutral", at="2026-11-02"),
        {"kind": "adr", "entry": 1},                                       # incompleta: se ignora
    ]
    r = review.build_review(closed, {"costs": {"adr": 0, "byma": 0}})
    assert r["all"]["n"] == 3
    assert r["all"]["win_rate"] == 66.7
    assert r["all"]["total_r_net"] == 2.0
    assert r["all"]["max_dd_r"] == 1.0
    assert [p["t"] for p in r["curve"]] == ["2026-11-01", "2026-11-02", "2026-11-03"]
    assert r["curve"][-1]["eq"] == 2.0
    gates = {g["key"]: g for g in r["by_gate"]}
    assert gates["restrictive"]["name"] == "defensivo" and gates["restrictive"]["avg_r_net"] == -1.0
    assert {g["key"] for g in r["by_setup"]} == {"vcp", "burst"}
    assert review.build_review([], {})["all"] == {"n": 0}


def test_costs_validated():
    import json
    cfg = json.load(open("engine/config.json"))
    assert config_check.validate_engine(cfg) == []
    cfg["costs"] = {"adr": -1}
    assert any("costs.adr" in e for e in config_check.validate_engine(cfg))
