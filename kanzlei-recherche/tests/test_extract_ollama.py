from recherche.extract.base import LlmErgebnis, llm_schema
from recherche.extract.ollama import OllamaExtractor
from recherche.models import Seite, Seitentyp


def _seiten():
    return [
        Seite(url="https://a.example/impressum", domain="a.example", seitentyp=Seitentyp.IMPRESSUM,
              text="Hansen & Petersen Steuerberater PartG mbB"),
        Seite(url="https://a.example/leer", domain="a.example", text=None),
    ]


def test_payload_erzwingt_schema_und_schaltet_thinking_ab(settings):
    p = OllamaExtractor(settings.llm).build_payload(_seiten())
    assert p["think"] is False
    assert p["format"] == llm_schema()
    assert p["stream"] is False
    assert p["options"]["temperature"] == 0.0
    assert p["model"] == settings.llm.modell
    assert "PartG mbB" in p["messages"][1]["content"]
    assert "nie" in p["messages"][0]["content"].lower()


def test_think_false_auch_bei_anderem_modell(settings):
    p = OllamaExtractor(settings.llm, modell="qwen3.6:27b").build_payload(_seiten())
    assert p["model"] == "qwen3.6:27b"
    assert p["think"] is False


def test_text_wird_gekuerzt(settings):
    cfg = settings.llm.model_copy(update={"max_text_chars": 50})
    seiten = [Seite(url="u", domain="d", text="x" * 500)]
    assert len(OllamaExtractor(cfg).seiten_text(seiten)) == 50


def test_schema_felder():
    props = llm_schema()["properties"]
    assert {"kanzleiname", "rechtsform", "ansprechpartner", "teamgroesse", "teamgroesse_klasse",
            "teamgroesse_basis"} <= set(props)
    # leeres Ergebnis = alles null, nichts geraten
    assert LlmErgebnis().model_dump() == {
        "kanzleiname": None, "rechtsform": None, "ansprechpartner": [], "teamgroesse": None,
        "teamgroesse_klasse": None, "teamgroesse_basis": None,
    }  # fmt: skip


def test_settings_modelle(settings):
    assert settings.llm.think is False
    assert settings.llm.modell.startswith("qwen3")
    assert settings.llm.fallback_modell == "llama3.1:8b"
