# Ausbildung-Bot

Ein Telegram-Bot, der Ausbildungsplätze aus der Jobsuche-API der Bundesagentur für Arbeit
abruft, neue Angebote meldet und Stellen herausfiltert, deren Deutschanforderungen über dem
eigenen Sprachniveau liegen.

Entstanden als persönliches Projekt für die eigene Ausbildungssuche (Fachinformatiker/in,
Kaufmann/-frau für Digitalisierungsmanagement). Der Bot läuft dauerhaft auf Railway und wird
direkt aus diesem Repository deployt.

## Funktionen

- **Einstellungen direkt im Bot** (`/settings`): Berufe, Bundesländer, Städte mit Umkreis,
  Branchen, Ausschlusswörter, frühester Ausbildungsbeginn, Duales Studium ein/aus,
  Zeitarbeit und private Arbeitsvermittlung ausblenden.
- **Stellenliste mit Blättern** (`/list`): 5 Angebote pro Seite, Navigation über Buttons.
  Über der Liste lassen sich gespeicherte Städte auswählen („nur in der Stadt“ oder „im
  Umkreis“), dazu Sortierung nach Eignung, Entfernung oder Aktualität.
- **Übersicht nach Orten**: Anzahl passender Stellen je gespeicherter Stadt und die Orte mit
  den meisten Angeboten, jeweils per Button aufrufbar.
- **Benachrichtigungen**: Neue passende Stellen kommen gesammelt in einer Nachricht statt
  als einzelne Meldungen.
- **Bewertung nach Anforderungen**: Der Bot lädt die Stellenbeschreibung jeder passenden
  Stelle und wertet die Deutschanforderungen aus (siehe unten). Stellen über dem eigenen
  Niveau werden ausgeblendet, die übrigen nach Eignung sortiert.
- **Historie**: Jede Stelle wird mit erstem und letztem Sichtungsdatum gespeichert, daraus
  ergeben sich Statistiken wie die durchschnittliche Laufzeit von Angeboten (`/stats`).

## Erkennung der Deutschanforderungen

Die Beschreibungen sind Freitext. `matching.py` sucht darin nach Sprachniveaus und typischen
Formulierungen und ordnet sie der richtigen Sprache zu (ein „B2“ nach „Englisch“ zählt nicht
als Deutschanforderung):

| Formulierung in der Anzeige | Ergebnis bei eigenem Niveau B2 |
|---|---|
| „Deutschkenntnisse auf B2-Niveau“, „mindestens B2, idealerweise C1“ | passt ✅ |
| „Deutsch C1 erforderlich“, „verhandlungssicher“ | ausgeblendet |
| „Deutsch als Muttersprache“ | ausgeblendet (C2) |
| „sehr gute / fließende Deutschkenntnisse“ | angezeigt mit Hinweis ⚠️, im strengen Modus ausgeblendet |
| „gute Deutschkenntnisse“ | passt ✅ |
| keine Angabe | angezeigt als „nicht angegeben“ |

Hinweise wie „Sprachkurs“, „Deutschkurs“ oder „internationale Bewerber“ werden erkannt und
verbessern die Sortierung.

## Zugriffsschutz

Der Bot ist nicht öffentlich: Nutzen dürfen ihn nur Personen auf einer Whitelist
(Telegram-ID).

- Der Inhaber wird über die Variable `OWNER_ID` festgelegt. Ist sie nicht gesetzt, wird der
  einzige bestehende Abonnent Inhaber, ansonsten erscheint beim Start ein einmaliger Code im
  Log, der mit `/claim <Code>` eingelöst wird.
- Weitere Personen lädt der Inhaber per Einmal-Link ein (`/invite`, 24 Stunden gültig) oder
  fügt sie per ID hinzu (`/allow <ID>`).
- Unbekannte Nutzer erhalten keinen Zugriff; der Inhaber bekommt eine Anfrage mit den Buttons
  „Zulassen / Ablehnen“.
- Der Bot reagiert nur in privaten Chats.

## Befehle

| Befehl | Funktion |
|---|---|
| `/start` | Abonnieren |
| `/list` | Passende Stellen anzeigen |
| `/settings` | Suchfilter einstellen |
| `/check` | Sofort nach neuen Stellen suchen |
| `/stats` | Statistik: aktive Stellen, wegen Deutschanforderungen ausgeblendete, Top-Orte und -Arbeitgeber |
| `/stop` | Abo beenden |
| `/users`, `/invite`, `/allow`, `/deny` | Zugriffsverwaltung (nur Inhaber) |

## Architektur

