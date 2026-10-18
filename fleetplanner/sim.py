"""
Step-by-step simulator. It does not trust the planner.

A plan is a list of actions per robot, one per time step:
  N S E W        move one cell
  wait           stay put
  pick:<task>    load one tote for a transport task (at its from_location)
  drop:<task>    unload one tote for that task (at its to_location)
  charge         charge at a charger

The simulator replays the actions and checks everything itself: walls, collisions,
closed zones, keep-clear zones, unavailable robots, battery, cargo, and task
completion against deadlines. A robot with no actions left parks in low-power
mode: it stays in place and its battery no longer drains. Waits inside a plan
still cost idle battery.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from .spec import Charge, KeepClear, MissionSpec, Park, RobotUnavailable, Transport, ZoneClosed
from .world import MOVES, Cell, World


class Plan(BaseModel):
    actions: dict[str, list[str]]
    assignments: dict[str, str] = Field(default_factory=dict, description="task id -> robot id")
    meta: dict[str, Any] = Field(default_factory=dict)

    @property
    def length(self) -> int:
        return max((len(a) for a in self.actions.values()), default=0)


@dataclass
class RobotState:
    id: str
    pos: Cell
    battery: float
    capacity: int
    cargo: list[str] = field(default_factory=list)
    dead: bool = False
    distance: int = 0


def _window(world: World, start: str | None, end: str | None) -> tuple[float, float]:
    lo = world.clock_to_step(start) if start else float("-inf")
    hi = world.clock_to_step(end) if end else float("inf")
    return lo, hi


def simulate(world: World, spec: MissionSpec, plan: Plan, extra_steps: int = 0) -> dict[str, Any]:
    robots = {r.id: RobotState(r.id, world.locations[r.home], float(r.battery), r.capacity)
              for r in world.robots}
    unknown = set(plan.actions) - set(robots)
    horizon = plan.length + extra_steps

    closed = [(c.zone, *_window(world, c.start, c.end)) for c in spec.constraints if isinstance(c, ZoneClosed)]
    clear = [(c.zone, *_window(world, c.start, c.end)) for c in spec.constraints if isinstance(c, KeepClear)]
    unavailable = {c.robot for c in spec.constraints if isinstance(c, RobotUnavailable)}

    tasks = {t.id: t for t in spec.tasks}
    picked: dict[str, int] = defaultdict(int)
    delivered: dict[str, int] = defaultdict(int)
    done_step: dict[str, int] = {}
    park_since: dict[str, int | None] = {}

    violations: list[dict[str, Any]] = []
    open_viol: dict[tuple, dict] = {}
    events: list[dict[str, Any]] = []
    frames: list[dict[str, list]] = []

    def snapshot() -> dict[str, list]:
        return {r.id: [r.pos[0], r.pos[1], round(r.battery, 2), len(r.cargo)] for r in robots.values()}

    def violate(step: int, kind: str, robot: str, detail: str) -> None:
        """Record a violation, merging consecutive steps of the same one into a span."""
        key = (kind, robot, detail)
        v = open_viol.get(key)
        if v and v["end_step"] == step - 1:
            v["end_step"] = step
            return
        v = {"kind": kind, "robot": robot, "detail": detail, "start_step": step, "end_step": step}
        open_viol[key] = v
        violations.append(v)

    for rid in sorted(unknown):
        violate(0, "invalid_action", rid, "unknown robot in plan")

    frames.append(snapshot())
    for i in range(horizon):
        step = i + 1
        moved: dict[str, tuple[Cell, Cell]] = {}

        for r in robots.values():
            action = plan.actions.get(r.id, [])
            finished = i >= len(action)
            act = "wait" if finished else action[i]
            if r.dead:
                continue
            if r.id in unavailable and act != "wait":
                violate(step, "unavailable_robot_used", r.id, act)
            prev = r.pos

            if act in MOVES:
                dx, dy = MOVES[act]
                nxt = (r.pos[0] + dx, r.pos[1] + dy)
                if world.is_free(nxt):
                    r.pos = nxt
                    r.distance += 1
                    r.battery -= world.battery.move_cost
                else:
                    violate(step, "invalid_move", r.id, f"{act} into blocked cell {list(nxt)}")
                    r.battery -= world.battery.idle_cost
            elif act == "wait":
                if not finished:
                    r.battery -= world.battery.idle_cost
            elif act == "charge":
                if r.pos in world.chargers.values():
                    r.battery = min(100.0, r.battery + world.battery.charge_rate)
                    events.append({"step": step, "robot": r.id, "kind": "charge", "battery": round(r.battery, 2)})
                else:
                    violate(step, "invalid_action", r.id, "charge away from a charger")
            elif act.startswith(("pick:", "drop:")):
                kind, tid = act.split(":", 1)
                t = tasks.get(tid)
                r.battery -= world.battery.idle_cost
                if not isinstance(t, Transport):
                    violate(step, "invalid_action", r.id, f"{act}: no such transport task")
                elif t.robot and t.robot != r.id:
                    violate(step, "invalid_action", r.id, f"{act}: task is pinned to {t.robot}")
                elif kind == "pick":
                    if r.pos != world.locations.get(t.from_location):
                        violate(step, "invalid_action", r.id, f"{act} away from {t.from_location}")
                    elif len(r.cargo) >= r.capacity:
                        violate(step, "invalid_action", r.id, f"{act} over capacity")
                    elif picked[tid] >= t.quantity:
                        violate(step, "invalid_action", r.id, f"{act}: all {t.quantity} already picked")
                    else:
                        picked[tid] += 1
                        r.cargo.append(tid)
                        events.append({"step": step, "robot": r.id, "kind": "pick", "task": tid})
                else:
                    if r.pos != world.locations.get(t.to_location):
                        violate(step, "invalid_action", r.id, f"{act} away from {t.to_location}")
                    elif tid not in r.cargo:
                        violate(step, "invalid_action", r.id, f"{act} without cargo")
                    else:
                        r.cargo.remove(tid)
                        delivered[tid] += 1
                        events.append({"step": step, "robot": r.id, "kind": "drop", "task": tid})
                        if delivered[tid] == t.quantity:
                            done_step[tid] = step
            else:
                violate(step, "invalid_action", r.id, f"unknown action {act!r}")
                r.battery -= world.battery.idle_cost

            if r.battery <= 0:
                r.battery, r.dead = 0.0, True
                violate(step, "battery_depleted", r.id, "battery reached 0%")
            moved[r.id] = (prev, r.pos)

            stationary = prev == r.pos
            for zone, lo, hi in clear:
                if stationary and lo <= step < hi and r.pos in world.zones.get(zone, ()):
                    violate(step, "keep_clear", r.id, zone)

        # Zone closures apply to every robot, including dead or idle ones.
        for r in robots.values():
            for zone, lo, hi in closed:
                if lo <= step < hi and r.pos in world.zones.get(zone, ()):
                    violate(step, "closed_zone", r.id, zone)

        # Collisions: two robots in one cell, or two robots swapping cells.
        by_cell: dict[Cell, list[str]] = defaultdict(list)
        for r in robots.values():
            by_cell[r.pos].append(r.id)
        for cell, ids in by_cell.items():
            if len(ids) > 1:
                violate(step, "collision", "+".join(sorted(ids)), f"same cell {list(cell)}")
        ids = sorted(moved)
        for a_i, a in enumerate(ids):
            for b in ids[a_i + 1:]:
                (a0, a1), (b0, b1) = moved[a], moved[b]
                if a0 != a1 and a0 == b1 and a1 == b0:
                    violate(step, "collision", f"{a}+{b}", f"swapped {list(a0)}<->{list(a1)}")

        # Charge and park tasks complete when their condition first holds (park must also hold at the end).
        for t in tasks.values():
            rid = t.robot or plan.assignments.get(t.id)
            candidates = [robots[rid]] if rid in robots else list(robots.values())
            if isinstance(t, Charge) and t.id not in done_step:
                if any(c.battery >= t.target_battery and c.pos in world.chargers.values() for c in candidates):
                    done_step[t.id] = step
            elif isinstance(t, Park):
                here = any(c.pos == world.locations.get(t.location) for c in candidates)
                if here and park_since.get(t.id) is None:
                    park_since[t.id] = step
                elif not here:
                    park_since[t.id] = None

        frames.append(snapshot())

    for tid, since in park_since.items():
        if since is not None:
            done_step[tid] = since

    task_report = {}
    for t in spec.tasks:
        deadline = world.clock_to_step(t.deadline) if t.deadline else None
        step = done_step.get(t.id)
        complete = step is not None
        task_report[t.id] = {
            "type": t.type,
            "complete": complete,
            "completion_step": step,
            "completion_time": world.step_to_clock(step) if complete else None,
            "deadline_step": deadline,
            "on_time": complete and (deadline is None or step <= deadline),
            **({"delivered": delivered[t.id], "quantity": t.quantity} if isinstance(t, Transport) else {}),
        }

    counts: dict[str, int] = defaultdict(int)
    for v in violations:
        counts[v["kind"]] += 1
    all_done = all(r["complete"] for r in task_report.values())
    all_on_time = all(r["on_time"] for r in task_report.values())

    return {
        "world": world.summary(),
        "spec": spec.model_dump(),
        "plan": plan.model_dump(),
        "frames": frames,
        "events": events,
        "violations": violations,
        "tasks": task_report,
        "metrics": {
            "success": all_done and all_on_time and not violations,
            "tasks_total": len(task_report),
            "tasks_complete": sum(r["complete"] for r in task_report.values()),
            "tasks_on_time": sum(r["on_time"] for r in task_report.values()),
            "violations": dict(counts),
            "violation_count": len(violations),
            "makespan_steps": horizon,
            "makespan_time": world.step_to_clock(horizon),
            "distance": {r.id: r.distance for r in robots.values()},
            "final_battery": {r.id: round(r.battery, 2) for r in robots.values()},
        },
    }
