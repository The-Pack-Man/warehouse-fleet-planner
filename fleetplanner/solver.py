"""
Classical planner: no LLM involved.

1. Allocation: tasks are taken in priority/deadline order and given to the
   robot that can finish them soonest. Charging stops are inserted when a
   robot would otherwise drop below its battery reserve.
2. Routing: prioritized planning. Each robot's full itinerary is routed with
   space-time A* against a reservation table of the robots already planned,
   so robots never share a cell or swap cells. Closed zones are blocked during
   their windows, and robots may not stop inside keep-clear zones.

The planner reports problems it notices (unreachable locations, predicted late
tasks) in plan.meta instead of failing silently. Whether a request should be
planned at all is the validator's job, not this module's.
"""
from __future__ import annotations

import heapq
import itertools
import math
from dataclasses import dataclass, field

from .sim import Plan
from .spec import Charge, KeepClear, MissionSpec, Park, RobotUnavailable, Transport, ZoneClosed
from .world import MOVES, Cell, World

INF = float("inf")
DIRS = {v: k for k, v in MOVES.items()}


class PlanningError(Exception):
    pass


class _LegFailed(Exception):
    def __init__(self, index: int, reason: str):
        super().__init__(reason)
        self.index, self.reason = index, reason


@dataclass
class Leg:
    goal: Cell
    dwell: list[str]          # actions performed at the goal, one per step
    task: str | None = None
    charge_target: float | None = None   # for charging legs: dwell is resized from the real arrival battery


@dataclass
class Itinerary:
    robot: str
    legs: list[Leg] = field(default_factory=list)
    rest: Cell | None = None  # where the robot ends up
    parked_by_task: bool = False


class Rules:
    """Time-dependent constraints, compiled to fast lookups."""

    def __init__(self, world: World, spec: MissionSpec):
        self.world = world
        self.closed: list[tuple[frozenset[Cell], float, float]] = []
        self.no_stop: list[tuple[frozenset[Cell], float, float]] = []
        for c in spec.constraints:
            if isinstance(c, (ZoneClosed, KeepClear)):
                lo = world.clock_to_step(c.start) if c.start else -INF
                hi = world.clock_to_step(c.end) if c.end else INF
                cells = world.zones.get(c.zone, frozenset())
                (self.closed if isinstance(c, ZoneClosed) else self.no_stop).append((cells, lo, hi))

    def is_closed(self, cell: Cell, t: int) -> bool:
        return any(lo <= t < hi and cell in cells for cells, lo, hi in self.closed)

    def can_stop(self, cell: Cell, t: int) -> bool:
        return not any(lo <= t < hi and cell in cells for cells, lo, hi in self.no_stop)

    def _exit_distance(self, cells: frozenset[Cell]) -> dict[Cell, int]:
        """Steps from each cell in a zone to the nearest cell outside it."""
        cache = self.__dict__.setdefault("_exit_cache", {})
        if cells not in cache:
            outside = [c for c in self.world.free_cells if c not in cells]
            dist = {c: 0 for c in outside}
            frontier = list(outside)
            while frontier:
                nxt = []
                for c in frontier:
                    for n in self.world.neighbors(c):
                        if n not in dist:
                            dist[n] = dist[c] + 1
                            nxt.append(n)
                frontier = nxt
            cache[cells] = dist
        return cache[cells]

    def trapped_after(self, cell: Cell, t: int, margin: int = 2) -> bool:
        """Would a robot still at `cell` at time t be caught inside a zone that closes soon?"""
        for cells, lo, hi in self.closed:
            if cell in cells and t < lo:
                if t + self._exit_distance(cells).get(cell, 10**6) + margin >= lo:
                    return True
        return False

    def never_stoppable(self, cell: Cell) -> bool:
        """Closed or keep-clear with no end time: nothing can ever dwell here."""
        return any(hi == INF and cell in cells for cells, lo, hi in self.closed + self.no_stop)

    def last_window_end(self) -> float:
        ends = [hi for _, _, hi in self.closed + self.no_stop if hi < INF]
        ends += [lo for _, lo, _ in self.closed + self.no_stop if lo > -INF]
        return max(ends, default=0)

    def ever_restricted_after(self, cell: Cell, t: int) -> bool:
        return any(hi > t and cell in cells for cells, lo, hi in self.closed + self.no_stop)


