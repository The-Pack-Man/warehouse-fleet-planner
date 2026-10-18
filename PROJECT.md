---
name: Warehouse Fleet Planner
repo: The-Pack-Man/warehouse-fleet-planner
status: active
one_liner: Multi-robot warehouse planning where an LLM translates, deterministic checks ground and validate, and a classical solver plans; 85-90% vs 5-15% for LLM-only planning on held-out requests, zero safety violations.
problem: Supervisors speak in plain-language requests; robot fleets need exact, collision-free plans. LLMs read requests well but plan unreliably, which is unsafe for physical robots.
user: Warehouse shift supervisor (portfolio project, synthetic warehouse).
tags: [eval-reliability, pilot-to-prod, problem-selection, shipped-evidence]
stack: [Python, Pydantic, Ollama, OpenAI-compatible API, structured JSON output, space-time A*, prioritized multi-agent path finding, HTML Canvas, JavaScript, Playwright, pytest]
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
  - id: C10
    text: Built the LLM translator as the only LLM step. It maps a request to a typed spec, a clarifying question, or a refusal, using schema-constrained JSON output, Pydantic validation, an error-feedback repair loop, and automatic fallback to plain JSON mode. It works with any OpenAI-compatible endpoint (local Ollama by default).
    evidence: [fleetplanner/translate.py, tests/test_language.py]
  - id: C11
    text: Made safety rules impossible for the LLM to remove. Standing site rules are merged into every spec after translation, so an LLM that ignores a closure or uses an out-of-service robot is overruled; both cases are covered by tests.
    evidence: [fleetplanner/pipeline.py, rules/today.yaml, tests/test_language.py]
  - id: C12
    text: Built a deterministic validator that asks for clarification on unknown names (with suggestions) and rejects contradictions, unavailable robots, past deadlines, and deadlines impossible even under an optimistic travel-time bound, each with a plain-language reason.
    evidence: [fleetplanner/validate.py, tests/test_language.py]
  - id: C13
    text: Built a 60-request evaluation (simple, multi-task, constrained, time-phrasing, ambiguous, infeasible, unsafe) with gold outcomes and specs, comparing four designs (LLM-only planning, translate-only, no validator, full system). Every plan is judged in the simulator against the correct spec plus site rules rather than the LLM's own spec.
    evidence: [evals/requests.yaml, fleetplanner/evaluate.py, fleetplanner/llm_planner.py, scripts/eval_requests.py]
  - id: C14
    text: Calibrated the harness with an oracle. A perfect LLM scores 100% in the full system, while designs without a validator still proceed with all 6 impossible requests, isolating the validator's contribution from model quality.
    evidence: [results/eval_20261018T035618Z_oracle.json, tests/test_eval.py]
  - id: C15
    text: Added a deterministic grounding check that traces every time, robot, place, and quantity in the LLM's spec back to the request. It corrects a value when the right one is unambiguous, otherwise asks or refuses, and reports every correction. It also refuses requests to set aside site rules.
    evidence: [fleetplanner/grounding.py, tests/test_grounding.py]
  - id: C16
    text: Measured the fix without re-running the model by replaying the saved LLM outputs with and without grounding, and checked for over-fitting (no false positives on all 51 correct specs; a held-out set committed before the fix).
    evidence: [scripts/replay_grounding.py, results/replay_grounding_20261018T042518Z_qwen2.5-7b.json, results/replay_grounding_20261018T044209Z_qwen2.5-14b.json, evals/holdout.yaml]
  - id: C17
    text: Caught and fixed a safety regression introduced by my own grounding check (it assigned a job to a robot the request said was going down). Added a regression test from the model's exact output and a property test over all 80 requests, and showed via offline replay of six saved runs that the fix changes exactly that one outcome.
    evidence: [fleetplanner/grounding.py, tests/test_grounding.py, results/replay_grounding_20261018T211125Z_qwen2.5-7b.json]
  - id: C18
    text: Extended the deterministic checks to refuse implied rule overrides ("aisle 3 looks clear now") while ignoring waiting phrases, and to turn refusals over nonexistent robots or aisles into clarifying questions; patterns derived from dev failures only, with a second held-out set committed before the code.
    evidence: [fleetplanner/grounding.py, tests/test_grounding.py, evals/holdout2.yaml]
  - id: C9
    text: Demonstrated the simulator as referee. A naive plan that ignores an aisle closure is flagged with three closed-zone violations, while the solver's plan for the same request waits out the closure with none.
    evidence: [plans/naive_ignores_closure.json, specs/examples/aisle_closed.yaml, docs/naive.gif, tests/test_core.py]

