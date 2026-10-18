import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from fleetplanner import MissionSpec, World
from fleetplanner.pipeline import handle_request
from fleetplanner.translate import Translation, load_rules, openai_chat, translate
from fleetplanner.validate import validate

ROOT = Path(__file__).resolve().parents[1]
W = World.load(ROOT / "maps" / "warehouse.yaml")
TODAY = load_rules(yaml.safe_load((ROOT / "rules" / "today.yaml").read_text()))   # aisle_3 closed 14:00-14:20, R2 out


def scripted(*replies):
    """A fake LLM that returns the given replies in order and records what it was sent."""
    it = iter(replies)
    calls = []

    def chat(messages):
        calls.append(messages)
        r = next(it)
        return r if isinstance(r, str) else json.dumps(r)

    chat.calls = calls
    chat.model = "scripted"
    return chat


def plan_reply(tasks, constraints=()):
    return {"kind": "plan", "spec": {"tasks": list(tasks), "constraints": list(constraints)}}


MOVE = {"id": "m", "type": "transport", "from_location": "A1-1", "to_location": "pick_1", "quantity": 2}


def spec(**kw):
    return MissionSpec.model_validate(kw)


# ── Translation schema ────────────────────────────────────────────────────────

def test_translation_kind_must_match_fields():
    with pytest.raises(ValidationError):
        Translation.model_validate({"kind": "plan"})
    with pytest.raises(ValidationError):
        Translation.model_validate({"kind": "clarify"})
    with pytest.raises(ValidationError):
        Translation.model_validate({"kind": "refuse"})


# ── Translator ────────────────────────────────────────────────────────────────

def test_translate_first_try():
    r = translate(W, "move 2 from A1-1 to pick 1", scripted(plan_reply([MOVE])))
    assert r.translation.kind == "plan" and r.attempts == 1


def test_translate_repairs_invalid_output():
    bad = {"kind": "plan", "spec": {"tasks": [{**MOVE, "quantity": 0}]}}       # quantity must be >= 1
    chat = scripted(bad, plan_reply([MOVE]))
    r = translate(W, "move totes", chat)
    assert r.translation and r.attempts == 2 and len(r.errors) == 1
    assert "invalid" in chat.calls[1][-1]["content"]                          # the error was fed back


def test_translate_accepts_code_fences():
    r = translate(W, "x", scripted("```json\n" + json.dumps(plan_reply([MOVE])) + "\n```"))
    assert r.translation.kind == "plan"


def test_translate_gives_up_after_repairs():
    r = translate(W, "x", scripted("not json", "{}", '{"kind": "plan"}'), max_repairs=2)
    assert r.translation is None and r.attempts == 3 and len(r.errors) == 3


def test_prompt_lists_standing_rules_and_locations():
    chat = scripted(plan_reply([MOVE]))
    translate(W, "x", chat, TODAY)
    system = chat.calls[0][0]["content"]
    assert "aisle_3 is closed from 14:00 until 14:20" in system and "R2 is out of service" in system
    assert "A4-2: aisle 4, south slot" in system and "home_1" not in system


# ── Validator ─────────────────────────────────────────────────────────────────

def problems(s):
    v = validate(W, s)
    return v.status, {p.code for p in v.problems}, v


def test_validator_accepts_clean_spec():
    assert problems(spec(tasks=[MOVE]))[0] == "accept"


def test_unknown_location_asks_with_suggestion():
    status, codes, v = problems(spec(tasks=[{**MOVE, "from_location": "A4-3"}]))
    assert status == "clarify" and "unknown_location" in codes and "Did you mean" in v.explain()


def test_charger_is_not_storage_and_same_place_rejected():
    assert "not_storage" in problems(spec(tasks=[{**MOVE, "to_location": "charger_1"}]))[1]
    assert "same_place" in problems(spec(tasks=[{**MOVE, "to_location": "A1-1"}]))[1]


def test_deadline_passed_and_impossible():
    assert "deadline_passed" in problems(spec(tasks=[{**MOVE, "deadline": "13:30"}]))[1]
    status, codes, v = problems(spec(tasks=[{**MOVE, "quantity": 10, "deadline": "14:01"}]))
    assert status == "reject" and "deadline_impossible" in codes and "earliest" in v.explain()


def test_feasible_deadline_is_not_rejected():
    # Lower-bound check: a generous deadline must pass.
    assert problems(spec(tasks=[{**MOVE, "deadline": "14:30"}]))[0] == "accept"


def test_location_blocked_all_shift():
    s = spec(tasks=[{**MOVE, "from_location": "dock", "to_location": "A4-1"}],
             constraints=[{"type": "keep_clear", "zone": "dock_area"}])
    status, codes, v = problems(s)
    assert status == "reject" and "location_blocked" in codes and "dock" in v.explain()


def test_temporary_closure_is_fine():
    s = spec(tasks=[{**MOVE, "from_location": "A3-1"}],
             constraints=[{"type": "zone_closed", "zone": "aisle_3", "start": "14:00", "end": "14:20"}])
    assert problems(s)[0] == "accept"


def test_robot_rules():
    assert "robot_unavailable" in problems(spec(tasks=[{**MOVE, "robot": "R2"}],
                                                constraints=[{"type": "robot_unavailable", "robot": "R2"}]))[1]
    assert problems(spec(tasks=[{**MOVE, "robot": "R9"}]))[0] == "clarify"
    everyone = [{"type": "robot_unavailable", "robot": r} for r in ("R1", "R2", "R3", "R4")]
    assert "no_robots" in problems(spec(tasks=[MOVE], constraints=everyone))[1]


