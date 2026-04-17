"""Battle state manager — persistent, one active battle per Discord channel.

Data model (SQLite):
  battles      : one row per channel with battle metadata
  fleets       : fleet stack entries (ship class + count + current HP per ship)

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
CREATE TABLE IF NOT EXISTS battles (
    channel_id    INTEGER PRIMARY KEY,
    player_a_id   INTEGER NOT NULL,
    player_b_id   INTEGER NOT NULL,
    phase         TEXT NOT NULL DEFAULT 'idle',
    round_number  INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS fleets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id        INTEGER NOT NULL,
    scope_channel   INTEGER,          -- NULL = persistent player fleet; else battle-scoped
    ship_name       TEXT NOT NULL,
    count           INTEGER NOT NULL,
    count_destroyed INTEGER NOT NULL DEFAULT 0,
    shields_current INTEGER NOT NULL,
    hull_current    INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fleets_owner    ON fleets(owner_id, scope_channel);
CREATE INDEX IF NOT EXISTS idx_fleets_channel  ON fleets(scope_channel);
"""


@dataclass
class FleetStack:
    id: int
    owner_id: int
    ship: Ship
    count: int                 # remaining ships in the stack (not yet destroyed)
    count_destroyed: int
    shields_current: int       # shields of the lead (currently damaged) ship
    hull_current: int          # hull of the lead ship

    @property
    def is_alive(self) -> bool:
        return self.count > 0


@dataclass
class Battle:
    channel_id: int
    player_a_id: int
    player_b_id: int
    phase: str          # 'idle' | 'awaiting_posts' | 'ready_to_resolve'
    round_number: int


async def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


# --- Fleet operations --------------------------------------------------------

async def add_ships(owner_id: int, ship_name: str, count: int, *, channel_id: int | None = None) -> FleetStack:
    """Add a stack of ships to a player's fleet. Merges with existing stacks
    of the same class in the same scope.
    """
    ship = SHIPS.get(ship_name)
    if ship is None:
        raise ValueError(f"Unknown ship class: {ship_name}")
    if count <= 0:
        raise ValueError("Count must be positive")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            """SELECT * FROM fleets
               WHERE owner_id = ? AND ship_name = ?
                 AND (scope_channel IS ? OR scope_channel = ?)""",
            (owner_id, ship.name, channel_id, channel_id),
        )).fetchone()

        if row is None:
            cur = await db.execute(
                """INSERT INTO fleets
                   (owner_id, scope_channel, ship_name, count,
                    count_destroyed, shields_current, hull_current)
                   VALUES (?, ?, ?, ?, 0, ?, ?)""",
                (owner_id, channel_id, ship.name, count, ship.shields, ship.hull),
            )
            fleet_id = cur.lastrowid
            await db.commit()
            return FleetStack(
                id=fleet_id, owner_id=owner_id, ship=ship,
                count=count, count_destroyed=0,
                shields_current=ship.shields, hull_current=ship.hull,
            )

        new_count = row["count"] + count
        await db.execute(
            "UPDATE fleets SET count = ? WHERE id = ?",
            (new_count, row["id"]),
        )
        await db.commit()
        return FleetStack(
            id=row["id"], owner_id=owner_id, ship=ship,
            count=new_count, count_destroyed=row["count_destroyed"],
            shields_current=row["shields_current"], hull_current=row["hull_current"],
        )


async def remove_ships(owner_id: int, ship_name: str, count: int, *, channel_id: int | None = None) -> int:
    """Remove up to *count* ships from a stack. Returns how many were removed.
    0 if the stack doesn't exist.
    """
    ship = SHIPS.get(ship_name)
    if ship is None:
        raise ValueError(f"Unknown ship class: {ship_name}")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            """SELECT * FROM fleets
               WHERE owner_id = ? AND ship_name = ?
                 AND (scope_channel IS ? OR scope_channel = ?)""",
            (owner_id, ship.name, channel_id, channel_id),
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


async def clear_fleet(owner_id: int, *, channel_id: int | None = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """DELETE FROM fleets
               WHERE owner_id = ? AND (scope_channel IS ? OR scope_channel = ?)""",
            (owner_id, channel_id, channel_id),
        )
        await db.commit()
        return cur.rowcount


async def get_fleet(owner_id: int, *, channel_id: int | None = None) -> list[FleetStack]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            """SELECT * FROM fleets
               WHERE owner_id = ? AND (scope_channel IS ? OR scope_channel = ?)
               ORDER BY id""",
            (owner_id, channel_id, channel_id),
        )).fetchall()

    stacks: list[FleetStack] = []
    for row in rows:
        ship = SHIPS.get(row["ship_name"])
        if ship is None:
            continue
        stacks.append(FleetStack(
            id=row["id"], owner_id=row["owner_id"], ship=ship,
            count=row["count"], count_destroyed=row["count_destroyed"],
            shields_current=row["shields_current"], hull_current=row["hull_current"],
        ))
    return stacks


async def update_stack(stack: FleetStack) -> None:
    """Persist the current state of a stack after damage has been applied."""
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
        player_a_id=row["player_a_id"],
        player_b_id=row["player_b_id"],
        phase=row["phase"],
        round_number=row["round_number"],
    )


async def create_battle(channel_id: int, player_a: int, player_b: int) -> Battle:
    import time
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO battles
               (channel_id, player_a_id, player_b_id, phase, round_number, created_at)
               VALUES (?, ?, ?, 'idle', 0, ?)""",
            (channel_id, player_a, player_b, int(time.time())),
        )
        await db.commit()

    # Copy each player's persistent fleet into a battle-scoped snapshot
    for player in (player_a, player_b):
        persistent = await get_fleet(player, channel_id=None)
        await clear_fleet(player, channel_id=channel_id)
        for stack in persistent:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    """INSERT INTO fleets
                       (owner_id, scope_channel, ship_name, count, count_destroyed,
                        shields_current, hull_current)
                       VALUES (?, ?, ?, ?, 0, ?, ?)""",
                    (
                        player, channel_id, stack.ship.name, stack.count,
                        stack.ship.shields, stack.ship.hull,
                    ),
                )
                await db.commit()

    return (await get_battle(channel_id))  # type: ignore[return-value]


async def set_phase(channel_id: int, phase: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE battles SET phase = ? WHERE channel_id = ?",
            (phase, channel_id),
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
