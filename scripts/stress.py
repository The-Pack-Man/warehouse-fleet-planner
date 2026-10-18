#!/usr/bin/env python3
"""
Stress-test the classical planner on random shifts and record the results.

Each shift has 1-6 transport jobs, sometimes a charge job, up to two timed
zone closures, a possible keep-clear zone, and a possible unavailable robot.
Some shifts are deliberately contradictory (e.g. a job at the dock while the
dock is keep-clear all shift); the planner must drop those jobs and say so.

Checks, judged by the simulator:
  safety          no collisions, wall moves, closed-zone entries, keep-clear stops,
                  unavailable-robot moves, or dead batteries
  no silent fail  every unfinished task is named in the planner's issues

    python scripts/stress.py --seeds 5000
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fleetplanner import Planner, World, simulate  # noqa: E402
from fleetplanner.scenarios import random_spec  # noqa: E402

SAFETY = {"collision", "invalid_move", "closed_zone", "keep_clear", "unavailable_robot_used", "battery_depleted"}


def classify(issue: str) -> str:
    if "never stop" in issue:
        return "contradictory: job inside an all-shift keep-clear/closed zone"
    if "no available robot" in issue or "unknown robot" in issue:
        return "no available robot"
    if "charging stop failed" in issue:
        return "job dropped with its failed charging stop"
    if "battery" in issue or "no robot can reach" in issue:
        return "battery: job would breach the safety floor"
    if "route" in issue or "unreachable" in issue:
        return "no conflict-free route"
    return "other"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--map", default=str(ROOT / "maps" / "warehouse.yaml"))
    p.add_argument("--seeds", type=int, default=5000)
    p.add_argument("--first-seed", type=int, default=0)
    p.add_argument("--out", default=str(ROOT / "results"))
    args = p.parse_args()

    world = World.load(args.map)
    t0 = time.monotonic()
    totals = Counter()
    unsafe_kinds, reasons = Counter(), Counter()
    unsafe_seeds, silent_seeds = [], []
    min_battery = 100.0
    for seed in range(args.first_seed, args.first_seed + args.seeds):
        spec = random_spec(world, random.Random(seed))
        plan = Planner(world).plan(spec)
        log = simulate(world, spec, plan)
        unsafe = [v for v in log["violations"] if v["kind"] in SAFETY]
        issues = " ".join(plan.meta["issues"])
        unfinished = [tid for tid, t in log["tasks"].items() if not t["complete"]]
        silent = [tid for tid in unfinished if tid not in issues]
        totals["shifts"] += 1
        totals["tasks"] += len(log["tasks"])
        totals["tasks_complete"] += len(log["tasks"]) - len(unfinished)
        totals["shifts_fully_complete"] += not unfinished and not unsafe
        totals["shifts_with_reported_drops"] += bool(unfinished) and not silent
        for v in unsafe:
            unsafe_kinds[v["kind"]] += 1
        if unsafe:
            unsafe_seeds.append(seed)
        if silent:
            silent_seeds.append(seed)
        for i in plan.meta["issues"]:
            reasons[classify(i)] += 1
        min_battery = min(min_battery, min(f[r][2] for f in log["frames"] for r in f))

    summary = {
        "shifts": totals["shifts"],
        "unsafe_shifts": len(unsafe_seeds),
        "silent_failure_shifts": len(silent_seeds),
        "shifts_fully_complete": totals["shifts_fully_complete"],
        "shifts_with_reported_drops": totals["shifts_with_reported_drops"],
        "task_completion_rate": round(totals["tasks_complete"] / totals["tasks"], 4),
        "lowest_battery_seen": round(min_battery, 2),
        "unsafe_violation_kinds": dict(unsafe_kinds),
        "drop_reasons": dict(reasons.most_common()),
    }
    print(json.dumps(summary, indent=2))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out / f"stress_{stamp}.json"
    path.write_text(json.dumps({
        "created_at": datetime.now(timezone.utc).isoformat(),
        "map": Path(args.map).name, "seeds": [args.first_seed, args.first_seed + args.seeds - 1],
        "duration_s": round(time.monotonic() - t0, 1), "summary": summary,
        "unsafe_seeds": unsafe_seeds, "silent_failure_seeds": silent_seeds,
    }, indent=2))
    print(f"wrote {path.relative_to(ROOT)}")
    sys.exit(1 if unsafe_seeds or silent_seeds else 0)


if __name__ == "__main__":
    main()
