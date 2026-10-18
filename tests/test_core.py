import random
from pathlib import Path

import pytest
from pydantic import ValidationError

from fleetplanner import MissionSpec, Plan, Planner, World, simulate
from fleetplanner.scenarios import random_spec
from fleetplanner.solver import Reservations, Rules, space_time_astar

MAPS = Path(__file__).resolve().parents[1] / "maps"
TINY = World.load(MAPS / "tiny.yaml")
WAREHOUSE = World.load(MAPS / "warehouse.yaml")
SAFETY = {"collision", "invalid_move", "closed_zone", "keep_clear", "unavailable_robot_used", "battery_depleted"}


def spec(**kw) -> MissionSpec:
    return MissionSpec.model_validate(kw)


# ── World and spec ────────────────────────────────────────────────────────────

def test_world_loads_and_locations_are_floor():
    assert WAREHOUSE.width == 21 and WAREHOUSE.height == 13
    assert all(WAREHOUSE.is_free(c) for c in WAREHOUSE.locations.values())
    assert len(WAREHOUSE.chargers) == 3


def test_clock_conversion_round_trip():
    assert WAREHOUSE.clock_to_step("14:00") == 0
    assert WAREHOUSE.clock_to_step("14:30") == 180
    assert WAREHOUSE.step_to_clock(180) == "14:30:00"


def test_spec_rejects_bad_time_and_duplicate_ids():
    with pytest.raises(ValidationError):
        spec(tasks=[{"id": "a", "type": "park", "robot": "R1", "location": "A", "deadline": "25:00"}])
    with pytest.raises(ValidationError):
        spec(tasks=[{"id": "a", "type": "park", "location": "A"}] * 2)


# ── Simulator catches violations by itself ────────────────────────────────────
# Tiny map: R1 home (1,4), R2 home (3,4); aisle cells (4,2),(4,3).

def kinds(log):
    return {v["kind"] for v in log["violations"]}


def test_sim_detects_vertex_collision():
    log = simulate(TINY, spec(), Plan(actions={"R1": ["E"], "R2": ["W"]}))  # both into (2,4)
    assert "collision" in kinds(log)


def test_sim_detects_swap_collision():
    log = simulate(TINY, spec(), Plan(actions={"R1": ["E", "E"], "R2": ["wait", "W"]}))  # R1 (2,4)->(3,4), R2 (3,4)->(2,4)
    assert "collision" in kinds(log)


def test_sim_detects_wall_move():
    assert "invalid_move" in kinds(simulate(TINY, spec(), Plan(actions={"R1": ["S"]})))


def test_sim_detects_closed_zone_and_keep_clear():
    s = spec(constraints=[{"type": "zone_closed", "zone": "west_lane"}])
    assert "closed_zone" in kinds(simulate(TINY, s, Plan(actions={"R1": ["N"]})))
    s = spec(constraints=[{"type": "keep_clear", "zone": "west_lane"}])
    assert "keep_clear" not in kinds(simulate(TINY, s, Plan(actions={"R1": ["N", "N", "N"]})))  # passing through
    assert "keep_clear" in kinds(simulate(TINY, s, Plan(actions={"R1": ["N", "wait"]})))         # stopping


def test_sim_detects_unavailable_robot_and_bad_pick():
    s = spec(tasks=[{"id": "t", "type": "transport", "from_location": "A", "to_location": "pick"}],
             constraints=[{"type": "robot_unavailable", "robot": "R2"}])
    log = simulate(TINY, s, Plan(actions={"R1": ["pick:t"], "R2": ["N"]}))
    assert {"unavailable_robot_used", "invalid_action"} <= kinds(log)


def test_sim_counts_delivery_and_deadline_miss():
    # R1 goes north around R2 (parked at (3,4)): up the west lane, east, down into A (4,2), then to pick (6,4).
    acts = ["N", "N", "N", "E", "E", "E", "S", "pick:t", "S", "S", "E", "E", "drop:t"]
    s = spec(tasks=[{"id": "t", "type": "transport", "from_location": "A", "to_location": "pick", "deadline": "08:01"}])
    log = simulate(TINY, s, Plan(actions={"R1": acts}))
    t = log["tasks"]["t"]
    assert t["complete"] and t["completion_step"] == 13 and not t["on_time"]   # 08:01 is step 6
    assert not log["violations"]


# ── Space-time A* ─────────────────────────────────────────────────────────────

def test_astar_shortest_path_when_unobstructed():
    rules, res = Rules(TINY, spec()), Reservations()
    path = space_time_astar(TINY, rules, res, (1, 4), 0, (4, 2))
    assert len(path) - 1 == TINY.distance((1, 4), (4, 2))


def test_astar_waits_for_reserved_cell():
    rules, res = Rules(TINY, spec()), Reservations()
    res.reserve_path("X", [(3, 4), (3, 4), (3, 4), (2, 4)], 0)       # blocker waits, then parks at (2,4) for good
    path = space_time_astar(TINY, rules, res, (4, 4), 0, (1, 4))
    assert path[-1] == (1, 4) and (2, 4) not in path[3:]             # goes around via the north corridor
    assert all(res.free(c, t) for t, c in enumerate(path) if t)


def test_astar_routes_around_closed_zone():
    s = spec(constraints=[{"type": "zone_closed", "zone": "aisle"}])
    path = space_time_astar(TINY, Rules(TINY, s), Reservations(), (4, 4), 0, (4, 1))
    assert not set(path) & TINY.zones["aisle"]


# ── Planner + simulator end to end ────────────────────────────────────────────

@pytest.mark.parametrize("name", ["restock_rush", "low_battery", "busy_shift"])
def test_examples_succeed(name):
    import yaml
    s = MissionSpec.model_validate(yaml.safe_load((MAPS.parent / "specs" / "examples" / f"{name}.yaml").read_text()))
    log = simulate(WAREHOUSE, s, Planner(WAREHOUSE).plan(s))
    assert log["metrics"]["success"], log["violations"]


@pytest.mark.parametrize("seed", range(40))
def test_random_shifts_are_safe_and_failures_are_reported(seed):
    s = random_spec(WAREHOUSE, random.Random(seed))
    plan = Planner(WAREHOUSE).plan(s)
    log = simulate(WAREHOUSE, s, plan)
    unsafe = [v for v in log["violations"] if v["kind"] in SAFETY]
    assert not unsafe, unsafe
    issues = " ".join(plan.meta["issues"])
    for tid, t in log["tasks"].items():
        assert t["complete"] or tid in issues or plan.assignments.get(tid, "?") in issues, (tid, plan.meta)
