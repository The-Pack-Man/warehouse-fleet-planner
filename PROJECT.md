---
name: Warehouse Fleet Planner
repo: The-Pack-Man/warehouse-fleet-planner
status: active
one_liner: Multi-robot warehouse planning where an LLM translates requests, a classical solver plans, and a validator checks; milestone 1 (world, simulator, solver) done.
problem: Supervisors speak in plain-language requests; robot fleets need exact, collision-free plans. LLMs read requests well but plan unreliably, which is unsafe for physical robots.
user: Warehouse shift supervisor (portfolio project, synthetic warehouse).
tags: [eval-reliability, pilot-to-prod, problem-selection, shipped-evidence]
stack: [Python, Pydantic, space-time A*, prioritized multi-agent path finding, pytest]
run: python -m fleetplanner run maps/warehouse.yaml specs/examples/busy_shift.yaml
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
    text: Stress-tested the planner on 5,000 random shifts with a reproducible script. The stress test surfaced four real bugs (estimate-sized charging, unsafe partial routes, orphaned jobs after a failed charging stop, missing trip-home battery check), each fixed and covered by tests.
    evidence: [scripts/stress.py, results/, tests/test_core.py]

metrics:
  - id: M1
    name: Safety violations across random shifts (simulator-judged)
    value: "0 / 5,000 shifts"
    status: measured
    method: scripts/stress.py --seeds 5000 on maps/warehouse.yaml
    artifact: results/stress_20261018T030148Z.json
    date: 2026-10-18
  - id: M2
    name: Silent failures (unfinished task not reported by the planner)
    value: "0 / 5,000 shifts"
    status: measured
    method: same run as M1
    artifact: results/stress_20261018T030148Z.json
    date: 2026-10-18
  - id: M3
    name: Task completion rate on random shifts, including deliberately contradictory requests
    value: "95.4%"
    status: measured
    method: same run as M1
    artifact: results/stress_20261018T030148Z.json
    date: 2026-10-18
  - id: M4
    name: Test suite passing
    value: 55
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
  - Milestone 1 only. No LLM translator, validator, viewer, or LLM evaluation yet.
  - Greedy allocation and prioritized routing are safe but not optimal.
  - Grid world with discrete time; no vehicle dynamics.
  - Synthetic warehouse layout and random workloads.
---

## Context
A testbed for the "LLM translates, solver plans, validator checks" pattern in a domain where bad plans have physical consequences.

## Results
See the README results section and `results/`.
