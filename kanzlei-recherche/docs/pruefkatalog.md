# Prüfkatalog Kanzlei-Recherche

Stand 30.10.2026. Diese Regeln sind verbindlich für search, crawl, extract, kanzlei-pruefung und export. Jede Regel bekommt einen Test; Abweichungen davon nur nach Rücksprache.

> **Zweck dieses Dokuments** Bisher wurden Fehler nach jedem Volllauf aus der Excel herausgelesen und einzeln nachgebessert. Dieser Katalog dreht das um: Alle bekannten Regeln stehen vollständig an einer Stelle, das Tool prüft sich mit einem Qualitätsbericht selbst, und ein Volllauf startet erst, wenn eine Stichprobe die Abnahmekriterien erfüllt. Ziel ist Vollständigkeit und Richtigkeit von ≥ 95 % je Bundesland, der Rest landet nachvollziehbar in einer Nachpflege-Liste – nicht stillschweigend im Hauptblatt.


## 1. Was eine Kanzlei ist – und was nicht

Die Zielmenge sind Steuerberaterkanzleien im Sinne der Kammerstatistik: Standorte, an denen mindestens ein Steuerberater, Steuerbevollmächtigter oder eine Steuerberatungsgesellschaft tätig ist. Jeder Treffer durchläuft erst die Ausschlussregeln (A), dann die Positivsignale (B).


### A. Ausschluss – nie eine Kanzlei (hart, vor dem LLM)

| Kategorie | Erkennungsmerkmal | Zielblatt / Grund |
|---|---|---|
| Lohnsteuerhilfeverein | „Lohnsteuerhilfe“, „Lohnsteuerhilfeverein“, „Lohnsteuerberatungsverbund“ im Namen oder in der Anbieterzeile („e. V.“ allein → Verein, s. u.) | Keine Kanzlei – „Lohnsteuerhilfeverein“ |
| Reine Rechtsanwaltskanzlei | „Rechtsanwalt/Rechtsanwälte/Anwaltskanzlei“ im Namen, kein „Steuerberater“ in Name, Impressum oder Berufsbezeichnung | Keine Kanzlei – „Rechtsanwaltskanzlei“ |
| Buchhaltungsbüro ohne Berufsträger | „Buchhaltung“, „Bilanzbuchhalter“, „Buchführungsservice“ ohne StB/StBv/WP im Impressum | Keine Kanzlei – „Buchhaltungsbüro“ |
| Kammer, Verband, Versorgungswerk | „Steuerberaterkammer“, „Steuerberaterverband“, „Versorgungswerk“, „Bundessteuerberaterkammer“ im Namen oder in der Anbieterzeile des Impressums. Eine Kammer- oder Verbandsadresse allein schließt nicht aus – es gilt Abschnitt 2 (nur Abweichungshinweis) | Keine Kanzlei – „Institution“ |
| Behörde, Gemeinde, Amt | Domain gemeinde-*, amt-*, stadt-*, kreis-*; „Amtsdirektor(in)“, „Amtsvorsteher“, „Bürgermeister“ als Person – die Rolle nur, wenn das Impressum weder eine Steuerberaterkammer noch eine StB-Berufsbezeichnung nennt (ein Steuerberater, der zugleich Amtsvorsteher ist, bleibt Kanzlei; die Person wird als Ansprechpartner verworfen) | Keine Kanzlei – „Behörde“ |
| Partei, Verein, Kirche, Schule | spd-*, cdu-*, gruene-*, *-verein, kirche-*, schule-*; „e. V.“, „…verein“, „Partei“ im Namen oder in der Anbieterzeile; Vereinsregister im Impressum nur ohne Positivsignal 1B Rang 1–3 (Mitgliedsverbände im Impressum) | Keine Kanzlei – „Verein/Partei“ |
| Portal, Verzeichnis, Jobbörse, Software | Blocklist (config/domain_blocklist.txt) plus Muster: *linklisten*, *firmenkataloge*, *jobs.*, *karriere.* als Hauptdomain | Keine Kanzlei – „Portal“ |
| Andere Branche | Osteopathie, IT-Systemhaus, Unternehmensberatung ohne StB, Bank, Versicherung – kein Berufsträger, keine Kammer | Keine Kanzlei – „Branchenfremd“ |
| Ausland | TLD .at .ch .li .tirol .wien .swiss; PLZ 4-stellig; Ort in AT/CH-Ortsliste; „Wirtschaftstreuhänder“, „Mag.“+„Steuerberater“ ohne deutsche PLZ | Außerhalb – „Ausland“ (ohne LLM) |

