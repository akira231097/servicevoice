"""Offline inbound admission tests; every database is a disposable fixture."""
import hashlib
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import pytest

from app.domain import DomainError, Store, TENANT
from app.gemini_gateway import GatewayLimits, SignatureStore
from app.telephony_ingress import IngressConfig, IngressError, IngressLimits, TelephonyIngress


ORG = "11111111-1111-4111-8111-111111111111"
PHONE = "22222222-2222-4222-8222-222222222222"
ASSISTANT = "33333333-3333-4333-8333-333333333333"
BROWSER = "44444444-4444-4444-8444-444444444444"
SECRET = "phone-only-credential-" + "a" * 32
AUTH = "Bearer " + SECRET


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "scheduler.sqlite3")


@pytest.fixture
def config():
    return IngressConfig(ORG, PHONE, ASSISTANT, SECRET)


def message(call_id=None, **call_fields):
    return {"type": "assistant-request", "call": {
        "id": call_id or str(uuid4()), "type": "inboundPhoneCall", "orgId": ORG,
        "phoneNumberId": PHONE, **call_fields,
    }}


def issued_call(request, response):
    return {**request["call"], **response}


def rows(store, table):
    with store.connect() as db:
        return [dict(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY rowid")]


def counts(store):
    return {name: len(rows(store, name)) for name in ("sessions", "telephony_bindings", "provider_tool_policies")}


def assert_error(code, fn, *args, **kwargs):
    with pytest.raises(IngressError) as error:
        fn(*args, **kwargs)
    assert error.value.code == code
    return error.value


def test_disabled_has_no_schema_side_effects_and_rejects_calls(store):
    assert IngressConfig.from_mapping({}) is None
    ingress = TelephonyIngress(store, None)
    assert_error("telephony_disabled", ingress.admit, message(), AUTH)
    with store.connect() as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='telephony_bindings'").fetchone() is None
    assert rows(store, "sessions") == []


def settings(**updates):
    return {"SERVICEVOICE_TELEPHONY_ENABLED": "on", "VAPI_PHONE_ORG_ID": ORG,
            "VAPI_PHONE_NUMBER_ID": PHONE, "VAPI_PHONE_ASSISTANT_ID": ASSISTANT,
            "VAPI_PHONE_WEBHOOK_SECRET": SECRET, "VAPI_ASSISTANT_ID": BROWSER, **updates}


@pytest.mark.parametrize("updates", [
    {"SERVICEVOICE_TELEPHONY_ENABLED": "true"}, {"VAPI_PHONE_ORG_ID": "bad"},
    {"VAPI_PHONE_NUMBER_ID": None}, {"VAPI_PHONE_ASSISTANT_ID": BROWSER},
    {"VAPI_PHONE_WEBHOOK_SECRET": "short"}, {"VAPI_PHONE_WEBHOOK_SECRET": "a"*32+"\n"},
    {"VAPI_WEBHOOK_SECRET": SECRET}, {"GEMINI_GATEWAY_SECRET": SECRET},
])
def test_invalid_or_shared_configuration_fails_closed(updates):
    with pytest.raises(ValueError):
        IngressConfig.from_mapping(settings(**updates))


def test_secret_not_in_config_repr_and_authentication_is_pure(store, config, monkeypatch):
    ingress = TelephonyIngress(store, config)
    assert SECRET not in repr(config)
    monkeypatch.setattr(ingress, "_transaction", lambda *args, **kwargs: pytest.fail("Authentication must not access DB"))
    assert ingress.authenticate(AUTH) is None
    assert_error("telephony_unauthorized", ingress.authenticate, "Bearer invalid")
    assert_error("telephony_unauthorized", ingress.authenticate, None)


def test_atomic_admission_discards_untrusted_identity_and_preserves_business(store, config):
    ingress = TelephonyIngress(store, config)
    before = {name: rows(store, name) for name in ("slots", "appointments", "proposals", "tool_results", "transcripts", "handoffs")}
    req = message(customer={"number": "+15125550142", "name": "Pretend verified"},
                  assistantOverrides={"variableValues": {"session_id": "attacker", "session_token": "attacker"}})
    response = ingress.admit(req, AUTH)
    sid = response["assistantOverrides"]["variableValues"]["session_id"]
    assert response == {"assistantId": ASSISTANT, "assistantOverrides": {"variableValues": {"session_id": sid}}}
    assert "token" not in json.dumps(response)
    session = rows(store, "sessions")[0]
    assert session["id"] == sid and session["tenant"] == TENANT and session["mode"] == "live"
    assert session["provider_call_id"] == req["call"]["id"]
    assert len(session["token_hash"]) == 64
    assert session["token_hash"] != hashlib.sha256(b"attacker").hexdigest()
    assert json.loads(session["state"]) == {"stage": "service", "slots": [], "customer": None, "service": None, "action": "book"}
    with pytest.raises(DomainError):
        store.authorize(sid, "attacker")
    assert rows(store, "provider_tool_policies")[0] == {"provider_call_id": req["call"]["id"], "session_id": sid, "assistant_id": ASSISTANT, "policy": "gemini-issued"}
    assert ingress.resolve(issued_call(req, response)) == sid
    for name, expected in before.items():
        assert rows(store, name) == expected
    signatures = SignatureStore(store.path, GatewayLimits())
    signatures.require_gemini_policy(sid, req["call"]["id"])
    signatures.check_live(sid, req["call"]["id"])


@pytest.mark.parametrize("change,code", [
    ({"type": "webCall"}, "telephony_identity_mismatch"),
    ({"type": "outboundPhoneCall"}, "telephony_identity_mismatch"),
    ({"orgId": BROWSER}, "telephony_identity_mismatch"),
    ({"phoneNumberId": BROWSER}, "telephony_identity_mismatch"),
    ({"assistantId": BROWSER}, "telephony_assistant_mismatch"),
    ({"id": "guess"}, "invalid_telephony_call"),
    ({"status": "ended"}, "telephony_call_ended"),
    ({"endedAt": "2026-10-18T00:00:00Z"}, "telephony_call_ended"),
])
def test_admission_identity_failure_has_no_effect(store, config, change, code):
    ingress = TelephonyIngress(store, config)
    assert_error(code, ingress.admit, message(**change), AUTH)
    assert counts(store) == {"sessions": 0, "telephony_bindings": 0, "provider_tool_policies": 0}


def test_conflicting_assistant_object_and_bad_bearer_are_rejected(store, config):
    ingress = TelephonyIngress(store, config)
    req = message(assistantId=ASSISTANT)
    req["assistant"] = {"id": BROWSER}
    assert_error("telephony_assistant_mismatch", ingress.admit, req, AUTH)
    assert_error("telephony_unauthorized", ingress.admit, message(), "Bearer wrong")
    assert counts(store)["sessions"] == 0


def test_preselection_create_assistant_dto_is_ignored_without_conflicting_id(store, config):
    ingress = TelephonyIngress(store, config)
    req = message()
    req["assistant"] = {"name": "caller override", "model": {"provider": "untrusted"}}
    result = ingress.admit(req, AUTH)
    assert result["assistantId"] == ASSISTANT
    assert "model" not in result and "name" not in result
    assert ingress.resolve(issued_call(req, result), req["assistant"])


def test_concurrent_duplicate_admissions_and_restart_return_same_binding(store, config):
    ingress = TelephonyIngress(store, config, limits=IngressLimits(workers=12, busy_seconds=.5))
    req = message()
    gate = threading.Barrier(8)
    def run():
        gate.wait()
        return ingress.admit(req, AUTH)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: run(), range(8)))
    assert all(result == results[0] for result in results)
    assert counts(store) == {"sessions": 1, "telephony_bindings": 1, "provider_tool_policies": 1}
    original = rows(store, "sessions")
    restarted = TelephonyIngress(store, config)
    assert restarted.admit(req, AUTH) == results[0]
    assert rows(store, "sessions") == original


