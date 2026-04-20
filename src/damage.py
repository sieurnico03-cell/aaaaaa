"""Damage resolution.

A resolved round produces a structured DamageReport for each side.
The caller decides:
  - Total outgoing damage (summed from the attacker's live ships, possibly
    with modifiers from the RP text analysis).
  - Which stacks are targeted (from the player's RP post — extracted by the
    Claude API in ai.py).

Damage is applied sequentially: each hit first eats shields, then hull.
When a lead ship's hull hits 0, the stack loses one ship and shields/hull
reset to the class maximum for the next.

Round semantics: one `/battle resolve` = ein Schadensbericht. In einem
Schadensbericht schreibt jede Seite genau eine RP-Nachricht, und der
`damage_per_report`-Wert aus der Schiffstabelle ist der Schaden, den
diese eine Seite in ihrer Nachricht austeilt.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .battle import FleetStack


@dataclass
class StackDamage:
    stack_id: int
    ship_name: str
    shields_lost: int = 0
    hull_lost: int = 0
    ships_destroyed: int = 0
    remaining_count: int = 0
    remaining_shields: int = 0
    remaining_hull: int = 0


@dataclass
class DamageReport:
    attacker_id: int
    defender_id: int
    total_damage: int
    modifier_reason: str = ""
    modifier_percent: int = 0      # e.g. +10 for flanking bonus, -20 for poor tactics
    stack_damages: list[StackDamage] = field(default_factory=list)
    unassigned_damage: int = 0     # damage that had no valid target left


def compute_outgoing_damage(stacks: list[FleetStack]) -> int:
    """Sum the 'damage per report' of all living ships in every stack.

    `damage_per_report` ist der Schaden, den ein Schiff in EINER RP-Nachricht
    austeilt (d.h. in einem Schadensbericht pro Seite). Keine Halbierung.
    """
    total = 0.0
    for stack in stacks:
        if not stack.is_alive:
            continue
        total += stack.ship.damage_per_report * stack.count
    return int(total)


def apply_modifier(base: int, modifier_percent: int) -> int:
    return max(0, int(round(base * (1 + modifier_percent / 100))))


def _destroy_one_and_reset(stack: FleetStack) -> None:
    stack.count -= 1
    stack.count_destroyed += 1
    if stack.count > 0:
        stack.shields_current = stack.ship.shields
        stack.hull_current = stack.ship.hull
    else:
        stack.shields_current = 0
        stack.hull_current = 0


def apply_damage_to_stack(stack: FleetStack, damage: int) -> StackDamage:
    """Apply *damage* to a stack. Mutates the stack in place.

    Shields absorb first, then hull. When a lead ship's hull is depleted,
    the stack loses one ship and the next one becomes the damage sink
    (fresh shields + hull), continuing until the damage is consumed or
    the stack is wiped out.
    """
    result = StackDamage(stack_id=stack.id, ship_name=stack.ship.name)
    remaining = damage

    while remaining > 0 and stack.count > 0:
        if stack.shields_current > 0:
            absorbed = min(remaining, stack.shields_current)
            stack.shields_current -= absorbed
            remaining -= absorbed
            result.shields_lost += absorbed
            continue

        if stack.hull_current > 0:
            absorbed = min(remaining, stack.hull_current)
            stack.hull_current -= absorbed
            remaining -= absorbed
            result.hull_lost += absorbed
            if stack.hull_current == 0:
                _destroy_one_and_reset(stack)
                result.ships_destroyed += 1
            continue

        # Should not happen — safety net
        break

    result.remaining_count = stack.count
    result.remaining_shields = stack.shields_current
    result.remaining_hull = stack.hull_current
    return result


def distribute_damage(
    damage: int,
    targets: list[FleetStack],
) -> tuple[list[StackDamage], int]:
    """Apply *damage* across a list of target stacks in order.

    Each stack absorbs as much as it can (shields + hull of all its ships),
    then overflow rolls to the next. Returns the per-stack damage records
    and any damage that had no target (all targets destroyed).
    """
    reports: list[StackDamage] = []
    remaining = damage
    for stack in targets:
        if remaining <= 0:
            break
        if not stack.is_alive:
            continue
        before_alive_damage = remaining
        report = apply_damage_to_stack(stack, remaining)
        reports.append(report)
        spent = report.shields_lost + report.hull_lost
        remaining = before_alive_damage - spent
        # If the stack wasn't fully destroyed, it absorbed everything.
        if stack.is_alive:
            remaining = 0
            break

    return reports, max(0, remaining)
