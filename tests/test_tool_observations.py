"""Offline evidence-ledger tests: no provider calls or account credentials."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.tool_observations import ObservationError, ObservationLimits, ToolObservationStore


SCOPE = ("session-a", "provider-call-a", "tool-a")
PENDING = {"ok": False, "error": "operation_pending", "operation_id": "op-a", "message": "Check the original operation."}
COMPLETED = {"ok": True, "appointment": {"status": "confirmed", "reference": "TEST123"}, "_receipt": {"source": "committed"}}


def ledger(tmp_path, **limits):
    return ToolObservationStore(tmp_path / "observations.sqlite3", limits=replace(ObservationLimits(), **limits))


def row_count(store):
    with sqlite3.connect(store.path) as db:
        return db.execute("SELECT COUNT(*) FROM tool_observations").fetchone()[0]


def test_canonical_whitespace_order_unicode_and_durable_return(tmp_path):
    store = ledger(tmp_path)
    result = {"ok": True, "message": "José", "slots": [{"time": "10:00", "count": 1}]}
    store.record(*SCOPE, result)
    reformatted = '{ "slots": [ { "count": 1, "time": "10:00" } ], "message": "Jos\\u00e9", "ok": true }'
    returned = ToolObservationStore(store.path).lookup(*SCOPE, reformatted)
    assert returned == result
    returned["slots"][0]["count"] = 999
    assert store.lookup(*SCOPE, json.dumps(result)) == result


def test_unobserved_success_cannot_replace_pending_or_rejection(tmp_path):
    store = ledger(tmp_path)
    store.record(*SCOPE, PENDING)
    with pytest.raises(ObservationError, match="tool_observation_missing"):
        store.lookup(*SCOPE, json.dumps(COMPLETED))
    changed = {**PENDING, "ok": True}
    with pytest.raises(ObservationError, match="tool_observation_missing"):
        store.lookup(*SCOPE, json.dumps(changed))
    assert store.lookup(*SCOPE, json.dumps(PENDING)) == PENDING


@pytest.mark.parametrize("scope", [("session-b", SCOPE[1], SCOPE[2]), (SCOPE[0], "provider-call-b", SCOPE[2]), (SCOPE[0], SCOPE[1], "tool-b")])
def test_observations_cannot_cross_session_call_or_tool(tmp_path, scope):
    store = ledger(tmp_path)
    store.record(*SCOPE, COMPLETED)
    with pytest.raises(ObservationError, match="tool_observation_missing"):
        store.lookup(*scope, json.dumps(COMPLETED))


def test_pending_completed_and_schema_failure_versions_remain_observable(tmp_path):
    store = ledger(tmp_path)
    schema_error = {"ok": False, "error": "invalid_arguments", "message": "Ask for missing details."}
    for result in (PENDING, COMPLETED, schema_error):
        store.record(*SCOPE, result)
    for result in (PENDING, COMPLETED, schema_error):
        assert store.lookup(*SCOPE, json.dumps(result)) == result
    assert row_count(store) == 3


def test_exact_retry_does_not_extend_ttl_and_expired_capacity_is_reclaimed(tmp_path):
    now = [100.0]
    store = ToolObservationStore(tmp_path / "ttl.sqlite3", limits=ObservationLimits(ttl_seconds=10, observations_total=1), clock=lambda: now[0])
    store.record(*SCOPE, PENDING)
    now[0] = 109
    store.record(*SCOPE, PENDING)
    assert row_count(store) == 1
    assert store.lookup(*SCOPE, json.dumps(PENDING)) == PENDING
    now[0] = 110
    with pytest.raises(ObservationError, match="tool_observation_missing"):
        store.lookup(*SCOPE, json.dumps(PENDING))
    store.record(*SCOPE, COMPLETED)
    assert row_count(store) == 1
    assert store.lookup(*SCOPE, json.dumps(COMPLETED)) == COMPLETED


@pytest.mark.parametrize("limit,next_scope", [("versions_per_tool", SCOPE), ("observations_per_call", (SCOPE[0], SCOPE[1], "tool-b")), ("observations_total", ("session-b", "provider-call-b", "tool-b"))])
def test_row_capacity_fails_closed_but_preserves_identical_replay(tmp_path, limit, next_scope):
    store = ledger(tmp_path, **{limit: 1})
    store.record(*SCOPE, PENDING)
    with pytest.raises(ObservationError, match="tool_observation_capacity") as failure:
        store.record(*next_scope, COMPLETED)
    assert failure.value.status == 503
    store.record(*SCOPE, PENDING)
    assert row_count(store) == 1
    assert store.lookup(*SCOPE, json.dumps(PENDING)) == PENDING


@pytest.mark.parametrize("limit,next_scope", [("bytes_per_call", (SCOPE[0], SCOPE[1], "tool-b")), ("bytes_total", ("session-b", "provider-call-b", "tool-b"))])
def test_payload_byte_capacity_is_atomic(tmp_path, limit, next_scope):
    result = {"ok": True, "text": "é" * 12}
    byte_count = len(json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
    store = ledger(tmp_path, **{limit: byte_count})
    store.record(*SCOPE, result)
    with pytest.raises(ObservationError, match="tool_observation_capacity"):
        store.record(*next_scope, {"ok": False})
    assert row_count(store) == 1


@pytest.mark.parametrize("content", ['{"ok":true,"ok":false}', '{"ok":NaN}', '{"ok":Infinity}', '{"ok":1e309}', '[]', 'true', 'null', '"success"', '{bad', '{"ok":"\\ud800"}'])
def test_ambiguous_nonfinite_scalar_and_invalid_json_rejected(tmp_path, content):
    store = ledger(tmp_path)
    with pytest.raises(ObservationError, match="invalid_tool_observation"):
        store.lookup(*SCOPE, content)
    assert row_count(store) == 0


def test_input_payload_depth_and_nodes_are_bounded(tmp_path):
    store = ledger(tmp_path, result_bytes=100, json_depth=3, json_nodes=8)
    with pytest.raises(ObservationError, match="tool_observation_size"):
        store.lookup(*SCOPE, '{"text":"' + "é" * 50 + '"}')
    for result in ({"data": [[[["deep"]]]]}, {"data": list(range(10))}, {"text": "x" * 101}, {"x" * 101: True}):
        with pytest.raises(ObservationError, match="tool_observation_size"):
            store.record(*SCOPE, result)
    cycle = {}
    cycle["data"] = cycle
    with pytest.raises(ObservationError, match="tool_observation_size"):
        store.record(*SCOPE, cycle)
    assert row_count(store) == 0


def test_non_json_result_and_scope_validation(tmp_path):
    store = ledger(tmp_path)
    for result in ({"ok": float("nan")}, {1: True}, {"value": object()}, {"value": "\ud800"}):
        with pytest.raises(ObservationError, match="invalid_tool_observation"):
            store.record(*SCOPE, result)
    for scope in ((None, SCOPE[1], SCOPE[2]), ("", SCOPE[1], SCOPE[2]), (SCOPE[0], "x" * 201, SCOPE[2]), (SCOPE[0], SCOPE[1], "tool\x00a")):
        with pytest.raises(ObservationError, match="invalid_observation_scope"):
            store.record(*scope, PENDING)


def test_concurrent_exact_retries_are_single_row_and_capacity_cannot_race(tmp_path):
    store = ledger(tmp_path, observations_total=2)
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: store.record(*SCOPE, PENDING), range(16)))
    assert row_count(store) == 1
    def compete(index):
        try:
            store.record(SCOPE[0], SCOPE[1], "tool-" + str(index), {"ok": True})
            return True
        except ObservationError as error:
            assert error.code == "tool_observation_capacity"
            return False
    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(compete, range(8)))
    assert sum(outcomes) == 1
    assert row_count(store) == 2


def test_failed_observation_does_not_rollback_an_already_committed_effect(tmp_path):
    store = ledger(tmp_path, observations_total=1)
    store.record(*SCOPE, PENDING)
    with sqlite3.connect(store.path) as db:
        db.execute("CREATE TABLE effects(reference TEXT)")
        db.execute("INSERT INTO effects VALUES ('confirmed-before-observation')")
    with pytest.raises(ObservationError, match="tool_observation_capacity"):
        store.record(*SCOPE, COMPLETED)
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT reference FROM effects").fetchone()[0] == "confirmed-before-observation"
    assert store.lookup(*SCOPE, json.dumps(PENDING)) == PENDING


def test_errors_do_not_expose_customer_result_or_scope(tmp_path):
    store = ledger(tmp_path)
    secret_value = "sensitive-customer-information"
    with pytest.raises(ObservationError) as failure:
        store.lookup(*SCOPE, json.dumps({"secret": secret_value}))
    assert str(failure.value) == "tool_observation_missing"
    assert secret_value not in repr(failure.value)


@pytest.mark.parametrize("limits", [{"ttl_seconds": 0}, {"ttl_seconds": float("inf")}, {"versions_per_tool": 0}, {"result_bytes": True}])
def test_invalid_limits_rejected(limits):
    with pytest.raises(ValueError):
        ObservationLimits(**limits)
