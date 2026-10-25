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