class Reservations:
    def __init__(self) -> None:
        self.vertex: dict[tuple[Cell, int], str] = {}
        self.edge: set[tuple[Cell, Cell, int]] = set()     # (from, to, t) move during t -> t+1
        self.parked_from: dict[Cell, int] = {}              # cell occupied forever from t
        self.static: set[Cell] = set()                      # unplanned robots sitting at home

    def free(self, cell: Cell, t: int) -> bool:
        return ((cell, t) not in self.vertex and cell not in self.static
                and self.parked_from.get(cell, INF) > t)

    def free_after(self, cell: Cell, t: int) -> bool:
        """Nobody else needs this cell at time t or later."""
        if cell in self.static or self.parked_from.get(cell, INF) < INF:
            return False
        return not any(c == cell and tt >= t for (c, tt) in self.vertex)

    def reserve_path(self, robot: str, cells: list[Cell], t0: int) -> None:
        for k, c in enumerate(cells):
            self.vertex[(c, t0 + k)] = robot
            if k:
                self.edge.add((cells[k - 1], c, t0 + k - 1))
        self.parked_from[cells[-1]] = t0 + len(cells) - 1


def space_time_astar(
    world: World, rules: Rules, res: Reservations, start: Cell, t0: int, goal: Cell,
    dwell: int = 0, final: bool = False, max_t: int = 4000,
) -> list[Cell]:
    """Cells from start (at t0) to goal, one per step, including waits.

    dwell: steps the robot must then stay at goal (picks, drops, charging).
    final: the robot stays at goal forever afterwards.
    """
    h = world.distances_from(goal)
    if start not in h:
        raise PlanningError(f"{list(goal)} unreachable from {list(start)}")
    if (dwell or final) and rules.never_stoppable(goal):
        raise PlanningError(f"{list(goal)} is in a zone where robots may never stop")
    # Enough time to wait out every time window plus generous detours.
    max_t = min(max_t, int(max(t0, rules.last_window_end())) + 4 * len(world.free_cells))

    def goal_ok(t: int) -> bool:
        for k in range(1, dwell + 1):
            if not res.free(goal, t + k) or rules.is_closed(goal, t + k) or not rules.can_stop(goal, t + k):
                return False
        # Don't finish inside a zone that is about to close unless there's time to get out.
        if not final and rules.trapped_after(goal, t + dwell):
            return False
        if final:
            end = t + dwell
            return res.free_after(goal, end) and not rules.ever_restricted_after(goal, end)
        return True

    tie = itertools.count()
    open_ = [(h[start], 0, next(tie), start, t0, None)]
    parent: dict[tuple[Cell, int], tuple[Cell, int] | None] = {}
    while open_:
        _, g, _, cell, t, prev = heapq.heappop(open_)
        if (cell, t) in parent:
            continue
        parent[(cell, t)] = prev
        if cell == goal and goal_ok(t):
            path = [(cell, t)]
            while parent[path[-1]] is not None:
                path.append(parent[path[-1]])
            return [c for c, _ in reversed(path)]
        if t >= max_t:
            continue
        nt = t + 1
        for nxt in [cell, *world.neighbors(cell)]:
            if (nxt, nt) in parent:
                continue
            if not res.free(nxt, nt) or rules.is_closed(nxt, nt):
                continue
            if nxt == cell and not rules.can_stop(cell, nt):
                continue
            if nxt != cell and (nxt, cell, t) in res.edge:       # head-on swap
                continue
            heapq.heappush(open_, (g + 1 + h.get(nxt, INF), g + 1, next(tie), nxt, nt, (cell, t)))
    raise PlanningError(f"no conflict-free route from {start} to {goal} before step {max_t}")


def _cells_to_actions(cells: list[Cell]) -> list[str]:
    out = []
    for a, b in zip(cells, cells[1:]):
        out.append("wait" if a == b else DIRS[(b[0] - a[0], b[1] - a[1])])
    return out


@dataclass
class _Est:
    pos: Cell
    t: float
    battery: float
    home: Cell
    parked: bool = False


