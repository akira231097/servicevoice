"""Actual SQLite regressions for partial offers and caller queue withdrawal."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from threading import Barrier

import pytest

from app import domain
from app.advisor_queue import AdvisorQueue
from app.domain import DomainError, Store
from app.harness import ToolHarness
from app.tools import DEFINITIONS, execute
from scripts.database_backup import copy_database

PRESENTATION={"response_scope":"receipt_only","response_language":"en","caller_timezone":"dealership"}
DAY="2026-10-22"

@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(domain,"now",lambda:datetime(2026,10,18,18,tzinfo=timezone.utc))
    return Store(tmp_path/"test.sqlite3")

def call(store,sid,name,args=None,key=None):
    return execute(store,sid,name,args or {},key or domain.uid("test"))

def session(store):
    return store.create_session()["id"]

def search(store,sid,**changes):
    return call(store,sid,"search_availability",{"service":"brakes","date":DAY,**changes})

def prepare(store,sid,slot):
    return call(store,sid,"prepare_booking",{"service":"brakes","slot_id":slot,"customer_name":"Synthetic Alex","phone":"5125550142","vehicle":"2021 Toyota Camry"})

def handoff(store,sid):
    result=call(store,sid,"request_handoff",{"reason":"Synthetic warranty question"},"handoff-create")
    assert result["ok"]
    return result

def test_partial_offers_do_not_hide_an_exact_afternoon_match_or_reserve_it(store):
    sid=session(store)
    partial=search(store,sid)
    assert [s["id"] for s in partial["slots"]]==[f"slot_{DAY}_{h}" for h in (9,10,11)]
    assert (partial["total_matching"],partial["returned_count"],partial["truncated"],partial["complete"])==(6,3,True,False)
    exact=search(store,sid,time_filter={"exact_time":"14:00"},presentation=PRESENTATION)
    assert [s["id"] for s in exact["slots"]]==[f"slot_{DAY}_14"]
    assert (exact["total_matching"],exact["returned_count"],exact["truncated"],exact["complete"])==(1,1,False,True)
    assert exact["presentation"]==PRESENTATION
    assert exact["query"]["timezone"]=="America/Chicago"
    assert store.state(sid)["availability_query"]==exact["query"]
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM appointments").fetchone()[0]==0
        assert db.execute("SELECT count(*) FROM proposals").fetchone()[0]==0
    proposal=prepare(store,sid,exact["slots"][0]["id"])
    assert proposal["ok"]
    assert call(store,sid,"confirm_action",{"proposal_id":proposal["proposal_id"],"confirmed":False})["error"]=="confirmation_required"

@pytest.mark.parametrize("time_filter,hours",[
    ({"start_time":"14:00","start_inclusive":False},[15]),
    ({"start_time":"14:00"},[14,15]),
    ({"end_time":"10:00","end_inclusive":False},[9]),
    ({"end_time":"10:00"},[9,10]),
    ({"start_time":"09:00","end_time":"14:00","start_inclusive":False,"end_inclusive":False},[10,11,13]),
    ({"start_time":"14:00","end_time":"14:00"},[14]),
])
def test_range_boundaries_filter_before_limit(store,time_filter,hours):
    result=search(store,session(store),time_filter=time_filter)
    assert result["ok"] and result["complete"]
    assert [s["id"] for s in result["slots"]]==[f"slot_{DAY}_{h}" for h in hours]

def test_next_exact_time_and_fused_context_share_complete_query_contract(store):
    sid=session(store)
    result=call(store,sid,"begin_service_request",{"service":"brakes","time_filter":{"exact_time":"14:00"}})
    assert result["ok"] and result["truncated"] and result["total_matching"]>3
    assert result["slots"][0]["start"]=="2026-10-19T14:00:00-05:00"
    assert all(s["start"][11:16]=="14:00" for s in result["slots"])
    assert result["query"]["date"] is None
    assert result["query"]["horizon_end"]=="2026-11-08"
    assert result["context"]["timezone"]=="America/Chicago"

def test_exact_zero_only_describes_queried_time_and_capacity_is_live(store):
    sid=session(store)
    missing=search(store,sid,time_filter={"exact_time":"12:00"})
    assert missing["slots"]==[] and missing["complete"] and not missing["truncated"]
    assert missing["total_matching"]==0 and missing["query"]["time_filter"]["exact_time"]=="12:00"
    available=search(store,sid,time_filter={"exact_time":"14:00"})
    proposal=prepare(store,sid,available["slots"][0]["id"])
    assert call(store,sid,"confirm_action",{"proposal_id":proposal["proposal_id"],"confirmed":True})["ok"]
    occupied=search(store,session(store),time_filter={"exact_time":"14:00"})
    assert occupied["slots"]==[] and occupied["complete"] and occupied["total_matching"]==0

@pytest.mark.parametrize("time_filter",[
    {},{"exact_time":"24:00"},{"exact_time":"2 PM"},{"exact_time":1400},
    {"exact_time":"14:00","start_time":"09:00"},
    {"start_time":"15:00","end_time":"09:00"},
    {"start_time":"14:00","end_time":"14:00","start_inclusive":False},
    {"start_time":"14:00","start_inclusive":"false"},
    {"start_time":"14:00","end_inclusive":False},
    {"exact_time":"14:00","timezone":"America/Los_Angeles"},
])
def test_invalid_filter_does_not_supersede_or_mutate_existing_proposal(store,time_filter):
    sid=session(store)
    proposal=prepare(store,sid,search(store,sid)["slots"][0]["id"])
    state=store.state(sid)
    result=search(store,sid,time_filter=time_filter)
    assert result["error"]=="invalid_arguments"
    assert store.state(sid)==state
    assert store.snapshot(sid)["proposal"]["id"]==proposal["proposal_id"]

def test_new_constrained_search_supersedes_old_proposal_without_booking(store):
    sid=session(store)
    old=prepare(store,sid,search(store,sid)["slots"][0]["id"])
    search(store,sid,time_filter={"exact_time":"14:00"})
    assert call(store,sid,"confirm_action",{"proposal_id":old["proposal_id"],"confirmed":True})["error"]=="stale_proposal"
    assert store.snapshot(sid)["appointment"] is None

@pytest.mark.parametrize("day",["20261022","2026-W43-4","2026-10-32"])
def test_noncanonical_or_invalid_dates_do_not_become_negative_availability(store,day):
    result=search(store,session(store),date=day,time_filter={"exact_time":"14:00"})
    assert result["ok"] is False and result["error"] in ("invalid_arguments","invalid_date")

@pytest.mark.parametrize("name",["begin_service_request","search_availability"])
def test_legacy_search_argument_fingerprint_and_cached_receipt_preserved(store,name):
    sid=session(store)
    legacy_args={"service":"brakes","date":None}
    assert DEFINITIONS[name][0].model_validate({"service":"brakes"}).model_dump()==legacy_args
    assert DEFINITIONS[name][0].model_validate({"service":"brakes","presentation":None,"time_filter":None}).model_dump()==legacy_args
    legacy_result={"ok":True,"slots":[],"timezone":"America/Chicago","message":"Historical fixture"}
    fingerprint=hashlib.sha256(json.dumps({"name":name,"args":legacy_args},sort_keys=True).encode()).hexdigest()
    with store.connect() as db:
        db.execute("INSERT INTO tool_results VALUES (?,?,?,?)",(sid,"old-search",fingerprint,json.dumps(legacy_result)))
    assert call(store,sid,name,{"service":"brakes"},"old-search")==legacy_result
    assert call(store,sid,name,{"service":"brakes","time_filter":{"exact_time":"14:00"}},"old-search")["error"]=="idempotency_conflict"

def test_withdrawal_is_saved_audited_session_scoped_and_original_receipt_stays_immutable(store):
    sid=session(store); saved=handoff(store,sid); stranger=session(store)
    assert call(store,stranger,"withdraw_handoff",{"handoff_id":saved["id"]})["error"]=="handoff_missing"
    result=call(store,sid,"withdraw_handoff",{"handoff_id":saved["id"],"presentation":PRESENTATION},"withdraw")
    assert result["ok"] and result["status"]=="withdrawn" and result["presentation"]==PRESENTATION
    assert result["reason"]==saved["reason"] and result["created"]==saved["created"]
    assert call(store,sid,"withdraw_handoff",{"handoff_id":saved["id"],"presentation":PRESENTATION},"withdraw")==result
    assert call(store,sid,"request_handoff",{"reason":"Synthetic warranty question"},"handoff-create")==saved
    context=call(store,sid,"get_session_context")
    assert context["handoff"]["status"]=="withdrawn"
    assert store.snapshot(sid)["handoff"]["status"]=="withdrawn"
    assert len([e for e in store.snapshot(sid)["events"] if e["name"]=="withdraw_handoff"])==1
    assert call(store,sid,"request_handoff",{"reason":"New request"})["error"]=="handoff_withdrawn"
    assert context["appointment"] is None

def test_withdrawal_closes_claimed_operator_case_and_cannot_be_reopened(store):
    queue=AdvisorQueue(store);sid=session(store);saved=handoff(store,sid)
    queue.update_case(saved["id"],0,"in_progress","Synthetic operator","Reviewing")
    call(store,sid,"withdraw_handoff",{"handoff_id":saved["id"]})
    assert queue.list_cases("open")["items"]==[]
    assert queue.list_cases("in_progress")["items"]==[]
    case=queue.get_case(saved["id"])
    assert case["status"]=="resolved" and case["handoff_status"]=="withdrawn"
    assert case["audit"][-1]["to_status"]=="in_progress"  # historical operator audit not rewritten
    with pytest.raises(DomainError,match="cannot be reopened"):
        queue.update_case(saved["id"],case["revision"],"open","Synthetic operator","Reopen")

@pytest.mark.parametrize("book_first",[False,True])
def test_withdrawal_never_changes_pending_proposal_or_saved_appointment(store,book_first):
    sid=session(store);saved=handoff(store,sid)
    proposal=prepare(store,sid,search(store,sid)["slots"][0]["id"])
    if book_first:
        assert call(store,sid,"confirm_action",{"proposal_id":proposal["proposal_id"],"confirmed":True})["ok"]
    before=store.snapshot(sid)
    assert call(store,sid,"withdraw_handoff",{"handoff_id":saved["id"]})["ok"]
    after=store.snapshot(sid)
    assert before["proposal"]==after["proposal"] and before["appointment"]==after["appointment"]
    assert before["stage"]==after["stage"]

def test_withdrawal_domain_error_rolls_back_effect_but_records_failure(store,monkeypatch):
    sid=session(store);saved=handoff(store,sid);original=store.update
    def fail_after_update(*args,**kwargs):
        original(*args,**kwargs)
        raise DomainError("injected_failure","Synthetic failure after business write")
    monkeypatch.setattr(store,"update",fail_after_update)
    result=call(store,sid,"withdraw_handoff",{"handoff_id":saved["id"]},"failing-withdraw")
    assert result["error"]=="injected_failure"
    state=store.snapshot(sid)
    assert state["handoff"]["status"]=="callback_requested" and state["stage"]=="handoff"
    assert state["events"][-1]["status"]=="error"

def test_concurrent_same_id_withdrawal_has_one_durable_effect_and_event(store):
    sid=session(store);saved=handoff(store,sid);barrier=Barrier(2)
    def withdraw(_):
        barrier.wait(timeout=3)
        return call(store,sid,"withdraw_handoff",{"handoff_id":saved["id"]},"same-withdraw")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(withdraw,range(2)))
    assert results[0]==results[1] and results[0]["status"]=="withdrawn"
    assert len([e for e in store.snapshot(sid)["events"] if e["name"]=="withdraw_handoff"])==1

def test_operator_claim_racing_withdrawal_cannot_leave_an_open_case(store):
    queue=AdvisorQueue(store);sid=session(store);saved=handoff(store,sid);barrier=Barrier(2)
    def withdraw():
        barrier.wait(timeout=3)
        return call(store,sid,"withdraw_handoff",{"handoff_id":saved["id"]})
    def claim():
        barrier.wait(timeout=3)
        try:return queue.update_case(saved["id"],0,"in_progress","Synthetic operator","Reviewing")
        except DomainError as exc:return {"error":exc.code}
    with ThreadPoolExecutor(max_workers=2) as pool:
        w,c=pool.submit(withdraw),pool.submit(claim)
        assert w.result()["ok"]
        outcome=c.result()
    assert outcome.get("error") in (None,"advisor_case_withdrawn")
    assert queue.get_case(saved["id"])["status"]=="resolved"
    assert queue.list_cases("open")["items"]==queue.list_cases("in_progress")["items"]==[]

def test_withdrawn_state_and_both_historical_receipts_survive_verified_restore(store,tmp_path):
    async def fixture():
        harness=ToolHarness(store);sid=session(store)
        created=await harness.run(sid,"request_handoff",{"reason":"Synthetic question"},"request")
        result=await harness.run(sid,"withdraw_handoff",{"handoff_id":created["id"]},"withdraw")
        await harness.close()
        return sid,created,result
    sid,created,withdrawn=asyncio.run(fixture())
    restored=tmp_path/"restored.sqlite3"
    assert copy_database(store.path,restored,operation="restore")["snapshot_content_match"]
    class Existing(Store):
        def __init__(self,path):self.path=str(path)
    recovered=Existing(restored)
    async def replay():
        harness=ToolHarness(recovered)
        old=await harness.run(sid,"request_handoff",{"reason":"Synthetic question"},"request")
        latest=await harness.run(sid,"withdraw_handoff",{"handoff_id":created["id"]},"withdraw")
        await harness.close()
        return old,latest
    assert asyncio.run(replay())==(created,withdrawn)
    assert recovered.snapshot(sid)["handoff"]["status"]=="withdrawn"
