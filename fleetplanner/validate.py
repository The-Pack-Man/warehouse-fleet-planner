"""
Validator: decides whether a MissionSpec should be planned at all. No LLM.

It runs before the solver and answers one of:
  accept   - plan it (possibly with warnings)
  clarify  - something is ambiguous or unknown; ask the supervisor
  reject   - the request contradicts itself, the site rules, or physics

Every problem carries a plain-language message for the supervisor. Checks are
conservative: deadline checks use a lower bound on travel time (ignoring other
robots), so a rejection means the deadline is impossible, not just tight.
"""
from __future__ import annotations

import difflib
import math
from typing import Literal

from pydantic import BaseModel

from .spec import Charge, KeepClear, MissionSpec, Park, RobotUnavailable, Transport, ZoneClosed
from .world import World

Severity = Literal["reject", "clarify", "warn"]
INF = float("inf")


class Problem(BaseModel):
    code: str
    severity: Severity
    message: str
    ref: str | None = None      # task id, or "constraint[i]"


class Validation(BaseModel):
    status: Literal["accept", "clarify", "reject"]
    problems: list[Problem]

    @property
    def errors(self) -> list[Problem]:
        return [p for p in self.problems if p.severity != "warn"]

    def explain(self) -> str:
        if not self.problems:
            return "Looks good."
        return "\n".join(f"- {p.message}" for p in self.problems)


def _suggest(name: str, options: list[str]) -> str:
    close = difflib.get_close_matches(name, options, n=2, cutoff=0.5)
    return f" Did you mean {' or '.join(close)}?" if close else ""


def _window(world: World, start: str | None, end: str | None) -> tuple[float, float]:
    return (world.clock_to_step(start) if start else -INF, world.clock_to_step(end) if end else INF)


