"""Parse raw CSV exports (Republic + CIS) into a normalized ships.json.

Run once whenever the Google Sheets is updated:
    python scripts/build_ship_data.py
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

SOURCES = {
    "Republic": DATA / "republic.csv",
    "CIS": DATA / "cis.csv",
}

CATEGORY_MARKER = "▸"
HEADER_ROW_FIRST_CELL = "Schiffsklasse"


def _num(text: str) -> float | None:
    """Extract a number like '14.500', '4,845', '52,5' — German number format."""
    if not text:
        return None
    m = re.search(r"\d[\d.,]*", text.replace("\u00a0", " "))
    if not m:
        return None
    raw = m.group(0)
    # Heuristic: if it contains both '.' and ',', the ',' is decimal (German).
    if "." in raw and "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        # Either thousand separator or decimal. If there are exactly one/two
        # digits after the comma, treat as decimal.
        left, _, right = raw.partition(",")
        if 0 < len(right) <= 2:
            raw = f"{left.replace('.', '')}.{right}"
        else:
            raw = raw.replace(",", "").replace(".", "")
    else:
        raw = raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return None


def _extract_stat(grunddaten: str, unit: str) -> float | None:
    """Pull numbers like '14.500 SBD', '3,050 RU', '60 MGLT' out of the stats column."""
    m = re.search(r"([\d][\d.,]*)\s*" + unit, grunddaten, re.IGNORECASE)
    if not m:
        return None
    return _num(m.group(1))


def _extract_hangar(hangar_text: str) -> int:
    """Pull out the max Sternenjäger count if present."""
    if not hangar_text or hangar_text.strip() in {"/", "-"}:
        return 0
    m = re.search(r"(?:max\.?\s*)?(\d[\d.,]*)\s*Sternenjäger", hangar_text)
    if m:
        val = _num(m.group(1))
        return int(val) if val else 0
    return 0


def _parse_csv(path: Path, faction: str) -> list[dict]:
    ships: list[dict] = []
    current_category = "Unknown"

    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        rows = list(reader)

    i = 0
    while i < len(rows):
        row = rows[i]
        if not row or not any(cell.strip() for cell in row):
            i += 1
            continue

        first = row[0].strip()

        if first.startswith(CATEGORY_MARKER):
            current_category = first.lstrip(CATEGORY_MARKER).strip().title()
            i += 1
            continue

        if first.startswith(HEADER_ROW_FIRST_CELL):
            i += 1
            continue

        # Skip title rows like "⚔ GALAKTISCHE REPUBLIK"
        if first.startswith("⚔") or "Flottenübersicht" in first:
            i += 1
            continue

        # Handle rows that got split across multiple lines in the CSV
        # by joining until we have at least 10 columns worth of content.
        while len(row) < 10 and i + 1 < len(rows):
            next_row = rows[i + 1]
            row = row[:-1] + [row[-1] + " " + (next_row[0] if next_row else "")] + next_row[1:]
            i += 1

        if len(row) < 10:
            i += 1
            continue

        name = row[0].strip().strip('"').replace("\n", " ")
        if not name:
            i += 1
            continue

        grunddaten = row[1]
        sbd = _extract_stat(grunddaten, "SBD") or 0
        ru = _extract_stat(grunddaten, "RU") or 0
        mglt = _extract_stat(grunddaten, "MGLT") or 0
        dpf = _extract_stat(grunddaten, "DPF") or 0

        hangar = _extract_hangar(row[4])
        # Spalte 6 ("Schaden bei opt. Feuerwinkel") ist laut Sheet-Autor der
        # maßgebliche Schaden-pro-Schadensbericht-Wert. Spalte 8
        # ("Damage pro Schadensbericht") ist im Export teilweise abweichend.
        damage_raw = row[6].strip()
        damage = _num(damage_raw) or 0

        ship = {
            "name": name,
            "faction": faction,
            "category": current_category,
            "shields": int(sbd),
            "hull": int(ru),
            "speed_mglt": int(mglt) if mglt else None,
            "maneuverability_dpf": int(dpf) if dpf else None,
            "fighter_capacity": hangar,
            "damage_per_report": damage,
            "optimal_angle": row[7].strip(),
            "primary_weapons": row[2].strip(),
            "secondary_weapons": row[3].strip(),
            "hangar_description": row[4].strip(),
            "notes": row[9].strip(),
        }
        ships.append(ship)
        i += 1

    return ships


def main() -> None:
    all_ships: list[dict] = []
    for faction, path in SOURCES.items():
        if not path.exists():
            raise SystemExit(f"Missing CSV: {path}")
        parsed = _parse_csv(path, faction)
        print(f"Parsed {len(parsed):3d} ships for {faction}")
        all_ships.extend(parsed)

    out_path = DATA / "ships.json"
    out_path.write_text(
        json.dumps(all_ships, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {len(all_ships)} ships -> {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
