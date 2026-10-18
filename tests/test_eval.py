import json
import re
from pathlib import Path

import pytest
import yaml

from fleetplanner import MissionSpec, Plan, Planner, World, simulate
from fleetplanner.evaluate import (compare_specs, evaluate, judge, load_requests, merge_rules, oracle_chats,
                                   summarize)
from fleetplanner.translate import load_rules
from fleetplanner.validate import validate

ROOT = Path(__file__).resolve().parents[1]
W = World.load(ROOT / "maps" / "warehouse.yaml")
STANDING = load_rules(yaml.safe_load((ROOT / "rules" / "today.yaml").read_text()))
REQS = load_requests(ROOT / "evals" / "requests.yaml")
BY_ID = {r.id: r for r in REQS}


def test_request_set_shape():
    assert len(REQS) == 60 and len(BY_ID) == 60
    assert {r.category for r in REQS} == {"simple", "multi", "constrained", "time", "ambiguous", "infeasible", "unsafe"}


@pytest.mark.parametrize("req", [r for r in REQS if r.gold_kind == "plan"], ids=lambda r: r.id)
def test_gold_plans_are_valid_and_achievable(req):
    spec = merge_rules(req.gold_spec, STANDING)
    assert validate(W, spec).status == "accept"
    log = simulate(W, spec, Planner(W).plan(spec))
    assert log["metrics"]["success"], (log["violations"], log["tasks"])


@pytest.mark.parametrize("req", [r for r in REQS if r.gold_kind == "reject"], ids=lambda r: r.id)
def test_gold_rejects_are_rejected_by_validator(req):
    assert validate(W, merge_rules(req.gold_spec, STANDING)).status == "reject"


def test_compare_specs():
    gold = BY_ID["t01"].gold_spec
    wrong_deadline = MissionSpec.model_validate({"tasks": [{**gold.tasks[0].model_dump(), "id": "x", "deadline": "16:30"}]})
    assert compare_specs(gold, wrong_deadline, STANDING)
    with_site_rules = merge_rules(MissionSpec.model_validate({"tasks": [{**gold.tasks[0].model_dump(), "id": "y"}]}), STANDING)
    assert compare_specs(gold, with_site_rules, STANDING) == []           # site rules don't count as extras
    rush = BY_ID["c01"].gold_spec
    not_urgent = MissionSpec.model_validate({"tasks": [{**rush.tasks[0].model_dump(), "priority": 1}]})
    assert any("urgent" in d for d in compare_specs(rush, not_urgent, STANDING))


def test_oracle_calibrates_the_harness():
    t, p = oracle_chats(W, REQS, STANDING)
    rows = evaluate(W, REQS, STANDING, t, planner_chat=p, progress=lambda s: None)
    s = summarize(rows)
    assert s["full"]["success"] == 1.0 and s["llm_only"]["success"] == 1.0
    for c in ("translate_only", "no_validator"):
        failed = {r["id"] for r in rows if r["condition"] == c and not r["success"]}
        assert failed == {r.id for r in REQS if r.category == "infeasible"}   # only a validator can stop these


def test_rule_dropping_llm_is_caught_only_where_rules_are_merged():
    """An LLM that forgets the aisle 3 closure: translate_only drives into the closed aisle; full doesn't."""
    req = BY_ID["s12"]

    def forgetful(messages):
        return json.dumps({"kind": "plan", "spec": req.gold_spec.model_dump()})   # never includes site rules

    rows = evaluate(W, [req], STANDING, forgetful, ("translate_only", "full"), progress=lambda s: None)
    by_cond = {r["condition"]: r for r in rows}
    assert by_cond["translate_only"]["unsafe"]
    assert not by_cond["full"]["unsafe"] and by_cond["full"]["success"]


def test_silent_robot_swap_counts_as_wrong():
    """u01: planning the job with another robot instead of refusing is safe but not correct."""
    req = BY_ID["u01"]
    task = {"id": "m", "type": "transport", "from_location": "A1-1", "to_location": "pick_1", "quantity": 2}

    def swapper(messages):
        return json.dumps({"kind": "plan", "spec": {"tasks": [task], "constraints": []}})

    row = evaluate(W, [req], STANDING, swapper, ("full",), progress=lambda s: None)[0]
    assert row["status"] == "planned" and not row["success"] and row["went_ahead_wrongly"] and not row["unsafe"]


def test_judge_maps_llm_task_ids_onto_gold():
    req = BY_ID["s01"]
    got = MissionSpec.model_validate({"tasks": [{"id": "my_job", "type": "transport", "from_location": "A1-1",
                                                 "to_location": "pick_1", "quantity": 2}]})
    plan = Planner(W).plan(merge_rules(got, STANDING))
    j = judge(W, req, STANDING, got, plan)
    assert j["gold_tasks_on_time"] == 1 and not j["safety_violations"]
