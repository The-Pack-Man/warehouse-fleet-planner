#!/usr/bin/env python3
"""Recompute spec-match scoring and summaries for saved eval results after a scoring fix.

Success and safety come from the simulator and are unchanged; only spec_exact is recomputed.
The original scoring is kept under "rescored.previous_summary".

    python scripts/rescore.py results/eval_*.json
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fleetplanner.evaluate import rescore_diffs, summarize  # noqa: E402

REASON = "Rule windows starting at or before the shift start (14:00) now match rules with no start time."

for name in sys.argv[1:]:
    path = Path(name)
    data = json.loads(path.read_text())
    if data.get("rescored", {}).get("reason") == REASON:
        print(f"{path.name}: already rescored")
        continue
    changed = 0
    for r in data["rows"]:
        if r.get("spec_diffs") is not None:
            new = rescore_diffs(r["spec_diffs"])
            if new != r["spec_diffs"]:
                changed += 1
            r["spec_diffs"], r["spec_exact"] = new, new == []
    data["rescored"] = {"at": datetime.now(timezone.utc).isoformat(), "reason": REASON,
                        "rows_changed": changed, "previous_summary": data["summary"]}
    data["summary"] = summarize(data["rows"])
    path.write_text(json.dumps(data, indent=1, default=str))
    print(f"{path.name}: {changed} rows rescored")