Gemischte Kanzleien (Steuerberater + Rechtsanwälte, StB + WP, Sozietät mit Notar) sind Kanzleien, sobald ein Steuerberater benannt ist.

**Großgesellschaften sind keine Ausschlusskategorie** (Entscheidung 30.10.2026): BDO, EY, PwC, KPMG, Deloitte, Rödl & Partner, Baker Tilly, Forvis Mazars, RSM Ebner Stolz, Grant Thornton, PKF, HLB, WTS u. a. sind Zielkanzleien – nicht auf die Blocklist, Kanzleityp „Großgesellschaft“ (Abschnitt 9).

Reihenfolge der Kategorien: Portal → Institution → Lohnsteuerhilfeverein → Behörde („Gemeinde X“, „Amt X“, Gemeindeverwaltung im Namen/Anbieter, Domain) → Verein/Partei → Rechtsanwaltskanzlei → Buchhaltungsbüro (Rücksprache 30.10.2026: „Steuerberaterverband … e.V.“ ist Institution, Gemeinde Behörde, Gewerbeverein Verein).

Namensmerkmale aus 1A (Lohnsteuerhilfe, „e. V.“, Kammer/Verband, Rechtsanwalt, Buchhaltung) gelten im Kanzleinamen und in der Anbieterzeile des Impressums, nicht im Aufsichts- oder Mitgliedschaftsteil („Mitglied im Steuerberaterverband … e. V.“) – Rücksprache 30.10.2026.


### B. Positivsignale (Rangfolge, erstes zutreffendes entscheidet die Konfidenz)

| Rang | Signal | Konfidenz |
|---|---|---|
| 1 | Zuständige Steuerberaterkammer im Impressum (Kammername oder „Steuerberaterkammer“ + Bundesland) | hoch |
| 2 | Berufsbezeichnung „Steuerberater/in“, „Steuerbevollmächtigte/r“ direkt bei einem Personennamen (Impressum, Team, Startseite) | hoch |
| 3 | Rechtsform mit „Steuerberatungsgesellschaft“ (mbH, mbB, PartG, KG, AG) im Impressum | hoch |
| 4 | Kanzleiwort im Namen (Steuerkanzlei, Steuerbüro, Steuerberatung) ohne Kammer und ohne benannte Person | mittel – Nachpflege-Liste |
| 5 | Nur Suchtreffer-Snippet nennt „Steuerberater“, Seite liefert nichts davon | niedrig – Nachpflege-Liste, nie Hauptblatt |


## 2. Feldregeln


### Kanzleiname

- Quelle in dieser Reihenfolge: Impressum-Anbieter → Rechtsform-Zeile → `<title>`/Logo-Alt → Ableitung aus Berufsträger („Steuerberater Vorname Nachname“).
- Abgeleitete Namen bekommen Konfidenz „mittel“, Abweichung „Name abgeleitet“. Ohne Kammer und ohne Berufsbezeichnung → nie Hauptblatt.
- Verboten als Name: Navigationswörter (Start, Kontakt, Impressum, Datenschutz, Leistungen, Aktuelles), Alt-Text-Reste, Ortsnamen allein, „Regionen“, „Homepages“, Rollen („Die Amtsdirektorin“).
- Mehrstandort-Gesellschaften (SHBB, wetreu, ETL, ECOVIS): Zentrale einmal, je Standort eine Zeile mit Spalte „Zentrale/Standort“; Standortseite schlägt Zentralseite bei Telefon und Ansprechpartner.


### Adresse und Bundesland

- Pflicht: deutsche 5-stellige PLZ. Ohne PLZ oder mit 4-stelliger PLZ → Blatt „Außerhalb Bundesland“ mit Grund „keine deutsche PLZ“. Nie ins Hauptblatt.
- PLZ ↔ Ort über OpenPLZ prüfen; stimmen sie nicht überein, gilt die PLZ, Abweichung „PLZ/Ort“ bleibt. PLZ-Zahlendreher (24678 statt 24768 Rendsburg) → Ort aus OpenPLZ gewinnt, wenn Ort eindeutig und PLZ ungültig.
- Bundesland ausschließlich aus der PLZ, nie aus Snippet oder Telefonvorwahl.
- Kammer- und Verbandsadressen (Ausschlussliste) als Kanzleiadresse nur mit Abweichungshinweis (Hopfenstraße 2d Kiel ist ein echtes Bürohaus).
- Straße ohne Hausnummer → Abweichung „Hausnummer fehlt“; Postfach-Adressen nur, wenn keine Straße vorhanden.


