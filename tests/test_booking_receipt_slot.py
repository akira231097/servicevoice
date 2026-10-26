"""Additive proposal selection metadata, never booking or consent authority."""
import asyncio
from copy import deepcopy
import json

import pytest

from app.tools import execute
from test_domain import store,call,search,prepare,confirm,active_count
from test_receipt_narration import EN,setup,issue,post,text_of


@pytest.mark.parametrize("first_tool",["prepare_first_available_booking","prepare_booking"])
def test_contact_only_replacement_uses_receipt_slot_without_new_availability(tmp_path,monkeypatch,first_tool):
    env=setup(tmp_path,monkeypatch)
    async def run():
        try:
            args={"service":"oil_change","customer_name":"Alex Morgan","phone":"5125500142","vehicle":"2021 Toyota Camry","presentation":EN}
            if first_tool=="prepare_booking":
                offers=await issue(env,"begin_service_request",{"service":"oil_change"})
                args["slot_id"]=offers["slots"][0]["id"]
            else:args["strategy"]="first_available"
            original=await issue(env,first_tool,args)
            with env.store.read_connection() as db:
                old=json.loads(db.execute("SELECT payload FROM proposals WHERE id=?",(original["proposal_id"],)).fetchone()[0])
            assert original["slot_id"]==old["slot_id"]
            assert "five five zero" in text_of(await post(env)) and not env.sent
            prior_event_count=len(env.store.snapshot(env.sid)["events"])
            # The direct correction path must not silently search again.
            monkeypatch.setattr(env.store,"availability_result",lambda *a,**k:pytest.fail("Contact correction must not search availability"))
            env.history.append({"role":"user","content":"Change only my callback to 5125550142. Read it back and ask me before booking."})
            correction={key:value for key,value in args.items() if key!="strategy"}
            correction.update(phone="5125550142",slot_id=original["slot_id"])
            replacement=await issue(env,"prepare_booking",correction)
            with env.store.read_connection() as db:
                rows={row['id']:dict(row) for row in db.execute("SELECT id,state,payload FROM proposals WHERE session_id=?",(env.sid,))}
                new=json.loads(rows[replacement['proposal_id']]['payload'])
                assert db.execute("SELECT COUNT(*) FROM appointments WHERE session_id=?",(env.sid,)).fetchone()[0]==0
            assert rows[original['proposal_id']]['state']=="superseded"
            assert rows[replacement['proposal_id']]['state']=="pending"
            assert replacement['slot_id']==original['slot_id']==new['slot_id']
            assert new['customer']['phone']=="+15125550142"
            expected=deepcopy(old);expected['customer']['phone']=new['customer']['phone'];expected['summary']=new['summary']
            assert new==expected
            events=env.store.snapshot(env.sid)['events']
            assert len(events)==prior_event_count+1 and events[-1]['name']=="prepare_booking"
            text=text_of(await post(env))
            assert "five one two, five five five, zero one four two" in text
            assert "confirm this booking?" in text and not env.sent
            # Replacement itself grants no assent; the old proposal cannot win.
            old_confirm=await env.harness.run(env.sid,"confirm_action",{"proposal_id":original['proposal_id'],"confirmed":True},"old-confirm")
            assert not old_confirm['ok']
            assert env.store.snapshot(env.sid)['appointment'] is None
        finally:await env.gateway.close()
    asyncio.run(run())


def test_replay_does_not_backfill_slot_into_historical_receipt(store):
    sid=store.create_session()['id'];slot=search(store,sid)[0]['id']
    args={"service":"oil_change","slot_id":slot,"customer_name":"Alex Morgan","phone":"5125550142","vehicle":"2021 Toyota Camry"}
    result=call(store,sid,"prepare_booking",args,"legacy-tool")
    # Seed the exact pre-upgrade result shape in this disposable fixture.
    legacy={key:value for key,value in result.items() if key!='slot_id'}
    with store.connect() as db:db.execute("UPDATE tool_results SET result=? WHERE session_id=? AND call_id=?",(json.dumps(legacy),sid,"legacy-tool"))
    assert call(store,sid,"prepare_booking",args,"legacy-tool")==legacy
    fresh=call(store,sid,"prepare_booking",args,"new-tool")
    assert fresh['slot_id']==slot and fresh['proposal_id']!=legacy['proposal_id']
    assert call(store,sid,"prepare_booking",args,"legacy-tool")==legacy
    assert active_count(store)==0


def test_returned_slot_is_not_cross_session_authority(store):
    owner=store.create_session()['id'];first=prepare(store,owner,search(store,owner)[0]['id'])
    other=store.create_session()['id']
    result=call(store,other,"prepare_booking",{"service":"oil_change","slot_id":first['slot_id'],"customer_name":"Other Caller","phone":"5125550155","vehicle":"2022 Toyota Camry"})
    assert not result['ok'] and result['error']=='slot_not_offered'
    assert active_count(store)==0


def test_contact_replacement_rechecks_capacity_even_with_known_receipt_slot(store):
    first_sid=store.create_session()['id'];slot=search(store,first_sid)[0]['id']
    original=prepare(store,first_sid,slot)
    other=store.create_session()['id'];search(store,other)
    competitor=prepare(store,other,slot)
    assert confirm(store,other,competitor)['ok']
    attempted=call(store,first_sid,"prepare_booking",{"service":"oil_change","slot_id":original['slot_id'],"customer_name":"Alex Morgan","phone":"5125550199","vehicle":"2021 Toyota Camry"})
    assert not attempted['ok'] and attempted['error']=='slot_unavailable'
    with store.read_connection() as db:
        original_row=db.execute("SELECT state,payload FROM proposals WHERE id=?",(original['proposal_id'],)).fetchone()
        assert original_row['state']=='pending'
        assert json.loads(original_row['payload'])['customer']['phone']=='+15125550142'
    assert active_count(store)==1
