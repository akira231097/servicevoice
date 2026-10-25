"""Late provider events cannot resurrect a call or consume tool capacity."""
import asyncio
from copy import deepcopy
from uuid import uuid4
import httpx
import pytest
from app.main import create_public_app
from app.telephony_ingress import IngressError
from test_twilio_api import lane_app, Provider, ORG, PHONE


@pytest.mark.parametrize('event',[
    {'type':'transcript','transcriptType':'partial','role':'assistant','transcript':'Goodbye.'},
    {'type':'speech-update','status':'stopped','role':'assistant'},
    {'type':'user-interrupted'},
])
@pytest.mark.parametrize('fault',[None,'session','call','assistant','authorization'])
def test_authenticated_late_event_is_noop_only_for_exact_ended_binding(lane_app,event,fault):
    async def run():
        lane=lane_app.state.twilio_lane;lane.client=Provider()
        row=lane.store.allocate('CA'+'1'*32)
        call={'id':str(uuid4()),'type':'inboundPhoneCall','orgId':ORG,'phoneNumberId':PHONE,
            'sip':{'headers':{'x-sv_bridge':row['id'],'x-sv_proof':lane.store.proof(lane.config,row)}}}
        auth={'authorization':'Bearer '+'e'*32}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_public_app(lane_app)),base_url='https://test') as client:
            response=await client.post('/webhooks/vapi/inbound',headers=auth,json={'message':{'type':'assistant-request','call':call}})
            assert response.status_code==200
            call.update(response.json());sid=call['assistantOverrides']['variableValues']['session_id']
            await lane.finish(row['id'])
            assert lane.store.get(row['id'])['cleanup_verified']==1
            with pytest.raises(IngressError):lane_app.state.telephony_ingress.resolve(call)
            before=lane_app.state.store.snapshot(sid)
            lane_app.state.telephony_http_capacity=0
            delivered=deepcopy(call)
            if fault=='session':delivered['assistantOverrides']['variableValues']['session_id']='other-session'
            if fault=='call':delivered['id']=str(uuid4())
            if fault=='assistant':delivered['assistantId']=str(uuid4())
            if fault=='authorization':auth={'authorization':'Bearer '+'x'*32}
            for _ in range(2):
                response=await client.post('/webhooks/vapi/inbound',headers=auth,json={'message':{**event,'call':delivered}})
                assert response.status_code==(200 if fault is None else 401 if fault=='authorization' else 403)
            assert lane_app.state.store.snapshot(sid)==before
            assert call['id'] not in lane.bindings and call['id'] not in lane.coordinator.calls
            assert not lane_app.state.telephony_http_pending
        await lane.close()
    asyncio.run(run())
