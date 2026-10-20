"""A missing secondary service must never be a successful booking evaluation."""
from pathlib import Path
import sqlite3

import pytest

from app.domain import Store
from app.live_eval_evidence import EvidenceError, integrity, read_snapshot


def combined(tmp_path):
    store = Store(tmp_path / "combined-evidence.sqlite3")
    sid = store.create_session("voice")["id"]
    cid = "combined-evidence-call"
    with store.connect() as db:
        db.execute("UPDATE sessions SET provider_call_id=? WHERE id=?", (cid, sid))
    search = store.dispatch(sid, "begin_service_request", {"service": "diagnostic", "services": ["diagnostic", "brakes"]}, "search")
    proposal = store.dispatch(sid, "prepare_booking", {"service": "diagnostic", "services": ["diagnostic", "brakes"],
        "slot_id": search["slots"][0]["id"], "customer_name": "Alex Morgan", "phone": "5125550142", "vehicle": "2022 Toyota Camry"}, "prepare")
    saved = store.dispatch(sid, "confirm_action", {"proposal_id": proposal["proposal_id"], "confirmed": True}, "confirm")
    assert saved["ok"]
    return store, sid, cid, saved["appointment"]


def test_combined_snapshot_is_readonly_and_checks_complete_scope(tmp_path):
    store, sid, cid, appointment = combined(tmp_path)
    before = Path(store.path).read_bytes()
    snapshot = read_snapshot(store.path, cid)
    assert Path(store.path).read_bytes() == before
    assert len(snapshot["appointments"]) == 1
    assert snapshot["appointments"][0]["services"] == ["diagnostic", "brakes"]
    assert integrity(snapshot)["verdict"] == "pass"


@pytest.mark.parametrize("location", ["saved", "receipt", "proposal"])
def test_dropped_secondary_service_fails(tmp_path, location):
    store, sid, cid, appointment = combined(tmp_path)
    snapshot = read_snapshot(store.path, cid)
    if location == "saved":
        snapshot["appointments"][0].pop("services")
    elif location == "receipt":
        snapshot["proposals"][0]["result"]["appointment"].pop("services")
    else:
        snapshot["proposals"][0]["payload"].pop("services")
    assert integrity(snapshot)["verdict"] == "fail"


def test_deleted_child_rows_are_detected_against_receipt(tmp_path):
    store, sid, cid, appointment = combined(tmp_path)
    with store.connect() as db:
        db.execute("DELETE FROM appointment_services WHERE appointment_id=? AND service='brakes'", (appointment["id"],))
    assert integrity(read_snapshot(store.path, cid))["verdict"] == "fail"


def test_malformed_child_order_does_not_become_complete_evidence(tmp_path):
    store, sid, cid, appointment = combined(tmp_path)
    with store.connect() as db:
        db.execute("UPDATE appointment_services SET position=3 WHERE appointment_id=? AND position=1", (appointment["id"],))
    with pytest.raises(EvidenceError, match="invalid_appointment_services"):
        read_snapshot(store.path, cid)


def test_service_only_change_reconciles_and_detects_tampered_change_receipt(tmp_path):
    store, sid, cid, appointment = combined(tmp_path)
    proposal = store.dispatch(sid, "prepare_reschedule", {"slot_id": appointment["slot_id"], "services": ["brakes"]}, "change")
    assert proposal["ok"]
    saved = store.dispatch(sid, "confirm_action", {"proposal_id": proposal["proposal_id"], "confirmed": True}, "change-confirm")
    assert saved["appointment"]["id"] == appointment["id"]
    snapshot = read_snapshot(store.path, cid)
    assert integrity(snapshot)["verdict"] == "pass"
    changed = next(p for p in snapshot["proposals"] if p["kind"] == "reschedule")
    changed["payload"]["services"] = ["diagnostic", "brakes"]
    assert integrity(snapshot)["verdict"] == "fail"


def test_cancellation_preserves_combined_scope_in_evidence(tmp_path):
    store, sid, cid, appointment = combined(tmp_path)
    proposal = store.dispatch(sid, "prepare_cancellation", {}, "cancel")
    result = store.dispatch(sid, "confirm_action", {"proposal_id": proposal["proposal_id"], "confirmed": True}, "cancel-confirm")
    assert result["appointment"]["status"] == "cancelled"
    snapshot = read_snapshot(store.path, cid)
    assert snapshot["appointments"][0]["services"] == ["diagnostic", "brakes"]
    assert integrity(snapshot)["verdict"] == "pass"


def test_legacy_database_without_child_table_still_reads(tmp_path):
    store = Store(tmp_path / "legacy.sqlite3")
    sid = store.create_session("voice")["id"]
    with store.connect() as db:
        db.execute("UPDATE sessions SET provider_call_id='legacy-call' WHERE id=?", (sid,))
        db.execute("DROP TABLE appointment_services")
    snapshot = read_snapshot(store.path, "legacy-call")
    assert integrity(snapshot)["verdict"] == "not_applicable"
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='appointment_services'").fetchone() is None


@pytest.mark.parametrize("mutation", ["slot", "reference", "status", "action"])
def test_consistent_but_wrong_reschedule_receipt_and_saved_row_fail(tmp_path, mutation):
    store, sid, cid, appointment = combined(tmp_path)
    proposal = store.dispatch(sid, "prepare_reschedule", {"slot_id": appointment["slot_id"], "services": ["brakes"]}, "change")
    store.dispatch(sid, "confirm_action", {"proposal_id": proposal["proposal_id"], "confirmed": True}, "change-confirm")
    snapshot = read_snapshot(store.path, cid)
    change = next(p for p in snapshot["proposals"] if p["kind"] == "reschedule")
    if mutation == "action":
        change["result"]["action"] = "book"
    else:
        field, value = {"slot": ("slot_id", "wrong-slot"), "reference": ("reference", "CSV-11111111"), "status": ("status", "cancelled")}[mutation]
        change["result"]["appointment"][field] = value
        snapshot["appointments"][0][field] = value
    assert integrity(snapshot)["verdict"] == "fail"


def test_cancellation_cannot_pass_if_receipt_and_row_still_confirmed(tmp_path):
    store, sid, cid, appointment = combined(tmp_path)
    proposal = store.dispatch(sid, "prepare_cancellation", {}, "cancel")
    store.dispatch(sid, "confirm_action", {"proposal_id": proposal["proposal_id"], "confirmed": True}, "cancel-confirm")
    snapshot = read_snapshot(store.path, cid)
    change = next(p for p in snapshot["proposals"] if p["kind"] == "cancel")
    change["result"]["appointment"]["status"] = "confirmed"
    snapshot["appointments"][0]["status"] = "confirmed"
    assert integrity(snapshot)["verdict"] == "fail"
