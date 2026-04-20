"""Gemini API integration.

Two distinct tasks:

1. analyze_battle_window(transcript, fleet_a, fleet_b, name_a, name_b)
   Reads a chronological transcript of RP posts (from both personas) and
   decides, per side:
     - Whether the fleet actually OPENED FIRE in this window (vs. still
       maneuvering, approaching, talking).
     - Which enemy ship classes are targeted (priority order).
     - A tactical modifier percentage.

2. write_damage_report_text(...)
   Produces the in-universe damage report narrative for the embed.
"""
from __future__ import annotations

import asyncio
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

# Retry policy for transient server errors (5xx, UNAVAILABLE, DEADLINE_EXCEEDED).
RETRY_ATTEMPTS = 4               # total tries including the first
RETRY_BACKOFF_SECONDS = (2, 5, 10)   # wait times between attempts 1→2, 2→3, 3→4

log = logging.getLogger("sw-rpg-bot.ai")


def _is_transient_error(exc: BaseException) -> bool:
    """Detect Gemini server-side overload / transient failure.
    Covers 5xx responses and RPC statuses like UNAVAILABLE / DEADLINE_EXCEEDED."""
    msg = f"{type(exc).__name__} {exc}".upper()
    markers = (
        "503", "UNAVAILABLE",
        "500", "INTERNAL",
        "504", "DEADLINE_EXCEEDED",
        "502", "BAD_GATEWAY",
        "429", "RESOURCE_EXHAUSTED",
    )
    return any(m in msg for m in markers)


async def _generate_with_retry(
    *,
    model: str,
    contents: str,
    config: types.GenerateContentConfig,
):
    """Call Gemini, retrying on transient server errors with backoff."""
    last_exc: BaseException | None = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return await client().aio.models.generate_content(
                model=model, contents=contents, config=config,
            )
        except Exception as e:
            last_exc = e
            if not _is_transient_error(e) or attempt == RETRY_ATTEMPTS - 1:
                raise
            delay = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
            log.warning(
                "Gemini transient error (attempt %d/%d): %s — retry in %ds",
                attempt + 1, RETRY_ATTEMPTS, type(e).__name__, delay,
            )
            await asyncio.sleep(delay)
    # Unreachable — the loop either returns or raises — but keeps type-checkers happy.
    assert last_exc is not None
    raise last_exc

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
class SideAnalysis:
    fired: bool
    targeted_ships: list[str]
    action_summary: str   # kurze, erzählerische Beschreibung dessen, was diese Seite tut


@dataclass
class WindowAnalysis:
    side_a: SideAnalysis
    side_b: SideAnalysis
    scene: str          # kurze szenische Beschreibung beider Flotten (1-2 Sätze)
    overall_note: str   # empty if combat resolved normally


def _fleet_summary(stacks: list[FleetStack]) -> str:
    lines = []
    for s in stacks:
        if s.is_alive:
            lines.append(f"- {s.ship.name} ({s.count}x, Klasse: {s.ship.category})")
    return "\n".join(lines) if lines else "(keine Schiffe)"


def _fallback(valid_a: list[str], valid_b: list[str], note: str) -> WindowAnalysis:
    return WindowAnalysis(
        side_a=SideAnalysis(False, [], ""),
        side_b=SideAnalysis(False, [], ""),
        scene="",
        overall_note=note,
    )


