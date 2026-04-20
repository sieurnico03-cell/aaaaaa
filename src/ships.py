"""Ship database — loads ships.json, provides lookup and fuzzy search."""
from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from difflib import get_close_matches
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "ships.json"
IMAGES_PATH = Path(__file__).resolve().parent.parent / "data" / "ship_images.json"


@dataclass(frozen=True)
class Ship:
    name: str
    faction: str
    category: str
    shields: int
    hull: int
    speed_mglt: int | None
    maneuverability_dpf: int | None
    fighter_capacity: int
    damage_per_report: float
    optimal_angle: str
    primary_weapons: str
    secondary_weapons: str
    hangar_description: str
    notes: str
    image_url: str | None = None

    @property
    def total_hp(self) -> int:
        return self.shields + self.hull


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower().strip()


def _load_image_map(path: Path) -> dict[str, str]:
    """Mapping Schiffsname → Bild-URL. Fehlende Datei ist okay."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {_normalize(k): v for k, v in raw.items() if isinstance(v, str) and v.strip()}


class ShipDatabase:
    def __init__(
        self,
        path: Path = DATA_PATH,
        images_path: Path = IMAGES_PATH,
    ) -> None:
        raw = json.loads(path.read_text(encoding="utf-8"))
        image_map = _load_image_map(images_path)
        ships: list[Ship] = []
        for entry in raw:
            # tolerate older ships.json without image_url
            entry.setdefault("image_url", None)
            if not entry["image_url"]:
                entry["image_url"] = image_map.get(_normalize(entry["name"]))
            ships.append(Ship(**entry))
        self._ships = ships
        self._by_norm: dict[str, Ship] = {_normalize(s.name): s for s in self._ships}

    def all(self) -> list[Ship]:
        return list(self._ships)

    def get(self, name: str) -> Ship | None:
        """Exact match (case/diacritic insensitive)."""
        return self._by_norm.get(_normalize(name))

    def find(self, query: str, limit: int = 10) -> list[Ship]:
        """Fuzzy search for ships matching *query*.

        Matches: exact (normalized), substring, then difflib close matches.
        Returns up to *limit* results, unique, ordered by match quality.
        """
        q = _normalize(query)
        if not q:
            return self._ships[:limit]

        seen: set[str] = set()
        out: list[Ship] = []

        # Exact
        if q in self._by_norm:
            ship = self._by_norm[q]
            out.append(ship)
            seen.add(ship.name)

        # Substring
        for ship in self._ships:
            if ship.name in seen:
                continue
            if q in _normalize(ship.name):
                out.append(ship)
                seen.add(ship.name)
                if len(out) >= limit:
                    return out

        # Close matches on normalized names
        if len(out) < limit:
            candidates = [n for n in self._by_norm if n not in {_normalize(s.name) for s in out}]
            for match in get_close_matches(q, candidates, n=limit - len(out), cutoff=0.5):
                ship = self._by_norm[match]
                if ship.name not in seen:
                    out.append(ship)
                    seen.add(ship.name)

        return out[:limit]

    def by_faction(self, faction: str) -> list[Ship]:
        f = _normalize(faction)
        return [s for s in self._ships if _normalize(s.faction) == f]


# Singleton — imported across the bot
SHIPS = ShipDatabase()
