import asyncio
import json
import pytest
from app.phone_closing import candidate
from app.call_lifecycle import schema
from test_receipt_narration import setup,issue,post
from test_listening_cues import setup as speaker_setup


@pytest.mark.parametrize('text', ['Goodbye.','Thank you, goodbye.','Okay. That is all, goodbye.',
    'Okay, stop. Do not book anything. I only wanted to test the conversation. That is all, goodbye.',
    'Please end the call.'])
def test_only_whole_explicit_closings_are_candidates(text):
    assert candidate([{'role':'user','content':text}])==text


@pytest.mark.parametrize('text', ['Stop.','Stop talking.','Thanks.','Yes, book it. Goodbye.',
    'Say goodbye.','Do not say goodbye.','If I say goodbye, hang up.','Goodbye, wait.',
    'Goodbye, but what are your hours?','"goodbye"','Tell me why you said goodbye.',
    'Please cancel my appointment, goodbye.','Adios.','Okay? Goodbye.'])
def test_ambiguity_actions_other_languages_and_quotes_fall_back(text):
    assert candidate([{'role':'user','content':text}]) is None


@pytest.mark.parametrize('case',['close','pending_proposal','pending_operation','caller_resumed','other_assistant','not_enabled'])
def test_fast_native_close_keeps_signed_issuance_and_business_gates(tmp_path,monkeypatch,case):
    env=setup(tmp_path,monkeypatch)
    env.config['SERVICEVOICE_NATIVE_END_CALL']='on' if case!='not_enabled' else 'off'
    env.gateway.waiting_cue_assistant='test-assistant' if case!='other_assistant' else 'another-assistant'
    env.gateway.closing_ready=lambda *args:case!='caller_resumed'
    env.history=[{'role':'user','content':'Thank you, goodbye.'}]
    context=dict(allow_turn=True,has_pending_proposal=case=='pending_proposal',
        has_pending_operation=case=='pending_operation',ended=False)
    monkeypatch.setattr(env.gateway,'_backchannel_context',lambda *a:context)
    async def run():
        try:
            response=await post(env,None if case=='not_enabled' else lambda b:b.update(tools=[*b['tools'],schema()]))
            assert response.status_code==200
            if case=='close':
                chunks=[json.loads(l[6:]) for l in response.text.splitlines() if l.startswith('data: {')]
                calls=chunks[0]['choices'][0]['delta']['tool_calls']
                assert calls[0]['function']=={'name':'endCall','arguments':'{}'}
                assert not env.sent
                public=[{k:v for k,v in calls[0].items() if k!='index'}]
                assert env.gateway.store.restore_group(env.sid,env.cid,public,allow_end_call=True)
                assert env.store.snapshot(env.sid)['appointment'] is None
            else:
                assert env.sent and 'Gemini fallback' in response.text
        finally:await env.gateway.close()
    asyncio.run(run())


def test_verified_withdrawal_can_skip_second_model_call(tmp_path,monkeypatch):
    from test_receipt_narration import prepare
    env=setup(tmp_path,monkeypatch)
    async def run():
        try:
            proposal=await prepare(env)
            env.history.extend([{'role':'assistant','content':'Please confirm this proposal.'},
                {'role':'user','content':'Do not book anything. Goodbye.'}])
            result=await issue(env,'dismiss_proposal',{'proposal_id':proposal['proposal_id']})
            assert result['ok']
            env.config['SERVICEVOICE_NATIVE_END_CALL']='on'
            env.gateway.waiting_cue_assistant='test-assistant'
            env.gateway.closing_ready=lambda *args:True
            response=await post(env,lambda b:b.update(tools=[*b['tools'],schema()]))
            assert response.status_code==200 and '"name":"endCall"' in response.text
            assert not env.sent and env.store.snapshot(env.sid)['appointment'] is None
        finally:await env.gateway.close()
    asyncio.run(run())


def test_closing_requires_exact_recent_final_and_observed_stop():
    async def run():
        c,clock,_,_,_,event,_=speaker_setup()
        event('transcript','user',transcriptType='final',transcript='Goodbye.')
        assert not c.closing_ready('fixture-call','session','Goodbye.')
        event('speech-update','user',status='stopped');clock.now+=.8
        assert c.closing_ready('fixture-call','session','Goodbye.')
        assert not c.closing_ready('fixture-call','other','Goodbye.')
        assert not c.closing_ready('fixture-call','session','Please hang up.')
        event('speech-update','user',status='started')
        assert not c.closing_ready('fixture-call','session','Goodbye.')
        await c.close()
    asyncio.run(run())


