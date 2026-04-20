"""Battle state manager — persistent, one active battle per Discord channel.

Data model (SQLite):
  fleets       : per-fleet ship stacks. A "fleet" is identified by a NAME
                 (typically a Tupperbox persona display name), scoped per guild.
  battles      : one row per channel with the two participating fleet names
                 plus the anchor_message_id (Discord message id from which the
                 next `/battle resolve` will start reading chat history).

A "stack" represents N identical ships in a fleet. Every ship in the stack
shares the same max HP, but the currently-damaged ship tracks its shields
and hull independently from its siblings. When the lead ship is destroyed,
the stack's count drops by 1 and the damage rolls onto the next ship.
"""
from __future__ import annotations

import os
import aiosqlite
from dataclasses import dataclass
from pathlib import Path

from .ships import SHIPS, Ship

_DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR = Path(os.environ.get("DATA_DIR", _DEFAULT_DATA_DIR))
DB_PATH = DATA_DIR / "battle.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS fleets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    fleet_name      TEXT NOT NULL,
    scope_channel   INTEGER,          -- NULL = persistent fleet; else battle-scoped snapshot
    ship_name       TEXT NOT NULL,
    count           INTEGER NOT NULL,
    count_destroyed INTEGER NOT NULL DEFAULT 0,
    shields_current INTEGER NOT NULL,
    hull_current    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS fleet_meta (
    guild_id    INTEGER NOT NULL,
    fleet_name  TEXT NOT NULL,
    faction     TEXT,                 -- 'Republik' oder 'KUS'
    PRIMARY KEY (guild_id, fleet_name)
);

