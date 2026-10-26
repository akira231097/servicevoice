"""Replay the 12ms blip, heard interruption, and continued long explanation."""
import asyncio
import pytest
from app.backchannel_budget import BackchannelBudget
from test_listening_cues import setup


@pytest.mark.parametrize('duration,refundable',[(.012,True),(.25,True),(.31,False),(.5,False)])
def test_observed_blip_is_distinct_from_heard_phrase_but_never_repeats_early(duration,refundable):
    async def run():
        c,clock,_,_,_,event,_=setup()
        event('speech-update','user',status='stopped');clock.now+=.8
        assert c.commit_waiting('fixture-call','session')
        c.track_waiting('fixture-call','session',123)
        clock.now+=.2;event('speech-update','assistant',status='started')
        clock.now+=duration;event('speech-update','assistant',status='stopped')
        c.track_waiting('fixture-call','session',123,abandoned=True)
        event('speech-update','user',status='started');clock.now+=1.1
        event('speech-update','user',status='stopped');clock.now+=1
        assert c.waiting_status('fixture-call','session')[1]=='cooldown'
        assert (c.refundable_waiting('fixture-call','session')==123)==refundable
        if refundable:c.reclaim_waiting('fixture-call','session',123)
        assert c.waiting_status('fixture-call','session')[1]=='cooldown'
        clock.now+=3
        assert c.waiting_status('fixture-call','session')[1]=='eligible'
        assert c.commit_waiting('fixture-call','session')
        assert not c.commit_waiting('fixture-call','session')
        await c.close()
    asyncio.run(run())


@pytest.mark.parametrize('caller_seconds',[0,.3,.99,1.01])
def test_quiet_or_short_noise_never_refills_a_hold(caller_seconds):
    async def run():
        c,clock,_,_,_,event,_=setup()
        event('speech-update','user',status='stopped');clock.now+=.8
        assert c.commit_waiting('fixture-call','session')
        clock.now+=1
        if caller_seconds:
            event('speech-update','user',status='started');clock.now+=caller_seconds
            event('speech-update','user',status='stopped')
        clock.now+=5
        assert c.waiting_status('fixture-call','session')[1]==('eligible' if caller_seconds>=1 else 'caller_continuation_too_short')
        await c.close()
    asyncio.run(run())


def test_scoped_spacing_does_not_relax_other_assistants_or_repeat_phrase():
    clock=[0.]
    budget=BackchannelBudget(clock=lambda:clock[0])
    assert budget.consume('call',1,'moment')[0]
    clock[0]=5
    assert not budget.consume('call',1,'one_sec')[0]
    assert budget.consume('call',1,'moment',observed_spacing=True)==(False,'repeated_phrase')
    assert budget.consume('call',1,'one_sec',observed_spacing=True)[0]
    clock[0]=8
    assert budget.consume('call',1,'just_a_sec',follow_up=True,observed_spacing=True)[0]


def test_followup_stays_single_even_after_caller_continues():
    async def run():
        c,clock,_,_,_,event,_=setup()
        event('speech-update','user',status='stopped');clock.now+=.8
        assert c.commit_waiting('fixture-call','session')
        clock.now+=2.6;assert c.commit_waiting('fixture-call','session',True)
        event('speech-update','user',status='started');clock.now+=4
        event('speech-update','user',status='stopped');clock.now+=1
        assert c.commit_waiting('fixture-call','session')
        clock.now+=5
        assert not c.commit_waiting('fixture-call','session',True)
        await c.close()
    asyncio.run(run())


def test_actual_12ms_blip_with_growing_asr_but_no_new_vad_boundary():
    """Owned 7:23 PM call: sample every 10ms, not only the old emission tick.

    Vapi logged no new User-start event during 3.55 seconds of continuing words.
    These are provider times, not a claim of exact webhook arrival or audio.
    """
    async def run():
        c,clock,_,_,_,event,_=setup()
        event('speech-update','user',status='stopped');clock.now+=3.225
        assert c.commit_waiting('fixture-call','session')
        c.track_waiting('fixture-call','session',123)
        start=clock.now
        events=[(.058,'start',0),(.065,'partial',1),(.070,'stop',0),
            (.308,'partial',2),(.545,'partial',3),(.785,'partial',4),(.907,'partial',5),
            (1.028,'partial',6),(1.272,'partial',7),(1.387,'partial',8),(1.630,'partial',9),
            (1.985,'partial',10),(2.230,'partial',12),(2.475,'partial',13),(2.712,'partial',14),
            (2.828,'partial',15),(3.185,'partial',16),(3.304,'partial',17),(3.548,'partial',20),
            (3.551,'final',20)]
        i=0;allowed=[]
        for tick in range(451):
            now=tick/100
            while i<len(events) and events[i][0]<=now:
                offset,kind,words=events[i];clock.now=start+offset
                if kind in {'start','stop'}:event('speech-update','assistant',status='started' if kind=='start' else 'stopped')
                else:event('transcript','user',transcriptType=kind,transcript=' '.join(['word']*words))
                i+=1
            clock.now=start+now
            if c.waiting_status('fixture-call','session')[1]=='eligible':allowed.append(now)
        # Growing words restore listening and revoke the old pre-interruption
        # stop. Neither the final ASR clause nor elapsed time proves a new end.
        assert not allowed
        assert c.waiting_status('fixture-call','session')[1]=='caller_speaking'
        assert c.calls['fixture-call'].caller_speech_total==c.calls['fixture-call'].waiting_speech_total
        c.track_waiting('fixture-call','session',123,abandoned=True)
        assert c.refundable_waiting('fixture-call','session') is None
        event('speech-update','user',status='stopped');clock.now+=.76
        assert c.waiting_status('fixture-call','session')[1]=='caller_pause_too_short'
        clock.now+=.75  # Long descriptive continuation needs the longer quiet floor.
        assert c.waiting_status('fixture-call','session')[1]=='eligible'
        assert c.refundable_waiting('fixture-call','session')==123
        await c.close()
    asyncio.run(run())


@pytest.mark.parametrize('mode',['repeated_text','revised_text','widely_spaced','stale'])
def test_idle_or_transcript_churn_does_not_invent_one_second_of_speech(mode):
    async def run():
        c,clock,_,_,_,event,_=setup()
        event('speech-update','user',status='stopped');clock.now+=1
        assert c.commit_waiting('fixture-call','session')
        for index in range(8):
            clock.now+=1 if mode=='widely_spaced' else .3
            text=('word '+str(index)) if mode=='revised_text' else ' '.join(['word']*(index+1 if mode in {'widely_spaced','stale'} else 2))
            if mode=='stale':c.event('fixture-call',{'type':'transcript','role':'user','transcriptType':'partial','transcript':text,'timestamp':(clock.wall()-3)*1000})
            else:event('transcript','user',transcriptType='partial',transcript=text)
        clock.now+=5
        assert c.waiting_status('fixture-call','session')[1]=='caller_continuation_too_short'
        await c.close()
    asyncio.run(run())
