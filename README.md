# Warehouse Fleet Planner

**LLM translates, classical solver plans, validator checks.** A warehouse supervisor types *"restock aisle 4 from the dock before 2:30, keep the charging bay clear."* A robot fleet needs exact tasks, assignments, and collision-free routes. This project tests whether that translation can be safe when an LLM is involved, by keeping the LLM out of the planning itself.

See [SPEC.md](SPEC.md) for the problem, user, success metrics, and scope.

> **Status: milestone 1 of 5.** The world model, simulator, and classical planner are done and stress-tested. The replay viewer, the LLM translator and validator, and the LLM-vs-solver evaluation come next.

## How it works

```
request ──► LLM translator ──► MissionSpec ──► Validator ──► Solver ──► Simulator ──► run log ──► Viewer
            (milestone 3)        (typed)      (milestone 3)  (done)      (done)                  (milestone 2)
```

- **World** ([`maps/warehouse.yaml`](maps/warehouse.yaml)): a 21×13 grid with five one-lane aisles, two-lane cross corridors, a charging lane, a dock, pick stations, and four robots with battery and carrying capacity.
- **MissionSpec** ([`fleetplanner/spec.py`](fleetplanner/spec.py)): the typed contract between language and planning. Tasks are `transport`, `charge`, and `park`, with optional deadline, priority, and pinned robot. Constraints are `zone_closed`, `keep_clear` (pass through but don't stop), and `robot_unavailable`, with optional time windows.
- **Solver** ([`fleetplanner/solver.py`](fleetplanner/solver.py)): no LLM involved.
  - **Allocation:** tasks in priority/deadline order go to the robot that finishes soonest. Charging stops are inserted automatically.
  - **Routing:** prioritized planning with space-time A* against a reservation table. Robots never share a cell or swap cells, closed zones are blocked during their windows, and robots never stop in keep-clear zones.
- **Simulator** ([`fleetplanner/sim.py`](fleetplanner/sim.py)): the referee.

## Design decisions

**The simulator doesn't trust the planner.** A plan is just a list of actions per robot (`N`, `S`, `E`, `W`, `wait`, `pick:T1`, `drop:T1`, `charge`). The simulator replays it and checks everything itself: walls, collisions (same cell or head-on swap), closed zones, keep-clear stops, unavailable robots, battery, cargo, and deadlines. That makes the planned LLM-only baseline a fair comparison, because a plan written by an LLM is judged by exactly the same referee.

**Failures are safe and loud.** If a robot can't complete part of its work, the planner drops that task, says why in `plan.meta["issues"]`, and re-plans the robot's remaining work. A robot left with nothing to do stays home, where every other robot already planned around it. The planner never quietly drops work.

**Battery has a hard floor.** Allocation estimates battery use and inserts charging stops to keep each robot above its reserve (20%). Those estimates ignore detours, so the router also enforces a hard floor of half the reserve on the real route. Charging time is sized from the robot's actual battery on arrival, not the estimate.

## Results so far

**Stress test** ([`results/stress_20261018T030148Z.json`](results/stress_20261018T030148Z.json)): 5,000 random shifts, each with 1 to 6 transport jobs, timed zone closures, keep-clear zones, low batteries, and unavailable robots. Some shifts are deliberately contradictory.

| Check | Result |
|---|---|
| Shifts with any safety violation (collision, wall, closed zone, keep-clear, dead battery) | **0 / 5,000** |
| Shifts where a task failed without being reported | **0 / 5,000** |
| Shifts fully completed | 4,396 (87.9%) |
| Shifts with dropped tasks (all reported) | 604 |
| Tasks completed | 95.4% |
| Lowest battery level reached by any robot | 10.2% |

Why tasks were dropped:

| Reason | Count |
|---|---|
| Contradictory request: job inside an all-shift keep-clear or closed zone | 656 |
| No available robot (e.g. job pinned to an unavailable robot) | 114 |
| Job would breach the battery safety floor | 51 |
| No conflict-free route | 49 |

Most drops are requests that should never reach the planner. Rejecting them up front, with an explanation, is the validator's job in milestone 3.

The stress test also found four real bugs during development, each fixed and covered by tests:
- Charging was sized from estimates.
- Failed routes left robots parked where others had planned to drive.
- Dropping a charging stop kept the job it was meant to power.
- The no-charger battery check ignored the trip home.

## Quick start

```bash
pip install -e ".[dev]"
python -m fleetplanner run maps/warehouse.yaml specs/examples/busy_shift.yaml --out runs/busy_shift.json
pytest
python scripts/stress.py --seeds 5000
```

```
map=warehouse  tasks=5  makespan=93 steps (ends 14:15:30)
success=True  complete=5/5  on_time=5/5  violations=0
  ✓    t1           transport robot=R1  done=14:07:50
  ✓    t2           transport robot=R3  done=14:09:20
  ✓    t3           transport robot=R3  done=14:04:30
  ✓    t4           transport robot=R4  done=14:13:00
  ✓    park_r3      park      robot=R3  done=14:12:20
```

The run log (`--out`) holds every frame, event, violation, and task result. It is the input for the replay viewer.

## Known limitations

- **Greedy allocation, prioritized routing.** Fast and safe, but not optimal. A task goes to one robot even when splitting it would finish sooner, and robot order can matter.
- **Battery-floor drops could be avoided.** When a real route detours, the planner drops the job instead of inserting another charging stop.
- **Grid world, discrete time.** One cell per step (10 s). No acceleration, turning, or physics. Phase 2 in the spec replaces this with a learned continuous controller.
- **Low-power parking.** A robot with no actions left doesn't drain its battery. Waits inside a plan do.

## Roadmap

1. ~~World, simulator, solver~~
2. Replay viewer (browser, exports a GIF for this README)
3. LLM translator (local Ollama by default) and validator
4. Evaluation suite: ~60 requests with gold outcomes. Ablations: LLM-only planning, no validator, full system, model size
5. Write-up with results and failure analysis