@pytest.mark.parametrize("failure", [RuntimeError("injected"), sqlite3.OperationalError("injected")])
def test_failure_after_binding_insert_rolls_back_entire_admission(store, config, monkeypatch, failure):
    ingress = TelephonyIngress(store, config)
    original = ingress._insert_binding
    def fault(*args):
        original(*args)
        raise failure
    monkeypatch.setattr(ingress, "_insert_binding", fault)
    with pytest.raises((RuntimeError, IngressError)):
        ingress.admit(message(), AUTH)
    assert counts(store) == {"sessions": 0, "telephony_bindings": 0, "provider_tool_policies": 0}


def test_expired_deadline_after_partial_write_rolls_back(store, config, monkeypatch):
    ticks = [10.0]
    ingress = TelephonyIngress(store, config, monotonic=lambda: ticks[0])
    original = ingress._insert_binding
    def delayed(*args):
        original(*args)
        ticks[0] += 2
    monkeypatch.setattr(ingress, "_insert_binding", delayed)
    assert_error("telephony_deadline", ingress.admit, message(), AUTH)
    assert counts(store)["sessions"] == 0


def test_abandoned_waiter_cannot_admit_even_before_absolute_deadline(store, config):
    ingress = TelephonyIngress(store, config)
    abandoned = threading.Event()
    abandoned.set()
    assert_error("telephony_deadline", ingress.admit, message(), AUTH,
                 deadline=time.monotonic()+60, abandoned=abandoned)
    assert counts(store) == {"sessions": 0, "telephony_bindings": 0, "provider_tool_policies": 0}