def validate(world: World, spec: MissionSpec) -> Validation:
    problems: list[Problem] = []

    def add(code: str, severity: Severity, message: str, ref: str | None = None) -> None:
        problems.append(Problem(code=code, severity=severity, message=message, ref=ref))

    robots = [r.id for r in world.robots]
    locations = list(world.locations)
    zones = list(world.zones)
    storage = [k for k in locations if not k.startswith(("charger", "home"))]

    # ── constraints ─────────────────────────────────────────────────────────
    closed: list[tuple[str, float, float]] = []
    no_stop: list[tuple[str, float, float]] = []
    unavailable: set[str] = set()
    for i, c in enumerate(spec.constraints):
        ref = f"constraint[{i}]"
        if isinstance(c, RobotUnavailable):
            if c.robot not in robots:
                add("unknown_robot", "clarify", f"There's no robot called {c.robot}.{_suggest(c.robot, robots)}", ref)
            unavailable.add(c.robot)
            continue
        if c.zone not in world.zones:
            add("unknown_zone", "clarify",
                f"I don't know an area called '{c.zone}'.{_suggest(c.zone, zones)}", ref)
            continue
        lo, hi = _window(world, c.start, c.end)
        if hi <= lo:
            add("bad_window", "reject", f"The {c.zone.replace('_', ' ')} rule ends ({c.end}) before it starts ({c.start}).", ref)
            continue
        if hi <= 0:
            add("window_over", "warn", f"The {c.zone.replace('_', ' ')} rule ended at {c.end}, before this shift starts.", ref)
        (closed if isinstance(c, ZoneClosed) else no_stop).append((c.zone, lo, hi))

    def blocking(loc: str, until: float, kinds) -> tuple[str, str] | None:
        """A rule that makes stopping at loc impossible for the whole time from now until `until`."""
        cell = world.locations[loc]
        for (zone, lo, hi), kind in kinds:
            if cell in world.zones[zone] and lo <= 0 and hi >= until:
                return zone, kind
        return None

    rules = [(z, "closed") for z in closed] + [(z, "keep clear") for z in no_stop]

    def rule_text(zone: str, kind: str, until: float) -> str:
        span = "all shift" if until == INF else "until after the deadline"
        return f"{zone.replace('_', ' ')} is {'closed' if kind == 'closed' else 'marked keep clear'} {span}"

    usable_robots = [r for r in world.robots if r.id not in unavailable]
    if spec.tasks and not usable_robots:
        add("no_robots", "reject", "Every robot is out of service, so nothing can be moved.")

    # ── tasks ───────────────────────────────────────────────────────────────
    parks: dict[str, str] = {}
    for t in spec.tasks:
        deadline = world.clock_to_step(t.deadline) if t.deadline else INF
        if t.deadline and deadline <= 0:
            add("deadline_passed", "reject", f"{t.id}: the deadline {t.deadline} is already past (the shift starts {world.start_time}).", t.id)
            continue
        if t.robot and t.robot not in robots:
            add("unknown_robot", "clarify", f"{t.id}: there's no robot called {t.robot}.{_suggest(t.robot, robots)}", t.id)
            continue
        if t.robot and t.robot in unavailable:
            add("robot_unavailable", "reject", f"{t.id} is assigned to {t.robot}, which is out of service.", t.id)
            continue

        if isinstance(t, Transport):
            bad = False
            for role, loc in (("pickup", t.from_location), ("drop-off", t.to_location)):
                if loc not in world.locations:
                    add("unknown_location", "clarify",
                        f"{t.id}: I don't know a location called '{loc}'.{_suggest(loc, locations)}", t.id)
                    bad = True
                elif loc not in storage:
                    add("not_storage", "reject",
                        f"{t.id}: {loc} is a {'charger' if loc.startswith('charger') else 'parking spot'}, not a place to {role} totes.", t.id)
                    bad = True
            if bad:
                continue
            if t.from_location == t.to_location:
                add("same_place", "reject", f"{t.id}: the pickup and drop-off are both {t.from_location}.", t.id)
                continue
            for loc in (t.from_location, t.to_location):
                hit = blocking(loc, deadline, rules)
                if hit:
                    add("location_blocked", "reject",
                        f"{t.id} needs a robot to stop at {loc}, but {rule_text(*hit, deadline)}.", t.id)
                    break
            else:
                _check_deadline(world, t, usable_robots, deadline, add)

        elif isinstance(t, Park):
            if t.location not in world.locations:
                add("unknown_location", "clarify",
                    f"{t.id}: I don't know a location called '{t.location}'.{_suggest(t.location, locations)}", t.id)
                continue
            cell = world.locations[t.location]
            for zone, lo, hi in closed + no_stop:
                if cell in world.zones[zone] and hi == INF:
                    add("location_blocked", "reject",
                        f"{t.id} would leave a robot at {t.location} for good, but {zone.replace('_', ' ')} has a rule with no end time.", t.id)
                    break
            if t.robot:
                if t.robot in parks:
                    add("double_park", "reject", f"{t.robot} is told to park in two places ({parks[t.robot]} and {t.location}).", t.id)
                parks[t.robot] = t.location

        elif isinstance(t, Charge):
            usable = [c for c, cell in world.chargers.items()
                      if not any(cell in world.zones[z] and lo <= 0 and hi == INF for z, lo, hi in closed + no_stop)]
            if not usable:
                add("no_chargers", "reject", f"{t.id}: every charger is in an area that's closed or keep clear all shift.", t.id)
            elif t.robot:
                battery = world.robot(t.robot).battery
                if battery >= t.target_battery:
                    add("already_charged", "warn",
                        f"{t.id}: {t.robot} is already at {battery:.0f}%, at or above the {t.target_battery:.0f}% target.", t.id)

    # ── decision ────────────────────────────────────────────────────────────
    if any(p.severity == "reject" for p in problems):
        status = "reject"
    elif any(p.severity == "clarify" for p in problems):
        status = "clarify"
    else:
        status = "accept"
    return Validation(status=status, problems=problems)


def _check_deadline(world: World, t: Transport, robots, deadline: float, add) -> None:
    """Reject only if even the best-placed robot, alone and unobstructed, can't make the deadline."""
    if deadline == INF:
        return
    src, dst = world.locations[t.from_location], world.locations[t.to_location]
    leg = world.distance(src, dst)
    if leg is None:
        add("unreachable", "reject", f"{t.id}: there's no path between {t.from_location} and {t.to_location}.", t.id)
        return
    best = INF
    for r in robots:
        if t.robot and r.id != t.robot:
            continue
        to_src = world.distance(world.locations[r.home], src)
        if to_src is None:
            continue
        trips = math.ceil(t.quantity / r.capacity)
        best = min(best, to_src + leg * (2 * trips - 1) + 2 * t.quantity)
    if best == INF:
        add("unreachable", "reject", f"{t.id}: no available robot can reach {t.from_location}.", t.id)
    elif best > deadline:
        add("deadline_impossible", "reject",
            f"{t.id}: the earliest any robot could finish is about {world.step_to_clock(int(best))[:5]}, "
            f"after the {t.deadline} deadline.", t.id)
