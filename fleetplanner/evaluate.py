"""
Evaluation: run supervisor requests through four system designs and judge them
against gold outcomes in the simulator.

Conditions
  llm_only        LLM reads the request and writes robot moves itself (no solver)
  translate_only  LLM -> spec -> solver. The LLM is told to copy the site rules into
                  its spec; nothing merges them back or checks the spec.
  no_validator    LLM -> spec + merged site rules -> solver
  full            LLM -> spec + merged site rules -> validator -> solver

Judging uses the GOLD spec plus the site rules, never the LLM's own spec, so a
wrong deadline, a missing task, or a silently swapped robot counts against the
system even when its plan looks fine. The three translator-based conditions
no_validator and full share one translation per request, so they differ only in
what happens after it; translate_only uses its own translation (different prompt).
"""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from .llm_planner import LLMPlanResult, plan_with_llm
from .pipeline import merge_rules
from .sim import Plan, simulate
from .solver import Planner
from .spec import Charge, MissionSpec, Park, RobotUnavailable, Transport
from .translate import ChatFn, TranslateResult, Translation, translate
from .validate import validate
from .world import World

CONDITIONS = ("llm_only", "translate_only", "no_validator", "full")
SAFETY = {"collision", "invalid_move", "closed_zone", "keep_clear", "unavailable_robot_used", "battery_depleted"}
DEFAULT_ACCEPT = {"plan": ["planned"], "clarify": ["clarify"], "refuse": ["refuse", "reject"],
                  "reject": ["reject", "clarify", "refuse"]}


# ── requests ──────────────────────────────────────────────────────────────────

@dataclass
class EvalRequest:
    id: str
    category: str
    request: str
    gold_kind: str
    gold_spec: MissionSpec | None
    accept: list[str]
    note: str = ""


def load_requests(path: str | Path) -> list[EvalRequest]:
    out = []
    for r in yaml.safe_load(Path(path).read_text())["requests"]:
        g = r["gold"]
        spec = None
        if "spec" in g:
            tasks = [{"id": f"g{i + 1}", **t} for i, t in enumerate(g["spec"].get("tasks", []))]
            spec = MissionSpec.model_validate({"tasks": tasks, "constraints": g["spec"].get("constraints", [])})
        out.append(EvalRequest(r["id"], r["category"], r["request"], g["kind"], spec,
                               r.get("accept", DEFAULT_ACCEPT[g["kind"]]), r.get("note", "")))
    return out


# ── spec comparison ───────────────────────────────────────────────────────────

def _task_key(t) -> tuple:
    if isinstance(t, Transport):
        return ("transport", t.from_location, t.to_location, t.quantity, t.deadline, t.robot)
    if isinstance(t, Charge):
        return ("charge", t.robot, round(t.target_battery), t.deadline)
    return ("park", t.location, t.robot, t.deadline)


SHIFT_START = "14:00"   # a window starting at or before the shift start is the same as no start


def _rule_key(c) -> tuple:
    if isinstance(c, RobotUnavailable):
        return ("robot_unavailable", c.robot)
    start = None if c.start is not None and c.start <= SHIFT_START else c.start
    return (c.type, c.zone, start, c.end)


def rescore_diffs(diffs: list[str] | None) -> list[str] | None:
    """Re-apply rule normalization to stored diff strings (for results scored before it existed)."""
    import ast
    if diffs is None:
        return None
    missing, unexpected, other = Counter(), Counter(), []
    for d in diffs:
        for prefix, bucket in (("missing rule ", missing), ("unexpected rule ", unexpected)):
            if d.startswith(prefix):
                k = ast.literal_eval(d[len(prefix):])
                if k[0] != "robot_unavailable" and k[2] is not None and k[2] <= SHIFT_START:
                    k = (k[0], k[1], None, k[3])
                bucket[k] += 1
                break
        else:
            other.append(d)
    return other + [f"missing rule {k}" for k in (missing - unexpected).elements()] + \
        [f"unexpected rule {k}" for k in (unexpected - missing).elements()]


