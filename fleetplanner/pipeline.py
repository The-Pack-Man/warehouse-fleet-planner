"""
End-to-end request handling:

  request ─► translate (LLM) ─► ground ─► merge standing site rules ─► validate ─► plan ─► simulate

Standing rules come from the site, not the request, so a request (or an LLM
mistake) can add rules but never remove one.
"""
from __future__ import annotations

from typing import Any

from .sim import simulate
from .solver import Planner
from .spec import MissionSpec
from .grounding import ground
from .translate import ChatFn, translate
from .validate import validate
from .world import World


def merge_rules(spec: MissionSpec, standing: list[Any]) -> MissionSpec:
    seen = {c.model_dump_json() for c in standing}
    extra = [c for c in spec.constraints if c.model_dump_json() not in seen]
    return MissionSpec(tasks=spec.tasks, constraints=[*standing, *extra])


def handle_request(world: World, request: str, chat: ChatFn, standing: list[Any] | None = None,
                   max_repairs: int = 2) -> dict[str, Any]:
    standing = standing or []
    tr = translate(world, request, chat, standing, max_repairs=max_repairs)
    out: dict[str, Any] = {
        "request": request,
        "model": getattr(chat, "model", None),
        "translation": tr.translation.model_dump() if tr.translation else None,
        "translator": {"attempts": tr.attempts, "latency_s": round(tr.latency_s, 3), "errors": tr.errors, "raw": tr.raw},
        "standing_rules": [c.model_dump() for c in standing],
    }
    t = tr.translation
    if t is None:
        return {**out, "status": "error",
                "message": "The language model didn't return a usable answer, so nothing was planned."}
    if t.kind == "clarify":
        return {**out, "status": "clarify", "message": t.question}
    if t.kind == "refuse":
        return {**out, "status": "refuse", "message": t.reason}

    g = ground(world, request, t.spec, standing)
    out["grounding"] = {"status": g.status, "notes": g.notes, "message": g.message}
    if g.status != "ok":
        return {**out, "status": g.status, "message": g.message, "notes": g.notes}
    spec = merge_rules(g.spec, standing)
    v = validate(world, spec)
    out["spec"] = spec.model_dump()
    out["validation"] = v.model_dump()
    if v.status != "accept":
        return {**out, "status": v.status, "message": v.explain()}

    plan = Planner(world).plan(spec)
    plan.meta["planner"] = f"solver, from a request translated by {out['model'] or 'an LLM'}"
    log = simulate(world, spec, plan)
    log["request"] = request
    m = log["metrics"]
    notes = g.notes + [p.message for p in v.problems] + plan.meta.get("issues", [])
    message = (f"Planned {m['tasks_complete']}/{m['tasks_total']} tasks, {m['tasks_on_time']} on time, "
               f"{m['violation_count']} violations. Done by {m['makespan_time'][:5]}.")
    return {**out, "status": "planned", "message": message, "notes": notes, "log": log}