def test_abandonment_after_partial_binding_rolls_back_before_commit(store, config, monkeypatch):
    ingress = TelephonyIngress(store, config)
    abandoned = threading.Event()
    original = ingress._insert_binding
    def lose_waiter(*args):
        original(*args)
        abandoned.set()
    monkeypatch.setattr(ingress, "_insert_binding", lose_waiter)
    assert_error("telephony_deadline", ingress.admit, message(), AUTH,
                 deadline=time.monotonic()+60, abandoned=abandoned)
    assert counts(store) == {"sessions": 0, "telephony_bindings": 0, "provider_tool_policies": 0}


def test_abandonment_does_not_undo_committed_admission_and_exact_retry_reconciles(store, config):
    ingress = TelephonyIngress(store, config)
    abandoned = threading.Event()
    req = message()
    committed = ingress.admit(req, AUTH, abandoned=abandoned)
    abandoned.set()
    assert ingress.admit(req, AUTH) == committed
    assert counts(store) == {"sessions": 1, "telephony_bindings": 1, "provider_tool_policies": 1}


def test_existing_browser_binding_is_not_hijacked(store, config):
    ingress = TelephonyIngress(store, config)
    browser = store.create_session("live")
    req = message()
    with store.connect() as db:
        db.execute("UPDATE sessions SET provider_call_id=? WHERE id=?", (req["call"]["id"], browser["id"]))
    assert_error("telephony_binding_conflict", ingress.admit, req, AUTH)
    assert counts(store) == {"sessions": 1, "telephony_bindings": 0, "provider_tool_policies": 0}
    assert store.authorize(browser["id"], browser["token"])["provider_call_id"] == req["call"]["id"]


@pytest.mark.parametrize("table,sql", [
    ("sessions", "UPDATE sessions SET tenant='foreign'"),
    ("sessions", "UPDATE sessions SET mode='rehearsal'"),
    ("provider_tool_policies", "UPDATE provider_tool_policies SET policy='provider-native'"),
    ("telephony_bindings", "UPDATE telephony_bindings SET organization_id='foreign'"),
])
def test_durable_scope_or_policy_conflict_is_not_repaired_or_rebound(store, config, table, sql):
    ingress = TelephonyIngress(store, config)
    req = message(); response = ingress.admit(req, AUTH)
    with store.connect() as db:
        db.execute(sql)
    before = rows(store, table)
    assert_error("telephony_binding_conflict", ingress.admit, req, AUTH)
    assert_error("telephony_binding_conflict", ingress.resolve, issued_call(req, response))
    assert rows(store, table) == before


@pytest.mark.parametrize("metadata", [None, {}, {"session_id": "wrong"}, {"session_id": "PLACEHOLDER", "session_token": "supplied"}])
def test_resolution_requires_exact_issued_metadata_and_never_admits(store, config, metadata):
    ingress = TelephonyIngress(store, config)
    req = message(); response = ingress.admit(req, AUTH)
    call = issued_call(req, response)
    call["assistantOverrides"] = {"variableValues": metadata}
    assert_error("telephony_metadata_mismatch", ingress.resolve, call)
    assert_error("telephony_not_admitted", ingress.resolve, {**call, "id": str(uuid4())})
    assert counts(store)["sessions"] == 1


def test_terminal_is_idempotent_and_duplicate_admission_cannot_resurrect(store, config):
    ingress = TelephonyIngress(store, config)
    req = message(); response = ingress.admit(req, AUTH)
    terminal = {"type": "status-update", "status": "ended", "call": req["call"]}
    assert ingress.end(terminal, AUTH) == {"received": True}
    ended = rows(store, "telephony_bindings")[0]["ended"]
    assert ingress.end({**terminal, "type": "end-of-call-report"}, AUTH) == {"received": True}
    assert rows(store, "telephony_bindings")[0]["ended"] == ended
    assert_error("telephony_call_ended", ingress.admit, req, AUTH)
    assert_error("telephony_call_ended", ingress.resolve, issued_call(req, response))
    assert ingress.resolve(issued_call(req, response), allow_ended=True) == response["assistantOverrides"]["variableValues"]["session_id"]
    assert_error("telephony_not_admitted", ingress.end, {**terminal, "call": message()["call"]}, AUTH)
    assert counts(store)["sessions"] == 1


def test_expiry_rejects_replay_but_late_terminal_closes_known_binding(store, config):
    now = [100.0]
    ingress = TelephonyIngress(store, config, clock=lambda: now[0], limits=IngressLimits(session_ttl_seconds=60))
    req = message(); response = ingress.admit(req, AUTH)
    now[0] += 60
    assert_error("telephony_call_expired", ingress.admit, req, AUTH)
    assert_error("telephony_call_expired", ingress.resolve, issued_call(req, response))
    assert_error("telephony_call_expired", ingress.resolve, issued_call(req, response), allow_ended=True)
    assert ingress.end({"type": "end-of-call-report", "call": req["call"]}, AUTH) == {"received": True}
    assert rows(store, "sessions")[0]["ended"] == 1


