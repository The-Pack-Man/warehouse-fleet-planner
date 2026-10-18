"""
python -m fleetplanner run MAP SPEC [--plan PLAN.json] [--out run.json] [--html run.html] [--open]
python -m fleetplanner view RUN.json [--html run.html] [--open]
python -m fleetplanner ask MAP "request" [--rules RULES.yaml] [--model M] [--html run.html] [--open]

`run` plans a MissionSpec with the classical solver (or replays a given plan)
and simulates it. `view` turns a run log into a standalone replay page.
"""
from __future__ import annotations

import argparse
import json
import webbrowser
from pathlib import Path

import yaml

from .sim import Plan, simulate
from .solver import Planner
from .spec import MissionSpec
from .viewer import write_html
from .world import World


def _html(log: dict, html: str | None, open_it: bool, name: str) -> None:
    if not html:
        return
    path = write_html(log, html, name)
    print(f"wrote {path}")
    if open_it:
        webbrowser.open(path.resolve().as_uri())


def _ask(args) -> None:
    from .pipeline import handle_request
    from .translate import LLMUnavailable, load_rules, openai_chat

    world = World.load(args.map)
    standing = load_rules(yaml.safe_load(Path(args.rules).read_text())) if args.rules else []
    chat = openai_chat(model=args.model, base_url=args.base_url)
    try:
        out = handle_request(world, args.request, chat, standing)
    except LLMUnavailable as exc:
        raise SystemExit(f"Model unavailable: {exc}")
    label = {"planned": "Planned", "clarify": "Needs clarification", "refuse": "Refused",
             "reject": "Rejected", "error": "Error"}[out["status"]]
    print(f"{label} ({out['model']}, {out['translator']['attempts']} attempt(s), {out['translator']['latency_s']:.1f}s)")
    print(out["message"])
    for note in out.get("notes", []):
        print(f"  note: {note}")
    if args.show_spec and out.get("spec"):
        print(yaml.safe_dump(out["spec"], sort_keys=False))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(out, indent=1, default=str))
        print(f"wrote {args.json}")
    if out.get("log"):
        _html(out["log"], args.html, args.open, "request")


def main() -> None:
    p = argparse.ArgumentParser(prog="fleetplanner")
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="plan a spec with the solver and simulate it")
    run.add_argument("map")
    run.add_argument("spec")
    run.add_argument("--plan", help="replay this plan (JSON with per-robot actions) instead of running the solver")
    run.add_argument("--out", help="write the run log (JSON) here")
    run.add_argument("--html", help="write a standalone replay page here")
    run.add_argument("--open", action="store_true", help="open the replay page in a browser")
    ask = sub.add_parser("ask", help="plain-language request -> LLM -> validator -> planner -> simulator")
    ask.add_argument("map")
    ask.add_argument("request")
    ask.add_argument("--rules", help="YAML list of standing site rules")
    ask.add_argument("--model", help="model name (default: $LLM_MODEL or qwen2.5:7b)")
    ask.add_argument("--base-url", help="OpenAI-compatible endpoint (default: $LLM_BASE_URL or local Ollama)")
    ask.add_argument("--json", help="write the full outcome (translation, validation, run log) here")
    ask.add_argument("--html", help="write a replay page here if a plan was made")
    ask.add_argument("--open", action="store_true")
    ask.add_argument("--show-spec", action="store_true", help="print the spec the LLM produced")
    view = sub.add_parser("view", help="turn a run log into a standalone replay page")
    view.add_argument("log")
    view.add_argument("--html", help="output path (default: next to the log)")
    view.add_argument("--open", action="store_true")
    args = p.parse_args()

    if args.cmd == "ask":
        _ask(args)
        return

    if args.cmd == "view":
        log = json.loads(Path(args.log).read_text())
        out = args.html or str(Path(args.log).with_suffix(".html"))
        _html(log, out, args.open, Path(args.log).stem)
        return

    world = World.load(args.map)
    spec = MissionSpec.model_validate(yaml.safe_load(Path(args.spec).read_text()))
    if args.plan:
        plan = Plan.model_validate(json.loads(Path(args.plan).read_text()))
        plan.meta.setdefault("planner", f"hand-written plan ({Path(args.plan).name})")
    else:
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
    _html(log, args.html, args.open, Path(args.spec).stem)


if __name__ == "__main__":
    main()