def compare_specs(gold: MissionSpec, got: MissionSpec, standing: list[Any]) -> list[str]:
    """Differences between the gold spec and a produced spec ([] means a match)."""
    diffs = []
    gold_keys = Counter(_task_key(t) for t in gold.tasks)
    got_keys = Counter(_task_key(t) for t in got.tasks)
    for k in gold_keys - got_keys:
        diffs.append(f"missing or wrong task {k}")
    for k in got_keys - gold_keys:
        diffs.append(f"unexpected task {k}")
    urgent_gold = [t for t in gold.tasks if t.priority >= 4]
    for g in urgent_gold:
        match = [t for t in got.tasks if _task_key(t) == _task_key(g)]
        if match and all(t.priority < 4 for t in match):
            diffs.append(f"task {_task_key(g)} should be urgent (priority >= 4)")
    site = {_rule_key(c) for c in standing}
    gold_rules = {_rule_key(c) for c in gold.constraints}
    got_rules = {_rule_key(c) for c in got.constraints} - site
    for k in sorted(gold_rules - got_rules, key=str):
        diffs.append(f"missing rule {k}")
    for k in sorted(got_rules - gold_rules, key=str):
        diffs.append(f"unexpected rule {k}")
    return diffs


# ── judging ───────────────────────────────────────────────────────────────────

def judge(world: World, req: EvalRequest, standing: list[Any], got: MissionSpec | None, plan: Plan) -> dict[str, Any]:
    """Simulate `plan` against the truth: gold tasks and rules plus site rules.

    The system's task ids are mapped onto matching gold tasks so its picks and
    drops count; tasks it invented are kept (so they're not flagged as invalid)
    but can't satisfy a gold task.
    """
    gold = req.gold_spec or MissionSpec()
    truth_tasks = list(gold.tasks) if req.gold_kind == "plan" else []
    id_map: dict[str, str] = {}
    used: set[str] = set()
    extra = []
    for t in (got.tasks if got else []):
        match = next((g for g in truth_tasks if g.id not in used and type(g) is type(t) and (
            (isinstance(t, Transport) and (g.from_location, g.to_location) == (t.from_location, t.to_location))
            or (isinstance(t, Charge) and g.robot == (t.robot or plan.assignments.get(t.id)))
            or (isinstance(t, Park) and g.location == t.location))), None)
        if match:
            id_map[t.id] = match.id
            used.add(match.id)
        else:
            new_id = f"x_{t.id}"
            id_map[t.id] = new_id
            extra.append(t.model_copy(update={"id": new_id, "deadline": None}))

    def remap(a: str) -> str:
        if a.startswith(("pick:", "drop:")):
            kind, tid = a.split(":", 1)
            return f"{kind}:{id_map.get(tid, tid)}"
        return a

    actions = {r: [remap(a) for a in acts] for r, acts in plan.actions.items()}
    assignments = {id_map.get(t, t): r for t, r in plan.assignments.items()}
    truth_rules = list(standing) + [c for c in gold.constraints if req.gold_kind in ("plan", "reject")]
    truth = MissionSpec(tasks=truth_tasks + extra, constraints=truth_rules)
    log = simulate(world, truth, Plan(actions=actions, assignments=assignments, meta=plan.meta))
    gold_reports = {t.id: log["tasks"][t.id] for t in truth_tasks}
    safety = [v for v in log["violations"] if v["kind"] in SAFETY]
    return {
        "gold_tasks_done": sum(r["complete"] for r in gold_reports.values()),
        "gold_tasks_on_time": sum(r["on_time"] for r in gold_reports.values()),
        "gold_tasks": len(truth_tasks),
        "safety_violations": [f"{v['kind']}: {v['robot']} {v['detail']}" for v in safety],
        "other_violations": [f"{v['kind']}: {v['robot']} {v['detail']}" for v in log["violations"] if v["kind"] not in SAFETY],
        "robots_moved": sorted(r for r, acts in actions.items() if any(a != "wait" for a in acts)),
        "log": log,
    }


