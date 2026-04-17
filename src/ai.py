"""Claude API integration.

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
import os
from dataclasses import dataclass

from anthropic import AsyncAnthropic

from .battle import FleetStack

MODEL = "claude-sonnet-4-6"
MAX_MODIFIER = 25   # caps both bonus and penalty at +/- 25%


_client: AsyncAnthropic | None = None


def client() -> AsyncAnthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        _client = AsyncAnthropic(api_key=api_key)
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

    response = await client().messages.create(
        model=MODEL,
        max_tokens=600,
        system=system,
        messages=[{"role": "user", "content": user}],
    )

    text = response.content[0].text.strip()
    # Tolerate ```json fences if the model adds them
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Last-resort fallback: no targets => attack everything proportionally
        return RPAnalysis(targeted_ships=valid_targets, modifier_percent=0,
                          modifier_reason="Analyse fehlgeschlagen — kein Modifier")

    # Sanitize
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

    response = await client().messages.create(
        model=MODEL,
        max_tokens=400,
        messages=[{"role": "user", "content": user}],
    )
    return response.content[0].text.strip()
