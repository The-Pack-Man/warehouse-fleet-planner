#!/usr/bin/env python3
"""
Run the request evaluation (evals/requests.yaml) against one or more models.

    python scripts/eval_requests.py --models qwen2.5:7b
    python scripts/eval_requests.py --models qwen2.5:7b,qwen2.5:14b
    python scripts/eval_requests.py --models qwen2.5:7b --conditions full,llm_only --ids t01,u01
    python scripts/eval_requests.py --oracle          # harness self-check, no LLM needed

Writes results/eval_<timestamp>_<model>.json with every row and a summary.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fleetplanner import World  # noqa: E402
from fleetplanner.evaluate import CONDITIONS, evaluate, load_requests, oracle_chats, summarize  # noqa: E402
from fleetplanner.llm_planner import LLMPlanOutput  # noqa: E402
from fleetplanner.translate import PROMPT_VERSION, LLMUnavailable, load_rules, openai_chat  # noqa: E402


def print_summary(model: str, summary: dict) -> None:
    conds = list(summary)
    print(f"\n{model}")
    print(f"{'':34s}" + "".join(f"{c:>16s}" for c in conds))
    lines = [
        ("Overall success", lambda s: f"{s['success']:.0%}"),
        ("Plan requests executed correctly", lambda s: s["plan_requests_executed_correctly"]),
        ("Spec exactly right", lambda s: s["spec_exact"]),
        ("Requests with safety violations", lambda s: str(s["requests_with_safety_violations"])),
        ("Stop requests handled correctly", lambda s: s["stop_requests_handled_correctly"]),
        ("Went ahead when it should stop", lambda s: str(s["went_ahead_when_it_should_stop"])),
        ("Stopped when it should plan", lambda s: str(s["stopped_when_it_should_plan"])),
        ("LLM output errors", lambda s: str(s["llm_errors"])),
        ("Median latency (s)", lambda s: str(s["median_latency_s"])),
    ]
    for label, f in lines:
        print(f"{label:34s}" + "".join(f"{f(summary[c]):>16s}" for c in conds))
    cats = sorted({k for c in conds for k in summary[c]["by_category"]})
    print("Success by category")
    for k in cats:
        print(f"  {k:32s}" + "".join(f"{summary[c]['by_category'].get(k, 0):>16.0%}" for c in conds))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="qwen2.5:7b", help="comma-separated model names")
    ap.add_argument("--oracle", action="store_true", help="use gold answers instead of an LLM (self-check)")
    ap.add_argument("--conditions", default=",".join(CONDITIONS))
    ap.add_argument("--ids", help="comma-separated request ids")
    ap.add_argument("--categories", help="comma-separated categories")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--base-url", help="OpenAI-compatible endpoint (default: local Ollama at 127.0.0.1)")
    ap.add_argument("--plan-max-tokens", type=int, default=4096, help="output cap for the LLM-only planner")
    ap.add_argument("--map", default=str(ROOT / "maps" / "warehouse.yaml"))
    ap.add_argument("--rules", default=str(ROOT / "rules" / "today.yaml"))
    ap.add_argument("--requests", default=str(ROOT / "evals" / "requests.yaml"))
    ap.add_argument("--out", default=str(ROOT / "results"))
    args = ap.parse_args()

    world = World.load(args.map)
    standing = load_rules(yaml.safe_load(Path(args.rules).read_text()))
    reqs = load_requests(args.requests)
    if args.ids:
        keep = set(args.ids.split(","))
        reqs = [r for r in reqs if r.id in keep]
    if args.categories:
        keep = set(args.categories.split(","))
        reqs = [r for r in reqs if r.category in keep]
    reqs = reqs[: args.limit] if args.limit else reqs
    conditions = tuple(c.strip() for c in args.conditions.split(","))

    models = ["oracle"] if args.oracle else [m.strip() for m in args.models.split(",")]
    for model in models:
        if args.oracle:
            chat, planner_chat = oracle_chats(world, reqs, standing)
        else:
            chat = openai_chat(model=model, base_url=args.base_url)
            planner_chat = openai_chat(model=model, base_url=args.base_url, schema=LLMPlanOutput,
                                       max_tokens=args.plan_max_tokens)
        print(f"Evaluating {len(reqs)} requests × {len(conditions)} conditions with {model}")
        t0 = time.monotonic()
        try:
            rows = evaluate(world, reqs, standing, chat, conditions, planner_chat)
        except LLMUnavailable as exc:
            sys.exit(f"Model unavailable: {exc}")
        summary = summarize(rows)
        print_summary(model, summary)
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        tag = "" if Path(args.requests).stem == "requests" else f"{Path(args.requests).stem}_"
        path = out / f"eval_{stamp}_{tag}{re.sub(r'[^A-Za-z0-9.-]+', '-', model)}.json"
        path.write_text(json.dumps({
            "created_at": datetime.now(timezone.utc).isoformat(), "model": model, "prompt_version": PROMPT_VERSION,
            "conditions": list(conditions), "requests_file": Path(args.requests).name,
            "n_requests": len(reqs), "rules": [c.model_dump() for c in standing],
            "duration_s": round(time.monotonic() - t0, 1), "summary": summary, "rows": rows,
        }, indent=1, default=str))
        print(f"\nwrote {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")


if __name__ == "__main__":
    main()
