"""Mi cartera (engine/cartera.py): normalización, análisis, cifrado y avisos, sin red."""
import json
from datetime import datetime, timezone

import pytest

from engine import cartera
from tests import synth

NOW = datetime(2026, 1, 15, 23, 0, tzinfo=timezone.utc)

# Datos inventados con la forma de la API v2 de IOL
API = {"source": "api", "portafolio": [{"pais": "argentina", "activos": [
    {"cantidad": 10, "ultimoPrecio": 30000, "ppc": 25000, "gananciaPorcentaje": 20, "gananciaDinero": 50000,
     "valorizado": 300000, "variacionDiaria": -1.2,
     "titulo": {"simbolo": "AAA", "descripcion": "Cedear AAA", "tipo": "CEDEARS", "mercado": "bcba", "moneda": "peso_Argentino"}},
    {"cantidad": 1000, "ultimoPrecio": 800, "valorizado": 800000, "variacionDiaria": 0.3,
     "titulo": {"simbolo": "BON30", "descripcion": "Bono", "tipo": "TitulosPublicos", "mercado": "bcba", "moneda": "peso_Argentino"}},
]}], "estadocuenta": {"cuentas": [{"moneda": "peso_Argentino", "disponible": 1500}, {"moneda": "dolar_Estadounidense", "disponible": 0}]}}

# Forma del conector de IOL
CONN = {"country": "ARG", "positions": [
    {"quantity": 100, "lot_price": 79620, "unit_price": 796.2, "daily_change_pct": -0.9,
     "asset": {"symbol": "BON30", "description": "Bono", "type": "TIT. PUBLICOS", "market": "BCBA", "currency": "ARS", "units_per_lot": 100}},
    {"quantity": 5, "lot_price": 20000, "unit_price": 20000, "daily_change_pct": 1.0,
     "asset": {"symbol": "BBB", "description": "Cedear BBB", "type": "CEDEARS", "market": "BCBA", "currency": "ARS", "units_per_lot": 1}},
]}


def fake_fetch(kind, sym):
    if sym == "AAA":
        return synth.make(seed=3, drift=0.002), "synth"
    if sym == "BBB":
        return synth.make(seed=5, crash=True), "synth"
    raise RuntimeError("sin datos")


def test_normalize_api_and_connector():
    h, cash = cartera.normalize(API)
    assert [x["symbol"] for x in h] == ["AAA", "BON30"]
    assert h[0]["type"] == "cedear" and h[1]["type"] == "bono" and h[0]["ppc"] == 25000
    assert cash == [{"currency": "$", "amount": 1500.0}]
    h2, _ = cartera.normalize(CONN)
    bon = h2[0]
    assert bon["type"] == "bono" and bon["value"] == pytest.approx(100 * 79620 / 100)
    assert h2[1]["type"] == "cedear" and h2[1]["value"] == 100000


def test_build_verdicts_and_totals():
    h, cash = cartera.normalize(API)
    h += cartera.normalize(CONN)[0][1:]
    rep = cartera.build(h, cash, json.load(open(cartera.CONFIG)), {"markets": {"adr": {"gate": "neutral"}}},
                        fetch=fake_fetch, now=NOW)
    by = {r["symbol"]: r for r in rep["holdings"]}
    assert by["BON30"]["analysis"]["verdict"] == "na"
    a = by["AAA"]["analysis"]
    assert a["verdict"] in ("favor", "ojo", "contra") and 0 < a["stop_pct"] <= 12
    assert a["stop"] < a["price"] < a["target"] and a["stop_local"] < 30000
    assert by["BBB"]["analysis"]["verdict"] == "contra"
    assert rep["totals"]["by_currency"]["$"] == pytest.approx(300000 + 800000 + 100000)
    assert sum(r["weight"] for r in rep["holdings"]) == pytest.approx(100, abs=0.1)
    assert rep["totals"]["at_risk"] > 0


def test_encrypt_roundtrip_and_wrong_key():
    obj = {"generated_at": "x", "holdings": [{"symbol": "AAA"}]}
    blob = cartera.encrypt(obj, "una frase bien larga")
    assert "AAA" not in json.dumps(blob)
    assert cartera.decrypt(blob, "una frase bien larga") == obj
    with pytest.raises(Exception):
        cartera.decrypt(blob, "otra frase cualquiera")


def test_verdict_changes():
    prev = {"holdings": [{"symbol": "AAA", "analysis": {"verdict": "favor"}}, {"symbol": "B", "analysis": {"verdict": "na"}}]}
    cur = {"holdings": [{"symbol": "AAA", "analysis": {"verdict": "contra", "why": "x"}}, {"symbol": "B", "analysis": {"verdict": "na"}}]}
    msgs = cartera.verdict_changes(prev, cur)
    assert len(msgs) == 1 and "AAA" in msgs[0] and "EN CONTRA" in msgs[0]
    assert cartera.verdict_changes(None, cur) == []


def test_main_refuses_plaintext_in_data(tmp_path, monkeypatch):
    f = tmp_path / "c.json"; f.write_text(json.dumps(CONN))
    assert cartera.main(["--desde", str(f), "--sin-cifrar", str(cartera.DATA / "x.json")]) == 2
    monkeypatch.delenv("CARTERA_CLAVE", raising=False)
    assert cartera.main(["--desde", str(f)]) == 0  # sin clave no escribe nada
    assert not (cartera.DATA / "x.json").exists()
