"""robots.txt nach RFC 9309 – ersetzt urllib.robotparser.

urllib.robotparser vergleicht den User-Agent als Teilstring („User-agent: re" sperrt
„kanzlei-recherche") und nimmt die erste passende Regel statt der längsten. Hier:

- Gruppen: aufeinanderfolgende `User-agent`-Zeilen teilen sich die folgenden Regeln.
- Für uns gilt jede Gruppe, deren User-Agent unser Produkt-Token ohne Groß/Klein **genau**
  trifft; gibt es keine, gelten die `*`-Gruppen; sonst ist alles erlaubt. Ein Disallow nur für
  GPTBot, ClaudeBot oder CCBot betrifft uns also nicht.
- Längste passende Regel gewinnt, bei Gleichstand `Allow`. `*` und `$` werden unterstützt,
  ein leeres `Disallow:` sperrt nichts.
- BOM, CRLF, Kommentare und HTML statt robots.txt (Soft-404) werden toleriert.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit


@dataclass
class Robots:
    gruppen: list[tuple[list[str], list[tuple[bool, str]]]] = field(default_factory=list)

    @classmethod
    def parse(cls, text: str | None) -> Robots:
        text = (text or "").lstrip("﻿")
        if text.lstrip()[:1] == "<":  # HTML-Seite statt robots.txt
            return cls()
        gruppen: list[tuple[list[str], list[tuple[bool, str]]]] = []
        agents: list[str] = []
        regeln: list[tuple[bool, str]] = []
        for zeile in text.splitlines():
            zeile = zeile.split("#", 1)[0].strip()
            if ":" not in zeile:
                continue
            schluessel, wert = (x.strip() for x in zeile.split(":", 1))
            schluessel = schluessel.lower()
            if schluessel == "user-agent":
                if regeln:  # neue Gruppe nach Regeln
                    gruppen.append((agents, regeln))
                    agents, regeln = [], []
                agents.append(wert.lower())
            elif schluessel in ("allow", "disallow") and agents:
                if wert:  # leeres Disallow sperrt nichts
                    regeln.append((schluessel == "allow", wert))
        if agents:
            gruppen.append((agents, regeln))
        return cls(gruppen)

    def regeln_fuer(self, token: str) -> list[tuple[bool, str]]:
        token = token.lower()
        eigene = [r for agents, regeln in self.gruppen if token in agents for r in regeln]
        if any(token in agents for agents, _ in self.gruppen):
            return eigene
        return [r for agents, regeln in self.gruppen if "*" in agents for r in regeln]

    def erlaubt(self, user_agent: str, url: str) -> bool:
        teile = urlsplit(url)
        pfad = unquote(teile.path or "/") + (f"?{teile.query}" if teile.query else "")
        beste: tuple[int, bool] | None = None
        for erlaubt, muster in self.regeln_fuer(produkt_token(user_agent)):
            if _passt(muster, pfad):
                kandidat = (len(muster), erlaubt)  # länger gewinnt, bei Gleichstand Allow (True > False)
                if beste is None or kandidat > beste:
                    beste = kandidat
        return True if beste is None else beste[1]

    def sperrt_alles(self, user_agent: str) -> bool:
        return not self.erlaubt(user_agent, "https://x/")


def produkt_token(user_agent: str) -> str:
    """„kanzlei-recherche/0.1 (Recherche-Tool …)" → „kanzlei-recherche"."""
    return re.split(r"[/\s]", user_agent.strip(), maxsplit=1)[0].lower()


def _passt(muster: str, pfad: str) -> bool:
    anker = muster.endswith("$")
    rumpf = unquote(muster[:-1] if anker else muster)
    regex = "".join(".*" if c == "*" else re.escape(c) for c in rumpf)
    return re.match(regex + ("$" if anker else ""), pfad) is not None