### Telefon

- Parse mit phonenumbers (Region DE), Ausgabe `format_number(…, INTERNATIONAL)` – Gruppierung kommt immer aus der Bibliothek, nie aus Rohtext oder LLM.
- Nur Nummern mit `is_valid_number`. Ungültig → leer lassen plus Abweichung „Telefon ungültig“ (kein Rätselraten).
- Vorwahl-Region muss zur PLZ-Region passen (Vorwahl-zu-Kreis-Tabelle der BNetzA; Hamburg 040 für SH-Umland zulässig). Passt sie nicht → Abweichung „Vorwahl passt nicht zur PLZ“, Konfidenz höchstens „mittel“.
- Festnetz vor Mobil, Mobil vor 0800/0180. Fax nie als Telefon (Zeilen mit „Fax“, „Telefax“, „F:“ ausschließen).
- Nummern aus `<script>`, `tel:`-Links und Schema.org (telephone) gelten als Quelle, Rang: tel:-Link → Schema.org → Impressum-Text → Kontaktseite. Nennt der sichtbare Text eine andere Nummer derselben Klasse als tel:-Link/Schema.org, gewinnt der Text; Abweichung „Telefon aus dem Text X, tel:-Link nennt Y“ (Entscheidung 30.10.2026, sievertundpartner.de). Der Rang entscheidet damit unter den sichtbaren Nummern; eine Link-Nummer allein gilt nur, wenn der Text keine nennt.


### E-Mail

- Bevorzugt die Adresse, deren Domain zur Website-Domain gehört; abweichende Domain erlaubt (z. B. voss-steuerberater.de bei steuerfuechse.tax), aber als Abweichung „E-Mail-Domain abweichend“.
- Ausgeschlossen: datenschutz@, dsb@, bewerbung@, karriere@, noreply@, webmaster@, Adressen von Agenturen/Hostern.
- Mehrere Adressen: info@/kanzlei@/mail@ vor Personenadressen; Listen sind erlaubt, Hauptadresse steht vorn.


### Ansprechpartner

- Pflichtform: Vorname + Nachname (mindestens zwei Wörter, erstes Wort kein Titel allein). Nur Nachname („Steuerberaterin Schröder“) → Feld leer, Nachname in Abweichung. Titel und Berufskürzel vor dem Namen (Dr., Prof., Dipl.-Kfm., WP, StB, M.Sc., B.A. …) werden vorher abgeschnitten und wandern in den Titel – „Dr. Dr. Rolf Harrie“ bleibt (Rückmeldung 30.10.2026, 900grad.de).
- Rang: Vertretungsberechtigter im Impressum → erster Berufsträger auf Teamseite → Inhaber laut Startseite. Rechtsanwälte und Notare nur, wenn kein Steuerberater benannt ist.
- Präfixe abschneiden: „Porträt von“, „Porträtfoto von“, „Foto von“, „Bild von“, „Portrait“, „Bild:“, „Abbildung“. Suffixe abschneiden: „Steuerberater“, „StB“, „Dipl.-Kfm.“ (gehen in Titel/Funktion).
- Verwerfen, wenn der Text enthält: „Datenschutz“, „Grundverordnung“, „Fachanwalt … [Ort]“, „Amtsdirektor“, „Amtsvorsteher“, „Bürgermeister“, „Präsident“, „Archiv“, „Regionen“, „Homepages“, „Anträge“, „Browser“, „Verein“, Ortsnamen, Ländernamen, Monatsnamen, Wochentage.
- Ein Name darf nicht gleichzeitig Kanzleiname und Ansprechpartner-Rest aus Navigation sein (Scheinnamen aus Menüs, Cookie-Bannern, Kundenstimmen).
- Mehrstandort: Standortleitung vor Geschäftsführung der Zentrale.


### Teamgröße und Berufsträger

- Basis dokumentieren (Teamseite gezählt / Text genannt / Einzelkanzlei) – bleibt wie heute in „Teamgröße-Basis“.
- Teamseite: Personen deduplizieren (Name + Rolle), Vornamen-nur-Listen zählen, Kundenstimmen/Partnerlogos/„Wir suchen“ nicht zählen.
- Textangaben: „über 30“ → 31, „rund 20“ → 20, „mehr als 35“ → 36; Spannen („20–25“) → Untergrenze.
- Größenklasse nur aus Teamgröße; ohne Teamgröße leer, nicht geraten.