async def analyze_battle_window(
    context_transcript: list[tuple[str, str]],   # already-resolved RP, narrative context only
    new_transcript: list[tuple[str, str]],       # new RP since last resolve, to decide on
    fleet_a: list[FleetStack],
    fleet_b: list[FleetStack],
    name_a: str,
    name_b: str,
) -> WindowAnalysis:
    valid_a = [s.ship.name for s in fleet_a if s.is_alive]
    valid_b = [s.ship.name for s in fleet_b if s.is_alive]

    def _fmt(tr: list[tuple[str, str]]) -> str:
        return "\n\n".join(f"[{persona}]: {text.strip()}" for persona, text in tr) or "(kein Beitrag)"

    context_text = _fmt(context_transcript)
    new_text = _fmt(new_transcript)

    system = (
        "Du bist ein Taktik- und Erzähl-Analyst für ein Star Wars-Weltraumkampf-RPG auf Deutsch. "
        "Du bekommst zwei Transkript-Blöcke zweier Parteien "
        f"(Seite A = '{name_a}', Seite B = '{name_b}') sowie beide Flotten. "
        "Der VORHERIGE KAMPFVERLAUF ist nur Hintergrund für narrative Kohärenz – er wurde bereits "
        "abgerechnet und darf NICHT erneut Schaden verursachen. "
        "Die NEUEN RP-POSTS sind das aktuelle Fenster: entscheide pro Seite, ob dort tatsächlich "
        "geschossen/angegriffen wird, und falls ja: welche Ziele. "
        "Taktik-Modifier gibt es NICHT – es zählt allein der Roh-Schaden der feuernden Schiffe. "
        "Wichtig: Die wörtliche Formulierung 'eröffnet das Feuer' ist NIEMALS erforderlich. "
        "JEDE konkrete Beschreibung von Waffeneinsatz, Beschuss, Torpedos/Raketen/Bomben im Anflug, "
        "Treffern, Einschlägen, brechenden Schilden oder detonierenden Rümpfen gilt als aktives Feuer. "
        "Im deutschen Space-RP werden Angriffe oft im Konjunktiv oder mit Modalverben geschrieben "
        "('würde feuern', 'soll beschießen', 'wird nutzen', 'konzentriert sich auf', 'greift an'); "
        "solche Formulierungen ZÄHLEN als aktiver Waffeneinsatz, sobald konkrete Waffensysteme "
        "konkreten Zielschiffen zugeordnet sind. Lies zwischen den Zeilen — wenn der Post beschreibt, "
        "wie/wogegen die Flotte kämpft, dann kämpft sie. "
        "Liefere außerdem eine kurze, erzählerische Szenenbeschreibung beider Flotten in 1-2 Sätzen, "
        "und pro Seite einen kurzen Aktionssatz, was sie in diesem Fenster tut. "
        "Antworte IMMER mit einem einzelnen JSON-Objekt ohne zusätzlichen Text."
    )

    user = f"""VORHERIGER KAMPFVERLAUF (nur Kontext, bereits abgerechnet):
\"\"\"
{context_text}
\"\"\"

NEUE RP-POSTS (seit letzter Schadensabrechnung — diese bewerten):
\"\"\"
{new_text}
\"\"\"

FLOTTE SEITE A — {name_a}:
{_fleet_summary(fleet_a)}

FLOTTE SEITE B — {name_b}:
{_fleet_summary(fleet_b)}

Antwortschema:
{{
  "scene": "1-2 kurze, erzählerische deutsche Sätze, was beide Flotten in diesem Moment gerade tun (bildlich, ohne Wertung)",
  "side_a": {{
    "fired": <true|false>,
    "targeted_ships": ["exakter Schiffsklassen-Name aus Flotte B", ...],
    "action_summary": "ein kurzer deutscher Satz: was tut {name_a} in diesem Fenster?"
  }},
  "side_b": {{
    "fired": <true|false>,
    "targeted_ships": ["exakter Schiffsklassen-Name aus Flotte A", ...],
    "action_summary": "ein kurzer deutscher Satz: was tut {name_b} in diesem Fenster?"
  }},
  "overall_note": "leer, ODER kurzer deutscher Hinweis falls z.B. gar kein Feuergefecht stattfand"
}}

Regeln:
- Deine Entscheidung bezieht sich AUSSCHLIESSLICH auf die NEUEN RP-POSTS. Der vorherige Kampfverlauf dient nur dazu, Kontext/Beziehungen/narrative Linien zu verstehen.
- "fired" = true, sobald im Text beschrieben wird, dass Waffen eingesetzt werden — z.B. Turbolaser feuern, Torpedos abgeschossen, Geschütze beharken Ziele, Jäger geben Salven ab, Treffer schlagen ein, Schilde brechen unter Beschuss, Trümmer fliegen.
- Konjunktiv-/Modalformen wie 'würde feuern', 'soll beschießen', 'wird angreifen', 'konzentriert sich auf Schiff X', 'Alle Waffen würden nutzen' ZÄHLEN als aktiver Waffeneinsatz, sobald konkrete Waffen/Schiffe konkreten Zielen zugeordnet sind. Das ist die in deutschem Space-RP übliche Form für laufende Angriffe.
- Auch Zielzuweisungen wie 'Dreadnought A konzentriert Feuer auf Venator B' oder 'Munificents nutzen Langstreckenwaffen gegen das Flaggschiff' sind Feuer, selbst ohne wörtliches 'schießen/feuern'.
- "fired" = false nur, wenn die neuen Posts ausschließlich Anflug, Ausweichmanöver, Rückzug/Abflug, Formationswechsel, Funkverkehr, Drohungen ohne Waffeneinsatz, reine Schildhochfahr-Beschreibungen oder Aufklärungsmanöver ohne Waffeneinsatz enthalten.
- Reine Zielvorgaben/Zielformulierungen ('Ziel: Zerstörung des Führungsschiffs') ohne konkrete Waffenzuordnung sind alleine KEIN Feuer.
- Wenn eine Seite im aktuellen Fenster nachweislich flieht / zurückzieht / abbricht, ohne zu schießen, dann ist "fired" = false für diese Seite.
- Wenn schon in einem früheren Fenster gefeuert wurde, aber im aktuellen Fenster nur manövriert wird, dann ist "fired" = false für dieses Fenster.
- Wenn "fired" = false ist, dürfen "targeted_ships" leer sein. action_summary beschreibt dann, was die Seite stattdessen tut (Anflug, Rückzug, Stellung halten ...).
- targeted_ships MÜSSEN exakt aus der gegnerischen Flottenliste stammen (Copy&Paste).
- Wenn gefeuert wird, aber kein konkretes Ziel genannt ist: wähle das plausibelste (Flaggschiff / größte Bedrohung).
- scene ist IMMER gefüllt — 1-2 knappe, bildliche deutsche Sätze im Präsens, die beide Flotten gleichzeitig zeigen (z.B. 'Die Venatoren der Republik brechen nach Backbord auseinander, während die Munificent-Fregatten der KUS ihre Langstreckenbatterien in Position bringen.'). Keine Wertung, kein Kitsch.
- overall_note ist "" wenn beide Seiten gefeuert haben. Falls keine Seite gefeuert hat, schreib dort eine kurze Feststellung wie 'Beide Flotten befinden sich noch im Anflug' oder 'Angreifer bricht ab und zieht sich zurück'.
"""

    t0 = time.perf_counter()
    try:
        response = await _generate_with_retry(
            model=ANALYSIS_MODEL,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                max_output_tokens=900,
            ),
        )
    except Exception:
        log.exception("analyze_battle_window API call failed")
        raise
    log.info("analyze_battle_window: %.2fs (model=%s)", time.perf_counter() - t0, ANALYSIS_MODEL)

    text = (response.text or "").strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("analyze_battle_window: JSON decode failed; raw=%r", text[:200])
        return _fallback(valid_a, valid_b, "Analyse-Fehler: KI lieferte ungültiges JSON.")

    def parse_side(block: dict, valid_targets: list[str]) -> SideAnalysis:
        fired = bool(block.get("fired", False))
        raw_targets = block.get("targeted_ships") or []
        targets = [t for t in raw_targets if isinstance(t, str) and t in valid_targets]
        if fired and not targets and valid_targets:
            targets = [valid_targets[0]]
        action = str(block.get("action_summary", ""))[:400]
        return SideAnalysis(fired=fired, targeted_ships=targets, action_summary=action)

    side_a = parse_side(data.get("side_a") or {}, valid_b)
    side_b = parse_side(data.get("side_b") or {}, valid_a)
    scene = str(data.get("scene", ""))[:600]
    note = str(data.get("overall_note", ""))[:400]

    return WindowAnalysis(side_a=side_a, side_b=side_b, scene=scene, overall_note=note)


