"""Replay discarded speculative holds without refunding a heard/cut-off cue."""
import asyncio
import pytest
from app.backchannel_budget import BackchannelBudget
from app.diagnostics import EVENTS, safe_attributes
from test_listening_cues import setup
from test_twilio_api import lane_app


def test_reclaim_reason_is_preserved_in_diagnostics():
    assert 'waiting_reclaimed' in EVENTS
    assert safe_attributes({'listening_reason':'cancelled_without_observed_speech'}) == {
        'listening_reason':'cancelled_without_observed_speech'}

@pytest.mark.parametrize('heard',['none','started','transcript','late_event'])
def test_reclaim_requires_cancel_resume_stop_grace_and_no_assistant_observation(heard):
    async def run():
        c,clock,_,_,_,event,_=setup()
        event('speech-update','user',status='stopped');clock.now+=.8
        assert c.commit_waiting('fixture-call','session')
        assert c.track_waiting('fixture-call','session',123)
        assert c.refundable_waiting('fixture-call','session') is None
        assert not c.track_waiting('fixture-call','session',999,abandoned=True)
        assert c.track_waiting('fixture-call','session',123,abandoned=True)
        clock.now+=.1
        if heard=='started':event('speech-update','assistant',status='started')
        if heard=='transcript':event('transcript','assistant',transcriptType='partial',transcript='One sec.')
        event('speech-update','user',status='started')
        clock.now+=.5
        if heard=='started':event('speech-update','assistant',status='stopped')
        event('speech-update','user',status='stopped');clock.now+=.8
        assert c.refundable_waiting('fixture-call','session') is None  # 2.1 s grace not elapsed
        clock.now+=2
        if heard=='late_event':
            c.event('fixture-call',{'type':'speech-update','role':'assistant','status':'started',
                'timestamp':(clock.wall()-3)*1000})
            event('speech-update','user',status='stopped');clock.now+=.8
        if heard=='none':
            assert c.refundable_waiting('fixture-call','session')==123
            assert c.reclaim_waiting('fixture-call','session',123)
            assert c.calls['fixture-call'].waiting_count==0
            assert not c.reclaim_waiting('fixture-call','session',123)
        else:
            assert c.refundable_waiting('fixture-call','session') is None
            assert c.calls['fixture-call'].waiting_count==1
        await c.close()
    asyncio.run(run())

def test_only_the_same_abandoned_reservation_can_restore_both_budgets(lane_app):
    async def run():
        c,clock,_,_,_,event,_=setup();lane=lane_app.state.twilio_lane
        original=lane.coordinator;lane.coordinator=c;lane.bindings['fixture-call']=('bridge','session')
        budget=BackchannelBudget(clock=clock);lane_app.state.gemini_gateway.backchannel_budget=budget
        event('speech-update','user',status='stopped');clock.now+=.8
        assert budget.consume('fixture-call',1,'one_sec')[0]
        stamp=budget.receipt('fixture-call')
        assert lane.commit_waiting('fixture-call','session')
        assert c.track_waiting('fixture-call','session',stamp,abandoned=False)
        clock.now+=.1;event('speech-update','user',status='started');clock.now+=3
        event('speech-update','user',status='stopped');clock.now+=.8
        assert lane.waiting_status('fixture-call','session')[1]=='turn_already_acknowledged'
        assert c.track_waiting('fixture-call','session',stamp,abandoned=True)
        assert lane.waiting_status('fixture-call','session')[1]=='eligible'
        assert budget.consume('fixture-call',1,'moment')[0]
        assert lane.commit_waiting('fixture-call','session')
        assert c.track_waiting('fixture-call','session',budget.receipt('fixture-call'))
        assert not c.track_waiting('fixture-call','session',stamp,abandoned=True)
        assert lane.waiting_status('fixture-call','session')[1]=='turn_already_acknowledged'
        await c.close();lane.coordinator=original
    asyncio.run(run())
