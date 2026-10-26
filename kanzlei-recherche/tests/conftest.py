import json
from pathlib import Path

import pytest

from recherche.config import load_settings
from recherche.models import Dublette, Kanzlei, LaufEintrag, NichtGefunden
from recherche.storage import Storage

FIXTURES = Path(__file__).parent / "fixtures"
PROJEKT = Path(__file__).parents[1]


@pytest.fixture(autouse=True)
def _keine_api_keys(monkeypatch):
    """Tests laufen nie mit echten Schlüsseln aus der Umgebung."""
    for name in ("GOOGLE_CSE_API_KEY", "GOOGLE_CSE_ID", "BRAVE_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="session")
def testdaten() -> dict:
    return json.loads((FIXTURES / "kanzleien_20.json").read_text(encoding="utf-8-sig"))


@pytest.fixture
def kanzleien(testdaten) -> list[Kanzlei]:
    return [Kanzlei(**k) for k in testdaten["kanzleien"]]


@pytest.fixture
def ddgs_antworten() -> dict[str, list[dict]]:
    d = json.loads((FIXTURES / "search" / "ddgs_antworten.json").read_text(encoding="utf-8-sig"))
    d.pop("_hinweis", None)
    return d


@pytest.fixture
def settings():
    return load_settings(PROJEKT / "config" / "settings.yaml")


@pytest.fixture
def db_pfad(tmp_path) -> Path:
    return tmp_path / "test.sqlite"


@pytest.fixture
def storage(db_pfad):
    with Storage(db_pfad) as st:
        yield st


@pytest.fixture
def befuellt(storage, testdaten, kanzleien) -> Storage:
    """Speicher mit allen Fixture-Daten (20 Kanzleien, Nicht gefunden, Dubletten, Protokoll)."""
    storage.import_kanzleien(kanzleien)
    for n in testdaten["nicht_gefunden"]:
        storage.add_nicht_gefunden(NichtGefunden(**n))
    for d in testdaten["dubletten"]:
        storage.add_dublette(Dublette(**d))
    for e in testdaten["lauf_protokoll"]:
        storage.log_lauf(LaufEintrag(**e))
    return storage
