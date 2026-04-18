"""Star Wars RPG Discord Bot — main entrypoint.

Slash commands:
  /ship info <name>
  /ship search <query>
  /fleet add <user> <ship> <count>        (admin only)
  /fleet remove <user> <ship> <count>     (admin only)
  /fleet show [user]
  /fleet clear <user>                     (admin only)
  /battle start
  /battle join
  /battle status
  /battle end
  /round start
  /round post <text>
  /round resolve
"""
from __future__ import annotations

import logging
import os
import time
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
            description="_Leer. Ein Admin kann mit `/fleet add` Schiffe hinzufügen._",
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


def _is_admin(inter: discord.Interaction) -> bool:
    perms = getattr(inter.user, "guild_permissions", None)
    return bool(perms and perms.administrator)


@fleet_group.command(name="add", description="Schiffe zur Flotte eines Spielers hinzufügen (nur Admin)")
@app_commands.describe(user="Spieler, dessen Flotte erweitert wird", ship="Schiffsklasse", count="Anzahl")
@app_commands.autocomplete(ship=_ship_name_autocomplete)
@app_commands.default_permissions(administrator=True)
async def fleet_add(inter: discord.Interaction, user: discord.Member, ship: str, count: int) -> None:
    if not _is_admin(inter):
        await inter.response.send_message("Nur Admins dürfen Flotten verwalten.", ephemeral=True)
        return
    if count <= 0:
        await inter.response.send_message("Anzahl muss positiv sein.", ephemeral=True)
        return
    try:
        stack = await battle.add_ships(user.id, ship, count)
    except ValueError as e:
        await inter.response.send_message(f"Fehler: {e}", ephemeral=True)
        return
    await inter.response.send_message(
        f"✓ {count}× **{stack.ship.name}** zu {user.mention}s Flotte hinzugefügt. "
        f"Stack hat jetzt {stack.count}.",
        ephemeral=True,
    )


@fleet_group.command(name="remove", description="Schiffe aus der Flotte eines Spielers entfernen (nur Admin)")
@app_commands.describe(user="Spieler, dessen Flotte reduziert wird", ship="Schiffsklasse", count="Anzahl")
@app_commands.autocomplete(ship=_ship_name_autocomplete)
@app_commands.default_permissions(administrator=True)
async def fleet_remove(inter: discord.Interaction, user: discord.Member, ship: str, count: int) -> None:
    if not _is_admin(inter):
        await inter.response.send_message("Nur Admins dürfen Flotten verwalten.", ephemeral=True)
        return
    if count <= 0:
        await inter.response.send_message("Anzahl muss positiv sein.", ephemeral=True)
        return
    try:
        removed = await battle.remove_ships(user.id, ship, count)
    except ValueError as e:
        await inter.response.send_message(f"Fehler: {e}", ephemeral=True)
        return
    if removed == 0:
        await inter.response.send_message(
            f"Dieses Schiff ist nicht in {user.mention}s Flotte.", ephemeral=True,
        )
    else:
        await inter.response.send_message(
            f"✓ {removed}× aus {user.mention}s Flotte entfernt.", ephemeral=True,
        )


@fleet_group.command(name="show", description="Flotte eines Spielers anzeigen")
@app_commands.describe(user="Optional: Flotte dieses Spielers (Standard: eigene)")
async def fleet_show(inter: discord.Interaction, user: discord.Member | None = None) -> None:
    target = user or inter.user
    stacks = await battle.get_fleet(target.id)
    embed = _fleet_embed(target, stacks)
    await inter.response.send_message(embed=embed)


@fleet_group.command(name="clear", description="Flotte eines Spielers komplett leeren (nur Admin)")
@app_commands.describe(user="Spieler, dessen Flotte gelöscht wird")
@app_commands.default_permissions(administrator=True)
async def fleet_clear(inter: discord.Interaction, user: discord.Member) -> None:
    if not _is_admin(inter):
        await inter.response.send_message("Nur Admins dürfen Flotten verwalten.", ephemeral=True)
        return
    removed = await battle.clear_fleet(user.id)
    await inter.response.send_message(
        f"✓ Flotte von {user.mention} gelöscht ({removed} Einträge).", ephemeral=True,
    )


bot.tree.add_command(fleet_group)


# --- /battle & /round -------------------------------------------------------

battle_group = app_commands.Group(name="battle", description="Raumschlacht verwalten")
round_group = app_commands.Group(name="round", description="Kampfrunden steuern")