## 3. Domain- und Crawl-Regeln

- Blocklist-Kategorien pflegen: Portale, Verzeichnisse, Jobbörsen, Kammern/Verbände, Behörden, Software, Nachrichten, Lexika. Neue Vorschläge über `blocklist-vorschlag` nach jedem Lauf, Übernahme nur nach Sichtung.
- Baukasten-/Hoster-Subdomains (*.strato-hosting.eu, *.webspaceconfig.de, *.jimdosite.com, *.wixsite.com): als Kanzlei zulässig, aber Domain der Kanzlei (Impressum/Canonical) gewinnt für Dublettenerkennung.
- Impressum-Suche: Pfade (/impressum, /impressum/, .html, .php, /imprint, /rechtliches, /rechtliche-hinweise, /anbieterkennzeichnung, /kontakt, /legal) und Link-Texte (Impressum, Imprint, Rechtliches, Anbieterkennzeichnung, Kontakt). Ohne Treffer: Startseite + Kontaktseite extrahieren, dann Playwright für Menü-Links. Erst danach „Kein Impressum gefunden“.
- Weiterleitungen auf andere Domain: Ziel-Domain übernehmen, Quelle als Alias (Dubletten „weitere Domains“).
- robots.txt nach RFC 9309; Verbot respektieren und als Grund festhalten; nicht erreichbar → Seite lesen.
- Fehlerklassen getrennt zählen: DNS tot, ConnectError, Timeout, HTTP 4xx/5xx, robots, kein Impressum, ohne Text. Nur DNS tot gilt als endgültig; alle anderen bekommen zwei Wiederholungen im Abstand von Tagen.


## 4. Automatischer Qualitätsbericht (neuer Befehl `recherche qualitaet -b SH`)

Das Tool prüft nach jedem Lauf selbst und schreibt die Treffer in ein Blatt „Qualität“ des Exports sowie als Tabelle ins Terminal. Jede Prüfung hat einen Schweregrad: **Sperre** (Zeile darf nicht ins Hauptblatt), **Warnung** (bleibt, Abweichung gesetzt), **Hinweis** (nur Statistik).

| Nr. | Prüfung | Grad | Reaktion |
|---|---|---|---|
| Q1 | Keine deutsche 5-stellige PLZ oder TLD .at/.ch/.li/.tirol | Sperre | → Außerhalb, Grund „keine deutsche PLZ“/„Ausland“ |
| Q2 | Name/Impressum matcht Ausschlussliste aus Abschnitt 1A | Sperre | → Keine Kanzlei mit Kategorie |
| Q3 | Ansprechpartner matcht Verwerf-Liste oder hat nur ein Wort | Sperre für das Feld | Feld leeren, Abweichung „Ansprechpartner verworfen“ |
| Q4 | Telefon ungültig nach phonenumbers oder Vorwahl passt nicht zur PLZ-Region | Warnung | Abweichung, Konfidenz ≤ mittel |
| Q5 | Kanzleiname abgeleitet und keine Kammer, keine Berufsbezeichnung | Sperre | → Nachpflege-Liste „zu prüfen“ |
| Q6 | Kanzleiname enthält Navigationswort, Ortsname allein, Rolle | Sperre | → Nachpflege-Liste |
| Q7 | PLZ und Ort passen laut OpenPLZ nicht zusammen | Warnung | Abweichung „PLZ/Ort“ |
| Q8 | E-Mail aus Ausschlussliste (datenschutz@, karriere@ …) | Warnung | nächste Adresse nehmen |
| Q9 | Gleiches Telefon + gleicher Ansprechpartner bei verschiedenen Domains | Hinweis | Dubletten-Blatt (wie heute) |
| Q10 | Gleiche Adresse (Straße+PLZ) bei verschiedenen Kanzleinamen | Hinweis | Bürogemeinschaft oder Dublette – Nachpflege-Liste |
| Q11 | Mehrstandort-Standort ohne eigenes Telefon/Ansprechpartner | Hinweis | Zählung, später Standortseiten nachladen |
| Q12 | Konfidenz niedrig im Hauptblatt | Sperre | → Nachpflege-Liste |
| Q13 | Teamgröße > 200 oder Berufsträger > Teamgröße | Warnung | Abweichung „Teamgröße unplausibel“ |
| Q14 | Coverage-Zahl ≠ Hauptblatt-Zeilen mit Bundesland | Hinweis | Beide Zahlen ausgeben, Differenz erklären |

