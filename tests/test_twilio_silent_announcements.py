"""A rejected announcement must be silent media, never Twilio's spoken error."""
import asyncio
import io
import wave
import pytest
import httpx
from app.main import create_public_app
from app.twilio_bridge import signature
from test_twilio_api import lane_app, Provider


def assert_silence(response):
    assert response.status_code == 200
    assert response.headers['content-type'] == 'audio/wav'
    assert response.headers['cache-control'] == 'no-store'
    with wave.open(io.BytesIO(response.content), 'rb') as w:
        assert (w.getframerate(),w.getnchannels(),w.getsampwidth()) == (8000,1,2)
        assert 0 < w.getnframes() <= 800
        assert not any(w.readframes(w.getnframes()))


@pytest.mark.parametrize('case', ['expired','missing_ticket','replay','duplicate_ticket','duplicate_cue',
    'wrong_phrase','missing_bank','unknown_cue','missing_bridge','recovering','ended','no_state',
    'assistant','answer_ready','closed','wrong_binding','wrong_selection','invalid_signature',
    'closing','no_client','recovery_expired','recovery_wrong_state','store_failure'])
def test_all_clip_refusals_return_silent_success(lane_app, case, monkeypatch):
    async def run():
        lane=lane_app.state.twilio_lane;lane.client=Provider()
        row=lane.store.allocate('CA'+'1'*32)
        lane.store.claim_dial(row['id'],'CF'+'2'*32,row['caller_sid'])
        with lane.store.db() as db:
            db.execute("UPDATE twilio_bridges SET state='joined',provider_call_id='fixture' WHERE id=?",(row['id'],))
        lane.coordinator.register('fixture','session');state=lane.coordinator.calls['fixture']
        state.assistant=False;state.caller=True
        buf=io.BytesIO()
        with wave.open(buf,'wb') as w:
            w.setnchannels(1);w.setsampwidth(2);w.setframerate(8000);w.writeframes(b'\x01\x00'*4000)
        lane.wav=buf.getvalue();lane.clips={'neutral_ack':(lane.wav,'a'*64)}
        item={'call_id':'fixture','bridge_id':row['id'],'phrase_id':'neutral_ack','epoch':0,'expires':lane.coordinator.clock()+.75}
        lane.announcements['ticket']=item
        suffix='clip/neutral_ack.wav?bridge='+row['id']+'&ticket=ticket'
        if case=='expired': item['expires']=0
        if case in {'missing_ticket','replay'}: lane.announcements.clear()
        if case=='duplicate_ticket': suffix+='&ticket=other'
        if case=='duplicate_cue': suffix+='&cue=neutral_ack&cue=neutral_ack'
        if case=='wrong_phrase': suffix=suffix.replace('neutral_ack.wav','unknown.wav')
        if case=='missing_bank': lane.wav=None
        if case=='unknown_cue': suffix+='&cue=unknown'
        if case=='missing_bridge': suffix=suffix.replace(row['id'],'missing')
        if case in {'recovering','ended'}:
            with lane.store.db() as db:db.execute('UPDATE twilio_bridges SET state=? WHERE id=?',(case,row['id']))
        if case=='no_state': lane.coordinator.calls.clear()
        if case in {'assistant','answer_ready','closed'}: setattr(state,case,True)
        if case=='wrong_binding': item['call_id']='wrong'
        if case=='wrong_selection': item['phrase_id']='wrong'
        if case=='closing': lane.closing=True
        if case=='no_client': lane.client=None
        if case.startswith('recovery_'):
            suffix+='&recovery=1'
            lane.recovery_announcements['ticket']=(row['id'],0 if case=='recovery_expired' else lane.coordinator.clock()+5)
        original_get=lane.store.get
        if case=='store_failure': monkeypatch.setattr(lane.store,'get',lambda *args: (_ for _ in ()).throw(OSError('fixture')))
        headers={'x-twilio-signature':signature(lane.config.auth_token,lane.config.url(suffix),[])}
        if case=='invalid_signature': headers={}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_public_app(lane_app)),base_url='https://test') as client:
            assert_silence(await client.get('/twilio/'+suffix,headers=headers))
            if case=='invalid_signature': assert lane.announcements['ticket'] is item
        assert state.clip_until < lane.coordinator.clock()
        if case=='store_failure': monkeypatch.setattr(lane.store,'get',original_get)
        await lane.close()
    asyncio.run(run())


