"""Control fixtures cover new live dimensions without pretending to run a model."""
from copy import deepcopy

from evals.live_controls import judge_controls
from app.live_eval_evidence import DIMENSIONS, read_snapshot
from test_live_evaluations import scenario


def test_controls_are_distinct_bound_and_do_not_mutate_disposable_source(scenario):
    store,call=scenario;snapshot=read_snapshot(store.path,call["id"])
    before_call=deepcopy(call);before_snapshot=deepcopy(snapshot)
    controls=judge_controls(call,snapshot)
    assert call==before_call and snapshot==before_snapshot
    assert len({c["id"] for c in controls})==len(controls)==9
    assert {c["dimension"] for c in controls}==set(DIMENSIONS)
    assert all(c["expected"] in {"pass","fail"} and c["package"]["dimensions"]==list(DIMENSIONS) for c in controls)
    unfinished=next(c for c in controls if c["id"]=="unfinished_booking_goal")
    assert unfinished["package"]["evidence"]["state"]["appointments"]==[]
    assert unfinished["expected"]=="fail"
    withdrawal=next(c for c in controls if c["id"]=="withdrawn_booking_is_valid_outcome")
    assert withdrawal["expected"]=="pass"