CREATE TABLE IF NOT EXISTS battles (
    channel_id         INTEGER PRIMARY KEY,
    guild_id           INTEGER NOT NULL,
    fleet_a_name       TEXT NOT NULL,
    fleet_b_name       TEXT NOT NULL,
    anchor_message_id  INTEGER NOT NULL,   -- fixed start-of-battle boundary
    resolve_cursor_id  INTEGER NOT NULL,   -- advances on each damage resolve
    phase              TEXT NOT NULL DEFAULT 'active',
    round_number       INTEGER NOT NULL DEFAULT 0,
    created_at         INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fleets_lookup ON fleets(guild_id, fleet_name, scope_channel);
CREATE INDEX IF NOT EXISTS idx_fleets_scope  ON fleets(scope_channel);
"""


@dataclass
class FleetStack:
    id: int
    fleet_name: str
    guild_id: int
    ship: Ship
    count: int
    count_destroyed: int
    shields_current: int
    hull_current: int

    @property
    def is_alive(self) -> bool:
        return self.count > 0


@dataclass
class Battle:
    channel_id: int
    guild_id: int
    fleet_a_name: str
    fleet_b_name: str
    anchor_message_id: int
    resolve_cursor_id: int
    phase: str
    round_number: int


async def _has_legacy_schema(db: aiosqlite.Connection) -> bool:
    """Detect schemas from the previous data model (owner-id / round_posts)."""
    cur = await db.execute("PRAGMA table_info(fleets)")
    cols = [row[1] for row in await cur.fetchall()]
    if cols and "owner_id" in cols:
        return True
    cur = await db.execute("PRAGMA table_info(battles)")
    cols = [row[1] for row in await cur.fetchall()]
    if cols and ("player_a_id" in cols or "player_b_id" in cols):
        return True
    return False


async def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        if await _has_legacy_schema(db):
            await db.executescript(
                "DROP TABLE IF EXISTS round_posts;"
                "DROP TABLE IF EXISTS battles;"
                "DROP TABLE IF EXISTS fleets;"
            )
            await db.commit()
        await db.executescript(SCHEMA)
        # Migrate: add resolve_cursor_id to existing battles tables if missing.
        cur = await db.execute("PRAGMA table_info(battles)")
        cols = [row[1] for row in await cur.fetchall()]
        if cols and "resolve_cursor_id" not in cols:
            await db.execute("ALTER TABLE battles ADD COLUMN resolve_cursor_id INTEGER")
            await db.execute(
                "UPDATE battles SET resolve_cursor_id = anchor_message_id "
                "WHERE resolve_cursor_id IS NULL"
            )
        await db.commit()


# --- Fleet operations --------------------------------------------------------

async def add_ships(
    guild_id: int,
    fleet_name: str,
    ship_name: str,
    count: int,
    *,
    channel_id: int | None = None,
) -> FleetStack:
    ship = SHIPS.get(ship_name)
    if ship is None:
        raise ValueError(f"Unknown ship class: {ship_name}")
    if count <= 0:
        raise ValueError("Count must be positive")
    if not fleet_name.strip():
        raise ValueError("Fleet name must not be empty")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            """SELECT * FROM fleets
               WHERE guild_id = ? AND fleet_name = ? AND ship_name = ?
                 AND (scope_channel IS ? OR scope_channel = ?)""",
            (guild_id, fleet_name, ship.name, channel_id, channel_id),
        )).fetchone()

        if row is None:
            cur = await db.execute(
                """INSERT INTO fleets
                   (guild_id, fleet_name, scope_channel, ship_name, count,
                    count_destroyed, shields_current, hull_current)
                   VALUES (?, ?, ?, ?, ?, 0, ?, ?)""",
                (guild_id, fleet_name, channel_id, ship.name, count, ship.shields, ship.hull),
            )
            fleet_id = cur.lastrowid
            await db.commit()
            return FleetStack(
                id=fleet_id, fleet_name=fleet_name, guild_id=guild_id, ship=ship,
                count=count, count_destroyed=0,
                shields_current=ship.shields, hull_current=ship.hull,
            )

        new_count = row["count"] + count
        await db.execute("UPDATE fleets SET count = ? WHERE id = ?", (new_count, row["id"]))
        await db.commit()
        return FleetStack(
            id=row["id"], fleet_name=fleet_name, guild_id=guild_id, ship=ship,
            count=new_count, count_destroyed=row["count_destroyed"],
            shields_current=row["shields_current"], hull_current=row["hull_current"],
        )


async def remove_ships(
    guild_id: int,
    fleet_name: str,
    ship_name: str,
    count: int,
    *,
    channel_id: int | None = None,
) -> int:
    ship = SHIPS.get(ship_name)
    if ship is None:
        raise ValueError(f"Unknown ship class: {ship_name}")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            """SELECT * FROM fleets
               WHERE guild_id = ? AND fleet_name = ? AND ship_name = ?
                 AND (scope_channel IS ? OR scope_channel = ?)""",
            (guild_id, fleet_name, ship.name, channel_id, channel_id),
        )).fetchone()
        if row is None:
            return 0
        removed = min(count, row["count"])
        new_count = row["count"] - removed
        if new_count == 0:
            await db.execute("DELETE FROM fleets WHERE id = ?", (row["id"],))
        else:
            await db.execute("UPDATE fleets SET count = ? WHERE id = ?", (new_count, row["id"]))
        await db.commit()
        return removed


async def clear_fleet(
    guild_id: int,
    fleet_name: str,
    *,
    channel_id: int | None = None,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """DELETE FROM fleets
               WHERE guild_id = ? AND fleet_name = ?
                 AND (scope_channel IS ? OR scope_channel = ?)""",
            (guild_id, fleet_name, channel_id, channel_id),
        )
        # Drop the faction entry only when the persistent fleet is wiped.
        if channel_id is None:
            await db.execute(
                "DELETE FROM fleet_meta WHERE guild_id = ? AND fleet_name = ?",
                (guild_id, fleet_name),
            )
        await db.commit()
        return cur.rowcount


async def set_fleet_faction(guild_id: int, fleet_name: str, faction: str) -> None:
    """Store/overwrite the faction label for a fleet ('Republik' or 'KUS')."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO fleet_meta (guild_id, fleet_name, faction)
               VALUES (?, ?, ?)
               ON CONFLICT(guild_id, fleet_name) DO UPDATE SET faction = excluded.faction""",
            (guild_id, fleet_name, faction),
        )
        await db.commit()


async def get_fleet_faction(guild_id: int, fleet_name: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT faction FROM fleet_meta WHERE guild_id = ? AND fleet_name = ?",
            (guild_id, fleet_name),
        )).fetchone()
    return row["faction"] if row and row["faction"] else None


async def get_fleet(
    guild_id: int,
    fleet_name: str,
    *,
    channel_id: int | None = None,
) -> list[FleetStack]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            """SELECT * FROM fleets
               WHERE guild_id = ? AND fleet_name = ?
                 AND (scope_channel IS ? OR scope_channel = ?)
               ORDER BY id""",
            (guild_id, fleet_name, channel_id, channel_id),
        )).fetchall()

    stacks: list[FleetStack] = []
    for row in rows:
        ship = SHIPS.get(row["ship_name"])
        if ship is None:
            continue
        stacks.append(FleetStack(
            id=row["id"], fleet_name=row["fleet_name"], guild_id=row["guild_id"],
            ship=ship, count=row["count"], count_destroyed=row["count_destroyed"],
            shields_current=row["shields_current"], hull_current=row["hull_current"],
        ))
    return stacks


async def list_fleet_names(guild_id: int) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            """SELECT DISTINCT fleet_name FROM fleets
               WHERE guild_id = ? AND scope_channel IS NULL
               ORDER BY fleet_name""",
            (guild_id,),
        )).fetchall()
    return [r["fleet_name"] for r in rows]


async def update_stack(stack: FleetStack) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        if stack.count <= 0:
            await db.execute("DELETE FROM fleets WHERE id = ?", (stack.id,))
        else:
            await db.execute(
                """UPDATE fleets
                   SET count = ?, count_destroyed = ?,
                       shields_current = ?, hull_current = ?
                   WHERE id = ?""",
                (
                    stack.count, stack.count_destroyed,
                    stack.shields_current, stack.hull_current,
                    stack.id,
                ),
            )
        await db.commit()


# --- Battle lifecycle --------------------------------------------------------

async def get_battle(channel_id: int) -> Battle | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM battles WHERE channel_id = ?", (channel_id,),
        )).fetchone()
    if row is None:
        return None
    return Battle(
        channel_id=row["channel_id"],
        guild_id=row["guild_id"],
        fleet_a_name=row["fleet_a_name"],
        fleet_b_name=row["fleet_b_name"],
        anchor_message_id=row["anchor_message_id"],
        resolve_cursor_id=row["resolve_cursor_id"] or row["anchor_message_id"],
        phase=row["phase"],
        round_number=row["round_number"],
    )


async def _snapshot_fleet(guild_id: int, fleet_name: str, channel_id: int) -> None:
    persistent = await get_fleet(guild_id, fleet_name, channel_id=None)
    await clear_fleet(guild_id, fleet_name, channel_id=channel_id)
    async with aiosqlite.connect(DB_PATH) as db:
        for stack in persistent:
            await db.execute(
                """INSERT INTO fleets
                   (guild_id, fleet_name, scope_channel, ship_name, count, count_destroyed,
                    shields_current, hull_current)
                   VALUES (?, ?, ?, ?, ?, 0, ?, ?)""",
                (
                    guild_id, fleet_name, channel_id, stack.ship.name, stack.count,
                    stack.ship.shields, stack.ship.hull,
                ),
            )
        await db.commit()


async def create_battle(
    channel_id: int,
    guild_id: int,
    fleet_a_name: str,
    fleet_b_name: str,
    anchor_message_id: int,
) -> Battle:
    import time
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO battles
               (channel_id, guild_id, fleet_a_name, fleet_b_name,
                anchor_message_id, resolve_cursor_id,
                phase, round_number, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'active', 0, ?)""",
            (channel_id, guild_id, fleet_a_name, fleet_b_name,
             anchor_message_id, anchor_message_id, int(time.time())),
        )
        await db.commit()

    await _snapshot_fleet(guild_id, fleet_a_name, channel_id)
    await _snapshot_fleet(guild_id, fleet_b_name, channel_id)

    return (await get_battle(channel_id))  # type: ignore[return-value]


async def set_anchor(channel_id: int, message_id: int) -> None:
    """Redefine the start-of-battle boundary. Also resets the resolve cursor
    to that same point, since everything before the anchor is ignored anyway."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE battles SET anchor_message_id = ?, resolve_cursor_id = ? "
            "WHERE channel_id = ?",
            (message_id, message_id, channel_id),
        )
        await db.commit()


async def set_resolve_cursor(channel_id: int, message_id: int) -> None:
    """Advance the cursor that separates already-resolved context from
    new messages. Does NOT touch the anchor."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE battles SET resolve_cursor_id = ? WHERE channel_id = ?",
            (message_id, channel_id),
        )
        await db.commit()


async def advance_round(channel_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE battles SET round_number = round_number + 1 WHERE channel_id = ?",
            (channel_id,),
        )
        await db.commit()
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT round_number FROM battles WHERE channel_id = ?", (channel_id,),
        )).fetchone()
    return int(row["round_number"]) if row else 0


async def end_battle(channel_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM fleets WHERE scope_channel = ?", (channel_id,))
        await db.execute("DELETE FROM battles WHERE channel_id = ?", (channel_id,))
        await db.commit()
