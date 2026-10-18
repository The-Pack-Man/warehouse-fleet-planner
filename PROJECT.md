---
name: Warehouse Fleet Planner
repo: The-Pack-Man/warehouse-fleet-planner
status: active
one_liner: Multi-robot warehouse planning where an LLM translates requests, a classical solver plans, and a validator checks; milestones 1-2 (world, simulator, solver, replay viewer) done.
problem: Supervisors speak in plain-language requests; robot fleets need exact, collision-free plans. LLMs read requests well but plan unreliably, which is unsafe for physical robots.
user: Warehouse shift supervisor (portfolio project, synthetic warehouse).
tags: [eval-reliability, pilot-to-prod, problem-selection, shipped-evidence]
stack: [Python, Pydantic, space-time A*, prioritized multi-agent path finding, HTML Canvas, JavaScript, Playwright, pytest]
run: python -m fleetplanner run maps/warehouse.yaml specs/examples/busy_shift.yaml --html runs/busy_shift.html --open
test: pytest

claims:
  - id: C1
    text: Wrote a product spec before any code, defining the user, the core question (LLM+solver+validator vs LLM-only planning), metrics, ablations, scope, and definition of done.
    evidence: [SPEC.md]
  - id: C2
    text: Designed a typed MissionSpec contract (transport/charge/park tasks; time-windowed zone closures, keep-clear zones, unavailable robots) as the boundary between language and planning.
    evidence: [fleetplanner/spec.py]
  - id: C3
    text: Built a multi-robot planner with priority/deadline-ordered allocation, automatic charging stops, and prioritized space-time A* routing over a reservation table that prevents vertex and swap collisions and honors time-windowed closures.
    evidence: [fleetplanner/solver.py]
  - id: C4
    text: Built an independent simulator that judges any plan (solver- or LLM-written) by checking walls, collisions, closed zones, keep-clear stops, unavailable robots, battery, cargo, and deadlines step by step.
    evidence: [fleetplanner/sim.py, tests/test_core.py]
  - id: C5
    text: Made planning failures safe and explicit. Infeasible tasks are dropped with a stated reason and the robot is re-planned; robots with no viable work stay where every other robot expects them.
    evidence: [fleetplanner/solver.py]
  - id: C6
    text: Stress-tested the planner on 5,000 random shifts with a reproducible script. Testing surfaced five real bugs (estimate-sized charging, unsafe partial routes, orphaned jobs after a failed charging stop, missing trip-home battery check, robots trapped by zones closing behind them), each fixed and covered by tests.
    evidence: [scripts/stress.py, results/, tests/test_core.py]
  - id: C7
    text: Added look-ahead for time-windowed closures so robots never finish work inside a zone they can't leave before it closes, cutting route failures in the stress test from 49 to 9.
    evidence: [fleetplanner/solver.py, tests/test_core.py]
  - id: C8
    text: Built a single-file browser replay viewer for run logs (canvas map, routes, battery, cargo, time-windowed zones, timeline of events and violations, jump-to-issue), styled on real warehouse floor markings, plus a headless-browser GIF recorder.
    evidence: [fleetplanner/viewer.html, fleetplanner/viewer.py, scripts/record_gif.py, docs/demo.gif]
  - id: C9
    text: Demonstrated the simulator as referee. A naive plan that ignores an aisle closure is flagged with three closed-zone violations, while the solver's plan for the same request waits out the closure with none.
    evidence: [plans/naive_ignores_closure.json, specs/examples/aisle_closed.yaml, docs/naive.gif, tests/test_core.py]

metrics:
  - id: M1
    name: Safety violations across random shifts (simulator-judged)
    value: "0 / 5,000 shifts"
    status: measured
    method: scripts/stress.py --seeds 5000 on maps/warehouse.yaml
    artifact: results/stress_20261018T031145Z.json
    date: 2026-10-18
  - id: M2
    name: Silent failures (unfinished task not reported by the planner)
    value: "0 / 5,000 shifts"
    status: measured
    method: same run as M1
    artifact: results/stress_20261018T031145Z.json
    date: 2026-10-18
  - id: M3
    name: Task completion rate on random shifts, including deliberately contradictory requests
    value: "95.6%"
    status: measured
    method: same run as M1
    artifact: results/stress_20261018T031145Z.json
    date: 2026-10-18
  - id: M4
    name: Test suite passing
    value: 60
    status: measured
    method: pytest, 2026-10-18
    artifact: ""
    date: 2026-10-18

decisions:
  - Kept the LLM out of planning. It will only translate requests into a typed spec, which a classical solver plans and a validator checks.
  - Plans are plain per-robot action lists judged by an independent simulator, so an LLM-written baseline can be compared fairly.
  - Chose prioritized planning with space-time A* over optimal multi-agent search (CBS) for speed and simplicity, accepting non-optimal plans.
  - A battery floor is enforced on actual routes, because allocation estimates ignore detours.

limitations:
  - Milestones 1-2 only. No LLM translator, validator, or LLM evaluation yet.
  - Greedy allocation and prioritized routing are safe but not optimal.
  - Grid world with discrete time; no vehicle dynamics.
  - Synthetic warehouse layout and random workloads.
---

## Context
A testbed for the "LLM translates, solver plans, validator checks" pattern in a domain where bad plans have physical consequences.

## Results
See the README results section and `results/`.
