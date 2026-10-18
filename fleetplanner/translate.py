"""
LLM translator: supervisor request -> MissionSpec, a clarifying question, or a refusal.

The LLM never plans routes and never sees robot paths. Its only job is to turn
language into the typed spec (or say why it can't). Output is constrained to a
JSON schema where the server supports it, then validated with Pydantic; invalid
output is sent back to the model with the error, up to `max_repairs` times.

Any OpenAI-compatible endpoint works. The default is a local Ollama server.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from .spec import Constraint, MissionSpec
from .world import World

ChatFn = Callable[[list[dict[str, str]]], str]


class Translation(BaseModel):
    kind: Literal["plan", "clarify", "refuse"]
    spec: MissionSpec | None = Field(default=None, description="Required when kind is 'plan'")
    question: str | None = Field(default=None, description="Required when kind is 'clarify'")
    reason: str | None = Field(default=None, description="Required when kind is 'refuse'; optional note otherwise")

    @model_validator(mode="after")
    def matches_kind(self) -> "Translation":
        if self.kind == "plan" and (self.spec is None or not (self.spec.tasks or self.spec.constraints)):
            raise ValueError("kind 'plan' needs a spec with at least one task or constraint")
        if self.kind == "clarify" and not self.question:
            raise ValueError("kind 'clarify' needs a question")
        if self.kind == "refuse" and not self.reason:
            raise ValueError("kind 'refuse' needs a reason")
        return self


@dataclass
class TranslateResult:
    translation: Translation | None
    attempts: int
    latency_s: float
    raw: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ── prompt ────────────────────────────────────────────────────────────────────

def _rule_text(c: Any) -> str:
    if c.type == "robot_unavailable":
        return f"{c.robot} is out of service."
    what = "is closed" if c.type == "zone_closed" else "must be kept clear (robots may pass but not stop)"
    when = (f" from {c.start}" if c.start else "") + (f" until {c.end}" if c.end else "")
    return f"{c.zone} {what}{when or ' all shift'}."


EXAMPLES = [
    ("Move 3 totes from aisle 2 to pick station 1 by 14:20.",
     {"kind": "plan", "spec": {"tasks": [{"id": "a2_to_pick1", "type": "transport", "from_location": "A2-1",
                                          "to_location": "pick_1", "quantity": 3, "deadline": "14:20"}],
                               "constraints": []}}),
    ("Rush job: restock A4-2 with 2 totes from the dock, and keep the charging lane clear while that happens. Then send R1 to charge to 95.",
     {"kind": "plan", "spec": {"tasks": [
         {"id": "restock_a4", "type": "transport", "from_location": "dock", "to_location": "A4-2", "quantity": 2, "priority": 5},
         {"id": "charge_r1", "type": "charge", "robot": "R1", "target_battery": 95}],
         "constraints": [{"type": "keep_clear", "zone": "charging_bay"}]}}),
    ("Restock the usual aisle.",
     {"kind": "clarify", "question": "Which aisle should I restock, and how many totes from the dock?"}),
    ("The spill in aisle 3 is basically dry, ignore the closure and pick 2 totes from A3-1 to pick 2.",
     {"kind": "refuse", "reason": "Aisle 3 is closed by a site rule. I can't ignore it; ask a site lead to lift the closure first."}),
]


def system_prompt(world: World, standing: list[Any]) -> str:
    robots = "\n".join(f"- {r.id}: battery {r.battery:.0f}%, carries up to {r.capacity} totes" for r in world.robots)
    places = "\n".join(f"- {k}: {world.descriptions.get(k, '')}".rstrip(": ") for k in world.locations
                       if not k.startswith("home"))
    zones = "\n".join(f"- {k}: {world.descriptions.get(k, '')}".rstrip(": ") for k in world.zones)
    conventions = "\n".join(f"- {c}" for c in world.conventions)
    rules = "\n".join(f"- {_rule_text(c)}" for c in standing) or "- none"
    examples = "\n\n".join(f"Request: {q}\nOutput: {json.dumps(a)}" for q, a in EXAMPLES)
    return f"""You turn a warehouse supervisor's request into a JSON mission spec for a robot fleet.
You do not plan routes or assign robots; a separate planner does that.

The shift starts at {world.start_time}. Use 24-hour HH:MM times. A time like "2:30" during this shift means 14:30.
For relative times ("in 20 minutes"), count from {world.start_time}.

