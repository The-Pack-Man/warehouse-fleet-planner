"""
Grounding check: every value in the LLM's spec must trace back to the request.

Deterministic, no LLM. Runs after translation and before site rules are merged.
For each kind of value it either confirms it, corrects it when the request makes
the right value unambiguous, or stops with a question or refusal. Corrections
are always reported, never applied silently.

  times       deadlines need a deadline phrase ("by 2:30", "within 20 minutes");
              rule windows need a window phrase ("until 2:40", "from 14:30 to 14:45")
  robots      a robot named in the request must be in the spec; an out-of-service
              robot named in the request means refuse; a robot the request never
              named is unpinned
  places      every location and area in the spec must be mentioned in the request
  quantities  every transport quantity must be stated in the request
  overrides   "ignore / reopen / turn off ..." aimed at a site rule means refuse
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from .spec import Charge, KeepClear, MissionSpec, Park, RobotUnavailable, Transport, ZoneClosed
from .world import World

NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "a": 1, "an": 1}
HOUR_WORDS = {w: n for w, n in NUMBER_WORDS.items() if n <= 12 and w not in ("a", "an")}


@dataclass
class Grounding:
    status: Literal["ok", "clarify", "refuse"]
    spec: MissionSpec | None
    notes: list[str] = field(default_factory=list)    # corrections made, told to the supervisor
    message: str | None = None                        # question or refusal reason


# ── time phrases ──────────────────────────────────────────────────────────────

_HOURS = "|".join(HOUR_WORDS)
TIME = rf"(?:(?:quarter past|half past|quarter to)\s+(?:{_HOURS}|\d{{1,2}})|\d{{1,2}}(?::\d{{2}})?(?:\s*(?:pm|p\.m\.))?)"
DURATION = rf"(?:(?:\d+|{'|'.join(NUMBER_WORDS)})\s+(?:minutes?|mins?|hours?)|half an hour|half hour|an hour|hour)"


def _fmt(m: int) -> str:
    return f"{m // 60 % 24:02d}:{m % 60:02d}"


def parse_time(s: str, shift_start_min: int) -> int:
    """Clock time during the shift, in minutes. With an afternoon shift, 2:30 means 14:30."""
    s = s.strip().lower()
    m = re.match(rf"(quarter past|half past|quarter to)\s+(\w+)", s)
    if m:
        h = HOUR_WORDS.get(m.group(2)) or int(m.group(2))
        offset = {"quarter past": 15, "half past": 30, "quarter to": -15}[m.group(1)]
        mm = 0
    else:
        h, mm, offset = int(re.match(r"(\d{1,2})", s).group(1)), int((re.search(r":(\d{2})", s) or [0, 0])[1]), 0
    if h < 12 and shift_start_min >= 12 * 60:
        h += 12
    return h * 60 + mm + offset


def parse_duration(s: str) -> int:
    s = s.lower()
    if "half" in s:
        return 30
    m = re.match(rf"(\d+|{'|'.join(NUMBER_WORDS)})\s+(minutes?|mins?|hours?)", s)
    if not m:
        return 60                                   # "an hour", "the hour"
    n = NUMBER_WORDS.get(m.group(1)) or int(m.group(1))
    return n * 60 if m.group(2).startswith("hour") else n


def time_phrases(text: str, shift_start: str) -> dict[str, set[str]]:
    """Deadline times and rule-window start/end times stated in a request."""
    sh, sm = map(int, shift_start.split(":"))
    start = sh * 60 + sm
    t = text.lower()
    out: dict[str, set[str]] = {"deadline": set(), "start": set(), "end": set()}
    for m in re.finditer(rf"\b(?:by|before|no later than)\s+({TIME})\b(?!\s*(?:totes?|more|each))", t):
        out["deadline"].add(_fmt(parse_time(m.group(1), start)))
    for m in re.finditer(rf"\b(?:within|in the next|in)\s+(?:the\s+)?({DURATION})", t):
        out["deadline"].add(_fmt(start + parse_duration(m.group(1))))
    for m in re.finditer(rf"\b(?:from|between)\s+({TIME})\s+(?:to|and|until|till)\s+({TIME})", t):
        out["start"].add(_fmt(parse_time(m.group(1), start)))
        out["end"].add(_fmt(parse_time(m.group(2), start)))
    for m in re.finditer(rf"\b(?:until|till|through)\s+({TIME})", t):
        out["end"].add(_fmt(parse_time(m.group(1), start)))
    for m in re.finditer(rf"\b(?:after|starting at)\s+({TIME})", t):
        out["start"].add(_fmt(parse_time(m.group(1), start)))
    return out


# ── names, robots, numbers ────────────────────────────────────────────────────

def mentioned_places(text: str, world: World) -> tuple[set[str], set[str]]:
    """Locations and zones a request refers to, by name or plain description."""
    t = text.lower()
    locs, zones = set(), set()
    for name in world.locations:
        if re.search(r"(?<![\w-])" + re.escape(name.lower()) + r"(?![\w-])", t):
            locs.add(name)
    for n in re.findall(r"\baisle\s*(\d+)", t) + re.findall(r"\ba(\d+)(?![\w-])", t):
        locs |= {k for k in world.locations if k.startswith(f"A{n}-")}
        zones |= {z for z in world.zones if z == f"aisle_{n}"}
    for n in (re.findall(r"\bpick(?:ing)?(?:\s+station)?\s*(\d+)(?!\s*(?:totes?|more|each))", t)
              + re.findall(r"\bstation\s*(\d+)", t)):
        locs |= {k for k in world.locations if k == f"pick_{n}"}
    for n in re.findall(r"\bcharger\s*(\d+)", t):
        locs |= {k for k in world.locations if k == f"charger_{n}"}
    if re.search(r"\bdock\b", t):
        locs |= {k for k in world.locations if k == "dock"}
        zones |= {z for z in world.zones if z == "dock_area"}
    phrases = {"charging_bay": r"charging (?:bay|lane|area)|chargers", "east_lane": r"east (?:lane|wall|side)",
               "north_corridor": r"north (?:corridor|side)", "south_corridor": r"south (?:corridor|side)",
               "dock_area": r"dock area"}
    for z, pat in phrases.items():
        if z in world.zones and re.search(pat, t):
            zones.add(z)
    for z in world.zones:
        if z.replace("_", " ") in t or z in t:
            zones.add(z)
    return locs, zones


def mentioned_robots(text: str) -> set[str]:
    return set(re.findall(r"\b(R\d+)\b", text))


def stated_numbers(text: str, world: World) -> list[int]:
    """Quantities stated in the request, after removing names, times, and robots that contain digits."""
    t = text.lower()
    for name in sorted(world.locations, key=len, reverse=True):
        t = re.sub(r"(?<![\w-])" + re.escape(name.lower()) + r"(?![\w-])", " ", t)
    t = re.sub(r"\b(?:aisle|a|pick(?:ing)?(?:\s+station)?|station|charger)\s*\d+(?:-\d+)?(?:'s)?"
               r"(?!\s*(?:totes?|more|each))", " ", t)
    t = re.sub(r"\br\d+\b", " ", t)
    t = re.sub(r"\b\d{1,2}:\d{2}\b", " ", t)
    t = re.sub(r"\b(?:by|before|until|till|after|from|to|at)\s+\d{1,2}\b(?!\s*(?:totes?|more|each))", " ", t)
    t = re.sub(r"\b\d+\s*(?:%|percent)", " ", t)
    t = re.sub(r"\b(?:\d+|" + "|".join(NUMBER_WORDS) + r")\s+(?:minutes?|mins?|hours?)\b", " ", t)
    t = re.sub(r"\b(?:quarter|half)\s+(?:past|to)\s+\w+", " ", t)
    nums = [int(n) for n in re.findall(r"\b(\d+)\b", t)]
    nums += [NUMBER_WORDS[w] for w in re.findall(r"\b(" + "|".join(w for w in NUMBER_WORDS if w not in ("a", "an")) +
                                                 r")\b", t)]
    if re.search(r"\b(?:a|an)\s+(?:single\s+)?tote\b", t):
        nums.append(1)
    return nums


OVERRIDE = re.compile(r"\b(ignore|disregard|don'?t worry about|never mind|forget(?: about)?|turn off|switch off|"
                      r"disable|lift|remove|reopen|re-open|override|bypass|skip|cancel)\b")
RULE_WORDS = re.compile(r"\b(closures?|closed|rules?|safety|maintenance|out of service|keep[- ]clear|restrictions?)\b")


# ── the check ─────────────────────────────────────────────────────────────────

def ground(world: World, request: str, spec: MissionSpec, standing: list[Any]) -> Grounding:
    notes: list[str] = []
    text = request

    # 1. Requests to weaken a site rule are refused outright.
    closed_zones = {c.zone for c in standing if isinstance(c, (ZoneClosed, KeepClear))}
    _, zones_named = mentioned_places(text, world)
    m = OVERRIDE.search(text.lower())
    if m and (RULE_WORDS.search(text.lower()) or zones_named & closed_zones):
        return Grounding("refuse", None, notes,
                         f"The request asks to set aside a site rule (\"{m.group(1)}\"). Site rules can only be changed "
                         "by a site lead, not from a request.")

    # 2. Robots.
    out_of_service = {c.robot for c in standing if isinstance(c, RobotUnavailable)}
    named = mentioned_robots(text)
    fleet = {r.id for r in world.robots}
    if named & out_of_service:
        r = sorted(named & out_of_service)[0]
        return Grounding("refuse", None, notes, f"{r} is out of service under a site rule, so it can't be used.")
    unknown = named - fleet
    if unknown:
        return Grounding("clarify", None, notes, f"There's no robot called {sorted(unknown)[0]}. Which robot did you mean?")

    tasks = [t.model_copy() for t in spec.tasks]
    constraints = list(spec.constraints)
    declared_down = {c.robot for c in constraints if isinstance(c, RobotUnavailable)}
    for i, t in enumerate(tasks):
        if t.robot and t.robot not in named:
            notes.append(f"{t.id}: the request didn't name a robot, so I let the planner choose instead of {t.robot}.")
            tasks[i] = t.model_copy(update={"robot": None})
    for r in sorted(named - declared_down):
        if any(t.robot == r for t in tasks):
            continue
        free = [i for i, t in enumerate(tasks) if t.robot is None]
        if len(tasks) == 1 and free:
            tasks[0] = tasks[0].model_copy(update={"robot": r})
            notes.append(f"{tasks[0].id}: assigned to {r}, as the request asked.")
        else:
            return Grounding("clarify", None, notes, f"You mentioned {r}. Which job should {r} do?")

    # 3. Places.
    locs, zones = mentioned_places(text, world)
    for t in tasks:
        places = ([t.from_location, t.to_location] if isinstance(t, Transport)
                  else [t.location] if isinstance(t, Park) else [])
        for p in places:
            if p not in locs and p in world.locations:
                label = world.descriptions.get(p, p)
                return Grounding("clarify", None, notes,
                                 f"The request doesn't say {label} ({p}). Where exactly should this happen?")
    site = {c.model_dump_json() for c in standing}
    for c in constraints:
        if isinstance(c, (ZoneClosed, KeepClear)) and c.model_dump_json() not in site and c.zone not in zones:
            return Grounding("clarify", None, notes,
                             f"The request doesn't mention {c.zone.replace('_', ' ')}. Which area should this rule cover?")

    # 4. Quantities.
    numbers = stated_numbers(text, world)
    for i, t in enumerate(tasks):
        if isinstance(t, Transport) and t.quantity not in numbers:
            transports = [x for x in tasks if isinstance(x, Transport)]
            if len(numbers) == 1 and len(transports) == 1:
                notes.append(f"{t.id}: the request says {numbers[0]} totes, not {t.quantity}; using {numbers[0]}.")
                tasks[i] = t.model_copy(update={"quantity": numbers[0]})
            else:
                return Grounding("clarify", None, notes, "How many totes should be moved?" if not numbers else
                                 f"The request doesn't say {t.quantity} totes for {t.from_location} to {t.to_location}. How many?")

    # 5. Times.
    times = time_phrases(text, world.start_time)
    deadlines = times["deadline"]
    for i, t in enumerate(tasks):
        if t.deadline and t.deadline not in deadlines:
            if len(deadlines) == 1:
                d = next(iter(deadlines))
                notes.append(f"{t.id}: read the request's deadline as {d}, not {t.deadline}.")
                tasks[i] = t.model_copy(update={"deadline": d})
            elif not deadlines:
                notes.append(f"{t.id}: the request gave no deadline, so I removed {t.deadline}.")
                tasks[i] = t.model_copy(update={"deadline": None})
            else:
                return Grounding("clarify", None, notes, f"Which deadline applies to {t.id}: {', '.join(sorted(deadlines))}?")
    if deadlines and not any(t.deadline for t in tasks):
        work = [i for i, t in enumerate(tasks) if not isinstance(t, Park)]
        if len(deadlines) == 1 and len(work) == 1:
            d = next(iter(deadlines))
            tasks[work[0]] = tasks[work[0]].model_copy(update={"deadline": d})
            notes.append(f"{tasks[work[0]].id}: added the request's deadline, {d}.")
        else:
            return Grounding("clarify", None, notes, f"Which job is due by {', '.join(sorted(deadlines))}?")

    shift = world.start_time
    for i, c in enumerate(constraints):
        if not isinstance(c, (ZoneClosed, KeepClear)) or c.model_dump_json() in site:
            continue
        update = {}
        for key, found in (("start", times["start"]), ("end", times["end"])):
            v = getattr(c, key)
            if key == "start" and v is not None and v <= shift:
                continue
            if v is not None and v not in found:
                if len(found) == 1:
                    update[key] = next(iter(found))
                    notes.append(f"{c.zone}: read the rule's {key} as {update[key]}, not {v}.")
                elif not found:
                    update[key] = None
                    notes.append(f"{c.zone}: the request gave no {key} time, so I removed {v}.")
                else:
                    return Grounding("clarify", None, notes, f"When should the {c.zone.replace('_', ' ')} rule {key}?")
            elif v is None and len(found) == 1 and not (key == "start" and next(iter(found)) <= shift):
                update[key] = next(iter(found))
                notes.append(f"{c.zone}: added the request's {key} time, {update[key]}.")
        if update:
            constraints[i] = c.model_copy(update=update)

    return Grounding("ok", MissionSpec(tasks=tasks, constraints=constraints), notes)
