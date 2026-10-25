"""Finalized caller clauses revoke listening, never grant waiting authority."""
import asyncio
import pytest
from test_listening_cues import setup, TEXT


@pytest.mark.parametrize('stopped', [True, False])
def test_final_clause_revokes_only_pause_ack_without_waiting_classifier(stopped):
    async def run():
        c,clock,_,_,_,event,_=setup()
        event('transcript','user',transcriptType='partial',transcript=TEXT)
        clock.now+=.01
        event('transcript','user',transcriptType='final',transcript=TEXT)
        if stopped:
            clock.now+=.01;event('speech-update','user',status='stopped')
        state=c.calls['fixture-call']
        assert c.waiting_content_finished(state) is stopped
        if stopped:
            assert c.timing_reason(state)=='caller_turn_finished'
            assert c.waiting_status('fixture-call','session')[1]=='caller_pause_too_short'
            clock.now+=.8
            assert c.waiting_status('fixture-call','session')[1]=='eligible'
        else:
            assert c.waiting_status('fixture-call','session')[1]=='caller_speaking'
        # New live words revoke the pause veto even before a late start event.
        clock.now+=.01
        event('transcript','user',transcriptType='partial',transcript='And the engine makes')
        assert not c.waiting_content_finished(state)
        await c.close()
    asyncio.run(run())