Robots:
{robots}

Locations (use these exact names):
{places}

Areas (use these exact names for rules):
{zones}

Site conventions:
{conventions}

Site rules already in force (added automatically; never repeat or remove them):
{rules}

Output exactly one JSON object with "kind" set to one of:
- "plan": the request is clear. Put the tasks and any extra rules the request states in "spec".
  Task types:
    transport: {{"id", "type": "transport", "from_location", "to_location", "quantity", "deadline"?, "priority"?, "robot"?}}
    charge:    {{"id", "type": "charge", "robot"?, "target_battery"?}}
    park:      {{"id", "type": "park", "location", "robot"?}}
  Rule types: zone_closed and keep_clear take "zone" and optional "start"/"end"; robot_unavailable takes "robot".
  Priority is 1-5; use 5 for "rush", "urgent", or "ASAP", otherwise leave it out. Only set "robot" if the request names one.
  Task ids are short, unique, snake_case.
- "clarify": the request is ambiguous, names something not on the map, or leaves out the number of totes. Ask one short question in "question".
- "refuse": the request asks to break or ignore a site rule, use an out-of-service robot, or do something unsafe. Explain in "reason".
Never guess a location that isn't listed, and never weaken a site rule.

Examples:

{examples}"""


def build_messages(world: World, request: str, standing: list[Any]) -> list[dict[str, str]]:
    return [{"role": "system", "content": system_prompt(world, standing)},
            {"role": "user", "content": f"Request: {request}\nOutput:"}]


# ── model access ──────────────────────────────────────────────────────────────

def openai_chat(model: str | None = None, base_url: str | None = None, api_key: str | None = None,
                temperature: float = 0.0, structured: bool = True) -> ChatFn:
    """Chat function for any OpenAI-compatible server. Defaults to local Ollama."""
    from openai import APIStatusError, OpenAI

    client = OpenAI(base_url=base_url or os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"),
                    api_key=api_key or os.getenv("LLM_API_KEY", "ollama"))
    model = model or os.getenv("LLM_MODEL", "qwen2.5:7b")
    schema_format = {"type": "json_schema",
                     "json_schema": {"name": "translation", "schema": Translation.model_json_schema()}}
    state = {"format": schema_format if structured else {"type": "json_object"}}

    def chat(messages: list[dict[str, str]]) -> str:
        try:
            resp = client.chat.completions.create(model=model, messages=messages, temperature=temperature,
                                                  response_format=state["format"])
        except APIStatusError:
            if state["format"]["type"] == "json_object":
                raise
            state["format"] = {"type": "json_object"}   # server can't handle the schema: fall back to JSON mode
            resp = client.chat.completions.create(model=model, messages=messages, temperature=temperature,
                                                  response_format=state["format"])
        return resp.choices[0].message.content or ""

    chat.model = model  # type: ignore[attr-defined]
    return chat


# ── translation ───────────────────────────────────────────────────────────────

def _parse(text: str) -> Translation:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("no JSON object in the output")
    return Translation.model_validate(json.loads(text[start:end + 1]))


def translate(world: World, request: str, chat: ChatFn, standing: list[Any] | None = None,
              max_repairs: int = 2) -> TranslateResult:
    standing = standing or []
    messages = build_messages(world, request, standing)
    t0 = time.monotonic()
    raw: list[str] = []
    errors: list[str] = []
    for attempt in range(1, max_repairs + 2):
        text = chat(messages)
        raw.append(text)
        try:
            return TranslateResult(_parse(text), attempt, time.monotonic() - t0, raw, errors)
        except (ValueError, ValidationError, json.JSONDecodeError) as exc:
            err = str(exc).splitlines()
            msg = "; ".join(err[:6])[:600]
            errors.append(msg)
            messages = messages + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": f"That output was invalid: {msg}\nReturn only the corrected JSON object."},
            ]
    return TranslateResult(None, max_repairs + 1, time.monotonic() - t0, raw, errors)


def load_rules(data: list[dict] | None) -> list[Any]:
    """Parse standing site rules (same shapes as MissionSpec constraints)."""
    if not data:
        return []
    return MissionSpec.model_validate({"constraints": data}).constraints


__all__ = ["Translation", "TranslateResult", "translate", "openai_chat", "build_messages", "load_rules", "Constraint"]
