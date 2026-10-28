# Kanzleiseiten-Fixtures

Messgrundlage für die Extraktion (`recherche fixture-eval`).

```
beispiele/<domain>/   fiktive Kanzleien – im Git
echt/<domain>/        echte Kanzleiseiten – NUR LOKAL (.gitignore, personenbezogene Daten)
```

Je Domain:

| Datei | Inhalt |
|---|---|
| `start.html`, `impressum.html`, `kontakt.html`, `team.html`, `team2.html` | Rohseiten (fehlende Seiten fehlen einfach; `team2` = zweite Teamseite, z. B. `/ueber-uns`) |
| `meta.json` | `domain`, `urls` je Seitentyp, `abgerufen_am` |
| `erwartet.json` | Sollwerte, von Hand; erst mit `"_status": "fertig"` bewertet |
| `llm/<modell>.json` | aufgezeichnete Ollama-Antworten (`fixture-eval --live`) |
| `llm/handgeschrieben_<modell>.json` | von Hand geschriebene Antworten der Beispiele (eine echte Aufzeichnung geht vor) |

`erwartet.json`:

```json
{
  "_status": "fertig",
  "kanzleiname": "Hansen & Petersen Steuerberater PartG mbB",
  "rechtsform": "PartG mbB",
  "strasse": "Holstenstraße 42", "plz": "24103", "ort": "Kiel",
  "telefon": "+49 431 9987610",
  "email": "info@hansen-steuerberatung.example",
  "erster_ansprechpartner": "Jens Hansen",
  "berufstraeger_anzahl": 3,
  "teamgroesse": 6,
  "teamgroesse_klasse": null
}
```

- `null` = die Angabe steht nicht auf der Website (dann muss auch das Ergebnis `null` sein).
- Feld weglassen = nicht bewerten.
- Verglichen wird normalisiert: Straße über den Adressschlüssel („Holstenstr. 42" =
  „Holstenstraße 42"), Telefon über die Ziffern, Personen ohne Titel, Namen ohne Satzzeichen.
- `teamgroesse`: Zahl, wenn ablesbar; sonst `null` und `teamgroesse_klasse` (`1`, `2-5`, `6-10`,
  `11-20`, `21-50`, `51-100`, `>100`; Grenzen in `extract.groessenklassen`) oder beides `null`.
- `erster_ansprechpartner`: Hauptansprechpartner (Berufsträger oder Leitung): erste Person im
  Impressum-Abschnitt „vertretungsberechtigt/vertreten durch", sonst erste auf der Teamseite.
  Bei gleichrangigen Partnern auch als Liste zulässiger Namen: `["Jörg Petersen", "Sandra Albers"]`.
- `rechtsform` wird kanonisch verglichen („Partnerschaftsgesellschaft mbB" = „PartG mbB").
- JSON-Dateien dürfen ein UTF-8-BOM haben (PowerShell).

Ablauf: `recherche fixture-add --domain X` → `erwartet.json` ausfüllen → `recherche fixture-eval --live`
(zeichnet für `llm.modell` auf) → danach jederzeit offline `recherche fixture-eval`.
Kürzung und Linkerkennung prüfen: `recherche fixture-prompt --domain X [--live]`.