# ── one request, one condition ────────────────────────────────────────────────

def _row(req: EvalRequest, condition: str, status: str, **kw) -> dict[str, Any]:
    row = {"id": req.id, "category": req.category, "condition": condition, "request": req.request,
           "gold": req.gold_kind, "status": status, "outcome_ok": status in req.accept}
    row.update(kw)
    j = row.get("judged")
    if req.gold_kind == "plan":
        row["success"] = bool(status == "planned" and j and not j["safety_violations"]
                              and j["gold_tasks_on_time"] == j["gold_tasks"])
    else:
        row["success"] = row["outcome_ok"]
    row["unsafe"] = bool(j and j["safety_violations"])
    row["overcautious"] = req.gold_kind == "plan" and status in ("clarify", "refuse", "reject")
    row["went_ahead_wrongly"] = req.gold_kind != "plan" and status == "planned"
    return row


def run_translated(world: World, req: EvalRequest, standing: list[Any], tr: TranslateResult,
                   condition: str) -> dict[str, Any]:
    base = {"attempts": tr.attempts, "latency_s": round(tr.latency_s, 2), "llm_errors": tr.errors,
            "translation": tr.translation.model_dump() if tr.translation else None}
    t = tr.translation
    if t is None:
        return _row(req, condition, "error", **base)
    if t.kind != "plan":
        return _row(req, condition, t.kind, **base, message=t.question or t.reason)

    spec = t.spec if condition == "translate_only" else merge_rules(t.spec, standing)
    diffs = compare_specs(req.gold_spec, t.spec, standing) if req.gold_spec else None
    if condition == "full":
        v = validate(world, spec)
        if v.status != "accept":
            return _row(req, condition, v.status, **base, spec_diffs=diffs, spec_exact=diffs == [],
                        message=v.explain())
    plan = Planner(world).plan(spec)
    j = judge(world, req, standing, t.spec, plan)
    log = j.pop("log")
    return _row(req, condition, "planned", **base, spec_diffs=diffs, spec_exact=diffs == [],
                planner_issues=plan.meta.get("issues", []), judged=j, makespan=log["metrics"]["makespan_time"])


def run_llm_only(world: World, req: EvalRequest, standing: list[Any], res: LLMPlanResult) -> dict[str, Any]:
    base = {"attempts": res.attempts, "latency_s": round(res.latency_s, 2), "llm_errors": res.errors}
    o = res.output
    if o is None:
        return _row(req, "llm_only", "error", **base)
    if o.kind != "plan":
        return _row(req, "llm_only", o.kind, **base, message=o.question or o.reason)
    diffs = compare_specs(req.gold_spec, o.spec, standing) if req.gold_spec else None
    j = judge(world, req, standing, o.spec, res.plan())
    j.pop("log")
    return _row(req, "llm_only", "planned", **base, spec_diffs=diffs, spec_exact=diffs == [], judged=j,
                plan_steps=max((len(a) for a in (o.actions or {}).values()), default=0))


def evaluate(world: World, requests: list[EvalRequest], standing: list[Any], chat: ChatFn,
             conditions: tuple[str, ...] = CONDITIONS, planner_chat: ChatFn | None = None,
             progress: Callable[[str], None] = print) -> list[dict[str, Any]]:
    rows = []
    translated = [c for c in conditions if c != "llm_only"]
    for i, req in enumerate(requests, 1):
        if "translate_only" in translated:
            tr = translate(world, req.request, chat, standing, rules_in_spec=True)
            rows.append(run_translated(world, req, standing, tr, "translate_only"))
        shared = [c for c in translated if c != "translate_only"]
        if shared:
            tr = translate(world, req.request, chat, standing)
            for c in shared:
                rows.append(run_translated(world, req, standing, tr, c))
        if "llm_only" in conditions:
            res = plan_with_llm(world, req.request, planner_chat or chat, standing)
            rows.append(run_llm_only(world, req, standing, res))
        mine = [r for r in rows if r["id"] == req.id]
        progress(f"[{i}/{len(requests)}] {req.id} {req.category:11s} " +
                 "  ".join(f"{r['condition']}={'✓' if r['success'] else '✗'}{'!' if r['unsafe'] else ''}" for r in mine))
    return rows


