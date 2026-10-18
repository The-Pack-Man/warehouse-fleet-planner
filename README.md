# Warehouse Fleet Planner

**LLM translates, classical solver plans, validator checks.** A warehouse supervisor types *"restock aisle 4 from the dock before 2:30, keep the charging bay clear."* A robot fleet needs exact tasks, assignments, and collision-free routes. This project tests whether that translation can be safe when an LLM is involved, by keeping the LLM out of the planning itself.

![Replay of a shift: four robots, one out of service, aisle 3 closed](docs/demo.gif)

*A replay of [`busy_shift`](specs/examples/busy_shift.yaml): five jobs, R2 out of service, aisle 3 closed all shift. Dashed lines are the planned routes, the ring on each robot is its battery, and orange badges count the totes it's carrying. R4 stops to charge before its job.*

See [SPEC.md](SPEC.md) for the problem, user, success metrics, and scope.

> **Status: milestone 2 of 5.** The world model, simulator, classical planner, and replay viewer are done. The LLM translator and validator, and the LLM-vs-solver evaluation, come next.

## How it works

```
request ──► LLM translator ──► MissionSpec ──► Validator ──► Solver ──► Simulator ──► run log ──► Viewer
            (milestone 3)        (typed)      (milestone 3)  (done)      (done)                  (done)
```

