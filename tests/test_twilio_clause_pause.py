"""Replay the 0.8-second clause pause that spent an unheard waiting cue."""
import asyncio
import pytest
from test_listening_cues import setup


@pytest.mark.parametrize('elapsed', [5, 14])
def test_long_clause_pause_is_protected_without_delaying_short_answers(elapsed):
    async def run():
        c, clock, _, _, _, event, _ = setup()
        clock.now = elapsed
        event('speech-update', 'user', status='stopped')
        event('transcript', 'user', transcriptType='partial',
              transcript='I can leave the car with the advisor while I am nearby.')
        event('transcript', 'user', transcriptType='final',
              transcript='I can leave the car with the advisor while I am nearby.')
        clock.now += .8
        assert c.waiting_status('fixture-call','session')[1] == (
            'caller_pause_too_short' if elapsed == 14 else 'eligible')
        if elapsed == 14:
            assert not c.commit_waiting('fixture-call','session')
            # Continued words arrive before Vapi corrects its early stop flag.
            event('transcript','user',transcriptType='partial',transcript='I am flexible')
            clock.now += .5
            event('transcript','user',transcriptType='partial',transcript='I am flexible before lunch.')
            event('transcript','user',transcriptType='final',transcript='I am flexible before lunch.')
            clock.now += .8
            assert not c.commit_waiting('fixture-call','session')
            clock.now += .21
            assert c.commit_waiting('fixture-call','session')
            assert c.calls['fixture-call'].waiting_count == 1
        await c.close()
    asyncio.run(run())
