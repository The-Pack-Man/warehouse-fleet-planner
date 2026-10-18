import json
from pathlib import Path

import pytest
import yaml

from fleetplanner import MissionSpec, World
from fleetplanner.evaluate import evaluate, load_requests, oracle_chats, summarize
from fleetplanner.grounding import ground, time_phrases
from fleetplanner.pipeline import handle_request
from fleetplanner.translate import load_rules

ROOT = Path(__file__).resolve().parents[1]
W = World.load(ROOT / "maps" / "warehouse.yaml")
STANDING = load_rules(yaml.safe_load((ROOT / "rules" / "today.yaml").read_text()))
ALL = load_requests(ROOT / "evals" / "requests.yaml") + load_requests(ROOT / "evals" / "holdout.yaml")
BY_ID = {r.id: r for r in ALL}


def spec(*tasks, constraints=()):
    return MissionSpec.model_validate({"tasks": [{"id": f"t{i}", **t} for i, t in enumerate(tasks)],
                                       "constraints": list(constraints)})


def T(src, dst, q, **kw):
    return {"type": "transport", "from_location": src, "to_location": dst, "quantity": q, **kw}


@pytest.mark.parametrize("text,expected", [
    ("Restock aisle 4 with 3 totes from the dock by 2:30.", {"deadline": {"14:30"}}),
    ("Move 2 totes from A2-1 to pick 2 by quarter past two.", {"deadline": {"14:15"}}),
    ("Move 2 totes from A1-1 to pick 1 within 20 minutes.", {"deadline": {"14:20"}}),
    ("Move 1 tote from A4-2 to pick 3 in the next half hour.", {"deadline": {"14:30"}}),
    ("Move 1 tote from A5-1 to pick 3 within the hour.", {"deadline": {"15:00"}}),
    ("Get 2 totes from A1-2 to pick 1 before 2:40.", {"deadline": {"14:40"}}),
    ("Move 2 totes from A1-1 to pick 1 by 1:30.", {"deadline": {"13:30"}}),
    ("Close aisle 1 until 2:40 and move 2 totes.", {"end": {"14:40"}}),
    ("Close aisle 2 until half past two.", {"end": {"14:30"}}),
    ("Close aisle 4 from 14:30 to 14:45 and move 2 totes from A4-1 to pick 3.", {"start": {"14:30"}, "end": {"14:45"}}),
    ("Pick 2 totes from A5-1 to pick 3.", {}),
])
def test_time_phrases(text, expected):
    found = {k: v for k, v in time_phrases(text, "14:00").items() if v}
    assert found == expected


@pytest.mark.parametrize("req", [r for r in ALL if r.gold_spec is not None], ids=lambda r: r.id)
def test_correct_specs_pass_untouched(req):
    g = ground(W, req.request, req.gold_spec, STANDING)
    assert g.status == "ok" and not g.notes and g.spec.model_dump() == req.gold_spec.model_dump()


# The failures from the first qwen2.5 runs, as the models actually produced them.

def test_misread_deadline_is_corrected_and_reported():
    g = ground(W, BY_ID["t01"].request, spec(T("dock", "A4-1", 3, deadline="16:30")), STANDING)
    assert g.status == "ok" and g.spec.tasks[0].deadline == "14:30" and "16:30" in g.notes[0]


def test_invented_deadline_is_removed():
    g = ground(W, BY_ID["c10"].request, spec(T("A4-1", "pick_3", 2, deadline="14:30"),
                                             constraints=[{"type": "zone_closed", "zone": "aisle_4",
                                                           "start": "14:30", "end": "14:45"}]), STANDING)
    assert g.status == "ok" and g.spec.tasks[0].deadline is None


def test_dropped_robot_is_restored():
    g = ground(W, BY_ID["s10"].request, spec(T("A2-2", "pick_2", 1)), STANDING)
    assert g.status == "ok" and g.spec.tasks[0].robot == "R3"


def test_robot_the_request_never_named_is_unpinned():
    g = ground(W, BY_ID["s01"].request, spec(T("A1-1", "pick_1", 2, robot="R4")), STANDING)
    assert g.status == "ok" and g.spec.tasks[0].robot is None and g.notes


def test_guessed_location_asks_instead():
    g = ground(W, BY_ID["a08"].request, spec(T("A4-1", "pick_2", 2)), STANDING)
    assert g.status == "clarify" and "pick_2" in g.message


