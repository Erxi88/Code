"""Brave Search API (Web Search) – Standardquelle für Stufe A.

- Key aus .env (BRAVE_API_KEY), Header X-Subscription-Token
- bis 20 Treffer je Anfrage (count), Paging über offset = Seitenindex (0..9)
- country=DE, search_lang=de
- Retry mit Backoff bei 5xx und 429 (Ratenlimit je Sekunde)
- 402 oder Guthaben-Fehlertext: Abbruch „Brave-Guthaben erschöpft …"; Monatslimit gesetzt (X-RateLimit-Limit
  Monatswert > 0) und Rest 0: Abbruch „Monatskontingent aufgebraucht …" (eine erfolgreiche Antwort mit Rest 0 wird
  noch genutzt); 429 ohne Guthabentext: Ratenlimit je Sekunde → warten bis X-RateLimit-Reset und weiter. Jede
  Abbruchmeldung nennt HTTP-Status und X-RateLimit-Limit/Remaining/Reset (Rückmeldung 05.11.2026: Monatswert 0 ohne
  gesetztes Limit galt fälschlich als „Restkontingent 0")
- 401/403: Key ungültig → Lauf endet sauber
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator

import httpx

from ..config import BraveConfig, env
from .base import KontingentErschoepft, SearchError, SearchHit, ZugangVerweigert
from .http_base import HttpQueryAdapter, Retry, fehlertext, text_ohne_html, url_id

ENV_KEY = "BRAVE_API_KEY"
ERSCHOEPFT = "Brave-Guthaben erschöpft – im Dashboard aufladen, dann erneut starten"
MONAT_AUFGEBRAUCHT = ("Brave-Monatskontingent aufgebraucht (Monatslimit erreicht) – Limit im Dashboard erhöhen oder "
                      "bis zum Reset warten, dann erneut starten")
_GUTHABEN_TEXT = re.compile(r"credit|usage.?limit|quota|insufficient|payment|balance|subscription", re.I)
MAX_OFFSET = 9  # Brave erlaubt Seitenindex 0..9


def _werte(resp: httpx.Response, kopf: str) -> list[int | None]:
    """'<je Sekunde>, <je Monat>' aus einer X-RateLimit-Kopfzeile (fehlend/unlesbar → None)."""
    teile = [t.strip() for t in (resp.headers.get(kopf) or "").split(",") if t.strip()]
    werte = []
    for t in teile:
        try:
            werte.append(int(float(t)))
        except ValueError:
            werte.append(None)
    return werte


def _monat(resp: httpx.Response, kopf: str) -> int | None:
    werte = _werte(resp, kopf)
    return werte[1] if len(werte) >= 2 else None


def _monatsrest(resp: httpx.Response) -> int | None:
    """Restkontingent des Monats aus X-RateLimit-Remaining – nur, wenn ein Monatslimit gesetzt ist (Limit > 0). Ohne
    Monatslimit (Abrechnung nach Verbrauch) meldet Brave dort 0 – das ist kein erschöpftes Kontingent (05.11.2026)."""
    limit, rest = _monat(resp, "X-RateLimit-Limit"), _monat(resp, "X-RateLimit-Remaining")
    if rest is None or not limit:
        return None
    return rest


def ratenlimit_kopf(resp: httpx.Response) -> str:
    """„HTTP 429 · Limit 1, 0 · Remaining 0, 0 · Reset 1, 2419200" – für Meldungen beim Abbruch."""
    teile = [f"HTTP {resp.status_code}"]
    for kopf in ("Limit", "Remaining", "Reset"):
        if (wert := resp.headers.get(f"X-RateLimit-{kopf}")) is not None:
            teile.append(f"{kopf} {wert}")
    return " · ".join(teile)


class BraveAdapter(HttpQueryAdapter):
    name = "brave"
    cfg: BraveConfig

    def __init__(self, cfg: BraveConfig | None = None, **kwargs):
        super().__init__(cfg or BraveConfig(), **kwargs)
        self.api_key = env(ENV_KEY)
        self._erschoepft = False
        self._grund: str | None = None

    def fehlende_keys(self) -> list[str]:
        return [] if self.api_key else [ENV_KEY]

    def _seiten(self, query: str, max_results: int) -> Iterator[tuple[dict, dict]]:
        per_page = min(self.cfg.per_page, 20)
        gesamt = min(max_results, self.cfg.max_per_query)
        seiten = min(math.ceil(gesamt / per_page), MAX_OFFSET + 1)
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": self.api_key or "",
        }
        for offset in range(seiten):
            yield (
                {
                    "q": query,
                    "count": per_page,
                    "offset": offset,
                    "country": self.cfg.country,
                    "search_lang": self.cfg.search_lang,
                    "safesearch": self.cfg.safesearch,
                    "result_filter": "web",
                },
                headers,
            )

    def _treffer(self, antwort: dict) -> list[SearchHit]:
        hits = []
        for r in (antwort.get("web") or {}).get("results") or []:
            url = (r.get("url") or "").strip()
            if not url:
                continue
            hits.append(
                SearchHit(
                    quelle=self.name,
                    quell_id=url_id(url),
                    name=text_ohne_html(r.get("title")),
                    website=url,
                    snippet=text_ohne_html(r.get("description")),
                    roh_url=url,
                )
            )
        return hits

    def _hat_mehr(self, antwort: dict, treffer: list[SearchHit], erwartet: int) -> bool:
        mehr = (antwort.get("query") or {}).get("more_results_available")
        if mehr is False:
            return False
        return len(treffer) >= erwartet

    def _hole(self, params: dict, headers: dict) -> dict:
        if self._erschoepft:
            raise KontingentErschoepft(self._grund or MONAT_AUFGEBRAUCHT)
        return super()._hole(params, headers)

    def _pruefe(self, resp: httpx.Response) -> Retry | None:
        s = resp.status_code
        kopf = ratenlimit_kopf(resp)
        if s == 200:
            if _monatsrest(resp) == 0:  # diese Antwort noch nutzen, dann Schluss
                self._erschoepft = True
                self._grund = f"{MONAT_AUFGEBRAUCHT} ({kopf})"
            return None
        text = fehlertext(resp)
        if s == 402 or (s == 429 and _GUTHABEN_TEXT.search(text)):
            raise KontingentErschoepft(f"{ERSCHOEPFT} ({kopf}: {text})")  # Guthaben
        if _monatsrest(resp) == 0:
            raise KontingentErschoepft(f"{MONAT_AUFGEBRAUCHT} ({kopf}: {text})")
        if s == 429:
            # Ratenlimit je Sekunde: warten bis X-RateLimit-Reset (Sekunden-Wert), dann weiter; bleibt es dabei, wird
            # nur diese Anfrage übersprungen – die Nachsuche/der Lauf geht weiter
            reset = (_werte(resp, "X-RateLimit-Reset") or [None])[0]
            return Retry(f"429 Ratenlimit je Sekunde ({kopf}: {text})", endgueltig=SearchError,
                         warte_s=max(float(reset or 1), 1.0))
        if s in (401, 403):
            raise ZugangVerweigert(f"brave: Zugriff verweigert – {ENV_KEY} prüfen ({text})")
        if s >= 500:
            return Retry(f"HTTP {s} ({text})")
        raise SearchError(f"brave: HTTP {s} ({text})")
