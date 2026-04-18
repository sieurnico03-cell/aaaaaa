"""Star Wars RPG Discord Bot — main entrypoint.

Fleets are identified by NAME (Tupperbox persona display name or any label
chosen by an admin) and belong to a guild. Admins create and manage fleets
and battles; regular users play through their Tupperbox personas by writing
messages normally in the channel.

Slash commands:
  /ship info <name>
  /ship search <query>
  /fleet add <fleet_name> <ship> <count>          (admin)
  /fleet remove <fleet_name> <ship> <count>       (admin)
  /fleet clear <fleet_name>                       (admin)
  /fleet show <fleet_name>
  /fleet list
  /battle start <fleet_a> <fleet_b>               (admin)
  /battle status
  /battle resolve                                 (admin)
  /battle cursor <message_link>                   (admin)
  /battle end                                     (admin)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from . import ai, battle, damage
from .ships import SHIPS

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("sw-rpg-bot")

FACTION_COLOR = {
    "Republic": discord.Color.from_rgb(200, 30, 30),
    "CIS":      discord.Color.from_rgb(30, 80, 200),
}
EMBED_COLOR_DEFAULT = discord.Color.from_rgb(255, 204, 0)

MAX_TRANSCRIPT_MESSAGES = 80   # safety cap for history scan


intents = discord.Intents.default()
intents.message_content = True
intents.members = True


class SWRPGBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=intents, help_command=None)

    async def setup_hook(self) -> None:
        await battle.init_db()
        await self.tree.sync()
        log.info("Slash commands synced.")


bot = SWRPGBot()


# --- Helpers ----------------------------------------------------------------

def _is_admin(inter: discord.Interaction) -> bool:
    perms = getattr(inter.user, "guild_permissions", None)
    return bool(perms and perms.administrator)


def _fmt_int(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _parse_message_link(link: str) -> int | None:
    """Extract the message id from a Discord message link.
    Returns None if the link can't be parsed."""
    try:
        parts = link.strip().rstrip("/").split("/")
        return int(parts[-1])
    except (ValueError, IndexError):
        return None