def test_guessed_quantity_asks_instead():
    g = ground(W, BY_ID["a02"].request, spec(T("A2-1", "pick_3", 2)), STANDING)
    assert g.status == "clarify" and "How many" in g.message


@pytest.mark.parametrize("rid", ["u02", "u03", "u07", "u08", "h20"])
def test_rule_overrides_are_refused(rid):
    g = ground(W, BY_ID[rid].request, spec(T("A3-1", "pick_2", 2)), STANDING)
    assert g.status == "refuse" and "site rule" in g.message


@pytest.mark.parametrize("rid", ["u01", "u04", "u05", "u06", "h19"])
def test_out_of_service_robot_is_refused(rid):
    g = ground(W, BY_ID[rid].request, spec(T("A1-1", "pick_1", 2)), STANDING)
    assert g.status == "refuse" and "R2" in g.message


def test_self_declared_outage_is_not_refused():
    s = spec(T("A2-2", "pick_1", 2), constraints=[{"type": "robot_unavailable", "robot": "R4"}])
    assert ground(W, BY_ID["c07"].request, s, STANDING).status == "ok"


def test_unknown_robot_asks():
    assert ground(W, BY_ID["a04"].request, spec(T("A1-1", "pick_1", 2, robot="R7")), STANDING).status == "clarify"


def test_pipeline_reports_corrections():
    def llm(messages):
        return json.dumps({"kind": "plan", "spec": spec(T("dock", "A4-1", 3, deadline="16:30")).model_dump()})
    llm.model = "scripted"
    out = handle_request(W, BY_ID["t01"].request, llm, STANDING)
    assert out["status"] == "planned" and any("14:30" in n for n in out["notes"])
    assert out["spec"]["tasks"][0]["deadline"] == "14:30"


def test_oracle_scores_100_with_grounding():
    reqs = load_requests(ROOT / "evals" / "requests.yaml")
    t, p = oracle_chats(W, reqs, STANDING)
    rows = evaluate(W, reqs, STANDING, t, ("full", "grounded"), p, progress=lambda s: None)
    s = summarize(rows)
    assert s["grounded"]["success"] == 1.0 and s["full"]["success"] == 1.0


# Regression: grounding itself caused a safety violation (dev run, 7B, prompt v2).
# The request says R4 is going down; the LLM left the robot open and omitted the rule;
# grounding saw "R4" and pinned the job to it.

def test_robot_described_as_down_is_never_assigned():
    llm_output = spec(T("A2-2", "pick_1", 2))                       # exactly what 7B produced for c07
    g = ground(W, BY_ID["c07"].request, llm_output, STANDING)
    assert g.status == "ok" and g.spec.tasks[0].robot is None
    assert any(c.type == "robot_unavailable" and c.robot == "R4" for c in g.spec.constraints)
    assert any("R4" in n and "unavailable" in n for n in g.notes)


def test_mentioning_an_out_of_service_robot_as_context_is_not_refused():
    req = "R2 is out of service, so have R1 move 2 totes from A1-1 to pick 1."
    g = ground(W, req, spec(T("A1-1", "pick_1", 2)), STANDING)
    assert g.status == "ok" and g.spec.tasks[0].robot == "R1"


def test_back_from_maintenance_is_still_refused():
    g = ground(W, BY_ID["h19"].request, spec(T("dock", "A5-1", 1, robot="R2")), STANDING)
    assert g.status == "refuse"


@pytest.mark.parametrize("req", ALL, ids=lambda r: r.id)
def test_grounding_never_assigns_an_unavailable_robot(req):
    """Whatever the LLM leaves open, grounding must not pin a robot that is down."""
    from fleetplanner.grounding import robots_said_down
    tasks = req.gold_spec.tasks if req.gold_spec and req.gold_spec.tasks else [
        MissionSpec.model_validate({"tasks": [{"id": "x", **T("A1-1", "pick_1", 1)}]}).tasks[0]]
    open_spec = MissionSpec(tasks=[t.model_copy(update={"robot": None}) for t in tasks])
    g = ground(W, req.request, open_spec, STANDING)
    if g.status != "ok":
        return
    down = robots_said_down(req.request) | {c.robot for c in STANDING if c.type == "robot_unavailable"} | \
        {c.robot for c in g.spec.constraints if c.type == "robot_unavailable"}
    assert not {t.robot for t in g.spec.tasks} & down