Jede Sperre verschiebt die Zeile, löscht sie nie. Nichts verschwindet; alles ist in einem Blatt mit Grund nachvollziehbar.


## 5. Abnahme vor jedem Volllauf

1. **Testdatensatz:** 12 echte Positiv-Fixtures (vorhanden) plus mindestens 15 Negativ-Fixtures (je Ausschlusskategorie aus 1A eine, dazu drei Alt-Text-Fälle, zwei Telefon-Fälle, zwei Ausland-Fälle). Alle Tests grün, bevor ein Lauf startet.
2. **Kleinlauf:** ein Kreis oder 5 Orte (z. B. Kiel-Stadtteile), vollständige Pipeline bis Export.
3. **Stichprobe:** neuer Befehl `recherche stichprobe -b SH -n 50 --seed 1` zieht 50 zufällige Zeilen des Hauptblatts in ein Blatt „Stichprobe“ mit Quell-URLs. Prüfung von Hand (ich): je Feld richtig / falsch / leer.
4. **Abnahmekriterium:** höchstens 2 falsche Werte je Kernfeld (Name, Straße, PLZ/Ort, Telefon, Ansprechpartner) in 50 Zeilen (≤ 4 %), null Zeilen, die keine Kanzlei sind, null Ausland. Nicht erfüllt → Regel nachschärfen, Kleinlauf wiederholen. Kein Volllauf vorher.
5. **Volllauf** nur mit Freigabe nach bestandener Stichprobe; nach dem Volllauf dieselbe Stichprobe noch einmal (zweiter Seed).
6. **Ein Thema je Änderung:** jede Nachricht an Claude Code enthält genau ein Thema mit Erfolgskriterium, Commit je Thema; Sammel-Listen nur als Katalog wie dieser.


## 6. Der Weg zu den letzten 10 % (Vollständigkeit)

Die Websuche allein erreicht in SH 75 % der Referenz. Die Lücke schließt sich nur durch Abgleich gegen eine vollständige Grundliste – nicht durch mehr Suchanfragen.

| Stufe | Quelle | Liefert | Ziel-Coverage |
|---|---|---|---|
| A | Websuche (Brave) + Kanzlei-Website | Vollständige Datensätze mit Teamgröße | 70–80 % |
| B | Amtliches Steuerberaterverzeichnis, PLZ für PLZ, als Grundliste | Name, Adresse, Berufsträger aller Kanzleien; Abgleich zeigt, wer fehlt | Lückenliste vollständig |
| B2 | Brave-Nachsuche „{Name} {Ort}“ nur für Lücken | Website → Crawl → Extraktion (Telefon, Ansprechpartner, Team) | 85–92 % |
| C | Branchenverzeichnisse (gelbeseiten, dasoertliche, 11880) nur Ergänzung | Telefon/Adresse für Kanzleien ohne Website | 93–96 % |
| D | Nachpflege-Liste (Blatt „Zu prüfen“) mit Grund und Quell-URL | Manuelle Prüfung der Restfälle, zurück ins Tool über `fixture-add`/Korrektur-CSV | ≥ 95 % + Rest dokumentiert |

Messgröße bleibt die Kammerstatistik (BStBK), je Kreis. Ein Bundesland gilt als fertig, wenn Coverage ≥ 95 %, Qualitätsbericht ohne Sperren im Hauptblatt und Stichprobe bestanden. Erst dann startet das nächste Bundesland.


## 7. Offene Fehler aus der Stichprobe sh_gesamt.xlsx (30.10.2026)

Diese Punkte sind Instanzen der Regeln oben; sie werden mit dem Katalog umgesetzt, nicht einzeln.