class Planner:
    def __init__(self, world: World):
        self.world = world
        self.rules: Rules | None = None

    # ── allocation ──────────────────────────────────────────────────────────
    def _nearest_charger(self, pos: Cell) -> Cell | None:
        usable = [c for c in self.world.chargers.values()
                  if not (self.rules and self.rules.never_stoppable(c))]
        options = [(self.world.distance(pos, c), c) for c in usable]
        options = [(d, c) for d, c in options if d is not None]
        return min(options)[1] if options else None

    def _transport_legs(self, t: Transport, capacity: int) -> list[Leg]:
        src, dst = self.world.locations[t.from_location], self.world.locations[t.to_location]
        legs, left = [], t.quantity
        while left:
            n = min(capacity, left)
            legs += [Leg(src, [f"pick:{t.id}"] * n, t.id), Leg(dst, [f"drop:{t.id}"] * n, t.id)]
            left -= n
        return legs

    def _run(self, est: _Est, legs: list[Leg]) -> tuple[float, float, float]:
        """Walk legs from est ignoring other robots: (steps, final battery, lowest battery)."""
        b = self.world.battery
        pos, steps, batt = est.pos, 0.0, est.battery
        low = batt
        for leg in legs:
            d = self.world.distance(pos, leg.goal)
            if d is None:
                return INF, -INF, -INF
            steps += d + len(leg.dwell)
            batt -= d * b.move_cost
            low = min(low, batt)
            for a in leg.dwell:
                batt = min(100.0, batt + b.charge_rate) if a == "charge" else batt - b.idle_cost
            low = min(low, batt)
            pos = leg.goal
        return steps, batt, low

    def _charge_legs(self, pos: Cell, battery_on_arrival: float, target: float, task: str | None) -> list[Leg]:
        charger = self._nearest_charger(pos)
        if charger is None:
            raise PlanningError("no reachable charger")
        n = max(1, math.ceil((target - battery_on_arrival) / self.world.battery.charge_rate))
        return [Leg(charger, ["charge"] * n, task, charge_target=target)]

    def _safe(self, est: _Est, legs: list[Leg]) -> bool:
        """Never below reserve, and still able to reach a charger above reserve afterwards."""
        steps, final, low = self._run(est, legs)
        if steps == INF:
            return False
        end = legs[-1].goal if legs else est.pos
        reserve = self.world.battery.reserve
        charger = self._nearest_charger(end)
        if charger is None:      # no usable charger this shift: the reserve must last, including the trip home
            home = self.world.distance(end, est.home)
            return home is not None and low >= reserve and final - home * self.world.battery.move_cost >= reserve
        back = self.world.distance(end, charger)
        if back is None:
            return False
        return low >= reserve and final - back * self.world.battery.move_cost >= reserve

    def _with_charging(self, est: _Est, legs: list[Leg]) -> list[Leg] | None:
        """Prepend a charging stop if needed to stay safe. None if the task can't be done safely."""
        if self._safe(est, legs):
            return legs
        charger = self._nearest_charger(est.pos)
        if charger is None:
            return None
        arrive = est.battery - self.world.distance(est.pos, charger) * self.world.battery.move_cost
        if arrive <= 0:
            return None
        with_charge = self._charge_legs(est.pos, arrive, 100, None) + legs
        return with_charge if self._safe(est, with_charge) else None

    def allocate(self, spec: MissionSpec) -> tuple[dict[str, Itinerary], dict[str, str], list[str]]:
        w = self.world
        issues: list[str] = []
        unavailable = {c.robot for c in spec.constraints if isinstance(c, RobotUnavailable)}
        its = {r.id: Itinerary(r.id) for r in w.robots}
        est = {r.id: _Est(w.locations[r.home], 0.0, float(r.battery), w.locations[r.home]) for r in w.robots}
        assignments: dict[str, str] = {}

        def deadline(t) -> float:
            return w.clock_to_step(t.deadline) if t.deadline else INF

        # Parking is done last so robots can work first; otherwise priority, then deadline.
        order = sorted(spec.tasks, key=lambda t: (isinstance(t, Park), -t.priority, deadline(t), t.id))
        for task in order:
            if task.robot and task.robot not in its:
                issues.append(f"{task.id}: unknown robot {task.robot}")
                continue
            names = ([task.from_location, task.to_location] if isinstance(task, Transport)
                     else [task.location] if isinstance(task, Park) else [])
            missing = [n for n in names if n not in w.locations]
            if missing:
                issues.append(f"{task.id}: unknown location {', '.join(missing)}")
                continue
            candidates = [task.robot] if task.robot else [r.id for r in w.robots]
            candidates = [r for r in candidates if r not in unavailable and not est[r].parked]
            if not candidates:
                issues.append(f"{task.id}: no available robot")
                continue

            best = None
            for rid in candidates:
                e = est[rid]
                if isinstance(task, Transport):
                    legs = self._with_charging(e, self._transport_legs(task, w.robot(rid).capacity))
                elif isinstance(task, Charge):
                    if not task.robot and e.battery >= task.target_battery:
                        continue
                    charger = self._nearest_charger(e.pos)
                    d = w.distance(e.pos, charger) if charger else None
                    legs = None if d is None else self._charge_legs(
                        e.pos, e.battery - d * w.battery.move_cost, task.target_battery, task.id)
                else:  # Park
                    legs = [Leg(w.locations[task.location], [], task.id)]
                if legs is None:
                    continue
                steps, _, _ = self._run(e, legs)
                if steps == INF:
                    continue
                # Charge tasks without a robot go to the emptiest battery; others to the soonest finish.
                key = (e.battery, e.t + steps) if isinstance(task, Charge) and not task.robot else (e.t + steps, rid)
                if best is None or key < best[0]:
                    best = (key, rid, legs, steps)

            if best is None:
                issues.append(f"{task.id}: no robot can reach it with enough battery")
                continue
            _, rid, legs, steps = best
            e = est[rid]
            _, final_battery, _ = self._run(e, legs)
            its[rid].legs += legs
            e.t += steps
            e.pos = legs[-1].goal
            e.battery = final_battery
            assignments[task.id] = rid
            if isinstance(task, Park):
                e.parked = True
                its[rid].rest = legs[-1].goal
                its[rid].parked_by_task = True
        return its, assignments, issues

    # ── routing ─────────────────────────────────────────────────────────────
    def _rest_cell(self, rules: Rules, res: Reservations, preferred: Cell, t: int, taken: set[Cell]) -> Cell:
        if preferred not in taken and not rules.ever_restricted_after(preferred, t):
            return preferred
        dist = self.world.distances_from(preferred)
        reserved_locations = set(self.world.locations.values())
        for cell, _ in sorted(dist.items(), key=lambda kv: (kv[1], kv[0])):
            if cell in taken or cell in reserved_locations or rules.ever_restricted_after(cell, t):
                continue
            return cell
        raise PlanningError("no safe resting cell")

    def _route(self, rid, legs, parked_by_task, rules, res, homes, taken_rest):
        """Route one robot's legs, then send it to a safe resting cell. Raises _LegFailed."""
        w = self.world
        pos, t, cells, acts = homes[rid], 0, [homes[rid]], []
        batt, bm = float(w.robot(rid).battery), w.battery
        finish: dict[str, int] = {}
        for i, leg in enumerate(legs):
            final = parked_by_task and i == len(legs) - 1
            dwell = list(leg.dwell)
            try:
                for _ in range(4):   # charging time depends on the real route, so resize and re-route
                    seg = space_time_astar(w, rules, res, pos, t, leg.goal, dwell=len(dwell), final=final)
                    moves = _cells_to_actions(seg)
                    arrive = batt - sum(a != "wait" for a in moves) * bm.move_cost \
                        - moves.count("wait") * bm.idle_cost
                    if leg.charge_target is None:
                        break
                    need = max(1, math.ceil((min(leg.charge_target, 100) - arrive) / bm.charge_rate))
                    if need == len(dwell):
                        break
                    dwell = ["charge"] * need
                # Estimates ignore detours, so enforce a hard floor on the real route.
                floor = 0 if leg.charge_target is not None else bm.reserve / 2
                after_dwell = arrive if leg.charge_target is not None else arrive - len(dwell) * bm.idle_cost
                if min(arrive, after_dwell) <= floor:
                    raise PlanningError(f"battery would fall to {arrive:.1f}% before reaching {list(leg.goal)}")
            except PlanningError as exc:
                raise _LegFailed(i, str(exc)) from exc
            batt = arrive
            for a in dwell:
                batt = min(100.0, batt + bm.charge_rate) if a == "charge" else batt - bm.idle_cost
            acts += moves + dwell
            cells += seg[1:] + [leg.goal] * len(dwell)
            t += len(seg) - 1 + len(dwell)
            pos = leg.goal
            if leg.task:
                finish[leg.task] = t
        if not parked_by_task:
            try:
                rest = self._rest_cell(rules, res, homes[rid], t, taken_rest)
                seg = space_time_astar(w, rules, res, pos, t, rest, final=True)
            except PlanningError as exc:
                if not legs:
                    raise _LegFailed(-1, f"cannot reach a safe resting cell: {exc}") from exc
                raise _LegFailed(len(legs) - 1, f"no safe way back afterwards: {exc}") from exc
            moves = _cells_to_actions(seg)
            if batt - sum(a != "wait" for a in moves) * bm.move_cost - moves.count("wait") * bm.idle_cost <= bm.reserve / 2:
                if not legs:
                    raise _LegFailed(-1, "not enough battery to reach a safe resting cell")
                raise _LegFailed(len(legs) - 1, "not enough battery to get back afterwards")
            acts += moves
            cells += seg[1:]
            pos = rest
        return cells, acts, finish, pos

    def plan(self, spec: MissionSpec) -> Plan:
        w = self.world
        rules = Rules(w, spec)
        self.rules = rules
        res = Reservations()
        its, assignments, issues = self.allocate(spec)
        homes = {r.id: w.locations[r.home] for r in w.robots}

        # Busy robots are routed first; idle robots move only if their home becomes off-limits.
        order = sorted(its, key=lambda rid: (-len(its[rid].legs), rid))
        res.static = {homes[r] for r in order}
        actions: dict[str, list[str]] = {}
        finish: dict[str, int] = {}
        taken_rest: set[Cell] = set()

        unavailable = {c.robot for c in spec.constraints if isinstance(c, RobotUnavailable)}
        for rid in order:
            it = its[rid]
            res.static.discard(homes[rid])
            if rid in unavailable:
                # An out-of-service robot never moves, even if its spot is later restricted.
                if rules.ever_restricted_after(homes[rid], 0):
                    issues.append(f"{rid}: out of service and parked inside a restricted area")
                res.reserve_path(rid, [homes[rid]], 0)
                taken_rest.add(homes[rid])
                actions[rid] = []
                continue
            legs = list(it.legs)
            park_task = it.legs[-1].task if it.parked_by_task else None
            while True:
                parked = bool(park_task) and bool(legs) and legs[-1].task == park_task
                try:
                    cells, acts, fin, pos = self._route(rid, legs, parked, rules, res, homes, taken_rest)
                    break
                except _LegFailed as exc:
                    if exc.index < 0:   # nothing left to drop: stay at home
                        issues.append(f"{rid}: stays home: {exc.reason}")
                        cells, acts, fin, pos = [homes[rid]], [], {}, homes[rid]
                        break
                    # Drop the task whose leg failed (with its auto-charge stop) and re-plan this robot.
                    # A robot with nothing left stays home, where every other robot already expects it.
                    bad = legs[exc.index].task
                    issues.append(f"{rid}: dropped {bad or 'charging stop'}: {exc.reason}")
                    if bad is None:
                        # An automatic charging stop failed: the job it was powering can't run either.
                        nxt = next((l.task for l in legs[exc.index + 1:] if l.task), None)
                        del legs[exc.index]
                        if nxt:
                            issues.append(f"{rid}: dropped {nxt}: its charging stop failed")
                            bad = nxt
                    if bad:
                        assignments.pop(bad, None)
                        legs = [l for l in legs if l.task != bad]
            finish.update(fin)
            taken_rest.add(pos)
            res.reserve_path(rid, cells, 0)
            while acts and acts[-1] == "wait":   # a finished robot waits by default
                acts.pop()
            actions[rid] = acts

        late = []
        for task in spec.tasks:
            if task.deadline and task.id in finish and finish[task.id] > w.clock_to_step(task.deadline):
                late.append(task.id)
        return Plan(actions=actions, assignments=assignments,
                    meta={"planner": "solver", "issues": issues, "predicted_late": late,
                          "predicted_finish": finish})