async def _ship_name_autocomplete(_inter: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    matches = SHIPS.find(current, limit=25)
    return [app_commands.Choice(name=f"[{s.faction}] {s.name}"[:100], value=s.name) for s in matches]


async def _fleet_name_autocomplete(inter: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    if inter.guild is None:
        return []
    names = await battle.list_fleet_names(inter.guild.id)
    current_lower = current.lower()
    return [
        app_commands.Choice(name=n[:100], value=n)
        for n in names
        if current_lower in n.lower()
    ][:25]


def _fleet_embed(fleet_name: str, stacks: list[battle.FleetStack]) -> discord.Embed:
    if not stacks:
        return discord.Embed(
            title=f"Flotte: {fleet_name}",
            description="_Keine Schiffe registriert. Admin kann mit `/fleet add` welche eintragen._",
            color=EMBED_COLOR_DEFAULT,
        )

    by_category: dict[str, list[battle.FleetStack]] = defaultdict(list)
    for s in stacks:
        by_category[s.ship.category].append(s)

    faction = stacks[0].ship.faction
    embed = discord.Embed(
        title=f"Flotte: {fleet_name}",
        color=FACTION_COLOR.get(faction, EMBED_COLOR_DEFAULT),
    )

    total_ships = sum(s.count for s in stacks)
    total_destroyed = sum(s.count_destroyed for s in stacks)
    embed.description = f"**{total_ships}** aktiv  ·  **{total_destroyed}** verloren"

    for category, entries in by_category.items():
        lines = []
        for s in entries:
            status = ""
            if s.shields_current < s.ship.shields or s.hull_current < s.ship.hull:
                status = (
                    f"  _(Lead: {_fmt_int(s.shields_current)}/{_fmt_int(s.ship.shields)} SBD, "
                    f"{_fmt_int(s.hull_current)}/{_fmt_int(s.ship.hull)} RU)_"
                )
            destroyed = f" ~~-{s.count_destroyed}~~" if s.count_destroyed else ""
            lines.append(f"• **{s.count}×** {s.ship.name}{destroyed}{status}")
        embed.add_field(name=category, value="\n".join(lines)[:1024], inline=False)

    return embed


def _status_bar(current: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return "—"
    filled = int(round(width * current / total))
    return "█" * filled + "░" * (width - filled)


# --- /ship ------------------------------------------------------------------

ship_group = app_commands.Group(name="ship", description="Schiffsdatenbank abfragen")


@ship_group.command(name="info", description="Detaillierte Info zu einer Schiffsklasse")
@app_commands.describe(name="Name der Schiffsklasse")
@app_commands.autocomplete(name=_ship_name_autocomplete)
async def ship_info(inter: discord.Interaction, name: str) -> None:
    ship = SHIPS.get(name)
    if ship is None:
        matches = SHIPS.find(name, limit=5)
        if not matches:
            await inter.response.send_message(f"Keine Schiffsklasse gefunden für: `{name}`", ephemeral=True)
            return
        await inter.response.send_message(
            "Mehrdeutige Eingabe. Meintest du:\n" + "\n".join(f"• {m.name}" for m in matches),
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title=ship.name,
        description=f"_{ship.faction} · {ship.category}_",
        color=FACTION_COLOR.get(ship.faction, EMBED_COLOR_DEFAULT),
    )
    embed.add_field(name="Schilde (SBD)", value=f"**{_fmt_int(ship.shields)}**", inline=True)
    embed.add_field(name="Hülle (RU)", value=f"**{_fmt_int(ship.hull)}**", inline=True)
    embed.add_field(name="Damage/Runde", value=f"**{ship.damage_per_report:g}**", inline=True)
    if ship.speed_mglt:
        embed.add_field(name="Speed", value=f"{ship.speed_mglt} MGLT", inline=True)
    if ship.maneuverability_dpf:
        embed.add_field(name="Wendigkeit", value=f"{ship.maneuverability_dpf} DPF", inline=True)
    if ship.fighter_capacity:
        embed.add_field(name="Jägerkapazität", value=f"{ship.fighter_capacity}", inline=True)
    embed.add_field(name="Optimaler Feuerwinkel", value=ship.optimal_angle or "—", inline=False)
    if ship.primary_weapons:
        embed.add_field(name="Hauptbewaffnung", value=ship.primary_weapons[:1024], inline=False)
    if ship.secondary_weapons:
        embed.add_field(name="Sekundärbewaffnung", value=ship.secondary_weapons[:1024], inline=False)
    if ship.notes:
        embed.add_field(name="Notizen", value=ship.notes[:1024], inline=False)
    await inter.response.send_message(embed=embed)


@ship_group.command(name="search", description="Schiffe suchen")
@app_commands.describe(query="Suchbegriff (Teil des Namens)")
async def ship_search(inter: discord.Interaction, query: str) -> None:
    matches = SHIPS.find(query, limit=15)
    if not matches:
        await inter.response.send_message(f"Keine Treffer für `{query}`.", ephemeral=True)
        return
    lines = [f"**[{m.faction}]** {m.name}  _(SBD {_fmt_int(m.shields)}, RU {_fmt_int(m.hull)}, DMG {m.damage_per_report:g})_" for m in matches]
    embed = discord.Embed(
        title=f"Suchergebnisse: {query}",
        description="\n".join(lines),
        color=EMBED_COLOR_DEFAULT,
    )
    await inter.response.send_message(embed=embed)


bot.tree.add_command(ship_group)


# --- /fleet -----------------------------------------------------------------

fleet_group = app_commands.Group(name="fleet", description="Flotten verwalten (Admin-only außer show/list)")


@fleet_group.command(name="add", description="Schiffe zu einer benannten Flotte hinzufügen (Admin)")
@app_commands.describe(
    fleet_name="Name der Flotte (typischerweise der Tupperbox-Persona-Name)",
    ship="Schiffsklasse",
    count="Anzahl",
)
@app_commands.autocomplete(ship=_ship_name_autocomplete, fleet_name=_fleet_name_autocomplete)
@app_commands.default_permissions(administrator=True)
async def fleet_add(inter: discord.Interaction, fleet_name: str, ship: str, count: int) -> None:
    if inter.guild is None:
        await inter.response.send_message("Nur in Servern nutzbar.", ephemeral=True)
        return
    if not _is_admin(inter):
        await inter.response.send_message("Nur Admins dürfen Flotten verwalten.", ephemeral=True)
        return
    if count <= 0:
        await inter.response.send_message("Anzahl muss positiv sein.", ephemeral=True)
        return
    try:
        stack = await battle.add_ships(inter.guild.id, fleet_name, ship, count)
    except ValueError as e:
        await inter.response.send_message(f"Fehler: {e}", ephemeral=True)
        return
    await inter.response.send_message(
        f"✓ {count}× **{stack.ship.name}** zur Flotte **{fleet_name}** hinzugefügt "
        f"(Stack jetzt {stack.count}).",
        ephemeral=True,
    )


@fleet_group.command(name="remove", description="Schiffe aus einer Flotte entfernen (Admin)")
@app_commands.describe(fleet_name="Name der Flotte", ship="Schiffsklasse", count="Anzahl")
@app_commands.autocomplete(ship=_ship_name_autocomplete, fleet_name=_fleet_name_autocomplete)
@app_commands.default_permissions(administrator=True)
async def fleet_remove(inter: discord.Interaction, fleet_name: str, ship: str, count: int) -> None:
    if inter.guild is None:
        await inter.response.send_message("Nur in Servern nutzbar.", ephemeral=True)
        return
    if not _is_admin(inter):
        await inter.response.send_message("Nur Admins dürfen Flotten verwalten.", ephemeral=True)
        return
    if count <= 0:
        await inter.response.send_message("Anzahl muss positiv sein.", ephemeral=True)
        return
    try:
        removed = await battle.remove_ships(inter.guild.id, fleet_name, ship, count)
    except ValueError as e:
        await inter.response.send_message(f"Fehler: {e}", ephemeral=True)
        return
    if removed == 0:
        await inter.response.send_message(
            f"Dieses Schiff ist nicht in der Flotte **{fleet_name}**.", ephemeral=True,
        )
    else:
        await inter.response.send_message(
            f"✓ {removed}× aus **{fleet_name}** entfernt.", ephemeral=True,
        )


@fleet_group.command(name="clear", description="Flotte komplett leeren (Admin)")
@app_commands.describe(fleet_name="Name der Flotte")
@app_commands.autocomplete(fleet_name=_fleet_name_autocomplete)
@app_commands.default_permissions(administrator=True)
async def fleet_clear(inter: discord.Interaction, fleet_name: str) -> None:
    if inter.guild is None:
        await inter.response.send_message("Nur in Servern nutzbar.", ephemeral=True)
        return
    if not _is_admin(inter):
        await inter.response.send_message("Nur Admins dürfen Flotten verwalten.", ephemeral=True)
        return
    removed = await battle.clear_fleet(inter.guild.id, fleet_name)
    await inter.response.send_message(
        f"✓ Flotte **{fleet_name}** gelöscht ({removed} Einträge).", ephemeral=True,
    )


@fleet_group.command(name="show", description="Flotte anzeigen")
@app_commands.describe(fleet_name="Name der Flotte")
@app_commands.autocomplete(fleet_name=_fleet_name_autocomplete)
async def fleet_show(inter: discord.Interaction, fleet_name: str) -> None:
    if inter.guild is None:
        await inter.response.send_message("Nur in Servern nutzbar.", ephemeral=True)
        return
    stacks = await battle.get_fleet(inter.guild.id, fleet_name)
    embed = _fleet_embed(fleet_name, stacks)
    await inter.response.send_message(embed=embed)


@fleet_group.command(name="list", description="Alle registrierten Flotten auf diesem Server anzeigen")
async def fleet_list(inter: discord.Interaction) -> None:
    if inter.guild is None:
        await inter.response.send_message("Nur in Servern nutzbar.", ephemeral=True)
        return
    names = await battle.list_fleet_names(inter.guild.id)
    if not names:
        await inter.response.send_message(
            "Keine Flotten registriert. Ein Admin kann welche mit `/fleet add` anlegen.",
            ephemeral=True,
        )
        return
    embed = discord.Embed(
        title="Registrierte Flotten",
        description="\n".join(f"• **{n}**" for n in names),
        color=EMBED_COLOR_DEFAULT,
    )
    await inter.response.send_message(embed=embed)


bot.tree.add_command(fleet_group)


# --- /battle ----------------------------------------------------------------

battle_group = app_commands.Group(name="battle", description="Raumschlacht verwalten (Admin)")


@battle_group.command(name="start", description="Kampf zwischen zwei Flotten in diesem Kanal starten (Admin)")
@app_commands.describe(
    fleet_a="Name der ersten Flotte",
    fleet_b="Name der zweiten Flotte",
    anchor_message_link=(
        "Optional: Link zu der Nachricht, ab der der Kampf beginnt. "
        "Ohne Angabe beginnt der Kampf ab der /battle start-Ankündigung."
    ),
)
@app_commands.autocomplete(fleet_a=_fleet_name_autocomplete, fleet_b=_fleet_name_autocomplete)
@app_commands.default_permissions(administrator=True)
async def battle_start(
    inter: discord.Interaction,
    fleet_a: str,
    fleet_b: str,
    anchor_message_link: str | None = None,
) -> None:
    if inter.guild is None:
        await inter.response.send_message("Nur in Servern nutzbar.", ephemeral=True)
        return
    if not _is_admin(inter):
        await inter.response.send_message("Nur Admins dürfen Kämpfe steuern.", ephemeral=True)
        return
    if fleet_a == fleet_b:
        await inter.response.send_message("Beide Flotten müssen unterschiedlich sein.", ephemeral=True)
        return

    existing = await battle.get_battle(inter.channel_id)
    if existing is not None:
        await inter.response.send_message(
            "In diesem Kanal läuft bereits ein Kampf. Beende ihn erst mit `/battle end`.",
            ephemeral=True,
        )
        return

    stacks_a = await battle.get_fleet(inter.guild.id, fleet_a)
    stacks_b = await battle.get_fleet(inter.guild.id, fleet_b)
    if not stacks_a:
        await inter.response.send_message(f"Flotte **{fleet_a}** hat keine Schiffe.", ephemeral=True)
        return
    if not stacks_b:
        await inter.response.send_message(f"Flotte **{fleet_b}** hat keine Schiffe.", ephemeral=True)
        return

    retro_anchor_id: int | None = None
    if anchor_message_link:
        retro_anchor_id = _parse_message_link(anchor_message_link)
        if retro_anchor_id is None:
            await inter.response.send_message(
                "Konnte die Nachrichten-ID aus dem Link nicht lesen. "
                "Rechtsklick auf eine Nachricht → 'Nachrichtenlink kopieren'.",
                ephemeral=True,
            )
            return

    if retro_anchor_id is not None:
        anchor_hint = (
            f"Anker gesetzt auf Nachricht `{retro_anchor_id}` — alle RP-Posts ab dort "
            "werden in die Schlacht einbezogen."
        )
    else:
        anchor_hint = (
            "Anker = diese Ankündigung. Alle nachfolgenden RP-Posts zählen zur Schlacht. "
            "(Rückwirkend kann der Anker mit `/battle cursor <link>` gesetzt werden.)"
        )

    embed = discord.Embed(
        title="Kampf initiiert",
        description=(
            f"**{fleet_a}** gegen **{fleet_b}**\n\n"
            "Die Spieler schreiben ihre RP-Nachrichten mit ihren Tupperbox-Personas "
            "(Anzeigename muss exakt einem der Flottennamen entsprechen).\n\n"
            "Ein Admin ruft `/battle resolve` auf, sobald ein Schadensbericht fällig ist.\n"
            "Die KI erkennt von selbst, ob schon Feuer eröffnet wurde — solange nur "
            "manövriert oder angeflogen wird, gibt es keinen Schaden.\n\n"
            f"_{anchor_hint}_"
        ),
        color=EMBED_COLOR_DEFAULT,
    )
    await inter.response.send_message(embed=embed)

    sent = await inter.original_response()
    anchor_id = retro_anchor_id if retro_anchor_id is not None else sent.id
    await battle.create_battle(
        channel_id=inter.channel_id,
        guild_id=inter.guild.id,
        fleet_a_name=fleet_a,
        fleet_b_name=fleet_b,
        anchor_message_id=anchor_id,
    )


@battle_group.command(name="status", description="Aktuellen Kampfstand in diesem Kanal anzeigen")
async def battle_status(inter: discord.Interaction) -> None:
    if inter.guild is None:
        await inter.response.send_message("Nur in Servern nutzbar.", ephemeral=True)
        return
    b = await battle.get_battle(inter.channel_id)
    if b is None:
        await inter.response.send_message("Kein laufender Kampf in diesem Kanal.", ephemeral=True)
        return

    stacks_a = await battle.get_fleet(b.guild_id, b.fleet_a_name, channel_id=b.channel_id)
    stacks_b = await battle.get_fleet(b.guild_id, b.fleet_b_name, channel_id=b.channel_id)

    embed = discord.Embed(
        title=f"Kampfstand — Runde {b.round_number}",
        description=f"**{b.fleet_a_name}** gegen **{b.fleet_b_name}**\nPhase: `{b.phase}`",
        color=EMBED_COLOR_DEFAULT,
    )
    for fleet_name, stacks in ((b.fleet_a_name, stacks_a), (b.fleet_b_name, stacks_b)):
        if not stacks:
            embed.add_field(name=fleet_name, value="_Flotte zerstört_", inline=False)
            continue
        lines = []
        for s in stacks:
            bar = _status_bar(s.shields_current + s.hull_current, s.ship.shields + s.ship.hull)
            lines.append(f"`{bar}` {s.count}× {s.ship.name}")
        embed.add_field(name=fleet_name, value="\n".join(lines)[:1024], inline=False)

    await inter.response.send_message(embed=embed)


@battle_group.command(name="cursor", description="Kampfstart-Anker manuell auf eine Nachricht setzen (Admin)")
@app_commands.describe(message_link="Discord-Nachrichtenlink (Rechtsklick → Nachrichtenlink kopieren)")
@app_commands.default_permissions(administrator=True)
async def battle_cursor(inter: discord.Interaction, message_link: str) -> None:
    if not _is_admin(inter):
        await inter.response.send_message("Nur Admins.", ephemeral=True)
        return
    b = await battle.get_battle(inter.channel_id)
    if b is None:
        await inter.response.send_message("Kein Kampf aktiv.", ephemeral=True)
        return
    msg_id = _parse_message_link(message_link)
    if msg_id is None:
        await inter.response.send_message(
            "Konnte die Nachrichten-ID aus dem Link nicht lesen. "
            "Rechtsklick auf eine Nachricht → 'Nachrichtenlink kopieren'.",
            ephemeral=True,
        )
        return
    # Setting the anchor also resets the resolve cursor — everything before
    # the new anchor is outside the battle scope anyway.
    await battle.set_anchor(inter.channel_id, msg_id)
    await inter.response.send_message(
        f"✓ Kampfstart-Anker gesetzt auf Nachricht `{msg_id}`. "
        "Alle RP-Posts ab dort werden beim nächsten `/battle resolve` analysiert.",
        ephemeral=True,
    )


@battle_group.command(name="end", description="Laufenden Kampf beenden (Admin)")
@app_commands.default_permissions(administrator=True)
async def battle_end(inter: discord.Interaction) -> None:
    if not _is_admin(inter):
        await inter.response.send_message("Nur Admins.", ephemeral=True)
        return
    b = await battle.get_battle(inter.channel_id)
    if b is None:
        await inter.response.send_message("Kein laufender Kampf in diesem Kanal.", ephemeral=True)
        return
    await battle.end_battle(inter.channel_id)
    await inter.response.send_message("Kampf beendet.")


# --- /battle resolve --------------------------------------------------------

async def _collect_transcript_split(
    channel: discord.abc.Messageable,
    anchor_id: int,
    resolve_cursor_id: int,
    fleet_a_name: str,
    fleet_b_name: str,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]], int | None]:
    """Fetch messages after *anchor_id* whose author display_name matches one
    of the two fleet names, and split them into:
      - context_transcript: messages with id <= resolve_cursor_id (already
        resolved — background for narrative continuity only)
      - new_transcript: messages with id > resolve_cursor_id (new, to decide on)
    Returns (context, new, last_message_id)."""
    context: list[tuple[str, str]] = []
    new: list[tuple[str, str]] = []
    last_id: int | None = None
    targets = {fleet_a_name.lower(), fleet_b_name.lower()}
    async for msg in channel.history(
        limit=MAX_TRANSCRIPT_MESSAGES,
        after=discord.Object(id=anchor_id),
        oldest_first=True,
    ):
        last_id = msg.id
        author_name = (msg.author.display_name or msg.author.name or "").strip()
        if author_name.lower() not in targets:
            continue
        text = (msg.content or "").strip()
        if not text:
            continue
        canonical = fleet_a_name if author_name.lower() == fleet_a_name.lower() else fleet_b_name
        entry = (canonical, text)
        if msg.id <= resolve_cursor_id:
            context.append(entry)
        else:
            new.append(entry)
    return context, new, last_id


def _damage_embed(
    attacker_name: str,
    defender_name: str,
    report: damage.DamageReport,
    narrative: str,
    defender_fleet_after: list[battle.FleetStack],
) -> discord.Embed:
    embed = discord.Embed(
        title=f"Schadensbericht — Angriff von {attacker_name} auf {defender_name}",
        description=narrative,
        color=EMBED_COLOR_DEFAULT,
    )
    mod = f"{report.modifier_percent:+d}%"
    embed.add_field(
        name="Effektiver Schaden",
        value=(
            f"Gesamtschaden: **{_fmt_int(report.total_damage)}**\n"
            f"Taktik-Modifier: {mod} — {report.modifier_reason or '—'}"
        ),
        inline=False,
    )

    if not report.stack_damages:
        embed.add_field(name="Ergebnis", value="Keine Treffer.", inline=False)
        return embed

    total_destroyed = 0
    total_shield_dmg = 0
    total_hull_dmg = 0
    for sd in report.stack_damages:
        total_destroyed += sd.ships_destroyed
        total_shield_dmg += sd.shields_lost
        total_hull_dmg += sd.hull_lost

        lines: list[str] = []
        if sd.shields_lost:
            lines.append(f"Schildschaden: {_fmt_int(sd.shields_lost)} SBD")
        if sd.hull_lost:
            lines.append(f"Hüllenschaden: {_fmt_int(sd.hull_lost)} RU")
        if sd.ships_destroyed:
            lines.append(f"Zerstörte Schiffe: {sd.ships_destroyed}")
        if not lines:
            lines.append("Kein nennenswerter Effekt.")
        if sd.remaining_count > 0:
            lines.append(
                f"Noch einsatzfähig: **{sd.remaining_count} Schiff"
                f"{'e' if sd.remaining_count != 1 else ''}** · "
                f"Lead-Schiff: {_fmt_int(sd.remaining_shields)} SBD / "
                f"{_fmt_int(sd.remaining_hull)} RU"
            )
        else:
            lines.append("Stack vollständig vernichtet.")

        embed.add_field(name=sd.ship_name, value="\n".join(lines), inline=False)

    if report.unassigned_damage > 0:
        embed.add_field(
            name="Nicht zugewiesener Schaden",
            value=(
                f"{_fmt_int(report.unassigned_damage)} Schadenspunkte verpufften — "
                "alle priorisierten Ziele waren bereits zerstört."
            ),
            inline=False,
        )

    fazit_lines: list[str] = []
    fazit_lines.append(
        f"{attacker_name} fügt {defender_name} insgesamt "
        f"{_fmt_int(total_shield_dmg)} SBD Schild- und "
        f"{_fmt_int(total_hull_dmg)} RU Hüllenschaden zu."
    )
    if total_destroyed:
        fazit_lines.append(
            f"Dabei werden **{total_destroyed} Schiff"
            f"{'e' if total_destroyed != 1 else ''}** vollständig vernichtet."
        )

    remaining_ships = sum(s.count for s in defender_fleet_after if s.is_alive)
    remaining_classes = sum(1 for s in defender_fleet_after if s.is_alive)
    if remaining_ships == 0:
        fazit_lines.append(f"{defender_name}s Flotte ist damit vollständig ausgelöscht.")
    else:
        fazit_lines.append(
            f"{defender_name}s verbleibende Streitmacht: **{remaining_ships} Schiff"
            f"{'e' if remaining_ships != 1 else ''}** in {remaining_classes} Klasse"
            f"{'n' if remaining_classes != 1 else ''}."
        )

    embed.add_field(name="Fazit", value="\n".join(fazit_lines), inline=False)
    return embed


async def _resolve_side(
    *,
    attacker_fleet: list[battle.FleetStack],
    defender_fleet: list[battle.FleetStack],
    analysis: ai.SideAnalysis,
) -> damage.DamageReport:
    base = damage.compute_outgoing_damage(attacker_fleet)
    total = damage.apply_modifier(base, analysis.modifier_percent)

    target_order: list[battle.FleetStack] = []
    name_to_stack = {s.ship.name: s for s in defender_fleet}
    for name in analysis.targeted_ships:
        stack = name_to_stack.get(name)
        if stack and stack.is_alive and stack not in target_order:
            target_order.append(stack)
    for stack in defender_fleet:
        if stack.is_alive and stack not in target_order:
            target_order.append(stack)

    stack_damages, unassigned = damage.distribute_damage(total, target_order)

    for stack in target_order:
        await battle.update_stack(stack)

    return damage.DamageReport(
        attacker_id=0, defender_id=0,
        total_damage=total,
        modifier_reason=analysis.modifier_reason,
        modifier_percent=analysis.modifier_percent,
        stack_damages=stack_damages,
        unassigned_damage=unassigned,
    )


@battle_group.command(name="resolve", description="Schadensbericht für den aktuellen Chat-Abschnitt berechnen (Admin)")
@app_commands.default_permissions(administrator=True)
async def battle_resolve(inter: discord.Interaction) -> None:
    if inter.guild is None:
        await inter.response.send_message("Nur in Servern nutzbar.", ephemeral=True)
        return
    if not _is_admin(inter):
        await inter.response.send_message("Nur Admins dürfen Kämpfe auflösen.", ephemeral=True)
        return
    b = await battle.get_battle(inter.channel_id)
    if b is None:
        await inter.response.send_message("Kein Kampf aktiv.", ephemeral=True)
        return

    await inter.response.defer(thinking=True)

    channel = inter.channel
    context_transcript, new_transcript, last_id = await _collect_transcript_split(
        channel, b.anchor_message_id, b.resolve_cursor_id,
        b.fleet_a_name, b.fleet_b_name,
    )

    if not new_transcript:
        await inter.followup.send(
            f"Seit der letzten Schadensabrechnung wurden keine neuen RP-Posts von "
            f"**{b.fleet_a_name}** oder **{b.fleet_b_name}** gefunden. "
            "(Tupperbox-Anzeigename muss exakt einem der Flottennamen entsprechen.)"
        )
        return

    fleet_a = await battle.get_fleet(b.guild_id, b.fleet_a_name, channel_id=b.channel_id)
    fleet_b = await battle.get_fleet(b.guild_id, b.fleet_b_name, channel_id=b.channel_id)
    if not fleet_a or not fleet_b:
        await inter.followup.send("Eine Flotte ist bereits vernichtet — Kampf ist effektiv vorbei.")
        return

    t_round = time.perf_counter()
    try:
        analysis = await ai.analyze_battle_window(
            context_transcript, new_transcript,
            fleet_a, fleet_b, b.fleet_a_name, b.fleet_b_name,
        )
    except Exception as e:
        log.exception("Analyse fehlgeschlagen")
        msg = f"{type(e).__name__}: {e}"[:500]
        await inter.followup.send(f"⚠ KI-Analyse fehlgeschlagen.\n```\n{msg}\n```")
        return
    log.info("analysis total: %.2fs", time.perf_counter() - t_round)

    # No firing on either side → no damage, but still advance the cursor past
    # these messages (they become narrative context for the next resolve).
    if not analysis.side_a.fired and not analysis.side_b.fired:
        note = analysis.overall_note or "Noch kein Feuergefecht erkennbar — keine Schadensabrechnung."
        embed = discord.Embed(
            title="Kein Schadensbericht",
            description=note,
            color=EMBED_COLOR_DEFAULT,
        )
        embed.add_field(
            name="Hinweis",
            value=(
                "Im aktuellen Fenster wurde noch nicht gefeuert. Diese Posts werden beim "
                "nächsten Resolve als Hintergrund behandelt — sobald eine Seite das Feuer "
                "eröffnet, ruf erneut `/battle resolve` auf."
            ),
            inline=False,
        )
        sent_note = await inter.followup.send(embed=embed, wait=True)
        new_cursor = max(last_id or 0, sent_note.id)
        if new_cursor > b.resolve_cursor_id:
            await battle.set_resolve_cursor(b.channel_id, new_cursor)
        return

    await battle.advance_round(b.channel_id)

    # Compute damage for any side that fired
    report_a: damage.DamageReport | None = None
    report_b: damage.DamageReport | None = None
    if analysis.side_a.fired:
        report_a = await _resolve_side(
            attacker_fleet=fleet_a, defender_fleet=fleet_b, analysis=analysis.side_a,
        )
    if analysis.side_b.fired:
        # Re-read fleet_a in case it was mutated (it wasn't — side_a fires onto B)
        report_b = await _resolve_side(
            attacker_fleet=fleet_b, defender_fleet=fleet_a, analysis=analysis.side_b,
        )

    # Narratives (only for sides that fired), in parallel
    narratives: dict[str, str] = {}
    coros = []
    keys = []
    if report_a is not None:
        keys.append("a")
        coros.append(ai.write_damage_report_text(
            attacker_name=b.fleet_a_name, defender_name=b.fleet_b_name,
            total_damage=report_a.total_damage,
            modifier_percent=report_a.modifier_percent,
            modifier_reason=report_a.modifier_reason,
            stack_damages=report_a.stack_damages,
        ))
    if report_b is not None:
        keys.append("b")
        coros.append(ai.write_damage_report_text(
            attacker_name=b.fleet_b_name, defender_name=b.fleet_a_name,
            total_damage=report_b.total_damage,
            modifier_percent=report_b.modifier_percent,
            modifier_reason=report_b.modifier_reason,
            stack_damages=report_b.stack_damages,
        ))
    if coros:
        try:
            results = await asyncio.gather(*coros)
            for k, v in zip(keys, results):
                narratives[k] = v
        except Exception:
            log.exception("Narrative-Phase fehlgeschlagen")
            for k in keys:
                narratives[k] = "(Funkverbindung zur KI unterbrochen — nur Rohdaten verfügbar.)"

    fleet_a_after = await battle.get_fleet(b.guild_id, b.fleet_a_name, channel_id=b.channel_id)
    fleet_b_after = await battle.get_fleet(b.guild_id, b.fleet_b_name, channel_id=b.channel_id)

    embeds: list[discord.Embed] = []
    if report_a is not None:
        embeds.append(_damage_embed(
            b.fleet_a_name, b.fleet_b_name, report_a, narratives["a"], fleet_b_after,
        ))
    if report_b is not None:
        embeds.append(_damage_embed(
            b.fleet_b_name, b.fleet_a_name, report_b, narratives["b"], fleet_a_after,
        ))

    if analysis.side_a.fired != analysis.side_b.fired:
        non_firing = b.fleet_b_name if not analysis.side_b.fired else b.fleet_a_name
        note_embed = discord.Embed(
            title=f"{non_firing} hat in diesem Fenster nicht gefeuert",
            description="Daher kein Rückangriff. Für die nächste Runde einfach weiterschreiben.",
            color=EMBED_COLOR_DEFAULT,
        )
        embeds.append(note_embed)

    sent = await inter.followup.send(embeds=embeds, wait=True)

    # Advance the resolve cursor (not the anchor — the anchor stays fixed as
    # the start-of-battle boundary, so earlier RP remains available as context).
    new_cursor = max(sent.id, last_id or 0)
    await battle.set_resolve_cursor(b.channel_id, new_cursor)

    # End-of-battle check
    alive_a = any(s.is_alive for s in fleet_a_after)
    alive_b = any(s.is_alive for s in fleet_b_after)
    if not alive_a or not alive_b:
        if alive_a and not alive_b:
            winner = b.fleet_a_name
        elif alive_b and not alive_a:
            winner = b.fleet_b_name
        else:
            winner = None
        end_embed = discord.Embed(
            title="Kampf beendet",
            description=f"Sieger: **{winner}**" if winner else "Beide Flotten vernichtet.",
            color=EMBED_COLOR_DEFAULT,
        )
        await inter.followup.send(embed=end_embed)
        await battle.end_battle(b.channel_id)


bot.tree.add_command(battle_group)


# --- /help ------------------------------------------------------------------

@bot.tree.command(name="help", description="Übersicht aller Bot-Befehle")
async def help_cmd(inter: discord.Interaction) -> None:
    embed = discord.Embed(title="SW RPG Bot — Befehle", color=EMBED_COLOR_DEFAULT)
    embed.add_field(
        name="Schiffsdatenbank (jeder)",
        value=(
            "`/ship info <name>` — Detaillierte Infos\n"
            "`/ship search <query>` — Schiffe suchen"
        ),
        inline=False,
    )
    embed.add_field(
        name="Flotten (Admin, außer show/list)",
        value=(
            "`/fleet add <fleet> <schiff> <anzahl>`\n"
            "`/fleet remove <fleet> <schiff> <anzahl>`\n"
            "`/fleet clear <fleet>`\n"
            "`/fleet show <fleet>` — jeder kann\n"
            "`/fleet list` — jeder kann"
        ),
        inline=False,
    )
    embed.add_field(
        name="Raumschlacht (Admin steuert, Spieler schreiben per Tupperbox)",
        value=(
            "`/battle start <fleet_a> <fleet_b> [anchor_link]`\n"
            "`/battle resolve` — aktuelle Runde auflösen\n"
            "`/battle cursor <message_link>` — Kampfstart rückwirkend setzen\n"
            "`/battle status`\n"
            "`/battle end`"
        ),
        inline=False,
    )
    embed.add_field(
        name="So läuft eine Runde",
        value=(
            "1. Admin legt zwei Flotten an (Name = Tupperbox-Persona-Anzeigename).\n"
            "2. Spieler schreiben RP mit ihren Tupperbox-Personas — jederzeit.\n"
            "3. Admin ruft `/battle start` auf. Optional mit Link auf die Nachricht, "
            "ab der der Kampf laufen soll (rückwirkend möglich).\n"
            "4. Wann immer ein Schadensbericht fällig ist → `/battle resolve`.\n"
            "5. Die KI erkennt selbst, ob schon gefeuert wurde (Anflug = kein Schaden) "
            "und behält den bisherigen Kampfverlauf als Kontext bei."
        ),
        inline=False,
    )
    await inter.response.send_message(embed=embed, ephemeral=True)


@bot.event
async def on_ready() -> None:
    log.info("Logged in as %s (id=%s)", bot.user, bot.user.id if bot.user else "?")
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="die Holonet-Feeds"),
    )


def main() -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