| Datei | Inhalt |
|---|---|
| `bot.py` | Telegram-Oberfläche, Zugriffsschutz, Zeitplanung, Benachrichtigungen |
| `api.py` | Anfragen an die Jobsuche-API (Suche, Stellendetails) und Geokodierung von Städten |
| `matching.py` | Filter, Auswertung der Deutschanforderungen, Bewertung und Sortierung |
| `db.py` | SQLite: Stellen, Nutzer mit ihren Einstellungen, Einladungen, Benachrichtigungen |
| `config.py` | Konfiguration über Umgebungsvariablen |
| `tests/` | Unit-Tests mit `pytest` |

Ablauf:

1. Alle `CHECK_INTERVAL_HOURS` Stunden ruft der Bot die Angebote (`angebotsart=4`: Ausbildung
   und Duales Studium) für alle Berufe der Nutzer deutschlandweit ab und speichert sie in
   SQLite. Geografische und sonstige Filter werden lokal angewendet.
2. Im Hintergrund lädt er die Stellenbeschreibungen in Paketen (`ENRICH_BATCH` alle
   `ENRICH_INTERVAL_MIN` Minuten), neue Stellen zuerst, und wertet die Anforderungen aus.
3. Neue Stellen, die zu den Filtern eines Nutzers passen, werden ihm als Liste geschickt.

Die API liefert je nach Endpunkt unterschiedliche Formate (v4 und v6). `api.py` probiert die
bekannten Endpunkte der Reihe nach durch, merkt sich den funktionierenden und normalisiert
beide Formate. Datenbankänderungen laufen über versionierte Migrationen beim Start, sodass
Updates die bestehenden Daten und Einstellungen erhalten.

## Technologien

- Python 3.13, `python-telegram-bot` 21 (asyncio, JobQueue), `httpx`
- SQLite
- Jobsuche-API der Bundesagentur für Arbeit (inoffiziell, dokumentiert von
  [bundesAPI/jobsuche-api](https://github.com/bundesAPI/jobsuche-api))
- OpenStreetMap Nominatim für die Koordinaten von Städten
- `pytest`
- Railway (Deployment aus GitHub, persistentes Volume für die Datenbank)

## Tests

Die Tests decken die fachlich kritischen Teile ab:

- **Erkennung der Deutschanforderungen**: parametrisierte Fälle mit echten Formulierungen aus
  Stellenanzeigen, darunter Grenzfälle wie „Englischkenntnisse (B2) und gute
  Deutschkenntnisse“, „B2B-Unternehmen in Deutschland“ oder „Deutsche Bahn“.
- **Filter**: Bundesländer und Städte mit Umkreis, Unterscheidung „nur in der Stadt“
  (z. B. Frankfurt am Main vs. Frankfurt (Oder)), Ausschlusswörter, Ausbildungsbeginn,
  Duales Studium, Zeitarbeit, Sortierung.
- **Zugriffsschutz**: Whitelist, Schutz des Inhabers vor dem Entfernen, Einmal-Einladungen
  inklusive Ablauf.

```
pip install -r requirements-dev.txt
pytest
```

## Lokale Installation

Voraussetzung: Python 3.10 oder neuer und ein Bot-Token von [@BotFather](https://t.me/BotFather).

```
pip install -r requirements.txt
cp .env.example .env   # BOT_TOKEN eintragen
python bot.py
```

Wichtige Umgebungsvariablen (vollständig in `.env.example`):

| Variable | Bedeutung |
|---|---|
| `BOT_TOKEN` | Token des Telegram-Bots |
| `OWNER_ID` | Telegram-ID des Inhabers (optional) |
| `SEARCH_TERMS` | Standardberufe für neue Nutzer, getrennt durch `;` |
| `CHECK_INTERVAL_HOURS` | Abstand zwischen den Abrufen in Stunden (Standard: 6) |
| `ENRICH_BATCH`, `ENRICH_INTERVAL_MIN` | Paketgröße und Intervall für Stellenbeschreibungen |

## Deployment auf Railway

- Railway baut den Dienst bei jedem Push auf `main` aus diesem Repository neu.
- Umgebungsvariablen werden im Dienst unter **Variables** gesetzt; die Suchfilter stellt jeder
  Nutzer im Bot selbst ein.
- Die Datenbank `jobs.db` liegt auf einem Volume unter `/data` und bleibt bei Redeploys
  erhalten.

## Hinweise

- Die Jobsuche-API ist nicht offiziell veröffentlicht; Endpunkte und Formate können sich
  ändern. Der Bot fragt sie in moderatem Tempo ab.
- Die Oberfläche des Bots ist derzeit auf Russisch.
