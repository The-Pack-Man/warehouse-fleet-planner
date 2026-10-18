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

# v1: original prompt (milestone 3-4 results). v2: deadline/window guidance, named robots,
# self-declared outages, clarify-not-refuse for unknown names (from milestone 4 failures).
# v3: requests may add rules; temporary closures are planned, not refused; claims that a
# rule's reason is over are refusals (from dev-set and held-out-1 failures).
PROMPT_VERSION = 3


class LLMUnavailable(RuntimeError):
    """The model server can't be reached or doesn't have the requested model."""


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


def system_prompt(world: World, standing: list[Any], rules_in_spec: bool = False) -> str:
    robots = "\n".join(f"- {r.id}: battery {r.battery:.0f}%, carries up to {r.capacity} totes" for r in world.robots)
    places = "\n".join(f"- {k}: {world.descriptions.get(k, '')}".rstrip(": ") for k in world.locations
                       if not k.startswith("home"))
    zones = "\n".join(f"- {k}: {world.descriptions.get(k, '')}".rstrip(": ") for k in world.zones)
    conventions = "\n".join(f"- {c}" for c in world.conventions)
    rules = "\n".join(f"- {_rule_text(c)}" for c in standing) or "- none"
    examples = "\n\n".join(f"Request: {q}\nOutput: {json.dumps(a)}" for q, a in EXAMPLES)
    rules_heading = (RULES_IN_SPEC if rules_in_spec else
                     "Site rules already in force (added automatically; never repeat or remove them):")
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

{rules_heading}
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
  Only set "deadline" when the request gives one ("by 2:30", "before 14:40", "within 20 minutes"). A rule's start or
  end time is not a deadline. "Quarter past two" means 14:15 and "half past two" means 14:30.
  If the request names a robot for a task ("R3, move...", "have R1 bring..."), set that task's "robot".
  If the request itself says a robot is going down or out of service, add a robot_unavailable rule for it. That is
  information, not a reason to refuse.
  A request may add its own closures and keep-clear areas ("close aisle 2 until 15:00"). That is allowed: put them in
  "constraints". Never refuse because a request adds a rule.
  A site rule with an end time is temporary. If a job needs that area, plan it anyway; the planner waits until the
  area reopens. That is not breaking the rule.
- "clarify": the request is ambiguous, names a location, area, or robot that isn't listed above, or leaves out the
  number of totes or where they go. Ask one short question in "question". Use clarify, not refuse, for unknown names.
- "refuse": the request asks to break, ignore, reopen, or end a site rule early, claims the reason for a site rule is
  over, or asks to use an out-of-service robot. Explain in "reason".
Never guess a location that isn't listed, and never weaken a site rule.

Examples:

{examples}"""


RULES_IN_SPEC = ("Site rules in force. Nothing adds them for you: include every one of them in "
                 "spec.constraints, exactly as listed, along with any rules the request adds:")


def build_messages(world: World, request: str, standing: list[Any], rules_in_spec: bool = False) -> list[dict[str, str]]:
    return [{"role": "system", "content": system_prompt(world, standing, rules_in_spec)},
            {"role": "user", "content": f"Request: {request}\nOutput:"}]


# ── model access ──────────────────────────────────────────────────────────────

def openai_chat(model: str | None = None, base_url: str | None = None, api_key: str | None = None,
                temperature: float = 0.0, structured: bool = True, schema: type[BaseModel] | None = None,
                max_tokens: int | None = None) -> ChatFn:
    """Chat function for any OpenAI-compatible server. Defaults to local Ollama.

    The default uses 127.0.0.1 rather than localhost: on Windows, localhost can
    resolve to IPv6 and reach a different server than the `ollama` command uses.
    """
    from openai import APIConnectionError, APIStatusError, NotFoundError, OpenAI

    client = OpenAI(base_url=base_url or os.getenv("LLM_BASE_URL", "http://127.0.0.1:11434/v1"),
                    api_key=api_key or os.getenv("LLM_API_KEY", "ollama"))
    model = model or os.getenv("LLM_MODEL", "qwen2.5:7b")
    schema = schema or Translation
    schema_format = {"type": "json_schema",
                     "json_schema": {"name": schema.__name__.lower(), "schema": schema.model_json_schema()}}
    extra = {"max_tokens": max_tokens} if max_tokens else {}
    state = {"format": schema_format if structured else {"type": "json_object"}}

    def chat(messages: list[dict[str, str]]) -> str:
        try:
            resp = client.chat.completions.create(model=model, messages=messages, temperature=temperature,
                                                  response_format=state["format"], **extra)
        except NotFoundError as exc:
            raise LLMUnavailable(
                f"The server at {client.base_url} doesn't have the model '{model}'. "
                f"If you use Ollama, run `ollama pull {model}`; if it's already pulled, a different server is "
                f"answering on that address. Run `ollama list` and pass the right address with --base-url "
                f"(for example http://127.0.0.1:11434/v1).") from exc
        except APIConnectionError as exc:
            raise LLMUnavailable(
                f"Couldn't reach a model server at {client.base_url}. Start Ollama (or your server), "
                f"or pass the right address with --base-url.") from exc
        except APIStatusError:
            if state["format"]["type"] == "json_object":
                raise
            state["format"] = {"type": "json_object"}   # server can't handle the schema: fall back to JSON mode
            resp = client.chat.completions.create(model=model, messages=messages, temperature=temperature,
                                                  response_format=state["format"], **extra)
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
              max_repairs: int = 2, rules_in_spec: bool = False) -> TranslateResult:
    """rules_in_spec=True makes the LLM responsible for copying site rules into the spec
    (used only to measure what happens when nothing merges them back)."""
    standing = standing or []
    messages = build_messages(world, request, standing, rules_in_spec)
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