@pytest.mark.parametrize('requested', ['neutral_ack','right_1'])
def test_transcript_churn_does_not_expire_live_approved_clip(lane_app, requested):
    async def run():
        lane=lane_app.state.twilio_lane;lane.client=Provider()
        row=lane.store.allocate('CA'+'1'*32)
        lane.store.claim_dial(row['id'],'CF'+'2'*32,row['caller_sid'])
        with lane.store.db() as db:db.execute("UPDATE twilio_bridges SET state='joined',provider_call_id='fixture' WHERE id=?",(row['id'],))
        lane.coordinator.register('fixture','session');state=lane.coordinator.calls['fixture'];state.assistant=False
        buf=io.BytesIO()
        with wave.open(buf,'wb') as w:
            w.setnchannels(1);w.setsampwidth(2);w.setframerate(8000);w.writeframes(b'\x01\x00'*4000)
        lane.wav=buf.getvalue();lane.clips={'neutral_ack':(lane.wav,'a'*64),'right_1':(b'not selected in a pause','b'*64)}
        lane.announcements['ticket']={'call_id':'fixture','bridge_id':row['id'],'phrase_id':requested,'epoch':0,'expires':lane.coordinator.clock()+.75}
        state.epoch=8;state.caller=False;state.received=lane.coordinator.clock()-2
        suffix='clip/neutral_ack.wav?bridge='+row['id']+'&ticket=ticket&cue='+requested
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_public_app(lane_app)),base_url='https://test') as client:
            response=await client.get('/twilio/'+suffix,headers={'x-twilio-signature':signature(lane.config.auth_token,lane.config.url(suffix),[])})
            assert response.status_code==200 and response.content==lane.wav
            assert_silence(await client.get('/twilio/'+suffix,headers={'x-twilio-signature':signature(lane.config.auth_token,lane.config.url(suffix),[])}))
        await lane.close()
    asyncio.run(run())


@pytest.mark.parametrize('request_or_pause', ['request','pause'])
def test_pause_and_request_acks_never_sound_like_assent(request_or_pause):
    from test_listening_cues import setup, drain, TEXT
    from app.listening_audio import VARIATIONS
    async def run():
        for offset in range(10):
            c,clock,_,sent,_,event,_=setup()
            c.available_phrases.update(VARIATIONS)
            state=c.calls['fixture-call'];state.rotation_start=offset
            text='I need an oil change and a tire rotation for my Camry' if request_or_pause=='request' else TEXT
            state.text=text;state.segment=text;state.received=clock.now
            if request_or_pause=='pause':
                state.caller=False;state.waiting_stopped=clock.now-.4
            await c._decide('fixture-call',state,state.epoch,text)
            await drain(c)
            assert sent and state.phrase_id in {'neutral_ack','uh_huh_1','uh_huh_2'}
            await c.close()
    asyncio.run(run())


def test_rotation_start_is_sampled_per_call(monkeypatch):
    from app.listening_cues import ListeningState
    offsets=iter([2,7])
    monkeypatch.setattr('app.listening_cues.secrets.randbelow',lambda n:next(offsets))
    assert ListeningState('one').rotation_start==2
    assert ListeningState('two').rotation_start==7


def test_logical_followup_requires_spacing_and_is_never_refunded():
    from test_listening_cues import setup
    c,clock,_,_,_,event,_=setup()
    event('speech-update','user',status='stopped');clock.now+=1
    assert c.commit_waiting('fixture-call','session')
    assert not c.commit_waiting('fixture-call','session')
    clock.now+=2.49
    assert c.waiting_status('fixture-call','session',True)[1]=='cooldown'
    clock.now+=.02
    assert c.commit_waiting('fixture-call','session',True)
    clock.now+=5
    assert not c.commit_waiting('fixture-call','session',True)
    # A cue-only interruption/continuation cannot replenish either allowance.
    event('transcript','assistant',transcript='One sec.',transcriptType='final')
    event('speech-update','user',status='started');clock.now+=1
    event('speech-update','user',status='stopped');clock.now+=1
    assert not c.commit_waiting('fixture-call','session',True)