@pytest.mark.parametrize('final',[False,True])
def test_matching_closing_clause_and_actual_stop_do_not_require_final_flag_or_old_prefix(final):
    async def run():
        c,clock,_,_,_,event,_=speaker_setup()
        state=c.calls['fixture-call'];state.completed='Earlier unrelated vehicle details.'
        event('transcript','user',transcriptType='partial',transcript='That is all, goodbye.')
        if final:event('transcript','user',transcriptType='final',transcript='That is all, goodbye.')
        assert not c.closing_ready('fixture-call','session','Okay. That is all, goodbye.')
        event('speech-update','user',status='stopped');clock.now+=.8
        assert c.closing_ready('fixture-call','session','Okay. That is all, goodbye.')
        assert not c.closing_ready('fixture-call','session','Tell me your hours, then goodbye.')
        clock.now+=.1;event('speech-update','user',status='started')
        clock.now+=.2;event('speech-update','user',status='stopped');clock.now+=.8
        assert not c.closing_ready('fixture-call','session','Okay. That is all, goodbye.')
        await c.close()
    asyncio.run(run())


def test_new_partial_after_goodbye_revokes_shortcut():
    async def run():
        c,clock,_,_,_,event,_=speaker_setup()
        event('transcript','user',transcriptType='partial',transcript='Goodbye.')
        event('speech-update','user',status='stopped');clock.now+=.8
        assert c.closing_ready('fixture-call','session','Goodbye.')
        clock.now+=.1;event('transcript','user',transcriptType='partial',transcript='Goodbye, wait.')
        clock.now+=.8
        assert c.closing_status('fixture-call','session','Goodbye.')=='closing_text_unverified'
        await c.close()
    asyncio.run(run())


def test_optional_closing_state_failure_uses_model_without_hanging_up(tmp_path,monkeypatch):
    import sqlite3
    env=setup(tmp_path,monkeypatch)
    env.config['SERVICEVOICE_NATIVE_END_CALL']='on'
    env.gateway.waiting_cue_assistant='test-assistant';env.gateway.closing_ready=lambda *args:True
    env.history=[{'role':'user','content':'Goodbye.'}]
    def unavailable(*args):raise sqlite3.OperationalError('synthetic-only')
    monkeypatch.setattr(env.gateway,'_backchannel_context',unavailable)
    async def run():
        try:
            response=await post(env,lambda b:b.update(tools=[*b['tools'],schema()]))
            assert response.status_code==200 and 'Gemini fallback' in response.text
            assert 'synthetic-only' not in response.text
        finally:await env.gateway.close()
    asyncio.run(run())


def test_real_coordinator_partial_closing_reaches_signed_native_path(tmp_path,monkeypatch):
    env=setup(tmp_path,monkeypatch)
    env.config['SERVICEVOICE_NATIVE_END_CALL']='on';env.gateway.waiting_cue_assistant='test-assistant'
    env.history=[{'role':'user','content':'Okay. That is all, goodbye.'}]
    async def run():
        c,clock,_,_,_,event,_=speaker_setup()
        event('transcript','user',transcriptType='partial',transcript='That is all, goodbye.')
        event('speech-update','user',status='stopped');clock.now+=.8
        state=c.calls.pop('fixture-call');state.session_id=env.sid;c.calls[env.cid]=state
        env.gateway.closing_ready=c.closing_ready;env.gateway.closing_status=c.closing_status
        try:
            response=await post(env,lambda b:b.update(tools=[*b['tools'],schema()]))
            assert response.status_code==200 and '"name":"endCall"' in response.text and not env.sent
            row=env.gateway.latency_snapshot(env.cid)[-1]
            assert row['route']=='deterministic_closing' and row['sse_response_id'] in response.text
        finally:await env.gateway.close();await c.close()
    asyncio.run(run())


def test_caller_resuming_during_native_signature_write_gets_valid_silent_completion(tmp_path,monkeypatch):
    env=setup(tmp_path,monkeypatch);ready=[True]
    env.config['SERVICEVOICE_NATIVE_END_CALL']='on';env.gateway.waiting_cue_assistant='test-assistant'
    env.history=[{'role':'user','content':'Goodbye.'}];env.gateway.closing_ready=lambda *args:ready[0]
    original=env.gateway.store.save_group
    def interrupted(*args,**kwargs):
        original(*args,**kwargs);ready[0]=False
    monkeypatch.setattr(env.gateway.store,'save_group',interrupted)
    async def run():
        try:
            response=await post(env,lambda b:b.update(tools=[*b['tools'],schema()]))
            assert response.status_code==200 and '[DONE]' in response.text and 'endCall' not in response.text
            assert env.gateway.latency_snapshot(env.cid)[-1]['counts']['tool_calls']==0
        finally:await env.gateway.close()
    asyncio.run(run())