metrics:
  - id: M1
    name: Safety violations across random shifts (simulator-judged)
    value: "0 / 5,000 shifts"
    status: measured
    method: scripts/stress.py --seeds 5000 on maps/warehouse.yaml
    artifact: results/stress_20261018T032455Z.json
    date: 2026-10-18
  - id: M2
    name: Silent failures (unfinished task not reported by the planner)
    value: "0 / 5,000 shifts"
    status: measured
    method: same run as M1
    artifact: results/stress_20261018T032455Z.json
    date: 2026-10-18
  - id: M3
    name: Task completion rate on random shifts, including deliberately contradictory requests
    value: "95.6%"
    status: measured
    method: same run as M1
    artifact: results/stress_20261018T032455Z.json
    date: 2026-10-18
  - id: M5
    name: Failing shifts the validator stopped before planning (stress test)
    value: "518 / 571 (90.7%), with 0 false rejections among 4,429 shifts that would succeed"
    status: measured
    method: scripts/stress.py --seeds 5000, validator run on each random shift before planning
    artifact: results/stress_20261018T032455Z.json
    date: 2026-10-18
  - id: M6
    name: Overall success on 60 requests, full system vs LLM-only planning (qwen2.5 14B, local)
    value: "95% vs 12%"
    status: measured
    method: scripts/eval_requests.py, judged in the simulator against gold specs plus site rules; temperature 0, single run
    artifact: results/eval_20261018T044209Z_qwen2.5-14b.json
    date: 2026-10-18
  - id: M7
    name: Overall success on 60 requests, full system vs LLM-only planning (qwen2.5 7B, local)
    value: "78% vs 15%"
    status: measured
    method: same as M6
    artifact: results/eval_20261018T042518Z_qwen2.5-7b.json
    date: 2026-10-18
  - id: M8
    name: Requests with a safety violation, by design (14B / 7B)
    value: "LLM-only 25 / 11; LLM carries rules itself 6 / 16; full system 0 / 0"
    status: measured
    method: same runs as M6 and M7
    artifact: results/eval_20261018T044209Z_qwen2.5-14b.json
    date: 2026-10-18
  - id: M9
    name: Validator contribution (full vs no-validator success, 7B / 14B)
    value: "+13 / +7 points; infeasible requests 0% -> 100% for both"
    status: measured
    method: same runs as M6 and M7
    artifact: results/eval_20261018T042518Z_qwen2.5-7b.json
    date: 2026-10-18
  - id: M10
    name: Grounding check on the same saved outputs, full -> grounded success (7B / 14B)
    value: "78% -> 92% / 95% -> 98%; went-ahead-when-it-should-stop 6 -> 0 / 2 -> 0; 0 requests made worse"
    status: measured
    method: scripts/replay_grounding.py on the milestone 4 results (no model re-run); dev set, so optimistic
    artifact: results/replay_grounding_20261018T042518Z_qwen2.5-7b.json
    date: 2026-10-18
  - id: M11
    name: Held-out success (20 requests written before the fix), full system with grounding vs LLM-only planning (7B / 14B)
    value: "85% vs 15% / 90% vs 5%; safety violations 0 vs 2 / 0 vs 8"
    status: measured
    method: scripts/eval_requests.py --requests evals/holdout.yaml, prompt v2, temperature 0, single run
    artifact: results/eval_20261018T210328Z_holdout_qwen2.5-7b.json
    date: 2026-10-18
  - id: M12
    name: Held-out effect of the grounding check alone (same translations, full -> grounded)
    value: "7B 70% -> 85% (3 fixed, 0 broken); 14B 90% -> 90% (no change)"
    status: measured
    method: same runs as M11
    artifact: results/eval_20261018T210939Z_holdout_qwen2.5-14b.json
    date: 2026-10-18
  - id: M4
    name: Test suite passing
    value: 323
    status: measured
    method: pytest, 2026-10-18
    artifact: ""
    date: 2026-10-18

decisions:
  - Measured the design against LLM-only planning instead of asserting it; reported the full system's remaining failures, including the ones that are safe but wrong.
  - Kept the LLM out of planning. It will only translate requests into a typed spec, which a classical solver plans and a validator checks.
  - Plans are plain per-robot action lists judged by an independent simulator, so an LLM-written baseline can be compared fairly.
  - Chose prioritized planning with space-time A* over optimal multi-agent search (CBS) for speed and simplicity, accepting non-optimal plans.
  - A battery floor is enforced on actual routes, because allocation estimates ignore detours.

limitations:
  - Each condition run once at temperature 0; Ollama is not fully deterministic, so differences of a request or two are noise.
  - Held-out failures remain (all safe): an implied rule override planned instead of refused (h18), an over-cautious refusal (h02), refusing instead of asking about unknown names on 7B (h13, h14).
  - Prompt v2 introduced an over-refusal on 7B for requests that add a closure (c03, c10).
  - Held-out set 1 has been used to design v3 and is now effectively a dev set; v3 is measured on held-out set 2 (run pending). No v3 results may be cited until then.
  - The request set is small (60) and written by the system's author, so it may favor the prompt's phrasing.
  - Greedy allocation and prioritized routing are safe but not optimal.
  - Grid world with discrete time; no vehicle dynamics.
  - Synthetic warehouse layout and random workloads.
---

## Context
A testbed for the "LLM translates, solver plans, validator checks" pattern in a domain where bad plans have physical consequences.

## Results
See the README results section and `results/`.
