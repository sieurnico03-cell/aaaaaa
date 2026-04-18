# SW RPG Discord Bot

Bot für Star-Wars-RPG-Raumschlachten auf Discord. Spieler registrieren Flotten, beschreiben ihre Angriffe im Roleplay, und der Bot berechnet Schilde-, Hüllen- und Schiffsverluste auf Basis der offiziellen Flottentabelle. Taktische Modifier (Flanke, Ambush, Frontalansturm) werden von der Gemini-API aus den RP-Texten abgeleitet.

## Features

- **117 Schiffsklassen** (Republik + CIS) mit SBD, RU, Damage pro Schadensbericht, Jägerkapazität, Bewaffnung, Notizen.
- **Slash-Commands** für Flotten-Verwaltung, Kampfablauf, Schiffsinfos.
- **Persistenter Kampfstand** pro Discord-Kanal (SQLite).
- **Gemini-gestützte RP-Analyse:** extrahiert Ziele und Taktik-Modifier aus den Spieler-Posts.
- **Atmosphärische Schadensberichte** im IC-Stil (ebenfalls Gemini-generiert).

## Befehle

| Command | Wirkung |
|---|---|
| `/ship info <name>` | Detaillierte Daten zu einer Schiffsklasse |
| `/ship search <query>` | Schiffe suchen |
| `/fleet add <schiff> <anzahl>` | Schiffe zur eigenen Flotte hinzufügen |
| `/fleet remove <schiff> <anzahl>` | Schiffe entfernen |
| `/fleet show [user]` | Flotte anzeigen |
| `/fleet clear` | Flotte komplett leeren |
| `/battle start <gegner>` | Kampf im aktuellen Kanal starten |
| `/battle status` | Aktuellen Kampfstand |
| `/battle end` | Kampf beenden |
| `/round start` | Neue Runde — beide Spieler posten ihre RP-Nachricht |
| `/round resolve` | Schadensbericht berechnen und posten |

## Setup (lokal)

```bash
# Virtualenv anlegen
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS/Linux

# Dependencies
pip install -r requirements.txt

# Config
copy .env.example .env            # Windows
# cp .env.example .env            # macOS/Linux
# .env editieren: DISCORD_BOT_TOKEN + GEMINI_API_KEY

# Schiffsdaten neu bauen (nur wenn die CSVs aktualisiert wurden)
python scripts/build_ship_data.py

# Bot starten
python run.py
```

## Discord-Setup

1. https://discord.com/developers/applications → **New Application**
2. Tab **Bot** → **Add Bot** → Token kopieren → in `.env` als `DISCORD_BOT_TOKEN`
3. **Privileged Gateway Intents** aktivieren: `MESSAGE CONTENT INTENT` und `SERVER MEMBERS INTENT`
4. Tab **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Send Messages`, `Embed Links`, `Read Message History`
5. Generierten Link öffnen → Bot auf den Server einladen

## Deployment auf Fly.io (24/7 online)

```bash
# Einmalig: Fly CLI installieren (https://fly.io/docs/flyctl/install/)
fly auth login
fly apps create sw-rpg-bot
fly volumes create sw_rpg_data --region fra --size 1   # 1 GB reicht dicke

# Secrets setzen (werden verschlüsselt gespeichert, nie im Code)
fly secrets set DISCORD_BOT_TOKEN=xxxx
fly secrets set GEMINI_API_KEY=xxxx

# Deploy
fly deploy

# Logs beobachten
fly logs
```

Im Free-Tier (3 shared-cpu-1x mit 256 MB RAM) reicht eine einzelne Machine für diesen Bot locker.

## Schiffsdaten aktualisieren

1. Sheet bearbeiten
2. Republik + CIS als CSV runterladen → nach `data/republic.csv` / `data/cis.csv`
3. `python scripts/build_ship_data.py`
4. Lokal testen, dann `fly deploy`

## Kampfablauf (Beispiel)

```
Spieler A: /fleet add Venator-class Star Destroyer 3
Spieler A: /fleet add ARC-170 Starfighter 36
Spieler B: /fleet add Munificent-class Star Frigate 4
Spieler B: /fleet add Vulture Droid Starfighter 200

Spieler A: /battle start @SpielerB
Spieler A: /round start

[Spieler A postet RP-Nachricht im Kanal]
> "Die drei Venatoren stoßen in Keilformation vor und konzentrieren das
>  Feuer auf die beiden führenden Munificents, während die ARC-170 Staffel
>  die Vulture-Schwärme bindet."

[Spieler B postet RP-Nachricht]
> "Die Munificents drehen auf Breitseitenposition und eröffnen mit allen
>  Heavy Prow-Waffen. Die Vulture Droids schwärmen in Discord-Missile-
>  Attacke auf die ARC-170."

Spieler A: /round resolve
→ Bot berechnet Schaden beider Seiten + Taktik-Modifier
→ Postet zwei Schadensbericht-Embeds
```

## Projektstruktur

```
sw-rpg-bot/
├── data/
│   ├── ships.json          # geparste Schiffsdatenbank
│   ├── republic.csv        # Rohdaten
│   ├── cis.csv             # Rohdaten
│   └── battle.db           # Laufzeit-DB (wird automatisch erstellt)
├── scripts/
│   └── build_ship_data.py  # CSV → ships.json
├── src/
│   ├── ai.py               # Gemini-API-Calls
│   ├── battle.py           # Persistenter Kampfstand (SQLite)
│   ├── bot.py              # Discord-Bot + Slash-Commands
│   ├── damage.py           # Schadensberechnung
│   └── ships.py            # Schiffsdatenbank-Lookup
├── .env.example
├── Dockerfile
├── fly.toml
├── requirements.txt
└── run.py                  # Entrypoint
```
