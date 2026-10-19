"""Business invariants for the fictional scheduler, without network/provider calls."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier

import pytest

from app import domain
from app.domain import Store
from app.rehearsal import respond
from app.tools import execute


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(domain, "now", lambda: datetime(2026, 10, 17, 18, tzinfo=timezone.utc))
    return Store(tmp_path / "scheduler.sqlite3")


def call(store, sid, name, arguments=None, call_id=None):
    return execute(store, sid, name, arguments or {}, call_id or domain.uid("test"))


def search(store, sid):
    result = call(store, sid, "search_availability", {"service": "oil_change"})
    assert result["ok"]
    assert result["slots"]
    return result["slots"]


def prepare(store, sid, slot_id):
    result = call(store, sid, "prepare_booking", {
        "service": "oil_change", "slot_id": slot_id, "customer_name": "Alex Morgan",
        "phone": "5125550142", "vehicle": "2021 Toyota Camry",
    })
    assert result["ok"], result
    return result


def confirm(store, sid, proposal, call_id=None):
    return call(store, sid, "confirm_action", {
        "proposal_id": proposal["proposal_id"], "confirmed": True,
    }, call_id)


def book(store, slot_index=0):
    sid = store.create_session()["id"]
    slots = search(store, sid)
    proposal = prepare(store, sid, slots[slot_index]["id"])
    result = confirm(store, sid, proposal)
    assert result["ok"], result
    return sid, result["appointment"]


def active_count(store, slot_id=None):
    with store.connect() as db:
        if slot_id:
            return db.execute("SELECT count(*) FROM appointments WHERE status='confirmed' AND slot_id=?", (slot_id,)).fetchone()[0]
        return db.execute("SELECT count(*) FROM appointments WHERE status='confirmed'").fetchone()[0]


def test_two_callers_competing_for_last_slot_get_exactly_one_booking(store):
    sessions = [store.create_session()["id"] for _ in range(2)]
    slot_id = search(store, sessions[0])[0]["id"]
    search(store, sessions[1])
    proposals = [prepare(store, sid, slot_id) for sid in sessions]
    barrier = Barrier(2)

    def race(index):
        barrier.wait(timeout=5)
        return confirm(store, sessions[index], proposals[index])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(race, range(2)))
    assert sum(result["ok"] for result in results) == 1
    assert next(result for result in results if not result["ok"])["error"] == "slot_unavailable"
    assert active_count(store, slot_id) == 1


def test_retry_after_lost_commit_response_replays_same_result(store):
    sid = store.create_session()["id"]
    proposal = prepare(store, sid, search(store, sid)[0]["id"])
    first = confirm(store, sid, proposal, "commit-1")
    # Pretend the transport lost `first`; retrying is allowed to return that exact result.
    retried = confirm(store, sid, proposal, "commit-1")
    assert retried == first
    assert active_count(store) == 1
    assert len([e for e in store.snapshot(sid)["events"] if e["name"] == "confirm_action"]) == 1


def test_same_tool_call_id_changed_payload_conflicts(store):
    sid = store.create_session()["id"]
    proposal = prepare(store, sid, search(store, sid)[0]["id"])
    original = confirm(store, sid, proposal, "commit-1")
    changed = call(store, sid, "confirm_action", {
        "proposal_id": proposal["proposal_id"], "confirmed": False,
    }, "commit-1")
    assert original["ok"]
    assert changed["error"] == "idempotency_conflict"
    assert active_count(store) == 1


def test_same_committed_proposal_new_tool_id_does_not_duplicate(store):
    sid = store.create_session()["id"]
    proposal = prepare(store, sid, search(store, sid)[0]["id"])
    original = confirm(store, sid, proposal, "commit-1")
    replay = confirm(store, sid, proposal, "commit-2")
    assert original == replay
    assert active_count(store) == 1


def test_new_search_invalidates_old_confirmation(store):
    sid = store.create_session()["id"]
    proposal = prepare(store, sid, search(store, sid)[0]["id"])
    search(store, sid)
    result = confirm(store, sid, proposal)
    assert result["error"] == "stale_proposal"
    assert active_count(store) == 0


def test_new_proposal_supersedes_previous_one(store):
    sid = store.create_session()["id"]
    slots = search(store, sid)
    original = prepare(store, sid, slots[0]["id"])
    replacement = prepare(store, sid, slots[1]["id"])
    assert confirm(store, sid, original)["error"] == "stale_proposal"
    assert confirm(store, sid, replacement)["appointment"]["slot_id"] == slots[1]["id"]


def test_expired_proposal_cannot_create_booking(store):
    sid = store.create_session()["id"]
    proposal = prepare(store, sid, search(store, sid)[0]["id"])
    with store.connect() as db:
        db.execute("UPDATE proposals SET expires=0 WHERE id=?", (proposal["proposal_id"],))
    assert confirm(store, sid, proposal)["error"] == "proposal_expired"
    assert active_count(store) == 0


@pytest.mark.parametrize("value,expected", [
    (False, "confirmation_required"), ("false", "invalid_arguments"),
    ("true", "invalid_arguments"), (1, "invalid_arguments"), (None, "invalid_arguments"),
])
def test_confirmation_is_strict_boolean_and_false_never_commits(store, value, expected):
    sid = store.create_session()["id"]
    proposal = prepare(store, sid, search(store, sid)[0]["id"])
    result = call(store, sid, "confirm_action", {
        "proposal_id": proposal["proposal_id"], "confirmed": value,
    })
    assert result["error"] == expected
    assert active_count(store) == 0


def test_other_session_cannot_confirm_or_view_first_sessions_booking(store):
    owner = store.create_session()["id"]
    stranger = store.create_session()["id"]
    proposal = prepare(store, owner, search(store, owner)[0]["id"])
    assert confirm(store, stranger, proposal)["error"] == "proposal_missing"
    assert confirm(store, owner, proposal)["ok"]
    assert call(store, stranger, "get_appointment")["appointment"] is None


def test_reschedule_conflict_preserves_original_booking(store):
    sid, original = book(store)
    target = search(store, sid)[0]["id"]
    move = call(store, sid, "prepare_reschedule", {"slot_id": target})
    assert move["ok"]
    competitor = store.create_session()["id"]
    search(store, competitor)
    assert confirm(store, competitor, prepare(store, competitor, target))["ok"]
    failed_move = confirm(store, sid, move)
    assert failed_move["error"] == "slot_unavailable"
    current = call(store, sid, "get_appointment")["appointment"]
    assert current["id"] == original["id"]
    assert current["slot_id"] == original["slot_id"]
    assert current["status"] == "confirmed"
    assert active_count(store, original["slot_id"]) == 1


def test_successful_reschedule_releases_old_slot_and_retains_identity(store):
    sid, original = book(store)
    target = search(store, sid)[0]["id"]
    move = call(store, sid, "prepare_reschedule", {"slot_id": target})
    moved = confirm(store, sid, move)["appointment"]
    assert moved["id"] == original["id"]
    assert moved["reference"] == original["reference"]
    assert moved["slot_id"] == target
    assert active_count(store, original["slot_id"]) == 0
    assert active_count(store, target) == 1


def test_cancellation_releases_capacity_once_and_replay_is_safe(store):
    sid, appointment = book(store)
    cancellation = call(store, sid, "prepare_cancellation")
    cancelled = confirm(store, sid, cancellation)
    assert cancelled["appointment"]["status"] == "cancelled"
    assert confirm(store, sid, cancellation) == cancelled
    assert active_count(store, appointment["slot_id"]) == 0
    new_sid = store.create_session()["id"]
    assert appointment["slot_id"] in [s["id"] for s in search(store, new_sid)]
    assert confirm(store, new_sid, prepare(store, new_sid, appointment["slot_id"]))["ok"]
    assert active_count(store, appointment["slot_id"]) == 1


@pytest.mark.parametrize("name,args", [
    ("search_availability", {}),
    ("search_availability", {"service": "oil_change", "tenant_id": "other"}),
    ("prepare_booking", {"service": "oil_change"}),
    ("confirm_action", {"proposal_id": "proposal_missing"}),
    ("get_appointment", {"session_id": "someone-else"}),
    ("request_handoff", {"reason": "   "}),
])
def test_missing_extra_and_invalid_arguments_are_rejected_without_mutation(store, name, args):
    sid = store.create_session()["id"]
    result = call(store, sid, name, args)
    assert result["error"] == "invalid_arguments"
    assert active_count(store) == 0
    assert store.snapshot(sid)["handoff"] is None


@pytest.mark.parametrize("requested_date,expected_error", [
    ("2026-02-30", "invalid_date"),
    ("yesterday", "invalid_arguments"),
    ("2026-10-16", "out_of_range"),
    ("2026-12-01", "out_of_range"),
])
def test_bad_or_out_of_horizon_date_is_not_silently_reinterpreted(store, requested_date, expected_error):
    sid = store.create_session()["id"]
    result = call(store, sid, "search_availability", {"service": "oil_change", "date": requested_date})
    assert not result["ok"]
    assert result["error"] == expected_error


def test_handoff_invalidates_pending_booking_and_is_explicitly_local(store):
    sid = store.create_session()["id"]
    proposal = prepare(store, sid, search(store, sid)[0]["id"])
    handoff = call(store, sid, "request_handoff", {"reason": "Caller asks about warranty coverage"})
    assert handoff["ok"]
    assert "local" in handoff["message"].lower()
    assert "No advisor was called" in handoff["message"]
    assert confirm(store, sid, proposal)["error"] == "stale_proposal"
    assert active_count(store) == 0


def test_rehearsal_corrected_details_require_fresh_confirmation(store):
    sid = store.create_session()["id"]
    respond(store, sid, "oil change")
    respond(store, sid, "next available")
    respond(store, sid, "1")
    respond(store, sid, "Alex Morgan, 5125550142, 2021 Toyota Camry")
    old_proposal = store.state(sid)["proposal_id"]
    corrected = respond(store, sid, "Alex Morgan, 5125550199, 2022 Toyota Camry")
    assert "2022 Toyota Camry" in corrected
    assert active_count(store) == 0
    assert store.state(sid)["proposal_id"] != old_proposal
    assert call(store, sid, "confirm_action", {"proposal_id": old_proposal, "confirmed": True})["error"] == "stale_proposal"
    respond(store, sid, "confirm")
    assert call(store, sid, "get_appointment")["appointment"]["vehicle"] == "2022 Toyota Camry"


def test_rehearsal_declining_readback_does_not_book(store):
    sid = store.create_session()["id"]
    respond(store, sid, "oil change")
    respond(store, sid, "next available")
    respond(store, sid, "1")
    respond(store, sid, "Alex Morgan, 5125550142, 2021 Toyota Camry")
    respond(store, sid, "no")
    respond(store, sid, "yes")
    assert active_count(store) == 0


@pytest.mark.parametrize("later_action", ["cancel", "reschedule"])
def test_new_tool_id_cannot_present_superseded_committed_proposal_as_current(store, later_action):
    sid = store.create_session()["id"]
    proposal = prepare(store, sid, search(store, sid)[0]["id"])
    original = confirm(store, sid, proposal, "original-booking")
    if later_action == "cancel":
        change = call(store, sid, "prepare_cancellation")
    else:
        target = search(store, sid)[0]["id"]
        change = call(store, sid, "prepare_reschedule", {"slot_id": target})
    current = confirm(store, sid, change)["appointment"]
    stale = confirm(store, sid, proposal, "new-id-old-proposal")
    assert stale["error"] == "historical_proposal"
    assert stale["operation_completed"] is True
    assert stale["historical_result"] == original
    assert stale["current_appointment"] == current
    assert call(store, sid, "get_appointment")["appointment"] == current
    # Transport replay keeps its immutable historical receipt; it cannot be
    # rewritten to a different action/result without breaking idempotency.
    assert confirm(store, sid, proposal, "original-booking") == original


def test_declined_proposal_cannot_be_confirmed_later(store):
    sid = store.create_session()["id"]
    proposal = prepare(store, sid, search(store, sid)[0]["id"])
    result = call(store, sid, "dismiss_proposal", {"proposal_id": proposal["proposal_id"]})
    assert result["ok"]
    assert store.snapshot(sid)["proposal"] is None
    assert confirm(store, sid, proposal)["error"] == "stale_proposal"
    assert active_count(store) == 0
    assert call(store, sid, "dismiss_proposal", {"proposal_id": proposal["proposal_id"]})["ok"]


def test_declining_cancellation_preserves_booking_and_owner_scope(store):
    sid, original = book(store)
    proposal = call(store, sid, "prepare_cancellation")
    other = store.create_session()["id"]
    assert call(store, other, "dismiss_proposal", {"proposal_id": proposal["proposal_id"]})["error"] == "proposal_missing"
    assert call(store, sid, "dismiss_proposal", {"proposal_id": proposal["proposal_id"]})["ok"]
    assert call(store, sid, "get_appointment")["appointment"] == original
    assert confirm(store, sid, proposal)["error"] == "stale_proposal"