@battle_group.command(name="start", description="Raumschlacht in diesem Kanal eröffnen (du bist Spieler A)")
async def battle_start(inter: discord.Interaction) -> None:
    existing = await battle.get_battle(inter.channel_id)
    if existing is not None:
        await inter.response.send_message(
            "In diesem Kanal läuft bereits ein Kampf. Beende ihn erst mit `/battle end`.",
            ephemeral=True,
        )
        return

    attacker_fleet = await battle.get_fleet(inter.user.id)
    if not attacker_fleet:
        await inter.response.send_message(
            "Du hast keine registrierte Flotte. Bitte einen Admin, dir eine Flotte zuzuweisen.",
            ephemeral=True,
        )
        return

    await battle.create_battle(inter.channel_id, inter.user.id, 0)

    embed = discord.Embed(
        title="Kampf eröffnet",
        description=(
            f"{inter.user.mention} hat einen Kampf in diesem Kanal eröffnet.\n\n"
            "Der zweite Spieler tritt mit `/battle join` bei. Danach startet ihr mit `/round start`."
        ),
        color=EMBED_COLOR_DEFAULT,
    )
    await inter.response.send_message(embed=embed)


@battle_group.command(name="join", description="Einem laufenden Kampf in diesem Kanal beitreten")
async def battle_join(inter: discord.Interaction) -> None:
    b = await battle.get_battle(inter.channel_id)
    if b is None:
        await inter.response.send_message(
            "Kein offener Kampf in diesem Kanal. Eröffne einen mit `/battle start`.",
            ephemeral=True,
        )
        return
    if b.player_b_id != 0:
        await inter.response.send_message(
            "Dieser Kampf hat schon beide Teilnehmer.", ephemeral=True,
        )
        return
    if b.player_a_id == inter.user.id:
        await inter.response.send_message(
            "Du hast den Kampf selbst eröffnet — der Gegner muss jemand anderes sein.",
            ephemeral=True,
        )
        return

    fleet = await battle.get_fleet(inter.user.id)
    if not fleet:
        await inter.response.send_message(
            "Du hast keine registrierte Flotte. Bitte einen Admin, dir eine Flotte zuzuweisen.",
            ephemeral=True,
        )
        return

    ok = await battle.join_battle(inter.channel_id, inter.user.id)
    if not ok:
        await inter.response.send_message("Beitritt nicht möglich — vielleicht ist jemand schneller gewesen.", ephemeral=True)
        return

    guild = inter.guild
    p_a = guild.get_member(b.player_a_id) if guild else None
    a_mention = p_a.mention if p_a else f"<@{b.player_a_id}>"

    embed = discord.Embed(
        title="Kampfparteien komplett",
        description=(
            f"{a_mention} gegen {inter.user.mention}.\n\n"
            "Startet die erste Runde mit `/round start`."
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

    embed = discord.Embed(
        title=f"Kampfstand — Runde {b.round_number}",
        description=f"Phase: `{b.phase}`",
        color=EMBED_COLOR_DEFAULT,
    )

    if b.player_b_id == 0:
        p_a = inter.guild.get_member(b.player_a_id) if inter.guild else None
        name_a = p_a.display_name if p_a else f"<@{b.player_a_id}>"
        embed.add_field(
            name="Status",
            value=f"{name_a} wartet auf einen Gegner. Beitreten mit `/battle join`.",
            inline=False,
        )
        await inter.response.send_message(embed=embed)
        return

    p_a = inter.guild.get_member(b.player_a_id) if inter.guild else None
    p_b = inter.guild.get_member(b.player_b_id) if inter.guild else None
    stacks_a = await battle.get_fleet(b.player_a_id, channel_id=b.channel_id)
    stacks_b = await battle.get_fleet(b.player_b_id, channel_id=b.channel_id)

    for player, stacks, fallback_id in (
        (p_a, stacks_a, b.player_a_id),
        (p_b, stacks_b, b.player_b_id),
    ):
        name = player.display_name if player else f"Spieler {fallback_id}"
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
    if b.player_b_id == 0:
        await inter.response.send_message(
            "Der zweite Spieler muss erst `/battle join` ausführen.", ephemeral=True,
        )
        return
    if inter.user.id not in (b.player_a_id, b.player_b_id):
        await inter.response.send_message("Nur die Kampfteilnehmer können Runden steuern.", ephemeral=True)
        return

    await battle.clear_round_posts(b.channel_id)
    await battle.set_phase(b.channel_id, "awaiting_posts")
    round_num = b.round_number + 1
    await battle.advance_round(b.channel_id)

    guild = inter.guild
    p_a = guild.get_member(b.player_a_id) if guild else None
    p_b = guild.get_member(b.player_b_id) if guild else None
    a_mention = p_a.mention if p_a else f"<@{b.player_a_id}>"
    b_mention = p_b.mention if p_b else f"<@{b.player_b_id}>"

    embed = discord.Embed(
        title=f"Runde {round_num} — Gefechtsdarstellung",
        description=(
            f"{a_mention} und {b_mention}: Reicht eure RP-Nachricht mit `/round post` ein.\n"
            "Beschreibt, **welche gegnerischen Schiffe ihr angreift** und **wie** "
            "(Taktik, Formation, Manöver).\n\n"
            "Sobald beide eingereicht haben, führt jemand `/round resolve` aus."
        ),
        color=EMBED_COLOR_DEFAULT,
    )
    await inter.response.send_message(embed=embed)


@round_group.command(name="post", description="Deine RP-Nachricht für die aktuelle Runde einreichen")
@app_commands.describe(text="Dein Roleplay-Text für die aktuelle Runde")
async def round_post(inter: discord.Interaction, text: str) -> None:
    b = await battle.get_battle(inter.channel_id)
    if b is None:
        await inter.response.send_message("Kein Kampf aktiv.", ephemeral=True)
        return
    if inter.user.id not in (b.player_a_id, b.player_b_id):
        await inter.response.send_message(
            "Nur die Kampfteilnehmer können RP-Posts einreichen.", ephemeral=True,
        )
        return
    if b.phase != "awaiting_posts":
        await inter.response.send_message(
            "Aktuell ist keine Runde offen. Starte eine mit `/round start`.", ephemeral=True,
        )
        return

    clean = text.strip()
    if len(clean) < 10:
        await inter.response.send_message(
            "Der RP-Text ist zu kurz (mind. 10 Zeichen).", ephemeral=True,
        )
        return

    await battle.save_round_post(b.channel_id, inter.user.id, clean)

    other_id = b.player_b_id if inter.user.id == b.player_a_id else b.player_a_id
    other_post = await battle.get_round_post(b.channel_id, other_id)
    both_ready = other_post is not None

    guild = inter.guild
    other_member = guild.get_member(other_id) if guild else None
    other_mention = other_member.mention if other_member else f"<@{other_id}>"

    embed = discord.Embed(
        title=f"RP-Post von {inter.user.display_name} registriert",
        description=clean[:3900],
        color=EMBED_COLOR_DEFAULT,
    )
    if both_ready:
        embed.set_footer(text="Beide Posts liegen vor — führt jetzt /round resolve aus.")
    else:
        embed.set_footer(text=f"Warte noch auf {other_member.display_name if other_member else 'Gegner'}.")

    await inter.response.send_message(embed=embed)
    if not both_ready and other_member:
        try:
            await inter.followup.send(f"{other_mention}, du bist dran — `/round post`.")
        except Exception:
            pass


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

    post_a_text = await battle.get_round_post(b.channel_id, b.player_a_id)
    post_b_text = await battle.get_round_post(b.channel_id, b.player_b_id)
    if post_a_text is None or post_b_text is None:
        missing = []
        if post_a_text is None:
            missing.append(f"<@{b.player_a_id}>")
        if post_b_text is None:
            missing.append(f"<@{b.player_b_id}>")
        await inter.followup.send(
            f"Es fehlen noch RP-Posts von: {', '.join(missing)} (einreichen mit `/round post`)."
        )
        return

    fleet_a = await battle.get_fleet(b.player_a_id, channel_id=b.channel_id)
    fleet_b = await battle.get_fleet(b.player_b_id, channel_id=b.channel_id)
    if not fleet_a or not fleet_b:
        await battle.set_phase(b.channel_id, "idle")
        await inter.followup.send("Eine Flotte ist bereits vernichtet — Kampf ist effektiv vorbei.")
        return

    import asyncio
    t_round = time.perf_counter()
    try:
        analysis_a, analysis_b = await asyncio.gather(
            ai.analyze_rp_text(post_a_text, fleet_a, fleet_b),
            ai.analyze_rp_text(post_b_text, fleet_b, fleet_a),
        )
    except Exception as e:
        log.exception("Analyse-Phase fehlgeschlagen")
        msg = f"{type(e).__name__}: {e}"[:500]
        await inter.followup.send(
            f"⚠ KI-Analyse fehlgeschlagen.\n```\n{msg}\n```"
        )
        return
    log.info("analysis phase total: %.2fs", time.perf_counter() - t_round)

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

    t_narr = time.perf_counter()
    try:
        narrative_a, narrative_b = await asyncio.gather(
            ai.write_damage_report_text(
                attacker_name=name_a, defender_name=name_b,
                rp_post_attacker=post_a_text, rp_post_defender=post_b_text,
                total_damage=report_a.total_damage,
                modifier_percent=report_a.modifier_percent,
                modifier_reason=report_a.modifier_reason,
                stack_damages=report_a.stack_damages,
            ),
            ai.write_damage_report_text(
                attacker_name=name_b, defender_name=name_a,
                rp_post_attacker=post_b_text, rp_post_defender=post_a_text,
                total_damage=report_b.total_damage,
                modifier_percent=report_b.modifier_percent,
                modifier_reason=report_b.modifier_reason,
                stack_damages=report_b.stack_damages,
            ),
        )
    except Exception:
        log.exception("Narrative-Phase fehlgeschlagen")
        narrative_a = "(Funkverbindung zur KI unterbrochen — nur Rohdaten verfügbar.)"
        narrative_b = narrative_a
    log.info("narrative phase total: %.2fs", time.perf_counter() - t_narr)
    log.info("round_resolve AI total: %.2fs", time.perf_counter() - t_round)

    fleet_a_after = await battle.get_fleet(b.player_a_id, channel_id=b.channel_id)
    fleet_b_after = await battle.get_fleet(b.player_b_id, channel_id=b.channel_id)

    embed_a = _damage_embed(name_a, name_b, report_a, narrative_a, fleet_b_after)
    embed_b = _damage_embed(name_b, name_a, report_b, narrative_b, fleet_a_after)

    await battle.clear_round_posts(b.channel_id)
    await battle.set_phase(b.channel_id, "idle")
    await inter.followup.send(embeds=[embed_a, embed_b])

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
            title="Kampf beendet",
            description=f"Sieger: **{winner}**" if winner else "Beide Flotten vernichtet.",
            color=EMBED_COLOR_DEFAULT,
        )
        await inter.followup.send(embed=end_embed)
        await battle.end_battle(b.channel_id)


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


def _fmt_int(n: int) -> str:
    return f"{n:,}".replace(",", ".")


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
        embed.add_field(name="Ergebnis", value="Keine Treffer — alle Ziele bereits zerstört oder verfehlt.", inline=False)
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

        embed.add_field(
            name=sd.ship_name,
            value="\n".join(lines),
            inline=False,
        )

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
    if total_destroyed == 0 and total_shield_dmg == 0 and total_hull_dmg == 0:
        fazit_lines.append(f"{attacker_name}s Angriff blieb ohne nennenswerten Effekt.")
    else:
        fazit_lines.append(
            f"{attacker_name} fügt {defender_name} insgesamt "
            f"{_fmt_int(total_shield_dmg)} SBD Schild- und "
            f"{_fmt_int(total_hull_dmg)} RU Hüllenschaden zu."
        )
        if total_destroyed:
            fazit_lines.append(
                f"Dabei werden **{total_destroyed} Schiff{'e' if total_destroyed != 1 else ''}** "
                f"vollständig vernichtet."
            )

    remaining_ships = sum(s.count for s in defender_fleet_after if s.is_alive)
    remaining_classes = sum(1 for s in defender_fleet_after if s.is_alive)
    if remaining_ships == 0:
        fazit_lines.append(
            f"{defender_name}s Flotte ist damit vollständig ausgelöscht."
        )
    else:
        fazit_lines.append(
            f"{defender_name}s verbleibende Streitmacht: **{remaining_ships} Schiff"
            f"{'e' if remaining_ships != 1 else ''}** in {remaining_classes} Klasse"
            f"{'n' if remaining_classes != 1 else ''}."
        )

    embed.add_field(name="Fazit", value="\n".join(fazit_lines), inline=False)

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
        name="Flotten (Admin-only außer show)",
        value=(
            "`/fleet add <user> <schiff> <anzahl>`\n"
            "`/fleet remove <user> <schiff> <anzahl>`\n"
            "`/fleet clear <user>`\n"
            "`/fleet show [user]`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Raumschlacht",
        value=(
            "`/battle start` — Kampf eröffnen (du bist Spieler A)\n"
            "`/battle join` — offenem Kampf als Spieler B beitreten\n"
            "`/battle status` — Aktuellen Kampfstand\n"
            "`/battle end` — Kampf beenden\n"
            "`/round start` — Neue Runde\n"
            "`/round post <text>` — eigenen RP-Text einreichen\n"
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
