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
    assert cartera.main(["--desde", str(f), "--subidas", str(tmp_path / "nada")]) == 0  # sin clave no escribe nada
    assert not (cartera.DATA / "x.json").exists()


def _browser_upload(login, src, phrase, pub):
    """Hace lo mismo que cartera.html: K = PBKDF2(frase), cartera cifrada con K y K envuelta con RSA-OAEP."""
    import base64, os
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    salt = os.urandom(16)
    k = cartera._key(phrase, salt)
    blob = cartera.encrypt(src, key=k, salt=salt)
    blob["login"] = login
    blob["key"] = base64.b64encode(pub.encrypt(k, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))).decode()
    return blob


def test_upload_from_browser_roundtrip(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    up_dir, out_dir = tmp_path / "carteras", tmp_path / "out"
    up_dir.mkdir()
    (up_dir / "leo-x.json").write_text(json.dumps(_browser_upload("leo-x", API, "frase de leo larga", priv.public_key())))
    # un archivo con el login cambiado no se procesa
    (up_dir / "otro.json").write_text(json.dumps(_browser_upload("leo-x", API, "frase de leo larga", priv.public_key())))
    monkeypatch.setenv("CARTERA_PRIVADA", pem)
    for k in ("IOL_USUARIO", "IOL_CLAVE", "CARTERA_CLAVE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(cartera, "OUT_DIR", out_dir)
    monkeypatch.setattr(cartera, "LEGACY", tmp_path / "legacy.enc")
    real_run = cartera.run_user
    monkeypatch.setattr(cartera, "run_user", lambda *a, **kw: real_run(*a, **{**kw, "fetch": fake_fetch, "out_dir": out_dir}))
    assert cartera.main(["--subidas", str(up_dir)]) == 0
    assert not (out_dir / "otro.enc").exists()
    rep = cartera.decrypt(json.loads((out_dir / "leo-x.enc").read_text()), "frase de leo larga")
    assert rep["login"] == "leo-x" and {h["symbol"] for h in rep["holdings"]} == {"AAA", "BON30"}
    # sin la llave privada no se abre nada
    monkeypatch.delenv("CARTERA_PRIVADA")
    (out_dir / "leo-x.enc").unlink()
    assert cartera.main(["--subidas", str(up_dir)]) == 0
    assert not (out_dir / "leo-x.enc").exists()


def test_chat_for_profile():
    assert cartera.chat_for({"telegram": {"chat": "default"}}, {"TELEGRAM_CHAT_ID": "-1001"}) == "-1001"
    assert cartera.chat_for({"telegram": {"chat": ""}}, {"TELEGRAM_CHAT_ID": "-1001"}) is None


def test_manual_holdings_priced_from_byma():
    src = {"source": "manual", "holdings": [{"symbol": "ko", "qty": 13, "ppc": 30000}, {"symbol": "AL30", "qty": 1000},
                                            {"symbol": "IOLCAMA", "qty": 100, "value": 1268}, {"symbol": "XXX", "qty": 0}]}
    h, _ = cartera.normalize(src)
    assert [x["symbol"] for x in h] == ["KO", "AL30", "IOLCAMA"]
    cartera.price_manual(h, {"KO": ("cedear", 34920.0, -0.6), "AL30": ("bono", 81800.0, 1.7)})
    ko, al, fci = h
    assert ko["type"] == "cedear" and ko["value"] == 13 * 34920 and ko["gain_pct"] == pytest.approx(16.4)
    assert ko["gain"] == pytest.approx(13 * 34920 - 13 * 30000)
    assert al["type"] == "bono" and al["value"] == pytest.approx(1000 * 818)
    assert fci["type"] == "fci" and fci["value"] == 1268 and fci["price"] is None
    rep = cartera.build(h, [], json.load(open(cartera.CONFIG)), {}, fetch=fake_fetch, now=NOW)
    assert {r["symbol"]: r["analysis"]["verdict"] for r in rep["holdings"]}["AL30"] == "na"