- **World** ([`maps/warehouse.yaml`](maps/warehouse.yaml)): a 21×13 grid with five one-lane aisles, two-lane cross corridors, a charging lane, a dock, pick stations, and four robots with battery and carrying capacity.
- **MissionSpec** ([`fleetplanner/spec.py`](fleetplanner/spec.py)): the typed contract between language and planning. Tasks are `transport`, `charge`, and `park`, with optional deadline, priority, and pinned robot. Constraints are `zone_closed`, `keep_clear` (pass through but don't stop), and `robot_unavailable`, with optional time windows.
- **Solver** ([`fleetplanner/solver.py`](fleetplanner/solver.py)): no LLM involved.
  - **Allocation:** tasks in priority/deadline order go to the robot that finishes soonest. Charging stops are inserted automatically.
  - **Routing:** prioritized planning with space-time A* against a reservation table. Robots never share a cell or swap cells, closed zones are blocked during their windows, and robots never stop in keep-clear zones.
- **Simulator** ([`fleetplanner/sim.py`](fleetplanner/sim.py)): the referee.
- **Viewer** ([`fleetplanner/viewer.html`](fleetplanner/viewer.html)): a single-file browser replay of any run log. It shows the map, robots, routes, battery, cargo, zones that hatch while closed, a timeline marking deliveries, completions, and violations, and lists of tasks, robots, rules, and issues. Click an issue to jump to it. Keys: Space to play, arrows to step (Shift for 10).

## Design decisions

**The simulator doesn't trust the planner.** A plan is just a list of actions per robot (`N`, `S`, `E`, `W`, `wait`, `pick:T1`, `drop:T1`, `charge`). The simulator replays it and checks everything itself: walls, collisions (same cell or head-on swap), closed zones, keep-clear stops, unavailable robots, battery, cargo, and deadlines. That makes the planned LLM-only baseline a fair comparison, because a plan written by an LLM is judged by exactly the same referee.

**Failures are safe and loud.** If a robot can't complete part of its work, the planner drops that task, says why in `plan.meta["issues"]`, and re-plans the robot's remaining work. A robot left with nothing to do stays home, where every other robot already planned around it. The planner never quietly drops work.

**Battery has a hard floor.** Allocation estimates battery use and inserts charging stops to keep each robot above its reserve (20%). Those estimates ignore detours, so the router also enforces a hard floor of half the reserve on the real route. Charging time is sized from the robot's actual battery on arrival, not the estimate.

## Results so far

### The referee catches a plan that ignores the rules

![A naive plan drives into aisle 3 while it's closed](docs/naive.gif)

[`aisle_closed`](specs/examples/aisle_closed.yaml): aisle 3 is closed 14:02–14:12 for a spill, and there are jobs in it. The [naive plan](plans/naive_ignores_closure.json) was made as if the closure didn't exist, a stand-in for a planner that doesn't respect constraints. The simulator flags three closed-zone violations. The solver's plan for the same request waits for the aisle to reopen: both jobs are done by 14:17, with zero violations.

| Plan | Jobs done | Violations | Last job done |
|---|---|---|---|
| Naive (ignores closure) | 2 / 2 | **3** (closed zone) | 14:06 |
| Solver | 2 / 2 | **0** | 14:17 |

Breaking the rule is faster, which is exactly why a planner can't be trusted to self-report.

### Stress test

[`results/stress_20261018T031145Z.json`](results/stress_20261018T031145Z.json): 5,000 random shifts, each with 1 to 6 transport jobs, timed zone closures, keep-clear zones, low batteries, and unavailable robots. Some shifts are deliberately contradictory.

| Check | Result |
|---|---|
| Shifts with any safety violation (collision, wall, closed zone, keep-clear, dead battery) | **0 / 5,000** |
| Shifts where a task failed without being reported | **0 / 5,000** |
| Shifts fully completed | 4,429 (88.6%) |
| Shifts with dropped tasks (all reported) | 571 |
| Tasks completed | 95.6% |
| Lowest battery reached | 9.8%, on the way to a charger (the 10% floor exempts trips to a charger) |

Why tasks were dropped:

| Reason | Count |
|---|---|
| Contradictory request: job inside an all-shift keep-clear or closed zone | 655 |
| No available robot (e.g. job pinned to an unavailable robot) | 114 |
| Job would breach the battery safety floor | 56 |
| No conflict-free route | 9 |

Most drops are requests that should never reach the planner. Rejecting them up front, with an explanation, is the validator's job in milestone 3.

The stress test and the examples found five real bugs during development, each fixed and covered by tests:
- Charging was sized from estimates.
- Failed routes left robots parked where others had planned to drive.
- Dropping a charging stop kept the job it was meant to power.
- The no-charger battery check ignored the trip home.
- A robot could finish work inside an aisle just before it closed and be trapped. The planner now only finishes inside a soon-to-close zone if there's time to get out, and otherwise waits for it to reopen. This alone cut route failures from 49 to 9.

## Quick start

```bash
pip install -e ".[dev]"

# Plan and simulate a shift, then open the replay in your browser
python -m fleetplanner run maps/warehouse.yaml specs/examples/busy_shift.yaml --out runs/busy_shift.json --html runs/busy_shift.html --open

# Replay a hand-written plan instead of the solver's
python -m fleetplanner run maps/warehouse.yaml specs/examples/aisle_closed.yaml --plan plans/naive_ignores_closure.json --html runs/naive.html --open

# Turn any saved run log into a replay page
python -m fleetplanner view runs/busy_shift.json --open

pytest
python scripts/stress.py --seeds 5000
```

To record a GIF of a replay: `pip install -e ".[gif]" && playwright install chromium`, then `python scripts/record_gif.py runs/busy_shift.json docs/demo.gif`.

```
map=warehouse  tasks=5  makespan=93 steps (ends 14:15:30)
success=True  complete=5/5  on_time=5/5  violations=0
  ✓    t1           transport robot=R1  done=14:07:50
  ✓    t2           transport robot=R3  done=14:09:20
  ✓    t3           transport robot=R3  done=14:04:30
  ✓    t4           transport robot=R4  done=14:13:00
  ✓    park_r3      park      robot=R3  done=14:12:20
```

The run log (`--out`) holds every frame, event, violation, and task result. The replay page (`--html`) is a single self-contained file you can open or share.

## Known limitations

- **Greedy allocation, prioritized routing.** Fast and safe, but not optimal. A task goes to one robot even when splitting it would finish sooner, and robot order can matter.
- **Battery-floor drops could be avoided.** When a real route detours, the planner drops the job instead of inserting another charging stop.
- **Grid world, discrete time.** One cell per step (10 s). No acceleration, turning, or physics. Phase 2 in the spec replaces this with a learned continuous controller.
- **Low-power parking.** A robot with no actions left doesn't drain its battery. Waits inside a plan do.

## Roadmap

1. ~~World, simulator, solver~~
2. ~~Replay viewer~~
3. LLM translator (local Ollama by default) and validator
4. Evaluation suite: ~60 requests with gold outcomes. Ablations: LLM-only planning, no validator, full system, model size
5. Write-up with results and failure analysis