def test_park_and_window_rules():
    assert "double_park" in problems(spec(tasks=[
        {"id": "a", "type": "park", "robot": "R1", "location": "dock"},
        {"id": "b", "type": "park", "robot": "R1", "location": "pick_1"}]))[1]
    assert "bad_window" in problems(spec(constraints=[
        {"type": "zone_closed", "zone": "aisle_1", "start": "14:30", "end": "14:10"}]))[1]


# ── Pipeline ──────────────────────────────────────────────────────────────────

def test_pipeline_plans_and_keeps_standing_rules():
    out = handle_request(W, "move 2 from aisle 3 to pick 1", scripted(plan_reply([{**MOVE, "from_location": "A3-1"}])), TODAY)
    assert out["status"] == "planned"
    kinds = {c["type"] for c in out["spec"]["constraints"]}
    assert {"zone_closed", "robot_unavailable"} <= kinds                      # added even though the LLM didn't
    assert out["log"]["metrics"]["success"] and out["log"]["request"]


def test_llm_cannot_use_out_of_service_robot():
    # The request names R2: grounding refuses before planning.
    out = handle_request(W, "have R2 move 2 totes from A1-1 to pick 1", scripted(plan_reply([{**MOVE, "robot": "R2"}])), TODAY)
    assert out["status"] == "refuse" and "out of service" in out["message"]
    # The LLM pins R2 on its own: grounding unpins it, and R2 never moves.
    out = handle_request(W, "move 2 totes from A1-1 to pick 1", scripted(plan_reply([{**MOVE, "robot": "R2"}])), TODAY)
    assert out["status"] == "planned" and not out["log"]["plan"]["actions"].get("R2")


def test_validator_still_rejects_out_of_service_robot_without_grounding():
    from fleetplanner.pipeline import merge_rules
    s = merge_rules(MissionSpec.model_validate({"tasks": [{**MOVE, "robot": "R2"}]}), TODAY)
    v = validate(W, s)
    assert v.status == "reject" and "out of service" in v.explain()


def test_llm_cannot_lift_a_closure_by_omitting_it():
    # The LLM 'forgets' the aisle 3 closure; the plan must still respect it.
    out = handle_request(W, "pick 2 totes from A3-1 to pick 1", scripted(plan_reply([{**MOVE, "from_location": "A3-1"}])), TODAY)
    log = out["log"]
    assert out["status"] == "planned" and not log["violations"]
    assert log["tasks"]["m"]["completion_step"] > W.clock_to_step("14:20")


def test_pipeline_passes_through_clarify_refuse_and_error():
    assert handle_request(W, "x", scripted({"kind": "clarify", "question": "Which aisle?"}))["status"] == "clarify"
    assert handle_request(W, "x", scripted({"kind": "refuse", "reason": "Rule."}))["status"] == "refuse"
    assert handle_request(W, "x", scripted("no", "no", "no"))["status"] == "error"


# ── OpenAI-compatible client against a fake server ───────────────────────────

class _Server(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.seen.append(body)
        if self.server.reject_schemas and body.get("response_format", {}).get("type") == "json_schema":
            data = json.dumps({"error": {"message": "json_schema not supported", "type": "invalid_request_error"}}).encode()
            self.send_response(400)
        else:
            content = json.dumps(plan_reply([MOVE]))
            data = json.dumps({"id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
                               "choices": [{"index": 0, "finish_reason": "stop",
                                            "message": {"role": "assistant", "content": content}}]}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(params=[False, True], ids=["schema-supported", "schema-rejected"])
def server(request, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    s = ThreadingHTTPServer(("127.0.0.1", 0), _Server)
    s.seen, s.reject_schemas = [], request.param
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield s
    s.shutdown()


def test_openai_client_structured_output_and_fallback(server):
    pytest.importorskip("openai")
    host, port = server.server_address
    chat = openai_chat(model="qwen2.5:7b", base_url=f"http://{host}:{port}/v1", api_key="x")
    r = translate(W, "move 2 from A1-1 to pick 1", chat)
    assert r.translation.kind == "plan"
    assert server.seen[0]["response_format"]["type"] == "json_schema"
    assert server.seen[0]["temperature"] == 0.0 and server.seen[0]["model"] == "qwen2.5:7b"
    if server.reject_schemas:
        assert server.seen[-1]["response_format"] == {"type": "json_object"}


def test_missing_model_and_unreachable_server_give_clear_errors(monkeypatch):
    pytest.importorskip("openai")
    from fleetplanner.translate import LLMUnavailable

    class NotFound(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            data = json.dumps({"error": {"message": "model 'qwen2.5:7b' not found", "type": "not_found_error"}}).encode()
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    s = ThreadingHTTPServer(("127.0.0.1", 0), NotFound)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        host, port = s.server_address
        chat = openai_chat(model="qwen2.5:7b", base_url=f"http://{host}:{port}/v1", api_key="x")
        with pytest.raises(LLMUnavailable, match="ollama pull qwen2.5:7b"):
            translate(W, "x", chat)
    finally:
        s.shutdown()
    dead = openai_chat(model="m", base_url="http://127.0.0.1:9/v1", api_key="x")
    with pytest.raises(LLMUnavailable, match="Couldn't reach"):
        translate(W, "x", dead)