def _fmt_damage_lines(stack_damages: list) -> str:
    lines = []
    for sd in stack_damages:
        parts = []
        if sd.shields_lost:
            parts.append(f"{sd.shields_lost} SBD Schildschaden")
        if sd.hull_lost:
            parts.append(f"{sd.hull_lost} RU Hüllenschaden")
        if sd.ships_destroyed:
            parts.append(f"{sd.ships_destroyed}× zerstört")
        lines.append(f"- {sd.ship_name}: {', '.join(parts) if parts else 'keine Wirkung'}")
    return "\n".join(lines) if lines else "- (keine nennenswerten Treffer)"


async def write_side_narrative(
    *,
    attacker_name: str,
    defender_name: str,
    action_summary: str,
    total_damage: int,
    stack_damages: list,
) -> str:
    """Erzählerischer 2-3-Satz-Block für EINE angreifende Seite. Keine Modifier mehr."""
    damage_lines = _fmt_damage_lines(stack_damages)

    user = f"""Schreibe 2-3 kurze deutsche Sätze, die beschreiben, wie die Flotte '{attacker_name}' ihren Angriff auf '{defender_name}' durchführt und was dabei passiert. Militärischer Lagemelder-Stil, kein Kitsch, keine Überschrift.

Aktion der Seite: {action_summary or '(keine Beschreibung — leite aus den Treffern ab)'}
Gesamtschaden: {total_damage}

Treffer:
{damage_lines}

Nur den Bericht, keine Einleitung."""

    t0 = time.perf_counter()
    response = await _generate_with_retry(
        model=NARRATIVE_MODEL,
        contents=user,
        config=types.GenerateContentConfig(max_output_tokens=300),
    )
    log.info("write_side_narrative: %.2fs", time.perf_counter() - t0)
    return (response.text or "").strip()
