"""
LLM-only baseline: the LLM reads the request AND writes every robot's moves.

This is the design the project argues against. It is given the same
information the solver uses (grid, coordinates, robots, batteries, rule
windows, movement and collision rules) so the comparison is fair, and its plan
is judged by the same simulator.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from .sim import Plan
from .spec import MissionSpec, RobotUnavailable
from .translate import ChatFn, _rule_text
from .world import World

ACTION = re.compile(r"^(N|S|E|W|wait|charge|pick:\S+|drop:\S+)$")


class LLMPlanOutput(BaseModel):
    kind: Literal["plan", "clarify", "refuse"]
    spec: MissionSpec | None = Field(default=None, description="The tasks you planned, with ids used in actions")
    actions: dict[str, list[str]] | None = Field(default=None, description="Robot id -> one action per time step")
    question: str | None = None
    reason: str | None = None

    @field_validator("actions")
    @classmethod
    def valid_actions(cls, actions):
        for rid, acts in (actions or {}).items():
            bad = [a for a in acts if not ACTION.match(a)]
            if bad:
                raise ValueError(f"{rid}: invalid actions {bad[:5]}; use N, S, E, W, wait, charge, pick:<task>, drop:<task>")
        return actions

    @model_validator(mode="after")
    def matches_kind(self):
        if self.kind == "plan" and (self.spec is None or self.actions is None):
            raise ValueError("kind 'plan' needs both 'spec' and 'actions'")
        if self.kind == "clarify" and not self.question:
            raise ValueError("kind 'clarify' needs a question")
        if self.kind == "refuse" and not self.reason:
            raise ValueError("kind 'refuse' needs a reason")
        return self


@dataclass
class LLMPlanResult:
    output: LLMPlanOutput | None
    attempts: int
    latency_s: float
    raw: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def plan(self) -> Plan | None:
        if not self.output or self.output.kind != "plan":
            return None
        return Plan(actions=self.output.actions or {}, meta={"planner": "LLM only"})


def _rect(cells) -> list[int]:
    xs, ys = [c[0] for c in cells], [c[1] for c in cells]
    return [min(xs), min(ys), max(xs), max(ys)]


def system_prompt(world: World, standing: list[Any]) -> str:
    rows = world.grid_text.splitlines()
    ruler = "    " + "".join(str(x % 10) for x in range(world.width))
    grid = "\n".join([ruler] + [f"{y:>3} {row}" for y, row in enumerate(rows)])
    robots = "\n".join(f"- {r.id}: starts at {list(world.locations[r.home])}, battery {r.battery:.0f}%, carries {r.capacity} totes"
                       for r in world.robots)
    places = "\n".join(f"- {k} at {list(v)}: {world.descriptions.get(k, '')}" for k, v in world.locations.items()
                       if not k.startswith("home"))
    zones = "\n".join(f"- {k}: x {r[0]}-{r[2]}, y {r[1]}-{r[3]} ({world.descriptions.get(k, '')})"
                      for k, cells in world.zones.items() for r in [_rect(cells)])
    rules = "\n".join(f"- {_rule_text(c)}" for c in standing) or "- none"
    b = world.battery
    return f"""You control a warehouse robot fleet. Read the supervisor's request and write every robot's moves yourself.

Grid ({world.width} wide, {world.height} tall). x grows to the right, y grows downward. '#' is wall or shelving; '.' is floor.
{grid}

Time: step 0 is {world.start_time}. Each step is {world.step_seconds} seconds, so 14:10 is step {world.clock_to_step('14:10') if world.start_time == '14:00' else 60}.

Robots:
{robots}

Locations:
{places}

Areas:
{zones}

Site conventions:
{chr(10).join('- ' + c for c in world.conventions)}

Site rules in force (you must obey them):
{rules}

Actions, one per robot per step:
- N (y-1), S (y+1), E (x+1), W (x-1): move one cell. Never into '#'.
- wait: stay put.
- pick:<task_id>: load one tote at that task's from_location. A robot holds at most {world.robots[0].capacity}.
- drop:<task_id>: unload one tote at that task's to_location.
- charge: +{b.charge_rate:g}% battery per step, only at a charger.
Moving costs {b.move_cost:g}% battery per step; waiting, picking, and dropping cost {b.idle_cost:g}%. Never reach 0%.
Two robots may never be in the same cell at the same step, and may never swap cells in one step.
A closed area may not contain any robot during its window. In a keep-clear area robots may pass but not wait, pick, drop, or charge.
An out-of-service robot must only wait. After its list ends, a robot stays where it is.

Output exactly one JSON object:
- {{"kind": "plan", "spec": {{"tasks": [...], "constraints": [...]}}, "actions": {{"R1": ["N", "E", ...], ...}}}}
  Tasks use the types transport (id, from_location, to_location, quantity, deadline?, priority?, robot?), charge (id, robot?, target_battery?), and park (id, location, robot?).
  Put any extra rules the request states in "constraints" (zone_closed / keep_clear with zone, start?, end?; robot_unavailable with robot).
- {{"kind": "clarify", "question": "..."}} if the request is ambiguous or names something not on the map.
- {{"kind": "refuse", "reason": "..."}} if the request asks to break a site rule or use an out-of-service robot,
  or if it is impossible or contradicts itself (for example a deadline that can't be met). No one checks your plan
  before it runs."""


def _parse(text: str) -> LLMPlanOutput:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("no JSON object in the output")
    return LLMPlanOutput.model_validate(json.loads(text[start:end + 1]))


def plan_with_llm(world: World, request: str, chat: ChatFn, standing: list[Any] | None = None,
                  max_repairs: int = 2) -> LLMPlanResult:
    messages = [{"role": "system", "content": system_prompt(world, standing or [])},
                {"role": "user", "content": f"Request: {request}\nOutput:"}]
    t0 = time.monotonic()
    raw, errors = [], []
    for attempt in range(1, max_repairs + 2):
        text = chat(messages)
        raw.append(text)
        try:
            return LLMPlanResult(_parse(text), attempt, time.monotonic() - t0, raw, errors)
        except (ValueError, ValidationError, json.JSONDecodeError) as exc:
            msg = "; ".join(str(exc).splitlines()[:6])[:600]
            errors.append(msg)
            messages = messages + [{"role": "assistant", "content": text},
                                   {"role": "user", "content": f"That output was invalid: {msg}\nReturn only the corrected JSON object."}]
    return LLMPlanResult(None, max_repairs + 1, time.monotonic() - t0, raw, errors)


__all__ = ["LLMPlanOutput", "LLMPlanResult", "plan_with_llm", "system_prompt", "RobotUnavailable"]