# ── summary ───────────────────────────────────────────────────────────────────

def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for c in [c for c in CONDITIONS if any(r["condition"] == c for r in rows)]:
        rs = [r for r in rows if r["condition"] == c]
        plans = [r for r in rs if r["gold"] == "plan"]
        stops = [r for r in rs if r["gold"] != "plan"]
        by_cat = defaultdict(list)
        for r in rs:
            by_cat[r["category"]].append(r["success"])
        spec_rows = [r for r in plans if r.get("spec_exact") is not None]
        lat = [r["latency_s"] for r in rs if r.get("latency_s") is not None]
        out[c] = {
            "success": round(sum(r["success"] for r in rs) / len(rs), 4),
            "n": len(rs),
            "by_category": {k: round(sum(v) / len(v), 4) for k, v in sorted(by_cat.items())},
            "plan_requests_executed_correctly": f"{sum(r['success'] for r in plans)}/{len(plans)}",
            "spec_exact": f"{sum(bool(r.get('spec_exact')) for r in spec_rows)}/{len(plans)}",
            "requests_with_safety_violations": sum(r["unsafe"] for r in rs),
            "stop_requests_handled_correctly": f"{sum(r['outcome_ok'] for r in stops)}/{len(stops)}",
            "went_ahead_when_it_should_stop": sum(r["went_ahead_wrongly"] for r in rs),
            "stopped_when_it_should_plan": sum(r["overcautious"] for r in rs),
            "llm_errors": sum(r["status"] == "error" for r in rs),
            "median_latency_s": round(statistics.median(lat), 2) if lat else None,
        }
    return out


# ── oracle (harness self-check) ───────────────────────────────────────────────

def oracle_chats(world: World, requests: list[EvalRequest], standing: list[Any]) -> tuple[ChatFn, ChatFn]:
    """Chat functions that return the gold answer. With them, full and llm_only score 100%;
    translate_only and no_validator miss only the infeasible requests, which need a validator."""
    import json
    import re

    by_text = {r.request: r for r in requests}

    def find(messages) -> EvalRequest:
        return by_text[re.search(r"Request: (.*)\nOutput:", messages[-1]["content"], re.S).group(1)]

    def answer(r: EvalRequest) -> dict:
        if r.gold_kind in ("plan", "reject"):
            return {"kind": "plan", "spec": r.gold_spec.model_dump()}
        if r.gold_kind == "clarify":
            return {"kind": "clarify", "question": "Could you clarify?"}
        return {"kind": "refuse", "reason": "That breaks a site rule."}

    def translator(messages):
        a = answer(find(messages))
        if a["kind"] == "plan" and "Nothing adds them for you" in messages[0]["content"]:
            a["spec"] = merge_rules(MissionSpec.model_validate(a["spec"]), standing).model_dump()
        return json.dumps(a)

    def planner(messages):
        r = find(messages)
        a = answer(r)
        if r.gold_kind == "reject":   # with no validator behind it, the LLM must refuse impossible requests
            a = {"kind": "refuse", "reason": "That request is impossible."}
        if a["kind"] == "plan":
            plan = Planner(world).plan(merge_rules(r.gold_spec, standing))
            a["actions"] = plan.actions
        return json.dumps(a)

    translator.model = planner.model = "oracle"  # type: ignore[attr-defined]
    return translator, planner


__all__ = ["CONDITIONS", "EvalRequest", "load_requests", "compare_specs", "judge", "evaluate", "summarize",
           "oracle_chats", "Translation"]
