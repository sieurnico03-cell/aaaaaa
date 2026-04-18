"""Gemini API integration.

Two distinct tasks:

1. analyze_rp_text(post, attacker_fleet, defender_fleet)
   Parses a player's roleplay post and extracts:
     - Which enemy ship classes are being targeted (priority order)
     - A tactical modifier percentage based on tactics described
       (flanking, ambush, poor positioning, etc.)

2. write_damage_report(attacker_report, defender_report, ...)
   Produces the in-universe damage report text for the Discord embed,
   given the raw numerical outcomes.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass

from google import genai
from google.genai import types

from .battle import FleetStack

ANALYSIS_MODEL = "gemini-2.5-flash"
NARRATIVE_MODEL = "gemini-2.5-flash"
REQUEST_TIMEOUT_MS = 30_000
MAX_MODIFIER = 25   # caps both bonus and penalty at +/- 25%

log = logging.getLogger("sw-rpg-bot.ai")

_client: genai.Client | None = None


def client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not set")
        _client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS),
        )
    return _client


@dataclass
class RPAnalysis:
    targeted_ships: list[str]   # exact ship class names, in priority order
    modifier_percent: int       # -25..+25
    modifier_reason: str        # short German sentence


def _fleet_summary(stacks: list[FleetStack]) -> str:
    lines = []
    for s in stacks:
        if s.is_alive:
            lines.append(f"- {s.ship.name} ({s.count}x, Klasse: {s.ship.category})")
    return "\n".join(lines) if lines else "(keine Schiffe)"


async def analyze_rp_text(
    rp_post: str,
    attacker_fleet: list[FleetStack],
    defender_fleet: list[FleetStack],
) -> RPAnalysis:
    """Extract targets and tactical modifier from a player's RP post."""

    valid_targets = [s.ship.name for s in defender_fleet if s.is_alive]

    system = (
        "Du bist ein Taktikanalyst für ein Star Wars-Weltraumkampf-RPG. "
        "Du bekommst einen Roleplay-Text eines Spielers und musst daraus ableiten, "
        "auf WELCHE gegnerischen Schiffsklassen gezielt wird und ob die beschriebene "
        "Taktik einen Bonus oder Malus rechtfertigt. Antworte IMMER mit einem einzelnen "
        "JSON-Objekt ohne zusätzlichen Text."
    )

    user = f"""ROLEPLAY-TEXT DES ANGREIFERS:
\"\"\"
{rp_post}
\"\"\"

EIGENE FLOTTE (Angreifer):
{_fleet_summary(attacker_fleet)}

GEGNERFLOTTE (mögliche Ziele):
{_fleet_summary(defender_fleet)}

Gib ein JSON zurück mit diesem Schema:
{{
  "targeted_ships": ["exakter Schiffsklassen-Name", ...],
  "modifier_percent": <ganze Zahl zwischen -{MAX_MODIFIER} und +{MAX_MODIFIER}>,
  "modifier_reason": "ein kurzer deutscher Satz"
}}

Regeln:
- "targeted_ships" MÜSSEN exakt aus der Gegnerflotte stammen (buchstabengetreu, Copy&Paste).
- Wenn der Text kein konkretes Ziel nennt, wähle das plausibelste Ziel (Flaggschiff / größte Bedrohung).
- Prioritätenreihenfolge: Hauptziel zuerst.
- Modifier-Bonus (+) für: Flankenangriff, Ambush, Ausnutzen von Schwachpunkten, gute Formation, überraschendes Manöver.
- Modifier-Malus (-) für: frontaler Ansturm auf schwere Kapitalschiffe, schlechte Positionierung, vorhersehbare Taktik, Rückzug ohne Deckung.
- Keine Werte. Nur das JSON."""

    t0 = time.perf_counter()
    response = await client().aio.models.generate_content(
        model=ANALYSIS_MODEL,
        contents=user,
        config=types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            max_output_tokens=600,
        ),
    )
    log.info("analyze_rp_text: %.2fs (model=%s)", time.perf_counter() - t0, ANALYSIS_MODEL)

    text = (response.text or "").strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return RPAnalysis(targeted_ships=valid_targets, modifier_percent=0,
                          modifier_reason="Analyse fehlgeschlagen — kein Modifier")

    raw_targets = data.get("targeted_ships") or []
    targets = [t for t in raw_targets if isinstance(t, str) and t in valid_targets]
    if not targets and valid_targets:
        targets = [valid_targets[0]]

    mod = int(data.get("modifier_percent", 0))
    mod = max(-MAX_MODIFIER, min(MAX_MODIFIER, mod))
    reason = str(data.get("modifier_reason", ""))[:280]

    return RPAnalysis(targeted_ships=targets, modifier_percent=mod, modifier_reason=reason)


async def write_damage_report_text(
    attacker_name: str,
    defender_name: str,
    rp_post_attacker: str,
    rp_post_defender: str,
    total_damage: int,
    modifier_percent: int,
    modifier_reason: str,
    stack_damages: list,
) -> str:
    """Compose a short in-universe damage report narrative (2-4 sentences)."""

    damage_lines = []
    for sd in stack_damages:
        parts = []
        if sd.shields_lost:
            parts.append(f"{sd.shields_lost} SBD Schildschaden")
        if sd.hull_lost:
            parts.append(f"{sd.hull_lost} RU Hüllenschaden")
        if sd.ships_destroyed:
            parts.append(f"{sd.ships_destroyed}× zerstört")
        damage_lines.append(f"- {sd.ship_name}: {', '.join(parts) if parts else 'keine Wirkung'}")

    user = f"""Du bist der KI-Kampfanalyst eines Star Wars-Weltraumkampf-RPGs. Schreibe einen kurzen, atmosphärischen Schadensbericht (3-4 Sätze, IC auf Deutsch) im Stil eines militärischen Lagemelders.

Angreifer: {attacker_name}
Verteidiger: {defender_name}
Taktik-Modifier: {modifier_percent:+d}% ({modifier_reason})
Effektiver Gesamtschaden: {total_damage}

Getroffene Ziele:
{chr(10).join(damage_lines)}

Schreibe nur den Bericht selbst, keine Einleitung, keine Überschriften. Nutze militärischen Funkverkehr-Stil."""

    t0 = time.perf_counter()
    response = await client().aio.models.generate_content(
        model=NARRATIVE_MODEL,
        contents=user,
        config=types.GenerateContentConfig(max_output_tokens=400),
    )
    log.info("write_damage_report_text: %.2fs (model=%s)", time.perf_counter() - t0, NARRATIVE_MODEL)
    return (response.text or "").strip()
