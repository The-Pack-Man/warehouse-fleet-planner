"""Random shift generator for stress-testing the planner (warehouse.yaml layout)."""
from __future__ import annotations

import random

from .spec import MissionSpec
from .world import World


def random_spec(world: World, rng: random.Random) -> MissionSpec:
    robots = [r.id for r in world.robots]
    slots = [k for k in world.locations if k[0] == "A"]
    sinks = [k for k in world.locations if k.startswith("pick")] + ["dock"]
    tasks = []
    for i in range(rng.randint(1, 6)):
        a, b = rng.choice(slots), rng.choice(sinks)
        if rng.random() < 0.5:
            a, b = b, a
        tasks.append({"id": f"t{i}", "type": "transport", "from_location": a, "to_location": b,
                      "quantity": rng.randint(1, 4), "priority": rng.randint(1, 5)})
    if rng.random() < 0.3:
        tasks.append({"id": "chg", "type": "charge", "robot": rng.choice(robots)})
    constraints = []
    lanes = sorted(z for z in world.zones if z.startswith("aisle") or z == "east_lane")
    for z in rng.sample(lanes, rng.randint(0, 2)):
        lo = rng.randint(0, 20)
        constraints.append({"type": "zone_closed", "zone": z,
                            "start": f"14:{lo:02d}", "end": f"14:{lo + rng.randint(2, 30):02d}"})
    if rng.random() < 0.5:
        constraints.append({"type": "keep_clear", "zone": "charging_bay" if not any(
            t["type"] == "charge" for t in tasks) else "dock_area"})
    if rng.random() < 0.3:
        constraints.append({"type": "robot_unavailable", "robot": rng.choice(robots)})
    return MissionSpec.model_validate({"tasks": tasks, "constraints": constraints})