| Befund | Umfang | Regel |
|---|---|---|
| Zeilen ohne Bundesland im Hauptblatt (AT/CH, Berlin, Portale) | 97 | Q1, 1A Ausland/Portal |
| Lohnsteuerhilfevereine als Kanzlei | 9 | 1A, Q2 |
| Reine Rechtsanwaltskanzleien als Kanzlei | 8 | 1A, Q2 |
| Alt-Text/Rollen als Ansprechpartner („Porträt von …“, „Amtsdirektorin“) | 6 | Ansprechpartner, Q3 |
| Fremde Domains mit abgeleitetem Namen (SPD-Ortsverein, Gemeinde, Amt, Osteopathie) | 5 | 1A Behörde/Verein, Q5 |
| Telefon falsch gruppiert („+49 41 5359740“) | 52 | Telefon, Q4 |
| Telefon ohne Bezug zur PLZ (z. B. +49 2357 … in Kiel) | einzelne | Q4 |
| SHBB-Standorte nur mit Zentralkontakt | 55 | Kanzleiname Mehrstandort, Q11 |
| „Kein Impressum gefunden“ – mögliche Kanzleien nicht gelesen | 454 | Crawl Impressum-Suche |
| Coverage 811 vs. 749 Hauptblatt-Zeilen SH | 62 | Q14 |
| Blocklist-Ergänzung: stbkammer-mv.de, stbk-koeln.de, stbk-stuttgart.de, steuerberaterkammer-westfalen-lippe.de, sbk-rlp.de, steuerberaterplattform-bstbk.de, stbvw-sh.de, deutsche-rentenversicherung.de, linklisten24.com, firmenkataloge.com, gruenderlexikon.de, gruenderthemen.de, selbststaendig.de, adzuna.de, driversnote.de, opengeodb.de, einwohner.info, urlm.de, steuermatch.com, autologg.com, handelskammer.se, bakertilly.de, berufsrecht-handbuch.de, jobs.denkerwulf.de | 24 Domains | Abschnitt 3 |


## 9. Ketten und Großgesellschaften

Mehrstandort-Gesellschaften und große Prüfungs-/Steuerberatungsgesellschaften sind Zielkanzleien; jeder Standort ist eine eigene Zeile, mit der Zentrale verknüpft (Spalten „Zentrale/Standort“ und „Zentrale“).

| Regel | Inhalt |
|---|---|
| Kanzleityp | Spalte „Kanzleityp“ im Export: **Großgesellschaft** (Namensmuster oder Domain aus `config/ketten.yaml`, Liste `grossgesellschaften`) vor **Kette** vor **Einzelkanzlei** (Rechtsform Einzelkanzlei) vor **Mittelstand** (alle übrigen). |
| Kette erkennen | Eintrag in `config/ketten.yaml` (Liste `ketten`) oder automatisch: mindestens 3 Standorte mit demselben Impressum-Anbieter (gleiche Impressum-URL) bzw. eine Standort-Gruppe (gleiche Stammdomain, gleiches Telefon + Ansprechpartner) mit mindestens 3 Zeilen. Jede Kette steht im Qualitätsbericht (K1, Hinweis). |
| Standortliste | Je Kette in `config/ketten.yaml`: Übersichts-URL, Link-Muster, Standortmuster (Subdomain / Pfad / eigene Domain), Zentrale. Ein generischer Standortlisten-Crawler legt je Standort einen Kandidaten an, verknüpft mit der Zentrale; die Standorte laufen danach durch crawl und extract. Erster Eintrag: SHBB (Übersicht www.shbb.de/kanzlei-suche, Standorte `<ort>.shbb.de`). Großgesellschaften laufen über denselben Crawler. Umgesetzt: `recherche ketten` (`ketten_crawl.py`); Pfad-Standorte ohne eigene Domain mit der Standort-URL als Website. |
| Standortseite vor Zentrale | Für Ketten ist die Standortseite die Quelle für Telefon (Zentrale des Standorts, nicht die Durchwahl einer Person), Adresse, E-Mail und Ansprechpartner (erste Person mit „Leiter“ bzw. Standortleitung); Impressum und Seiten der Zentrale nur als Rückfall (Q11). |

## 8. Reihenfolge der Umsetzung

1. Katalog nach `docs/pruefkatalog.md` ins Repo, CLAUDE.md verweist darauf (Regeln sind Pflicht, nicht Vorschlag).
2. Negativ-Fixtures und Tests für Abschnitt 1A, 2 (Ansprechpartner, Telefon, PLZ) – ein Commit.
3. Qualitätsbericht `qualitaet` und Blatt „Qualität“/„Zu prüfen“ im Export – ein Commit.
4. `stichprobe`-Befehl – ein Commit.
5. Impressum-Suche erweitern, `extract --wiederholen` für die 454 – ein Commit.
6. Kleinlauf Kiel, Stichprobe, Abnahme. Erst dann `extract --alle` für den SH-Bestand und Export v2.
7. Stufe B (amtliches Verzeichnis) für SH starten, sobald v2 abgenommen ist.
