#!/usr/bin/env python3
"""
Replay saved LLM translations through the grounding check, offline.

Uses the exact outputs stored in an eval results file, so the only thing that
changes is the grounding step. No model is called.

    python scripts/replay_grounding.py results/eval_<...>.json
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fleetplanner import World  # noqa: E402
from fleetplanner.evaluate import load_requests, run_translated, summarize  # noqa: E402
from fleetplanner.translate import TranslateResult, Translation, load_rules  # noqa: E402

world = World.load(ROOT / "maps" / "warehouse.yaml")
for name in sys.argv[1:]:
    data = json.loads(Path(name).read_text())
    standing = load_rules(data["rules"])
    reqs = {r.id: r for r in load_requests(ROOT / "evals" / data.get("requests_file", "requests.yaml"))}
    rows = []
    for row in data["rows"]:
        if row["condition"] != "full":
            continue
        req = reqs[row["id"]]
        t = Translation.model_validate(row["translation"]) if row.get("translation") else None
        tr = TranslateResult(t, row.get("attempts", 1), row.get("latency_s") or 0.0, [], row.get("llm_errors", []))
        rows += [run_translated(world, req, standing, tr, "full"), run_translated(world, req, standing, tr, "grounded")]
    s = summarize(rows)
    src = Path(name).stem.replace("eval_", "")
    out = ROOT / "results" / f"replay_grounding_{src}.json"
    out.write_text(json.dumps({"created_at": datetime.now(timezone.utc).isoformat(), "source": Path(name).name,
                               "model": data["model"], "note": "Saved LLM outputs replayed with and without grounding; "
                               "no model was called.", "summary": s, "rows": rows}, indent=1, default=str))
    print(f"\n{data['model']}  (same saved LLM outputs, prompt as recorded)")
    for k in ("success", "plan_requests_executed_correctly", "spec_exact", "requests_with_safety_violations",
              "stop_requests_handled_correctly", "went_ahead_when_it_should_stop", "stopped_when_it_should_plan"):
        print(f"  {k:34s} full={str(s['full'][k]):>8s}   grounded={str(s['grounded'][k]):>8s}")
    print("  by category:", {c: (s['full']['by_category'][c], s['grounded']['by_category'][c]) for c in s['full']['by_category']})
    full = {r["id"]: r for r in rows if r["condition"] == "full"}
    recorded = {r["id"]: r for r in data["rows"] if r["condition"] == "grounded"}
    if recorded:
        changed = [(i, recorded[i]["success"], r["success"], recorded[i]["unsafe"], r["unsafe"])
                   for r in rows if r["condition"] == "grounded" for i in [r["id"]]
                   if (recorded[i]["success"], recorded[i]["unsafe"]) != (r["success"], r["unsafe"])]
        print(f"  vs grounding as recorded in the run: {len(changed)} changed " +
              ", ".join(f"{i} (success {a}->{b}, unsafe {c}->{d})" for i, a, b, c, d in changed))
    for r in rows:
        if r["condition"] == "grounded" and r["success"] != full[r["id"]]["success"]:
            arrow = "fixed " if r["success"] else "BROKE "
            print(f"  {arrow} {r['id']:4s} {r['status']:8s} {(r.get('message') or '; '.join(r.get('grounding_notes', [])))[:110]}")
        elif r["condition"] == "grounded" and r.get("grounding_notes") and r["success"]:
            print(f"  noted  {r['id']:4s} {'; '.join(r['grounding_notes'])[:110]}")
    print(f"  wrote {out.relative_to(ROOT)}")
