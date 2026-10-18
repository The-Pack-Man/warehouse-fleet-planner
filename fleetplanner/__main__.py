"""
python -m fleetplanner run MAP SPEC [--out runs/name.json]

Plans a hand-written MissionSpec with the classical solver and simulates it.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from .sim import simulate
from .solver import Planner
from .spec import MissionSpec
from .world import World


def main() -> None:
    p = argparse.ArgumentParser(prog="fleetplanner")
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="plan a spec with the solver and simulate it")
    run.add_argument("map")
    run.add_argument("spec")
    run.add_argument("--out", help="write the run log (JSON) here for the viewer")
    args = p.parse_args()

    world = World.load(args.map)
    spec = MissionSpec.model_validate(yaml.safe_load(Path(args.spec).read_text()))
    plan = Planner(world).plan(spec)
    log = simulate(world, spec, plan)
    m = log["metrics"]

    print(f"map={world.name}  tasks={m['tasks_total']}  makespan={m['makespan_steps']} steps (ends {m['makespan_time']})")
    print(f"success={m['success']}  complete={m['tasks_complete']}/{m['tasks_total']}  "
          f"on_time={m['tasks_on_time']}/{m['tasks_total']}  violations={m['violation_count']}")
    for tid, t in log["tasks"].items():
        mark = "✓" if t["on_time"] else ("late" if t["complete"] else "✗")
        print(f"  {mark:4s} {tid:12s} {t['type']:9s} robot={plan.assignments.get(tid, '-'):3s} done={t['completion_time']}")
    for v in log["violations"]:
        print(f"  ! {v['kind']}: {v['robot']} {v['detail']} (steps {v['start_step']}-{v['end_step']})")
    for issue in plan.meta.get("issues", []):
        print(f"  ? planner: {issue}")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(log))
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