@pytest.mark.parametrize("terminal", [{"status": "ended"}, {"endedAt": "2026-10-18T00:00:00Z"}])
def test_terminal_provider_metadata_rejects_new_resolution_before_stored_end(store, config, terminal):
    ingress = TelephonyIngress(store, config)
    req = message(); response = ingress.admit(req, AUTH)
    call = {**issued_call(req, response), **terminal}
    assert rows(store, "sessions")[0]["ended"] == 0
    assert_error("telephony_call_ended", ingress.resolve, call)
    assert ingress.resolve(call, allow_ended=True)


def test_atomic_active_quota_and_exact_replay_before_quota(store, config):
    # Keep the actual eight-way race, with an explicit correctness-fixture
    # budget. Separate deadline tests cover the unchanged one-second default.
    assert IngressLimits().deadline_seconds == 1
    ingress = TelephonyIngress(store, config,
        limits=IngressLimits(active_calls=1, workers=10, busy_seconds=.5, deadline_seconds=5))
    requests = [message() for _ in range(8)]
    def run(req):
        try:
            return ingress.admit(req, AUTH)
        except IngressError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(run, requests))
    successes = [index for index, result in enumerate(results) if isinstance(result, dict)]
    assert len(successes) == 1
    assert set(result for result in results if isinstance(result, str)) == {"telephony_active_capacity"}
    assert ingress.admit(requests[successes[0]], AUTH) == results[successes[0]]
    assert counts(store)["sessions"] == 1


def test_rate_and_retention_quotas_do_not_delete_replay_history(store, config):
    now = [100.0]
    ingress = TelephonyIngress(store, config, limits=IngressLimits(active_calls=5, admissions_per_window=1, bindings_total=2), clock=lambda: now[0])
    req = message(); ingress.admit(req, AUTH)
    ingress.end({"type": "end-of-call-report", "call": req["call"]}, AUTH)
    assert_error("telephony_admission_rate", ingress.admit, message(), AUTH)
    now[0] += 61
    second = message(); result = ingress.admit(second, AUTH)
    now[0] += 61
    assert_error("telephony_retention_capacity", ingress.admit, message(), AUTH)
    assert ingress.admit(second, AUTH) == result
    assert counts(store)["sessions"] == 2


def test_sqlite_lock_returns_bounded_busy_and_safe_retry(store, config):
    ingress = TelephonyIngress(store, config, limits=IngressLimits(busy_seconds=.03, deadline_seconds=.2))
    req = message()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        start = time.monotonic()
        assert_error("telephony_busy", ingress.admit, req, AUTH)
        assert time.monotonic()-start < .75
        db.rollback()
    assert counts(store)["sessions"] == 0
    assert ingress.admit(req, AUTH)["assistantId"] == ASSISTANT


def test_external_deadline_and_worker_capacity_do_not_admit(store, config, monkeypatch):
    ingress = TelephonyIngress(store, config, limits=IngressLimits(workers=1))
    assert_error("telephony_deadline", ingress.admit, message(), AUTH, deadline=time.monotonic()-1)
    entered, release = threading.Event(), threading.Event()
    original = ingress._insert_binding
    def slow(*args):
        entered.set()
        assert release.wait(2)
        original(*args)
    monkeypatch.setattr(ingress, "_insert_binding", slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(ingress.admit, message(), AUTH)
        assert entered.wait(1)
        assert_error("telephony_worker_capacity", ingress.admit, message(), AUTH)
        release.set()
        assert first.result()["assistantId"] == ASSISTANT
    assert counts(store)["sessions"] == 1


def test_wrong_event_types_and_unselected_assistant_do_not_pass(store, config):
    ingress = TelephonyIngress(store, config)
    req = message(); response = ingress.admit(req, AUTH)
    assert_error("invalid_admission_event", ingress.admit, {**req, "type": "tool-calls"}, AUTH)
    assert_error("invalid_terminal_event", ingress.end, {**req, "type": "status-update", "status": "in-progress"}, AUTH)
    assert_error("telephony_assistant_mismatch", ingress.resolve, {**req["call"], "assistantOverrides": response["assistantOverrides"]})


def test_missing_database_is_not_created(config, tmp_path):
    class Missing:
        path = tmp_path / "missing.sqlite3"
    with pytest.raises(IngressError) as error:
        TelephonyIngress(Missing(), config)
    assert error.value.code == "telephony_storage_unavailable"
    assert not Missing.path.exists()
