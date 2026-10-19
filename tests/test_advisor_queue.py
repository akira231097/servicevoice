"""Disposable-database tests of the local operator workflow; no providers."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
from threading import Barrier

import pytest

from app.advisor_queue import AdvisorQueue
from app.domain import DomainError, Store, uid
from app.tools import execute


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "advisor.sqlite3")


def tool(store, sid, name, arguments, call_id=None):
    result = execute(store, sid, name, arguments, call_id or uid("tool"))
    assert result["ok"], result
    return result


def handoff(store, *, reason="Caller requests warranty help", session=None):
    session = session or store.create_session()
    result = tool(store, session["id"], "request_handoff", {"reason": reason}, "handoff-request")
    return session, result


def booking(store):
    session = store.create_session()
    sid = session["id"]
    slots = tool(store, sid, "search_availability", {"service": "oil_change"})["slots"]
    proposal = tool(store, sid, "prepare_booking", {
        "service": "oil_change", "slot_id": slots[0]["id"], "customer_name": "Alex Morgan",
        "phone": "5125550142", "vehicle": "2021 Toyota Camry",
    })
    appointment = tool(store, sid, "confirm_action", {"proposal_id": proposal["proposal_id"], "confirmed": True})["appointment"]
    # Keep a different, uncommitted proposal in history so the inbox must not
    # conflate a cancellation proposal with a canceled appointment.
    cancellation = tool(store, sid, "prepare_cancellation", {})
    return session, appointment, cancellation


def original_tables(store):
    with store.connect() as db:
        names = ("sessions", "slots", "proposals", "appointments", "tool_results", "tool_invocations", "events", "transcripts", "handoffs")
        return {name: [tuple(row) for row in db.execute(f"SELECT * FROM {name} ORDER BY rowid")] for name in names}


def assert_error(code, function, *args, **kwargs):
    with pytest.raises(DomainError) as error:
        function(*args, **kwargs)
    assert error.value.code == code


def test_existing_handoff_migrates_without_changing_voice_data_or_receipt(store):
    session, original = handoff(store, reason="  Keep original reason exactly.  ")
    before = original_tables(store)
    queue = AdvisorQueue(store)
    case = queue.get_case(original["id"])
    assert case["status"] == "open" and case["revision"] == 0
    assert case["reason"] == original["reason"]
    assert case["handoff_status"] == "callback_requested"
    assert case["assigned_operator"] is None and case["audit"] == []
    assert case["context"]["customer"] is None
    assert original_tables(store) == before
    replay = tool(store, session["id"], "request_handoff", {"reason": "  Keep original reason exactly.  "}, "handoff-request")
    assert replay == original
    assert original_tables(store) == before


def test_ended_session_case_survives_restart_and_tracks_operator_audit(store):
    session, result = handoff(store)
    with store.connect() as db:
        db.execute("UPDATE sessions SET ended=1 WHERE id=?", (session["id"],))
    queue = AdvisorQueue(store)
    claimed = queue.update_case(result["id"], 0, "in_progress", "  Sam  ", "Reviewing warranty question")
    assert claimed["revision"] == 1 and claimed["assigned_operator"] == "Sam"
    restarted = AdvisorQueue(Store(store.path))
    assert restarted.get_case(result["id"]) == claimed
    resolved = restarted.update_case(result["id"], 1, "resolved", "Taylor", "Recorded an internal resolution; no callback sent.")
    assert resolved["context"]["session"]["ended"] is True
    assert resolved["assigned_operator"] == "Sam" and resolved["last_operator"] == "Taylor"
    assert [(entry["revision"], entry["from_status"], entry["to_status"]) for entry in resolved["audit"]] == [
        (1, "open", "in_progress"), (2, "in_progress", "resolved")]
    reopened = restarted.update_case(result["id"], 2, "open", "Taylor", "Needs another review")
    assert reopened["assigned_operator"] is None and reopened["revision"] == 3
    assert reopened["handoff_status"] == "callback_requested"


def test_two_operators_claiming_same_revision_have_one_winner(store):
    _, result = handoff(store)
    queue = AdvisorQueue(store)
    barrier = Barrier(2)

    def claim(operator):
        barrier.wait(timeout=5)
        try:
            return queue.update_case(result["id"], 0, "in_progress", operator, "")
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(claim, ["Operator A", "Operator B"]))
    assert outcomes.count("advisor_revision_conflict") == 1
    saved = queue.get_case(result["id"])
    assert saved["revision"] == 1 and len(saved["audit"]) == 1
    assert saved["assigned_operator"] in {"Operator A", "Operator B"}


def test_replayed_update_and_same_status_do_not_append_duplicate_audit(store):
    _, result = handoff(store)
    queue = AdvisorQueue(store)
    saved = queue.update_case(result["id"], 0, "in_progress", "Sam", "")
    assert_error("advisor_revision_conflict", queue.update_case, result["id"], 0, "in_progress", "Sam", "")
    assert_error("advisor_invalid_transition", queue.update_case, result["id"], 1, "in_progress", "Other", "Changed note")
    assert queue.get_case(result["id"]) == saved


def test_multiline_note_persists_and_operator_remains_single_line(store):
    _, result = handoff(store)
    queue = AdvisorQueue(store)
    saved = queue.update_case(result["id"], 0, "resolved", "Sam", "Checked local record.\r\nNo callback sent.\nNeeds separate follow-up.")
    expected = "Checked local record.\nNo callback sent.\nNeeds separate follow-up."
    assert saved["last_note"] == expected and saved["audit"][-1]["note"] == expected
    assert AdvisorQueue(Store(store.path)).get_case(result["id"])["last_note"] == expected
    assert_error("advisor_invalid_input", queue.update_case, result["id"], 1, "open", "Sam\nOther", "")
    for note in ("text\x00secret", "text\x1b[31m", "text\rbare return"):
        assert_error("advisor_invalid_input", queue.update_case, result["id"], 1, "open", "Sam", note)
    assert queue.get_case(result["id"]) == saved


def test_audit_write_failure_rolls_back_case_metadata(store):
    _, result = handoff(store)
    queue = AdvisorQueue(store)
    with store.connect() as db:
        db.execute("""CREATE TRIGGER reject_case_audit BEFORE INSERT ON advisor_case_audit
            BEGIN SELECT RAISE(ABORT,'injected audit failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected audit failure"):
        queue.update_case(result["id"], 0, "in_progress", "Sam", "")
    saved = queue.get_case(result["id"])
    assert saved["status"] == "open" and saved["revision"] == 0 and saved["audit"] == []
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM advisor_case_metadata").fetchone()[0] == 0


def test_operator_changes_never_mutate_scheduler_or_original_handoff(store):
    session, appointment, cancellation = booking(store)
    _, result = handoff(store, session=session)
    before = original_tables(store)
    queue = AdvisorQueue(store)
    detail = queue.update_case(result["id"], 0, "resolved", "Sam", "Local review finished; no delivery attempted.")
    assert original_tables(store) == before
    assert detail["context"]["appointment"] == appointment
    assert detail["context"]["proposal"]["id"] == cancellation["proposal_id"]
    assert detail["context"]["proposal"]["status"] == "superseded"
    assert detail["context"]["proposal"]["kind"] == "cancel"
    assert detail["context"]["appointment"]["status"] == "confirmed"
    assert detail["context"]["customer"] == {
        "name": "Alex Morgan", "phone": "+15125550142", "vehicle": "2021 Toyota Camry",
        "verification": "caller_provided_unverified"}
    assert "does not send a callback" in " ".join(detail["limitations"])


def test_foreign_tenant_and_unknown_case_cannot_be_read_or_changed(store):
    local, result = handoff(store)
    foreign, other = handoff(store)
    with store.connect() as db:
        db.execute("UPDATE sessions SET tenant='other-tenant' WHERE id=?", (foreign["id"],))
    queue = AdvisorQueue(store)
    assert [case["id"] for case in queue.list_cases("all")["items"]] == [result["id"]]
    for identifier in [other["id"], "handoff_does_not_exist"]:
        assert_error("advisor_case_not_found", queue.get_case, identifier)
        assert_error("advisor_case_not_found", queue.update_case, identifier, 0, "resolved", "Sam", "reviewed")
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM advisor_case_metadata").fetchone()[0] == 0
    assert queue.get_case(result["id"])["session_id"] == local["id"]


def test_context_omits_tokens_provider_binding_and_unrecognized_state_fields(store):
    session, result = handoff(store)
    with store.connect() as db:
        row = db.execute("SELECT token_hash FROM sessions WHERE id=?", (session["id"],)).fetchone()
        token_hash = row[0]
        db.execute("UPDATE sessions SET provider_call_id=? WHERE id=?", ("private-provider-call", session["id"]))
    store.update(session["id"], {"system_prompt": "private-system-prompt", "provider_secret": "private-secret"})
    encoded = json.dumps(AdvisorQueue(store).get_case(result["id"]))
    for private in [session["token"], token_hash, "private-provider-call", "private-system-prompt", "private-secret"]:
        assert private not in encoded


def test_transcript_pending_and_audit_are_bounded_and_uncertainty_stays_visible(store):
    session, result = handoff(store)
    sid = session["id"]
    for index in range(45):
        store.transcript(sid, "user", f"untrusted caller text {index}", f"line-{index}")
    with store.connect() as db:
        for index in range(22):
            db.execute("INSERT INTO tool_invocations VALUES (?,?,?,?,?,?,?,?,?)",
                       (f"operation_{index:02}", sid, f"call-{index}", "fingerprint", "owner", "unknown", index, index, None))
        db.execute("INSERT INTO tool_results VALUES (?,?,?,?)", (sid, "call-0", "fingerprint", '{"ok":true}'))
    queue = AdvisorQueue(store)
    for revision in range(102):
        queue.update_case(result["id"], revision, "in_progress" if revision % 2 == 0 else "open", "Sam", "")
    detail = queue.get_case(result["id"])
    assert len(detail["transcript"]) == 40 and detail["transcript_truncated"]
    assert detail["transcript"][0]["text"] == "untrusted caller text 5"
    assert detail["transcript"][-1]["text"] == "untrusted caller text 44"
    assert len(detail["audit"]) == 100 and detail["audit_truncated"]
    assert [entry["revision"] for entry in detail["audit"]] == list(range(3, 103))
    assert len(detail["pending_operations"]) == 20 and detail["pending_operations_truncated"]
    assert detail["pending_operations"][0] == {"id": "operation_01", "status": "unknown"}
    assert all(entry["status"] == "unknown" for entry in detail["pending_operations"])
    assert "require reconciliation" in " ".join(detail["limitations"])


def test_keyset_pagination_handles_equal_times_and_new_cases_without_duplicates(store):
    identifiers = [handoff(store)[1]["id"] for _ in range(5)]
    with store.connect() as db:
        db.execute("UPDATE handoffs SET created='2026-10-18T12:00:00+00:00'")
    queue = AdvisorQueue(store)
    first = queue.list_cases("all", limit=2)
    assert first["next_cursor"] and len(first["items"]) == 2
    _, newer = handoff(store)
    with store.connect() as db:
        db.execute("UPDATE handoffs SET created='2026-10-19T12:00:00+00:00' WHERE id=?", (newer["id"],))
    second = queue.list_cases("all", limit=2, before=first["next_cursor"])
    third = queue.list_cases("all", limit=2, before=second["next_cursor"])
    found = [entry["id"] for page in (first, second, third) for entry in page["items"]]
    assert found == sorted(identifiers, reverse=True)
    assert third["next_cursor"] is None
    queue.update_case(identifiers[0], 0, "resolved", "Sam", "Local review complete")
    assert [item["id"] for item in queue.list_cases("resolved")["items"]] == [identifiers[0]]
    assert identifiers[0] not in [item["id"] for item in queue.list_cases("open")["items"]]


def test_oversized_legacy_transcript_is_bounded_and_marked_truncated(store):
    session, result = handoff(store)
    with store.connect() as db:
        db.execute("INSERT INTO transcripts(session_id,event_id,role,text) VALUES (?,?,?,?)",
                   (session["id"], "legacy", "user", "x" * 7000))
    detail = AdvisorQueue(store).get_case(result["id"])
    assert detail["transcript"][-1] == {"role": "user", "text": "x" * 6000}
    assert detail["transcript_truncated"]


@pytest.mark.parametrize("changes", [
    {"expected_revision": True}, {"expected_revision": -1}, {"expected_revision": 1.0},
    {"expected_revision": "0"}, {"status": "callback_sent"}, {"status": []},
    {"operator": " "}, {"operator": "x" * 81}, {"operator": "Alex\u202e"},
    {"note": "x" * 2001}, {"note": "secret\x00data"}, {"note": None},
    {"status": "resolved", "note": " "},
])
def test_invalid_updates_are_atomic(store, changes):
    _, result = handoff(store)
    queue = AdvisorQueue(store)
    arguments = {"expected_revision": 0, "status": "in_progress", "operator": "Sam", "note": ""}
    arguments.update(changes)
    before = queue.get_case(result["id"])
    assert_error("advisor_invalid_input", queue.update_case, result["id"], **arguments)
    assert queue.get_case(result["id"]) == before


@pytest.mark.parametrize("arguments", [
    {"limit": True}, {"limit": 0}, {"limit": 101}, {"limit": "5"},
    {"status": []}, {"status": "closed"}, {"before": "A"},
    {"before": "not a cursor"}, {"before": "e30"}, {"before": "a" * 513},
])
def test_invalid_list_input_fails_closed(store, arguments):
    queue = AdvisorQueue(store)
    assert_error("advisor_invalid_input", queue.list_cases, **arguments)


def test_case_created_after_queue_initialization_is_immediately_available(store):
    queue = AdvisorQueue(store)
    assert queue.list_cases()["items"] == []
    _, result = handoff(store)
    assert queue.list_cases()["items"][0]["id"] == result["id"]
