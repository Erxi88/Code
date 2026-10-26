import json

import httpx
import pytest

from recherche.extract.base import (
    AnbieterNichtErreichbar,
    ExtraktionFehler,
    LlmErgebnis,
    llm_schema,
)
from recherche.extract.ollama import OllamaExtractor
from recherche.models import Seite, Seitentyp

SEITEN = {
    Seitentyp.IMPRESSUM: Seite(url="https://a.example/impressum", domain="a.example", seitentyp=Seitentyp.IMPRESSUM,
                               text="Hansen & Petersen Steuerberater PartG mbB\nHolstenstraße 42\n24103 Kiel"),
    Seitentyp.START: Seite(url="https://a.example/", domain="a.example", text="Willkommen " * 50),
    Seitentyp.KONTAKT: Seite(url="https://a.example/impressum", domain="a.example", text="doppelt"),
}
ANTWORT = {"kanzleiname": "Hansen & Petersen Steuerberater PartG mbB", "personen": [
    {"name": "Jens Hansen", "titel": "Dipl.-Kfm.", "funktion": "Steuerberater, Partner"}], "verantwortliche_person": "Jens Hansen"}


def _ollama(antworten):
    gesendet = []

    def handler(req: httpx.Request):
        gesendet.append(json.loads(req.content))
        a = antworten.pop(0)
        if isinstance(a, Exception):
            raise a
        status, daten = a
        return httpx.Response(status, json=daten) if isinstance(daten, dict) else httpx.Response(status, text=daten)

    return httpx.Client(transport=httpx.MockTransport(handler)), gesendet


def _ok(inhalt=ANTWORT, **extra):
    return 200, {"model": "qwen3.5:9b", "message": {"role": "assistant", "content": json.dumps(inhalt)},
                 "prompt_eval_count": 1200, "eval_count": 180, "total_duration": 4_500_000_000, **extra}


@pytest.fixture
def cfg(settings):
    return settings.llm


def test_payload(cfg):
    p = OllamaExtractor(cfg).build_payload(SEITEN)
    assert p["think"] is False
    assert p["format"] == llm_schema()
    assert p["stream"] is False
    assert p["options"] == {"temperature": 0.0, "num_ctx": 16384}
    assert p["model"] == cfg.modell == "qwen3.5:9b"
    inhalt = p["messages"][1]["content"]
    assert inhalt.startswith("### IMPRESSUM (https://a.example/impressum)")
    assert "doppelt" not in inhalt  # gleiche URL wie Impressum nur einmal
    assert "nie" in p["messages"][0]["content"].lower() and "kammer" in p["messages"][0]["content"].lower()


def test_think_false_bei_jedem_modell(cfg):
    for modell in ("qwen3.6:27b", "gemma4:26b", "llama3.1:8b"):
        p = OllamaExtractor(cfg, modell=modell).build_payload(SEITEN)
        assert (p["model"], p["think"]) == (modell, False)


def test_text_budget(cfg):
    klein = cfg.model_copy(update={"max_text_chars": 200})
    text = OllamaExtractor(klein).seiten_text({Seitentyp.START: Seite(url="u", domain="d", text="x" * 5000)})
    assert len(text) <= 200


def test_antwort_mit_laufzeit_und_tokens(cfg):
    client, gesendet = _ollama([_ok()])
    a = OllamaExtractor(cfg, client=client).extract(SEITEN)
    assert a.ergebnis.kanzleiname == "Hansen & Petersen Steuerberater PartG mbB"
    assert a.ergebnis.personen[0].titel == "Dipl.-Kfm."
    assert (a.modell, a.laufzeit_s, a.prompt_tokens, a.antwort_tokens, a.tokens) == ("qwen3.5:9b", 4.5, 1200, 180, 1380)
    assert gesendet[0]["think"] is False


def test_ungueltige_antwort_wird_wiederholt(cfg):
    client, gesendet = _ollama([(200, {"message": {"content": "kein json"}}), _ok()])
    assert OllamaExtractor(cfg, client=client).extract(SEITEN).ergebnis.kanzleiname
    assert len(gesendet) == 2


def test_dauerhaft_ungueltig(cfg):
    client, _ = _ollama([(200, {"message": {"content": "{"}})] * 2)
    with pytest.raises(ExtraktionFehler, match="ungültige Antwort"):
        OllamaExtractor(cfg, client=client).extract(SEITEN)


def test_fallback_modell(cfg):
    client, gesendet = _ollama([(404, '{"error":"model \\"qwen3.5:9b\\" not found, try pulling it first"}'), _ok()])
    e = OllamaExtractor(cfg, client=client)
    e.extract(SEITEN)
    assert [g["model"] for g in gesendet] == ["qwen3.5:9b", "llama3.1:8b"]
    assert e.modell == "llama3.1:8b"


def test_modell_fehlt_ohne_fallback(cfg):
    client, _ = _ollama([(404, '{"error":"model not found"}')])
    with pytest.raises(AnbieterNichtErreichbar, match="ollama pull"):
        OllamaExtractor(cfg.model_copy(update={"fallback_modell": None}), client=client).extract(SEITEN)


def test_nicht_erreichbar(cfg):
    client, _ = _ollama([httpx.ConnectError("refused")])
    with pytest.raises(AnbieterNichtErreichbar, match="localhost:11434"):
        OllamaExtractor(cfg, client=client).extract(SEITEN)


def test_schema_felder():
    props = llm_schema()["properties"]
    assert {"kanzleiname", "rechtsform", "strasse", "plz", "ort", "telefon", "email", "verantwortliche_person",
            "personen", "mitarbeiterzahl", "mitarbeiterzahl_beleg", "teamgroesse_klasse"} == set(props)
    leer = LlmErgebnis().model_dump()
    assert leer["personen"] == [] and all(v is None for k, v in leer.items() if k != "personen")


def test_settings_modelle(settings):
    assert settings.llm.think is False and settings.llm.num_ctx >= 8192
    assert settings.llm.modell == "qwen3.5:9b" and settings.llm.fallback_modell == "llama3.1:8b"


def test_abbruchgrund_und_belegte_felder(cfg):
    client, _ = _ollama([_ok({"kanzleiname": "Kanzlei X", "personen": []}, done_reason="stop", eval_count=52)])
    a = OllamaExtractor(cfg, client=client).extract(SEITEN)
    assert a.abbruchgrund == "stop" and a.antwort_tokens == 52
    assert a.belegte_felder == (1, 12)
