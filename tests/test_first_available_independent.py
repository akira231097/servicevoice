"""Independent fused-proposal invariants using disposable local databases only.

These tests verify transactional state and explicit confirmation arguments.
They do not establish spoken assent, completed audio readback, or language intent.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from threading import Barrier
import time

import pytest

from app import domain
from app.domain import DomainError, Store
from app.tools import execute


FUSED = "prepare_first_available_booking"
INTAKE = {
    "strategy": "first_available", "service": "oil_change",
    "customer_name": "Alex Morgan", "phone": "5125550142",
    "vehicle": "2021 Toyota Camry",
}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(domain, "now", lambda: datetime(2026, 10, 18, 18, tzinfo=timezone.utc))
    return Store(tmp_path / "independent-fused.sqlite3")


def call(store, sid, name, arguments, call_id=None):
    return execute(store, sid, name, arguments, call_id or domain.uid("independent"))


def prepare(store, sid, call_id=None, **overrides):
    return call(store, sid, FUSED, {**INTAKE, **overrides}, call_id)


def confirm(store, sid, proposal, confirmed=True):
    return call(store, sid, "confirm_action", {
        "proposal_id": proposal["proposal_id"], "confirmed": confirmed,
    })


def counters(store, sid):
    with store.read_connection() as db:
        return {table: db.execute(f"SELECT COUNT(*) FROM {table} WHERE session_id=?", (sid,)).fetchone()[0]
                for table in ("proposals", "appointments", "handoffs")}


def test_rejected_phone_never_leaves_offer_or_intake_state(store):
    sid = store.create_session()["id"]
    before = store.state(sid)
    # Length passes the transport schema, but this is not a US demo number.
    result = prepare(store, sid, phone="123456789012")
    assert not result["ok"] and result["error"] == "invalid_phone"
    assert store.state(sid) == before
    assert counters(store, sid) == {"proposals": 0, "appointments": 0, "handoffs": 0}


@pytest.mark.parametrize("error_type", [DomainError, RuntimeError])
def test_failure_after_proposal_insert_rolls_back_all_fused_state(store, monkeypatch, error_type):
    sid = store.create_session()["id"]
    before = store.state(sid)
    original = store._proposal
    def fail_after_write(*args, **kwargs):
        result = original(*args, **kwargs)
        assert result["ok"]
        if error_type is DomainError:
            raise DomainError("synthetic_late_failure", "Injected failure after proposal write")
        raise RuntimeError("Injected failure after proposal write")
    monkeypatch.setattr(store, "_proposal", fail_after_write)
    if error_type is DomainError:
        result = prepare(store, sid, call_id="fused-fails-after-write")
        assert not result["ok"] and result["error"] == "synthetic_late_failure"
    else:
        with pytest.raises(RuntimeError, match="Injected failure"):
            prepare(store, sid, call_id="fused-fails-after-write")
    assert store.state(sid) == before
    assert counters(store, sid) == {"proposals": 0, "appointments": 0, "handoffs": 0}
    # A new, valid request can prepare after rollback; no partial proposal or
    # leaked intake makes the fresh-state gate reject it.
    monkeypatch.setattr(store, "_proposal", original)
    assert prepare(store, sid)["ok"]
    assert counters(store, sid) == {"proposals": 1, "appointments": 0, "handoffs": 0}


def test_no_openings_does_not_create_partial_state(store):
    sid = store.create_session()["id"]
    before = store.state(sid)
    with store.transaction() as db:
        db.execute("DELETE FROM slots")
    result = prepare(store, sid)
    assert not result["ok"]
    assert store.state(sid) == before
    assert counters(store, sid) == {"proposals": 0, "appointments": 0, "handoffs": 0}


def test_two_unreserved_proposals_still_compete_at_confirmation(store):
    sids = [store.create_session()["id"] for _ in range(2)]
    proposals = [prepare(store, sid) for sid in sids]
    assert all(result["ok"] for result in proposals)
    selected = [store.state(sid)["selected_slot"] for sid in sids]
    assert selected[0] == selected[1]
    assert all(counters(store, sid)["appointments"] == 0 for sid in sids)
    barrier = Barrier(2)
    def competing_confirm(index):
        barrier.wait(timeout=5)
        return confirm(store, sids[index], proposals[index])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(competing_confirm, range(2)))
    assert sum(result["ok"] for result in results) == 1
    assert next(result for result in results if not result["ok"])["error"] == "slot_unavailable"
    with store.read_connection() as db:
        assert db.execute("SELECT COUNT(*) FROM appointments WHERE slot_id=? AND status='confirmed'", (selected[0],)).fetchone()[0] == 1


def test_competing_new_ids_for_one_session_cannot_create_two_proposals(store):
    sid = store.create_session()["id"]
    barrier = Barrier(2)
    def competing_prepare(index):
        barrier.wait(timeout=5)
        return prepare(store, sid, call_id=f"fresh-fused-{index}")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(competing_prepare, range(2)))
    assert sum(result["ok"] for result in results) == 1
    assert counters(store, sid) == {"proposals": 1, "appointments": 0, "handoffs": 0}
    assert store.state(sid)["proposal_id"] == next(result for result in results if result["ok"])["proposal_id"]


def test_false_confirmation_does_not_commit_then_explicit_true_can_commit(store):
    sid = store.create_session()["id"]
    proposal = prepare(store, sid)
    assert proposal["ok"]
    assert confirm(store, sid, proposal, confirmed=False)["error"] == "confirmation_required"
    assert counters(store, sid)["appointments"] == 0
    assert store.state(sid)["proposal_id"] == proposal["proposal_id"]
    result = confirm(store, sid, proposal)
    assert result["ok"] and result["appointment"]["status"] == "confirmed"
    assert counters(store, sid)["appointments"] == 1


@pytest.mark.parametrize("change", ["expired", "corrected_service", "cross_session"])
def test_old_or_other_sessions_proposal_cannot_commit(store, change):
    sid = store.create_session()["id"]
    proposal = prepare(store, sid)
    assert proposal["ok"]
    confirmer = sid
    if change == "expired":
        with store.transaction() as db:
            db.execute("UPDATE proposals SET expires=? WHERE id=?", (time.time()-1, proposal["proposal_id"]))
    elif change == "corrected_service":
        assert call(store, sid, "search_availability", {"service": "tires"})["ok"]
    else:
        confirmer = store.create_session()["id"]
    result = confirm(store, confirmer, proposal)
    assert not result["ok"]
    assert result["error"] == {"expired": "proposal_expired", "corrected_service": "stale_proposal", "cross_session": "proposal_missing"}[change]
    assert counters(store, sid)["appointments"] == 0
    assert counters(store, confirmer)["appointments"] == 0


def test_dismissed_history_stays_ineligible_even_if_visible_state_is_reset(store):
    sid = store.create_session()["id"]
    initial = store.state(sid)
    proposal = prepare(store, sid)
    assert proposal["ok"]
    assert call(store, sid, "dismiss_proposal", {"proposal_id": proposal["proposal_id"]})["ok"]
    with store.transaction() as db:
        db.execute("UPDATE sessions SET state=? WHERE id=?", (json.dumps(initial), sid))
    result = prepare(store, sid)
    assert not result["ok"]
    assert store.state(sid) == initial
    assert counters(store, sid) == {"proposals": 1, "appointments": 0, "handoffs": 0}


def test_unresolved_prior_invocation_blocks_fused_proposal_even_if_marked_completed(store):
    sid = store.create_session()["id"]
    before = store.state(sid)
    with store.transaction() as db:
        db.execute("INSERT INTO tool_invocations VALUES (?,?,?,?,?,?,?,?,?)",
                   ("op_prior_unknown", sid, "prior-call", "synthetic", "old-owner", "completed", time.time(), time.time(), None))
    # A journal status alone is not an authoritative tool result.
    result = prepare(store, sid)
    assert not result["ok"]
    assert store.state(sid) == before
    assert counters(store, sid) == {"proposals": 0, "appointments": 0, "handoffs": 0}


def test_completed_context_read_keeps_fresh_session_eligible(store):
    sid = store.create_session()["id"]
    assert call(store, sid, "get_session_context", {})["ok"]
    assert call(store, sid, "get_dealership_info", {})["ok"]
    result = prepare(store, sid)
    assert result["ok"]
    assert counters(store, sid) == {"proposals": 1, "appointments": 0, "handoffs": 0}
