"""One supported intake visit may durably cover several requested services."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import hashlib
import json
from threading import Barrier

import pytest

from app import domain
from app.domain import DomainError,Store,normalize_services
from app.tools import DEFINITIONS,TOOL_SCHEMAS,execute


PAIR=["diagnostic","brakes"]
CUSTOMER={"customer_name":"Alex Morgan","phone":"5125550142","vehicle":"2022 Toyota Camry"}


@pytest.fixture
def store(tmp_path,monkeypatch):
    monkeypatch.setattr(domain,"now",lambda:datetime(2026,10,17,18,tzinfo=timezone.utc))
    return Store(tmp_path/"combined.sqlite3")


def call(store,sid,name,args=None,identity=None):
    return execute(store,sid,name,args or {},identity or domain.uid("combined-test"))


def search(store,sid,services=PAIR):
    result=call(store,sid,"search_availability",{"service":services[0],"services":services})
    assert result["ok"],result
    return result


def proposal(store,sid,services=PAIR,slot_id=None,**customer):
    slot_id=slot_id or search(store,sid,services)["slots"][0]["id"]
    result=call(store,sid,"prepare_booking",{"service":services[0],"services":services,
        "slot_id":slot_id,**CUSTOMER,**customer})
    assert result["ok"],result
    return result


def confirm(store,sid,prepared,identity=None):
    return call(store,sid,"confirm_action",{"proposal_id":prepared["proposal_id"],"confirmed":True},identity)


def book(store,services=PAIR):
    sid=store.create_session()["id"]
    prepared=proposal(store,sid,services)
    committed=confirm(store,sid,prepared)
    assert committed["ok"],committed
    return sid,prepared,committed


def rows(store,table):
    with store.connect() as db:return [dict(row) for row in db.execute("SELECT * FROM "+table)]


def test_combined_readback_and_receipt_are_one_capacity_block_one_appointment(store):
    sid=store.create_session()["id"]
    availability=search(store,sid)
    assert availability["query"]["services"]==PAIR
    assert availability["query"]["intake_duration_minutes"]==60
    prepared=proposal(store,sid,slot_id=availability["slots"][0]["id"])
    assert prepared["services"]==PAIR and prepared["intake_duration_minutes"]==60
    assert "Diagnostic appointment" in prepared["summary"] and "Brake inspection" in prepared["summary"]
    assert "One 60-minute intake visit" in prepared["summary"] and "Repair completion is not promised" in prepared["summary"]
    assert rows(store,"appointments")==[] and rows(store,"appointment_services")==[]
    saved=confirm(store,sid,prepared)["appointment"]
    assert saved["services"]==PAIR and saved["service_names"]==["Diagnostic appointment","Brake inspection"]
    assert saved["intake_duration_minutes"]==60 and saved["service"]==PAIR[0]
    assert len(rows(store,"appointments"))==1
    assert [row["service"] for row in rows(store,"appointment_services")]==PAIR
    stranger=store.create_session()["id"]
    assert saved["slot_id"] not in [slot["id"] for slot in search(store,stranger)["slots"]]


def test_unrestricted_first_available_prepares_complete_scope_without_booking(store):
    sid=store.create_session()["id"]
    prepared=call(store,sid,"prepare_first_available_booking",{"strategy":"first_available",
        "service":PAIR[0],"services":PAIR,**CUSTOMER})
    assert prepared["ok"] and prepared["services"]==PAIR
    assert rows(store,"appointments")==[]
    assert confirm(store,sid,prepared)["appointment"]["services"]==PAIR


def test_all_four_supported_requests_share_only_one_intake_slot(store):
    selected=list(domain.SERVICES)
    _,_,result=book(store,selected)
    assert result["appointment"]["services"]==selected
    assert result["appointment"]["intake_duration_minutes"]==60
    assert len(rows(store,"appointments"))==1 and len(rows(store,"appointment_services"))==4


def test_explicit_decline_never_commits_combined_proposal(store):
    sid=store.create_session()["id"];prepared=proposal(store,sid)
    result=call(store,sid,"confirm_action",{"proposal_id":prepared["proposal_id"],"confirmed":False})
    assert result["error"]=="confirmation_required"
    assert rows(store,"appointments")==[] and rows(store,"appointment_services")==[]
    assert call(store,sid,"dismiss_proposal",{"proposal_id":prepared["proposal_id"]})["ok"]
    assert confirm(store,sid,prepared)["error"]=="stale_proposal"


def test_service_correction_replaces_complete_proposal_and_requires_fresh_confirmation(store):
    sid=store.create_session()["id"];old=proposal(store,sid,["diagnostic"])
    availability=search(store,sid,PAIR)
    updated=proposal(store,sid,PAIR,availability["slots"][0]["id"])
    assert confirm(store,sid,old)["error"]=="stale_proposal"
    assert rows(store,"appointments")==[]
    assert confirm(store,sid,updated)["appointment"]["services"]==PAIR


def test_contact_correction_preserves_complete_service_scope(store):
    sid=store.create_session()["id"];old=proposal(store,sid)
    updated=proposal(store,sid,slot_id=old["slot_id"],vehicle="2023 Toyota Camry")
    assert updated["services"]==PAIR and "2023 Toyota Camry" in updated["summary"]
    assert confirm(store,sid,old)["error"]=="stale_proposal"
    assert confirm(store,sid,updated)["appointment"]["vehicle"]=="2023 Toyota Camry"


def test_prepare_cannot_silently_drop_a_searched_service(store):
    sid=store.create_session()["id"];availability=search(store,sid)
    result=call(store,sid,"prepare_booking",{"service":"diagnostic",
        "slot_id":availability["slots"][0]["id"],**CUSTOMER})
    assert result["error"]=="service_changed" and rows(store,"appointments")==[]


def test_scope_correction_search_required_error_also_invalidates_old_readback(store):
    sid=store.create_session()["id"];old=proposal(store,sid,["diagnostic"])
    changed=call(store,sid,"prepare_booking",{"service":"diagnostic","services":PAIR,
        "slot_id":old["slot_id"],**CUSTOMER})
    assert changed["error"]=="service_changed"
    assert store.snapshot(sid)["proposal"] is None
    assert confirm(store,sid,old)["error"]=="stale_proposal"
    assert rows(store,"appointments")==[]
    new=proposal(store,sid)
    assert rows(store,"appointments")==[]
    assert confirm(store,sid,new)["appointment"]["services"]==PAIR


def test_malformed_scope_does_not_mutate_old_proposal(store):
    sid=store.create_session()["id"];old=proposal(store,sid,["diagnostic"])
    changed=call(store,sid,"prepare_booking",{"service":"diagnostic","services":[],
        "slot_id":old["slot_id"],**CUSTOMER})
    assert changed["error"]=="invalid_arguments"
    assert store.snapshot(sid)["proposal"]["id"]==old["proposal_id"]
    assert rows(store,"appointments")==[]


def test_service_only_correction_keeps_one_appointment_reference_and_time(store):
    sid,_,original=book(store,["diagnostic"]);saved=original["appointment"]
    prepared=call(store,sid,"prepare_reschedule",{"slot_id":saved["slot_id"],"services":PAIR})
    assert prepared["ok"] and all(name in prepared["summary"] for name in ["Diagnostic appointment","Brake inspection"])
    assert call(store,sid,"get_appointment")["appointment"]==saved
    updated=confirm(store,sid,prepared)["appointment"]
    assert updated["services"]==PAIR
    assert all(updated[key]==saved[key] for key in ["id","reference","slot_id","start"])
    assert len(rows(store,"appointments"))==1
    context=call(store,sid,"get_session_context")
    assert context["services"]==PAIR and context["appointment"]["services"]==PAIR


def test_removing_service_is_a_fresh_scope_change_and_single_receipt_shape_compatible(store):
    sid,_,original=book(store);saved=original["appointment"]
    prepared=call(store,sid,"prepare_reschedule",{"slot_id":saved["slot_id"],"services":["brakes"]})
    assert prepared["ok"]
    assert call(store,sid,"get_appointment")["appointment"]["services"]==PAIR
    current=confirm(store,sid,prepared)["appointment"]
    assert current["service"]=="brakes" and "services" not in current
    assert [row["service"] for row in rows(store,"appointment_services")]==["brakes"]
    assert current["reference"]==saved["reference"] and len(rows(store,"appointments"))==1


def test_declining_service_only_change_keeps_original_scope(store):
    sid,_,original=book(store,["diagnostic"]);saved=original["appointment"]
    prepared=call(store,sid,"prepare_reschedule",{"slot_id":saved["slot_id"],"services":PAIR})
    assert call(store,sid,"dismiss_proposal",{"proposal_id":prepared["proposal_id"]})["ok"]
    assert confirm(store,sid,prepared)["error"]=="stale_proposal"
    assert call(store,sid,"get_appointment")["appointment"]==saved


def test_plain_time_reschedule_preserves_every_service_and_appointment_identity(store):
    sid,_,original=book(store);saved=original["appointment"]
    target=search(store,sid)["slots"][0]["id"]
    prepared=call(store,sid,"prepare_reschedule",{"slot_id":target})
    assert prepared["services"]==PAIR and "Diagnostic appointment" in prepared["summary"] and "Brake inspection" in prepared["summary"]
    moved=confirm(store,sid,prepared)["appointment"]
    assert moved["services"]==PAIR and moved["reference"]==saved["reference"] and moved["id"]==saved["id"]
    assert moved["slot_id"]==target and len(rows(store,"appointments"))==1
    other=store.create_session()["id"]
    assert saved["slot_id"] in [slot["id"] for slot in search(store,other)["slots"]]


def test_cancellation_mentions_and_cancels_whole_visit_releasing_one_block(store):
    sid,_,original=book(store);saved=original["appointment"]
    prepared=call(store,sid,"prepare_cancellation")
    assert prepared["services"]==PAIR
    assert all(name in prepared["summary"] for name in ["Diagnostic appointment","Brake inspection"])
    result=confirm(store,sid,prepared)
    assert result["appointment"]["status"]=="cancelled" and result["appointment"]["services"]==PAIR
    assert confirm(store,sid,prepared)==result
    another=store.create_session()["id"]
    assert saved["slot_id"] in [slot["id"] for slot in search(store,another)["slots"]]
    assert len(rows(store,"appointments"))==1 and len(rows(store,"appointment_services"))==2


def test_competing_combined_visits_cannot_double_book_one_capacity_block(store):
    sessions=[store.create_session()["id"] for _ in range(2)]
    slot=search(store,sessions[0])["slots"][0]["id"]
    search(store,sessions[1])
    prepared=[proposal(store,sid,slot_id=slot) for sid in sessions]
    barrier=Barrier(2)
    def race(index):
        barrier.wait(timeout=5)
        return confirm(store,sessions[index],prepared[index])
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(race,range(2)))
    assert sum(item["ok"] for item in results)==1
    assert next(item for item in results if not item["ok"])["error"]=="slot_unavailable"
    assert len(rows(store,"appointments"))==1 and len(rows(store,"appointment_services"))==2


def test_same_tool_and_new_tool_replays_cannot_duplicate_combined_appointment(store):
    sid=store.create_session()["id"];prepared=proposal(store,sid)
    original=confirm(store,sid,prepared,"combined-commit")
    assert confirm(store,sid,prepared,"combined-commit")==original
    assert confirm(store,sid,prepared,"combined-reconcile")==original
    assert len(rows(store,"appointments"))==1 and len(rows(store,"appointment_services"))==2


def test_same_tool_id_changed_service_scope_conflicts(store):
    sid=store.create_session()["id"]
    original=call(store,sid,"search_availability",{"service":"diagnostic","services":PAIR},"fixed-service-id")
    changed=call(store,sid,"search_availability",{"service":"diagnostic","services":["diagnostic","tires"]},"fixed-service-id")
    assert original["ok"] and changed["error"]=="idempotency_conflict"
    assert store.state(sid)["services"]==PAIR


def test_same_time_and_unchanged_scope_is_noop_not_another_proposal(store):
    sid,_,original=book(store)
    changed=call(store,sid,"prepare_reschedule",{"slot_id":original["appointment"]["slot_id"],"services":PAIR})
    assert changed["error"]=="same_slot"
    assert call(store,sid,"get_appointment")["appointment"]==original["appointment"]
    assert store.snapshot(sid)["proposal"] is None


def test_old_committed_receipt_is_historical_after_service_scope_correction(store):
    sid=store.create_session()["id"];prepared=proposal(store,sid,["diagnostic"])
    original=confirm(store,sid,prepared,"original-scope")
    changed=call(store,sid,"prepare_reschedule",{"slot_id":original["appointment"]["slot_id"],"services":PAIR})
    current=confirm(store,sid,changed)["appointment"]
    stale=confirm(store,sid,prepared,"new-id-old-scope")
    assert stale["error"]=="historical_proposal" and stale["current_appointment"]==current
    assert confirm(store,sid,prepared,"original-scope")==original


def test_child_manifest_failure_rolls_back_primary_appointment_and_proposal(store,monkeypatch):
    sid=store.create_session()["id"];prepared=proposal(store,sid)
    actual=store._save_services
    def partial(db,aid,selected):
        actual(db,aid,selected[:1])
        raise DomainError("synthetic_manifest_failure","Synthetic fault after the first manifest row.")
    monkeypatch.setattr(store,"_save_services",partial)
    failed=confirm(store,sid,prepared)
    assert failed["error"]=="synthetic_manifest_failure"
    assert rows(store,"appointments")==[] and rows(store,"appointment_services")==[]
    assert store.snapshot(sid)["proposal"]["id"]==prepared["proposal_id"]


def test_child_manifest_failure_during_service_change_preserves_original(store,monkeypatch):
    sid,_,original=book(store,["diagnostic"]);saved=original["appointment"]
    prepared=call(store,sid,"prepare_reschedule",{"slot_id":saved["slot_id"],"services":PAIR})
    actual=store._save_services
    def partial(db,aid,selected):
        actual(db,aid,selected)
        raise DomainError("synthetic_manifest_failure","Synthetic fault after service replacement.")
    monkeypatch.setattr(store,"_save_services",partial)
    assert confirm(store,sid,prepared)["error"]=="synthetic_manifest_failure"
    assert call(store,sid,"get_appointment")["appointment"]==saved
    assert [row["service"] for row in rows(store,"appointment_services")]==["diagnostic"]


def test_foreign_session_cannot_change_or_cancel_another_combined_visit(store):
    owner,_,original=book(store);stranger=store.create_session()["id"]
    assert call(store,stranger,"get_appointment")["appointment"] is None
    assert call(store,stranger,"prepare_reschedule",{"slot_id":original["appointment"]["slot_id"],"services":["tires"]})["error"]=="no_appointment"
    assert call(store,stranger,"prepare_cancellation")["error"]=="no_appointment"
    assert call(store,owner,"get_appointment")["appointment"]==original["appointment"]


@pytest.mark.parametrize("services",[[],["diagnostic","diagnostic"],["brakes","diagnostic"],["diagnostic","unsupported"],["diagnostic",1],"diagnostic",["diagnostic"]*5])
def test_invalid_or_inconsistent_scope_rejected_without_proposal_or_booking(store,services):
    sid=store.create_session()["id"]
    result=call(store,sid,"search_availability",{"service":"diagnostic","services":services})
    assert result["error"]=="invalid_arguments"
    assert rows(store,"appointments")==[] and rows(store,"proposals")==[]


@pytest.mark.parametrize("name,args",[
    ("search_availability",{"service":"brakes","date":None}),
    ("prepare_booking",{"service":"brakes","slot_id":"slot-synthetic",**CUSTOMER}),
    ("prepare_first_available_booking",{"strategy":"first_available","service":"brakes",**CUSTOMER}),
    ("prepare_reschedule",{"slot_id":"slot-synthetic"}),
])
def test_legacy_omitted_or_null_services_normalizes_to_identical_arguments_and_fingerprint(name,args):
    original=DEFINITIONS[name][0].model_validate(args).model_dump()
    with_null=DEFINITIONS[name][0].model_validate({**args,"services":None}).model_dump()
    assert original==args and with_null==args
    canonical=lambda value:hashlib.sha256(json.dumps({"name":name,"args":value},sort_keys=True).encode()).hexdigest()
    assert canonical(with_null)==canonical(args)


def test_model_schema_requires_explicit_nullable_service_scope_but_backend_accepts_legacy():
    for tool in TOOL_SCHEMAS:
        if "services" not in tool["function"]["parameters"].get("properties",{}):continue
        assert "services" in tool["function"]["parameters"]["required"]


def test_old_single_service_appointment_without_child_rows_keeps_receipt_shape(store):
    sid,prepared,original=book(store,["diagnostic"])
    expected_keys={"id","reference","service","service_name","start","label","status","slot_id","customer_name","vehicle"}
    assert set(original["appointment"])==expected_keys
    with store.connect() as db:db.execute("DELETE FROM appointment_services WHERE appointment_id=?",(original["appointment"]["id"],))
    assert call(store,sid,"get_appointment")["appointment"]==original["appointment"]
    assert confirm(store,sid,prepared)==original


def test_normalization_never_mutates_or_silently_sorts_requested_scope():
    requested=PAIR.copy()
    assert normalize_services("diagnostic",requested)==PAIR
    assert requested==PAIR and normalize_services("brakes",["brakes","diagnostic"])==["brakes","diagnostic"]
    with pytest.raises(DomainError):normalize_services(None,PAIR)
