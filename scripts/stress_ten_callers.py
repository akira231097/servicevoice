"""Reproducible OFFLINE four- or ten-caller admission/gateway/tool stress.

Always uses temporary databases, synthetic credentials and HTTPX fake upstream
responses. No microphone, carrier, real model, runtime database or paid API.
Usage: python scripts/stress_ten_callers.py --callers 4 --output NEW_REPORT.json [--rounds 3]
"""
from __future__ import annotations
import argparse
import asyncio
from collections import Counter
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import dotenv
import httpx
from app.gemini_gateway import MODEL,sse
from app.tools import TOOL_SCHEMAS

ORG='11111111-1111-4111-8111-111111111111'
PHONE='22222222-2222-4222-8222-222222222222'
ASSISTANT='33333333-3333-4333-8333-333333333333'
BROWSER='44444444-4444-4444-8444-444444444444'
PHONE_SECRET='synthetic-phone-only-'+('p'*32)
GATEWAY_SECRET='synthetic-gateway-only-'+('g'*32)
SOURCES=('app/capacity.py','app/main.py','app/domain.py','app/harness.py','app/gemini_gateway.py',
         'app/telephony_ingress.py','app/jev_faq.py','scripts/stress_ten_callers.py')


def hashes():return {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in SOURCES}
def frame(delta=None,reason=None):return sse({'model':MODEL,'choices':[{'index':0,'delta':delta or {},'finish_reason':reason}]})
def parsed_events(response):return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: {')]
async def reconcile_terminal_events(inbound, calls):
    """Exercise bounded retries of identical, idempotent terminal events.

    Keep first-attempt rejections visible. Only deadline/busy responses are
    retried; authentication, validation and capacity failures remain failures.
    This helper never replays booking tools or changes production deadlines.
    """
    messages=[{'type':'status-update','status':'ended','call':call} for call in calls]
    results=await asyncio.gather(*(inbound(message) for message in messages))
    attempts=[dict(Counter(str(result.status_code) for result in results))]
    for _ in range(2):
        retry=[i for i,result in enumerate(results) if result.status_code in (503,504)]
        if not retry:break
        await asyncio.sleep(0.05)
        responses=await asyncio.gather(*(inbound(messages[i]) for i in retry))
        attempts.append(dict(Counter(str(result.status_code) for result in responses)))
        for i,result in zip(retry,responses):results[i]=result
    return results,attempts


def check(value,message):
    if not value:raise AssertionError(message)


async def exercise(directory,progress=None,*,callers=4):
    if type(callers) is not int or callers not in (4,10):raise ValueError('Callers must be4 or10.')
    progress=progress if progress is not None else {}
    progress.update(passed=False,last_phase='setup')
    env={'SERVICEVOICE_DB':str(directory/'import.sqlite3'),'SERVICEVOICE_CAPACITY_PROFILE':'demo10' if callers==10 else 'default',
         'SERVICEVOICE_TELEPHONY_ENABLED':'on','VAPI_PHONE_ORG_ID':ORG,'VAPI_PHONE_NUMBER_ID':PHONE,
         'VAPI_PHONE_ASSISTANT_ID':ASSISTANT,'VAPI_PHONE_WEBHOOK_SECRET':PHONE_SECRET,
         'VAPI_ASSISTANT_ID':BROWSER,'VAPI_MODEL_PROFILE':'gemini-3.8-flash-custom',
         'VAPI_WEBHOOK_SECRET':'synthetic-browser-only-'+('b'*32),'GEMINI_API_KEY':'synthetic-not-real',
         'GEMINI_GATEWAY_SECRET':GATEWAY_SECRET,'SERVICEVOICE_RECEIPT_NARRATION':'off',
         'SERVICEVOICE_JEV_FAQ':'off','SERVICEVOICE_LANGSMITH_ENABLED':'off'}
    with patch.dict(os.environ,env,clear=True),patch.object(dotenv,'load_dotenv',return_value=False),patch.object(dotenv,'dotenv_values',return_value={}):
        module=importlib.import_module('app.main')
        app=module.create_app(directory/'exercise.sqlite3');public=module.create_public_app(app)
        gateway=app.state.gemini_gateway
        await gateway.client.aclose()
        active=0;peak=0;entered=0;barrier=asyncio.Event();hold_entered=0;hold_ready=asyncio.Event();closed=0
        requests=Counter();stages={};checks={}

        class HeldStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                nonlocal hold_entered
                yield frame({'content':'Obsolete buffered prose must never escape'})
                hold_entered+=1
                if hold_entered==callers:hold_ready.set()
                await asyncio.Event().wait()
            async def aclose(self):
                nonlocal closed
                closed+=1

        async def upstream(request):
            nonlocal active,peak,entered
            body=json.loads(request.content);command=json.loads(next(m['content'] for m in reversed(body['messages']) if m['role']=='user'))
            phase=command['phase'];requests[phase]+=1
            if phase=='hold':return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=HeldStream())
            active+=1;peak=max(peak,active)
            try:
                if phase=='info':
                    entered+=1
                    if entered==callers:barrier.set()
                    await asyncio.wait_for(barrier.wait(),10)
                if phase=='latest':
                    content=frame({'content':'Latest request only'})+frame(reason='stop')+b'data: [DONE]\n\n'
                else:
                    tool={'index':0,'id':f"stress-{command['caller']}-{phase}",'type':'function',
                          'function':{'name':command['name'],'arguments':json.dumps(command['arguments'])},
                          'extra_content':{'google':{'thought_signature':'synthetic-signature-'+str(command['caller'])+'-'+phase}}}
                    content=frame({'tool_calls':[tool]})+frame(reason='tool_calls')+b'data: [DONE]\n\n'
                return httpx.Response(200,headers={'content-type':'text/event-stream'},content=content)
            finally:active-=1
        gateway.client=httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public),base_url='http://offline') as client:
            calls=[];histories=[[] for _ in range(callers)];saved_commands={}
            progress['checks']=checks;progress['stage_durations_ms']=stages;progress['http_errors']=[]
            async def inbound(message):
                response=await client.post('/webhooks/vapi/inbound',json={'message':message},headers={'authorization':'Bearer '+PHONE_SECRET})
                if response.status_code!=200:
                    progress['http_errors'].append({'phase':progress['last_phase'],'event':message['type'],'status':response.status_code,
                                                   'code':response.json().get('error',response.json().get('detail'))})
                return response
            admission_messages=[{'type':'assistant-request','call':{'id':str(uuid4()),'type':'inboundPhoneCall','orgId':ORG,'phoneNumberId':PHONE}} for _ in range(callers)]
            try:
                progress['last_phase']='admission'
                began=time.perf_counter()
                admissions=await asyncio.gather(*(inbound(value) for value in admission_messages))
                stages['admission_ms']=round((time.perf_counter()-began)*1000,3)
                progress['admission_statuses']=[r.status_code for r in admissions]
                progress['admission_errors']=[r.json().get('error') for r in admissions if r.status_code!=200]
                check(all(r.status_code==200 for r in admissions),'All configured simultaneous phone admissions must succeed')
                calls=[{**message['call'],**response.json()} for message,response in zip(admission_messages,admissions)]
                sids=[c['assistantOverrides']['variableValues']['session_id'] for c in calls]
                check(len(set(sids))==callers,'Every caller needs an isolated session')
                overflow=await inbound({'type':'assistant-request','call':{**admission_messages[0]['call'],'id':str(uuid4())}})
                check(overflow.status_code==429,'Call above configured limit must fail closed')
                progress['last_phase']='admission_replay'
                replays=await asyncio.gather(*(inbound(value) for value in admission_messages))
                progress['replay_statuses']=[r.status_code for r in replays]
                progress['replay_errors']=[r.json().get('error') for r in replays if r.status_code!=200]
                check(all(r.status_code==200 and r.json()==a.json() for r,a in zip(replays,admissions)),
                      'Admission replay must reuse exact binding; statuses='+json.dumps([r.status_code for r in replays])+
                      '; errors='+json.dumps([r.json().get('error') for r in replays if r.status_code!=200]))
                checks.update(configured_callers_admitted=True,overflow_rejected=True,admission_replay_exact=True,isolated_sessions=True)

                async def generation(index,phase,name=None,arguments=None):
                    command={'caller':index,'phase':phase,'name':name,'arguments':arguments or {}}
                    histories[index].append({'role':'user','content':json.dumps(command)})
                    response=await client.post('/llm/chat/completions',json={'model':MODEL,'stream':True,'call':calls[index],
                        'messages':histories[index],'tools':TOOL_SCHEMAS},headers={'authorization':'Bearer '+GATEWAY_SECRET})
                    check(response.status_code==200,'A valid admitted generation was rejected: '+str(response.status_code))
                    return response

                async def tool(index,phase,name,arguments):
                    response=await generation(index,phase,name,arguments)
                    items=[call for event in parsed_events(response) for call in event.get('choices',[{'delta':{}}])[0]['delta'].get('tool_calls',[])]
                    check(len(items)==1,'Exactly one signed synthetic command is expected')
                    command=items[0];saved_commands[(index,phase)]=command
                    result=await inbound({'type':'tool-calls','call':calls[index],'toolCallList':[command]})
                    check(result.status_code==200,'Signed command webhook was rejected')
                    raw=json.loads(result.json()['results'][0]['result'])
                    check(raw.get('error') not in ('operation_pending','status_unavailable','operation_unknown'),'Unexpected unsettled tool within this bounded exercise')
                    histories[index].extend([{'role':'assistant','content':'','tool_calls':[command]},
                        {'role':'tool','tool_call_id':command['id'],'content':json.dumps(raw)}])
                    return raw

                progress['last_phase']='synchronized_gateway'
                began=time.perf_counter()
                info=await asyncio.gather(*(tool(i,'info','get_dealership_info',{}) for i in range(callers)))
                stages['synchronized_gateway_and_info_ms']=round((time.perf_counter()-began)*1000,3)
                check(peak==callers and all(r['ok'] for r in info),'All configured gateway generations must overlap and succeed')
                checks['configured_peak_gateway_requests']=True
                wrong=await inbound({'type':'tool-calls','call':calls[1],'toolCallList':[saved_commands[(0,'info')]]})
                check(wrong.status_code==409,'A command from another caller must not execute')
                checks['cross_call_command_rejected']=True

                progress['last_phase']='booking_race'
                offers=await asyncio.gather(*(tool(i,'search','search_availability',{'service':'oil_change'}) for i in range(callers)))
                slot=offers[0]['slots'][0]['id'];check(all(r['slots'][0]['id']==slot for r in offers),'Shared final-slot race fixture must align')
                preparations=await asyncio.gather(*(tool(i,'prepare','prepare_booking',{'service':'oil_change','slot_id':slot,
                    'customer_name':'Synthetic Caller '+str(i),'phone':'51255501'+str(i).zfill(2),'vehicle':'2021 Toyota Camry'}) for i in range(callers)))
                check(all(r['ok'] for r in preparations),'Unreserved proposals must be independently preparable')
                with app.state.store.read_connection() as db:
                    check(db.execute('SELECT COUNT(*) FROM appointments').fetchone()[0]==0,'Preparation must not book')
                checks['no_booking_before_confirmation']=True
                confirmations=await asyncio.gather(*(tool(i,'confirm','confirm_action',{'proposal_id':preparations[i]['proposal_id'],'confirmed':True}) for i in range(callers)))
                winners=[i for i,r in enumerate(confirmations) if r.get('ok')]
                check(len(winners)==1,'Capacity race must produce exactly one booking')
                check(all(r.get('ok') or r.get('error')=='slot_unavailable' for r in confirmations),'Losing confirmations must report capacity conflict')
                replay=await asyncio.gather(*(inbound({'type':'tool-calls','call':calls[winners[0]],'toolCallList':[saved_commands[(winners[0],'confirm')]]}) for _ in range(callers)))
                check(all(r.status_code==200 and json.loads(r.json()['results'][0]['result'])==confirmations[winners[0]] for r in replay),'Concurrent retry must recover identical receipt')
                with app.state.store.read_connection() as db:
                    count=db.execute('SELECT COUNT(*) FROM appointments').fetchone()[0]
                    check(count==1,'Retries must never duplicate the committed booking')
                checks.update(last_slot_single_winner=True,concurrent_duplicate_receipts=True)

                progress['last_phase']='interruption_and_cancellation'
                held=[asyncio.create_task(generation(i,'hold')) for i in range(callers)]
                try:
                    await asyncio.wait_for(hold_ready.wait(),10)
                    latest=await generation(0,'latest')
                    stale=await held[0]
                    check('Latest request only' in latest.text and 'Obsolete' not in stale.text and '"tool_calls"' not in stale.text,'Superseded output must remain private')
                    check('[DONE]' in stale.text and 'error' not in stale.text,'Supersession must complete neutrally')
                    for pending in held[1:]:pending.cancel()
                    endings=await asyncio.gather(*held[1:],return_exceptions=True)
                    check(all(isinstance(value,asyncio.CancelledError) for value in endings),'External cancellation must not be reported as success')
                    check(not gateway.active and not gateway.owners and closed==callers,'All canceled transports must close and release ownership')
                    checks.update(supersession_suppresses_obsolete_output=True,cancellation_closes_all_streams=True)
                finally:
                    for pending in held:
                        if not pending.done():pending.cancel()
                    await asyncio.gather(*held,return_exceptions=True)

                progress['last_phase']='teardown'
                endings,terminal_attempts=await reconcile_terminal_events(inbound,calls)
                progress['terminal_event_attempt_statuses']=terminal_attempts
                check(all(r.status_code==200 for r in endings),'Known terminal events must reconcile within three attempts: '+str(terminal_attempts))
                with app.state.store.read_connection() as db:
                    check(app.state.live_admission.count(db)==0,'Ended calls must release shared admission')
                    check(db.execute('SELECT COUNT(*) FROM appointments').fetchone()[0]==1,'Call teardown must preserve appointment')
                checks['ended_calls_release_admission']=True
                return {'passed':True,'callers':callers,'checks':checks,'stage_durations_ms':stages,
                        'peak_fake_upstream_header_requests':peak,'fake_requests_by_phase':dict(requests),
                        'admission_statuses':dict(Counter(str(r.status_code) for r in admissions)),
                        'intentional_rejection_statuses':{'overflow':overflow.status_code,'cross_call_command':wrong.status_code},
                        'confirmed_appointments':1,'losing_capacity_conflicts':callers-1,'closed_canceled_streams':closed,
                        'gateway_outcomes':dict(Counter(r['outcome'] for r in gateway.timings)),
                        'configured_limits':app.state.capacity.public_limits(),
                        'terminal_event_attempt_statuses':terminal_attempts}
            finally:
                await app.state.shutdown_resources()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--rounds',type=int,default=1,choices=range(1,11))
    parser.add_argument('--callers',type=int,default=4,choices=(4,10))
    args=parser.parse_args()
    if args.output.exists():parser.error('Choose a new output file; existing evidence is never overwritten.')
    if not args.output.parent.is_dir():parser.error('The report parent directory must already exist.')
    report={'kind':'offline_caller_capacity_stress','callers':args.callers,'paid_requests':0,'real_audio':False,'runtime_database_used':False,
            'before_sources':hashes(),'rounds':[],'limitations':[
                'Synthetic signed model responses and explicit fixture confirmations, not proof of actual speech understanding or human assent.',
                'Durations measure this local ASGI/SQLite/fake-upstream exercise, not AWS, carrier, provider quota or voice latency.',
                'Configured call capacity still requires actual provider entitlement and staged real voice acceptance.']}
    try:
        for index in range(args.rounds):
            with tempfile.TemporaryDirectory(prefix='servicevoice-capacity-') as temporary:
                progress={'round':index+1};report['rounds'].append(progress)
                result=asyncio.run(exercise(Path(temporary),progress,callers=args.callers));progress.update(result)
                print(json.dumps({'round':index+1,'passed':result['passed'],'peak':result['peak_fake_upstream_header_requests']}),flush=True)
        report['after_sources']=hashes()
        check(report['before_sources']==report['after_sources'],'Source changed during the run')
        report['passed']=True
    except BaseException as exc:
        report['passed']=False;report['failure']={'type':type(exc).__name__,'message':str(exc)}
        report['after_sources']=hashes()
    with args.output.open('x',encoding='utf-8') as handle:json.dump(report,handle,indent=2);handle.write('\n')
    print(json.dumps({'passed':report['passed'],'output':str(args.output)}))
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
