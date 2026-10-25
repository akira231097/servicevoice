"""Regressions from the handset call: fragment stalls, ASR resumes and weak vetoes."""
import asyncio
from dataclasses import replace

import httpx
import pytest

from app.backchannel_policy import BackchannelDecision, guarded_waiting_fallback, waiting_state, neutral_language_compatible
from app.gemini_tts import GeminiTTS, ATOMIC_FAREWELL, FAREWELL_TAIL_SECONDS, TTSError
from test_gemini_tts import Stream, event as pcm_event, envelope, body
from test_listening_cues import setup, drain

CONTEXT=dict(allow_turn=True, ended=False, neutral_waiting=True, stage='service',
    active_confirmation=False, has_pending_operation=False, has_pending_proposal=False)


def test_farewell_never_releases_a_tiny_segment_before_complete_synthesis():
    async def run():
        gate=asyncio.Event()
        first=b'\x01\x00'*80  # The handset failure had a five-millisecond blip.
        rest=b'\x20\x01'*32000
        stream=Stream([pcm_event(first),pcm_event(rest)],gate)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(
                200,headers={'content-type':'text/event-stream'},stream=stream))) as client:
            tts=GeminiTTS(lambda:{'GEMINI_API_KEY':'fixture','SERVICEVOICE_TTS_ATOMIC_FAREWELL':'on'},client=client)
            task=asyncio.create_task(tts.response(envelope(ATOMIC_FAREWELL)))
            await asyncio.sleep(.02)
            assert not task.done()  # No response headers or partial farewell.
            gate.set();response=await task
            chunks=[x async for x in response.body_iterator]
            assert chunks==[first+rest+b'\x00'*(32000*FAREWELL_TAIL_SECONDS)] and stream.closed
    asyncio.run(run())


@pytest.mark.parametrize('atomic,text',[(False,ATOMIC_FAREWELL),(True,'What time works for you?')])
def test_ordinary_speech_and_unconfigured_main_continue_streaming(atomic,text):
    async def run():
        gate=asyncio.Event();stream=Stream([pcm_event(),pcm_event()],gate)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(
                200,headers={'content-type':'text/event-stream'},stream=stream))) as client:
            tts=GeminiTTS(lambda:{'GEMINI_API_KEY':'fixture','SERVICEVOICE_TTS_ATOMIC_FAREWELL':'on' if atomic else 'off'},client=client)
            response=await asyncio.wait_for(tts.response(envelope(text)),1)
            assert await anext(response.body_iterator)
            gate.set();await body(response)
    asyncio.run(run())


@pytest.mark.parametrize('bad', ['incomplete','oversized'])
def test_broken_farewell_fails_before_any_audio_and_releases_capacity(bad):
    async def run():
        stream=Stream([pcm_event(b'\x01\x00'*(16000*9 if bad=='oversized' else 80))],completed=bad!='incomplete')
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(
                200,headers={'content-type':'text/event-stream'},stream=stream))) as client:
            tts=GeminiTTS(lambda:{'GEMINI_API_KEY':'fixture','SERVICEVOICE_TTS_ATOMIC_FAREWELL':'on'},client=client,max_concurrency=1)
            with pytest.raises(TTSError):await tts.response(envelope(ATOMIC_FAREWELL))
            assert stream.closed and tts.capacity._value==1
    asyncio.run(run())


def test_growing_partials_restore_listening_after_missing_vad_start_but_never_grant_hold():
    async def run():
        c,clock,_,sent,_,event,_=setup()
        event('speech-update','user',status='stopped')
        for i,text in enumerate(['The car makes a rattling sound', 'The car makes a rattling sound when',
                'The car makes a rattling sound when I', 'The car makes a rattling sound when I drive']):
            clock.now+=.2
            event('transcript','user',transcriptType='partial',transcript=text)
            await asyncio.sleep(0)
        await drain(c)
        assert c.calls['fixture-call'].caller and sent
        assert c.waiting_status('fixture-call','session')[1]=='caller_speaking'
        clock.now+=3
        assert c.waiting_status('fixture-call','session')[1]=='caller_speaking'
        await c.close()
    asyncio.run(run())


@pytest.mark.parametrize('mode',['repeat','revision','stale'])
def test_silence_revisions_and_delayed_partials_do_not_restore_speech(mode):
    async def run():
        c,clock,_,sent,_,event,_=setup()
        event('speech-update','user',status='stopped')
        for i in range(4):
            clock.now+=.2
            values={'transcriptType':'partial','transcript':'The engine makes a rattling sound'}
            if mode=='revision':values['transcript']='The engine makes a '+('rattling' if i%2 else 'shaking')+' sound'
            if mode=='stale':values['timestamp']=(clock.wall()-3)*1000
            event('transcript','user',**values)
        assert not c.calls['fixture-call'].caller
        await c.close()
    asyncio.run(run())


def test_weak_exclusion_can_receive_only_a_guarded_neutral_hold():
    text='I do not know what the issue is, so I want an appointment as soon as possible.'
    state,_=waiting_state([{'role':'user','content':text}],CONTEXT)
    d=BackchannelDecision(reason='classifier_silence',probability=.82,content_kind='scheduling_request',
        content_probability=.82,language_choice='cache_compatible',language_probability=1)
    assert guarded_waiting_fallback(d,state,CONTEXT)
    for replacement in [dict(probability=.99),dict(content_kind='other'),dict(content_probability=.79),
            dict(language_choice='not_compatible'),dict(reason='invalid_response'),dict(reason='provider_unavailable')]:
        assert not guarded_waiting_fallback(replace(d,**replacement),state,CONTEXT)


@pytest.mark.parametrize('text',["Just stop the call. I'm done.",'Please end this call.',
    'Wait a second.','I am still thinking.','Uh um','My brakes cannot stop the car.'])
def test_neutral_hold_does_not_override_quiet_noise_or_danger(text):
    state,_=waiting_state([{'role':'user','content':text}],CONTEXT)
    assert state is None


def test_hows_it_going_is_a_greeting_not_language_uncertainty():
    state,_=waiting_state([{'role':'user','content':"How's it going?"}],CONTEXT)
    assert neutral_language_compatible('cache_compatible',.97,state)
    assert not neutral_language_compatible('not_compatible',.97,state)


@pytest.mark.parametrize('tail,quiet',[
    ('It has been shaking for quite some time and I have been worried about the problem.',1.5),
    ('So what can you do?',.75),
    ('Because I just want to book an appointment as soon as possible.',.75),
])
def test_long_description_pause_is_not_a_direct_question_handoff(tail,quiet):
    async def run():
        c,clock,_,_,_,event,_=setup();clock.now=10
        text='The engine rattles every morning when I drive around my neighborhood. '+tail
        event('transcript','user',transcriptType='final',transcript=text)
        event('speech-update','user',status='stopped')
        clock.now+=quiet-.01
        assert c.waiting_status('fixture-call','session')[1]=='caller_pause_too_short'
        clock.now+=.02
        assert c.waiting_status('fixture-call','session')[1]=='eligible'
        await c.close()
    asyncio.run(run())
