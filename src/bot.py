"""Star Wars RPG Discord Bot — main entrypoint.

Slash commands:
  /ship info <name>
  /ship search <query>
  /fleet add <ship> <count>
  /fleet remove <ship> <count>
  /fleet show [user]
  /fleet clear
  /battle start <opponent>
  /battle status
  /battle end
  /round start
  /round resolve
"""
from __future__ import annotations

import logging
import os
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from . import ai, battle, damage
from .ships import SHIPS, Ship

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("sw-rpg-bot")

FACTION_COLOR = {
    "Republic": discord.Color.from_rgb(200, 30, 30),     # crimson
    "CIS":      discord.Color.from_rgb(30, 80, 200),     # deep blue
}
EMBED_COLOR_DEFAULT = discord.Color.from_rgb(255, 204, 0)    # SW yellow


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

async def _ship_name_autocomplete(_inter: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    matches = SHIPS.find(current, limit=25)
    return [app_commands.Choice(name=f"[{s.faction}] {s.name}"[:100], value=s.name) for s in matches]


def _fleet_embed(user: discord.abc.User, stacks: list[battle.FleetStack], title_suffix: str = "") -> discord.Embed:
    if not stacks:
        embed = discord.Embed(
            title=f"Flotte von {user.display_name}{title_suffix}",
            description="_Leer. Füge Schiffe mit `/fleet add` hinzu._",
            color=EMBED_COLOR_DEFAULT,
        )
        return embed

    by_category: dict[str, list[battle.FleetStack]] = defaultdict(list)
    for s in stacks:
        by_category[s.ship.category].append(s)

    faction = stacks[0].ship.faction
    embed = discord.Embed(
        title=f"Flotte von {user.display_name}{title_suffix}",
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
                status = f"  _(Lead: {s.shields_current}/{s.ship.shields} SBD, {s.hull_current}/{s.ship.hull} RU)_"
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
    embed.add_field(name="Schilde (SBD)", value=f"**{ship.shields:,}**".replace(",", "."), inline=True)
    embed.add_field(name="Hülle (RU)", value=f"**{ship.hull:,}**".replace(",", "."), inline=True)
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
    lines = [f"**[{m.faction}]** {m.name}  _(SBD {m.shields}, RU {m.hull}, DMG {m.damage_per_report:g})_" for m in matches]
    embed = discord.Embed(
        title=f"Suchergebnisse: {query}",
        description="\n".join(lines),
        color=EMBED_COLOR_DEFAULT,
    )
    await inter.response.send_message(embed=embed)


bot.tree.add_command(ship_group)


# --- /fleet -----------------------------------------------------------------

fleet_group = app_commands.Group(name="fleet", description="Persönliche Flotte verwalten")


@fleet_group.command(name="add", description="Schiffe zur eigenen Flotte hinzufügen")
@app_commands.describe(ship="Schiffsklasse", count="Anzahl")
@app_commands.autocomplete(ship=_ship_name_autocomplete)
async def fleet_add(inter: discord.Interaction, ship: str, count: int) -> None:
    if count <= 0:
        await inter.response.send_message("Anzahl muss positiv sein.", ephemeral=True)
        return
    try:
        stack = await battle.add_ships(inter.user.id, ship, count)
    except ValueError as e:
        await inter.response.send_message(f"Fehler: {e}", ephemeral=True)
        return
    await inter.response.send_message(
        f"✓ {count}× **{stack.ship.name}** hinzugefügt. Stack hat jetzt {stack.count}.",
        ephemeral=True,
    )


@fleet_group.command(name="remove", description="Schiffe aus der eigenen Flotte entfernen")
@app_commands.describe(ship="Schiffsklasse", count="Anzahl")
@app_commands.autocomplete(ship=_ship_name_autocomplete)
async def fleet_remove(inter: discord.Interaction, ship: str, count: int) -> None:
    if count <= 0:
        await inter.response.send_message("Anzahl muss positiv sein.", ephemeral=True)
        return
    try:
        removed = await battle.remove_ships(inter.user.id, ship, count)
    except ValueError as e:
        await inter.response.send_message(f"Fehler: {e}", ephemeral=True)
        return
    if removed == 0:
        await inter.response.send_message("Dieses Schiff ist nicht in deiner Flotte.", ephemeral=True)
    else:
        await inter.response.send_message(f"✓ {removed}× entfernt.", ephemeral=True)


@fleet_group.command(name="show", description="Eigene Flotte (oder die eines anderen Spielers) anzeigen")
@app_commands.describe(user="Optional: Flotte dieses Spielers anzeigen")
async def fleet_show(inter: discord.Interaction, user: discord.Member | None = None) -> None:
    target = user or inter.user
    stacks = await battle.get_fleet(target.id)
    embed = _fleet_embed(target, stacks)
    await inter.response.send_message(embed=embed)


@fleet_group.command(name="clear", description="Eigene Flotte komplett leeren")
async def fleet_clear(inter: discord.Interaction) -> None:
    removed = await battle.clear_fleet(inter.user.id)
    await inter.response.send_message(f"✓ Flotte gelöscht ({removed} Einträge).", ephemeral=True)


bot.tree.add_command(fleet_group)


# --- /battle & /round -------------------------------------------------------

battle_group = app_commands.Group(name="battle", description="Raumschlacht verwalten")
round_group = app_commands.Group(name="round", description="Kampfrunden steuern")


@battle_group.command(name="start", description="Raumschlacht in diesem Kanal starten")
@app_commands.describe(opponent="Gegner")
async def battle_start(inter: discord.Interaction, opponent: discord.Member) -> None:
    if opponent.id == inter.user.id:
        await inter.response.send_message("Du kannst nicht gegen dich selbst kämpfen.", ephemeral=True)
        return
    if opponent.bot:
        await inter.response.send_message("Bots können nicht als Gegner registriert werden.", ephemeral=True)
        return

    existing = await battle.get_battle(inter.channel_id)
    if existing is not None:
        await inter.response.send_message(
            "In diesem Kanal läuft bereits ein Kampf. Beende ihn erst mit `/battle end`.",
            ephemeral=True,
        )
        return

    attacker_fleet = await battle.get_fleet(inter.user.id)
    defender_fleet = await battle.get_fleet(opponent.id)
    if not attacker_fleet or not defender_fleet:
        await inter.response.send_message(
            "Beide Spieler brauchen eine registrierte Flotte (`/fleet add`) bevor ein Kampf starten kann.",
            ephemeral=True,
        )
        return

    await battle.create_battle(inter.channel_id, inter.user.id, opponent.id)

    embed = discord.Embed(
        title="⚔ KAMPF INITIIERT",
        description=(
            f"{inter.user.mention} fordert {opponent.mention} heraus.\n\n"
            "Nutze `/round start` um die erste Runde zu beginnen."
        ),
        color=EMBED_COLOR_DEFAULT,
    )
    await inter.response.send_message(embed=embed)


@battle_group.command(name="status", description="Aktuellen Kampfstand in diesem Kanal anzeigen")
async def battle_status(inter: discord.Interaction) -> None:
    b = await battle.get_battle(inter.channel_id)
    if b is None:
        await inter.response.send_message("Kein laufender Kampf in diesem Kanal.", ephemeral=True)
        return

    p_a = inter.guild.get_member(b.player_a_id) if inter.guild else None
    p_b = inter.guild.get_member(b.player_b_id) if inter.guild else None
    stacks_a = await battle.get_fleet(b.player_a_id, channel_id=b.channel_id)
    stacks_b = await battle.get_fleet(b.player_b_id, channel_id=b.channel_id)

    embed = discord.Embed(
        title=f"⚔ Kampfstand · Runde {b.round_number}",
        description=f"Phase: `{b.phase}`",
        color=EMBED_COLOR_DEFAULT,
    )
    for player, stacks in ((p_a, stacks_a), (p_b, stacks_b)):
        name = player.display_name if player else f"Spieler {stacks[0].owner_id if stacks else '?'}"
        if not stacks:
            embed.add_field(name=name, value="_Flotte zerstört_", inline=False)
            continue
        lines = []
        for s in stacks:
            bar = _status_bar(s.shields_current + s.hull_current, s.ship.shields + s.ship.hull)
            lines.append(f"`{bar}` {s.count}× {s.ship.name}")
        embed.add_field(name=name, value="\n".join(lines)[:1024], inline=False)

    await inter.response.send_message(embed=embed)


@battle_group.command(name="end", description="Laufenden Kampf beenden")
async def battle_end(inter: discord.Interaction) -> None:
    b = await battle.get_battle(inter.channel_id)
    if b is None:
        await inter.response.send_message("Kein laufender Kampf in diesem Kanal.", ephemeral=True)
        return
    if inter.user.id not in (b.player_a_id, b.player_b_id):
        await inter.response.send_message("Nur die Kampfteilnehmer können den Kampf beenden.", ephemeral=True)
        return
    await battle.end_battle(inter.channel_id)
    await inter.response.send_message("Kampf beendet.")


@round_group.command(name="start", description="Neue Kampfrunde starten")
async def round_start(inter: discord.Interaction) -> None:
    b = await battle.get_battle(inter.channel_id)
    if b is None:
        await inter.response.send_message("Kein Kampf aktiv. Erst `/battle start`.", ephemeral=True)
        return
    if inter.user.id not in (b.player_a_id, b.player_b_id):
        await inter.response.send_message("Nur die Kampfteilnehmer können Runden steuern.", ephemeral=True)
        return

    await battle.set_phase(b.channel_id, "awaiting_posts")
    round_num = b.round_number + 1
    await battle.advance_round(b.channel_id)

    guild = inter.guild
    p_a = guild.get_member(b.player_a_id) if guild else None
    p_b = guild.get_member(b.player_b_id) if guild else None
    a_mention = p_a.mention if p_a else f"<@{b.player_a_id}>"
    b_mention = p_b.mention if p_b else f"<@{b.player_b_id}>"

    embed = discord.Embed(
        title=f"🔔 Runde {round_num} — Gefechtsdarstellung",
        description=(
            f"{a_mention} und {b_mention}: Postet jetzt eure RP-Nachrichten für diese Runde.\n"
            "Beschreibt, **welche gegnerischen Schiffe ihr angreift** und **wie** "
            "(Taktik, Formation, Manöver).\n\n"
            "Sobald beide gepostet haben → `/round resolve` für den Schadensbericht."
        ),
        color=EMBED_COLOR_DEFAULT,
    )
    await inter.response.send_message(embed=embed)


async def _latest_post_since(channel: discord.abc.Messageable, user_id: int, marker_id: int) -> discord.Message | None:
    """Scan recent channel history for the most recent non-bot message by *user_id*
    after the message with id *marker_id* (the /round start announcement).
    """
    async for msg in channel.history(limit=200, after=discord.Object(id=marker_id), oldest_first=False):
        if msg.author.id == user_id and not msg.author.bot and msg.content.strip():
            return msg
    return None


@round_group.command(name="resolve", description="Schaden der aktuellen Runde berechnen und Bericht posten")
async def round_resolve(inter: discord.Interaction) -> None:
    b = await battle.get_battle(inter.channel_id)
    if b is None:
        await inter.response.send_message("Kein Kampf aktiv.", ephemeral=True)
        return
    if inter.user.id not in (b.player_a_id, b.player_b_id):
        await inter.response.send_message("Nur die Kampfteilnehmer können Runden steuern.", ephemeral=True)
        return
    if b.phase != "awaiting_posts":
        await inter.response.send_message("Starte erst eine Runde mit `/round start`.", ephemeral=True)
        return

    await inter.response.defer(thinking=True)

    channel = inter.channel
    # Find the /round start announcement (most recent bot embed with that title prefix)
    marker: discord.Message | None = None
    async for msg in channel.history(limit=100):
        if msg.author.id == bot.user.id and msg.embeds and msg.embeds[0].title and "Gefechtsdarstellung" in msg.embeds[0].title:
            marker = msg
            break
    if marker is None:
        await inter.followup.send("Konnte den Rundenstart nicht finden. Starte die Runde mit `/round start`.")
        return

    post_a = await _latest_post_since(channel, b.player_a_id, marker.id)
    post_b = await _latest_post_since(channel, b.player_b_id, marker.id)
    if post_a is None or post_b is None:
        missing = []
        if post_a is None:
            missing.append(f"<@{b.player_a_id}>")
        if post_b is None:
            missing.append(f"<@{b.player_b_id}>")
        await inter.followup.send(f"Es fehlen noch RP-Nachrichten von: {', '.join(missing)}")
        return

    fleet_a = await battle.get_fleet(b.player_a_id, channel_id=b.channel_id)
    fleet_b = await battle.get_fleet(b.player_b_id, channel_id=b.channel_id)
    if not fleet_a or not fleet_b:
        await battle.set_phase(b.channel_id, "idle")
        await inter.followup.send("Eine Flotte ist bereits vernichtet — Kampf ist effektiv vorbei.")
        return

    # Parallel AI analyses
    analysis_a, analysis_b = await _safe_parallel(
        ai.analyze_rp_text(post_a.content, fleet_a, fleet_b),
        ai.analyze_rp_text(post_b.content, fleet_b, fleet_a),
    )

    report_a = await _resolve_side(
        attacker_fleet=fleet_a, defender_fleet=fleet_b,
        analysis=analysis_a, attacker_id=b.player_a_id, defender_id=b.player_b_id,
    )
    report_b = await _resolve_side(
        attacker_fleet=fleet_b, defender_fleet=fleet_a,
        analysis=analysis_b, attacker_id=b.player_b_id, defender_id=b.player_a_id,
    )

    guild = inter.guild
    name_a = guild.get_member(b.player_a_id).display_name if guild and guild.get_member(b.player_a_id) else "Spieler A"
    name_b = guild.get_member(b.player_b_id).display_name if guild and guild.get_member(b.player_b_id) else "Spieler B"

    narrative_a, narrative_b = await _safe_parallel(
        ai.write_damage_report_text(
            attacker_name=name_a, defender_name=name_b,
            rp_post_attacker=post_a.content, rp_post_defender=post_b.content,
            total_damage=report_a.total_damage,
            modifier_percent=report_a.modifier_percent,
            modifier_reason=report_a.modifier_reason,
            stack_damages=report_a.stack_damages,
        ),
        ai.write_damage_report_text(
            attacker_name=name_b, defender_name=name_a,
            rp_post_attacker=post_b.content, rp_post_defender=post_a.content,
            total_damage=report_b.total_damage,
            modifier_percent=report_b.modifier_percent,
            modifier_reason=report_b.modifier_reason,
            stack_damages=report_b.stack_damages,
        ),
    )

    embed_a = _damage_embed(name_a, name_b, report_a, narrative_a)
    embed_b = _damage_embed(name_b, name_a, report_b, narrative_b)

    await battle.set_phase(b.channel_id, "idle")
    await inter.followup.send(embeds=[embed_a, embed_b])

    # Check for end-of-battle
    fleet_a_after = await battle.get_fleet(b.player_a_id, channel_id=b.channel_id)
    fleet_b_after = await battle.get_fleet(b.player_b_id, channel_id=b.channel_id)
    alive_a = any(s.is_alive for s in fleet_a_after)
    alive_b = any(s.is_alive for s in fleet_b_after)
    if not alive_a or not alive_b:
        if alive_a and not alive_b:
            winner = name_a
        elif alive_b and not alive_a:
            winner = name_b
        else:
            winner = None
        end_embed = discord.Embed(
            title="🏁 KAMPF BEENDET",
            description=f"Sieger: **{winner}**" if winner else "Beide Flotten vernichtet.",
            color=EMBED_COLOR_DEFAULT,
        )
        await inter.followup.send(embed=end_embed)
        await battle.end_battle(b.channel_id)


async def _safe_parallel(*coros):
    import asyncio
    return await asyncio.gather(*coros)


async def _resolve_side(
    *,
    attacker_fleet: list[battle.FleetStack],
    defender_fleet: list[battle.FleetStack],
    analysis: ai.RPAnalysis,
    attacker_id: int,
    defender_id: int,
) -> damage.DamageReport:
    base = damage.compute_outgoing_damage(attacker_fleet)
    total = damage.apply_modifier(base, analysis.modifier_percent)

    # Build target list in the priority order returned by the AI.
    target_order: list[battle.FleetStack] = []
    name_to_stack = {s.ship.name: s for s in defender_fleet}
    for name in analysis.targeted_ships:
        stack = name_to_stack.get(name)
        if stack and stack.is_alive and stack not in target_order:
            target_order.append(stack)
    # Append any remaining live stacks as fallback
    for stack in defender_fleet:
        if stack.is_alive and stack not in target_order:
            target_order.append(stack)

    stack_damages, unassigned = damage.distribute_damage(total, target_order)

    # Persist
    for stack in target_order:
        await battle.update_stack(stack)

    return damage.DamageReport(
        attacker_id=attacker_id,
        defender_id=defender_id,
        total_damage=total,
        modifier_reason=analysis.modifier_reason,
        modifier_percent=analysis.modifier_percent,
        stack_damages=stack_damages,
        unassigned_damage=unassigned,
    )


def _damage_embed(attacker_name: str, defender_name: str, report: damage.DamageReport, narrative: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"📡 Schadensbericht — Angriff von {attacker_name}",
        description=narrative,
        color=EMBED_COLOR_DEFAULT,
    )
    mod = f"{report.modifier_percent:+d}%"
    embed.add_field(name="Effektiver Schaden", value=f"**{report.total_damage}** ({mod} · {report.modifier_reason or '—'})", inline=False)

    if not report.stack_damages:
        embed.add_field(name="Ergebnis", value="Keine Treffer.", inline=False)
        return embed

    for sd in report.stack_damages:
        parts = []
        if sd.shields_lost:
            parts.append(f"🛡 -{sd.shields_lost} SBD")
        if sd.hull_lost:
            parts.append(f"🔧 -{sd.hull_lost} RU")
        if sd.ships_destroyed:
            parts.append(f"💥 {sd.ships_destroyed}× zerstört")
        status = f"Verbleibend: {sd.remaining_count}× · Lead {sd.remaining_shields} SBD / {sd.remaining_hull} RU"
        embed.add_field(
            name=f"→ {sd.ship_name}",
            value="\n".join([" · ".join(parts) if parts else "Kein Effekt", status]),
            inline=False,
        )

    if report.unassigned_damage > 0:
        embed.add_field(
            name="Nicht zugewiesener Schaden",
            value=f"{report.unassigned_damage} (alle Ziele dieser Priorität zerstört)",
            inline=False,
        )

    return embed


bot.tree.add_command(battle_group)
bot.tree.add_command(round_group)


# --- /help ------------------------------------------------------------------

@bot.tree.command(name="help", description="Übersicht aller Bot-Befehle")
async def help_cmd(inter: discord.Interaction) -> None:
    embed = discord.Embed(title="SW RPG Bot — Befehle", color=EMBED_COLOR_DEFAULT)
    embed.add_field(
        name="Schiffsdatenbank",
        value=(
            "`/ship info <name>` — Detaillierte Infos\n"
            "`/ship search <query>` — Schiffe suchen"
        ),
        inline=False,
    )
    embed.add_field(
        name="Flotten-Management",
        value=(
            "`/fleet add <schiff> <anzahl>`\n"
            "`/fleet remove <schiff> <anzahl>`\n"
            "`/fleet show [user]`\n"
            "`/fleet clear`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Raumschlacht",
        value=(
            "`/battle start <gegner>` — Kampf im Kanal starten\n"
            "`/battle status` — Aktuellen Kampfstand\n"
            "`/battle end` — Kampf beenden\n"
            "`/round start` — Neue Runde (beide posten RP)\n"
            "`/round resolve` — Schadensbericht berechnen"
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
