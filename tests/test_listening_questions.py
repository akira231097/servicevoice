import asyncio
import pytest
from app.listening_cues import listening_gate, information_question, neutral_listening_only
from app.listening_audio import VARIATIONS
from test_listening_cues import setup, SAFE, TEXT, drain

QUESTIONS=[
    'Do you think a diagnostic and brake inspection cover the unusual engine sounds or would you recommend anything else?',
    'Would I be charged for both inspections and can I just cancel when I get there if it is not necessary?',
    'Would I be charged for both appointments and can. I just cancel when I get there?',
    'How much would the diagnostic inspection cost and what does that service normally cover?',
]

@pytest.mark.parametrize('text',QUESTIONS)
def test_long_information_questions_receive_only_neutral_listening_cues(text):
    async def run():
        c,clock,_,sent,_,_,_=setup();c.available_phrases.update(VARIATIONS)
        state=c.calls['fixture-call'];state.text=text;state.received=clock.now
        assert listening_gate(text,'Tell me about your car.',SAFE)=='eligible'
        assert information_question(text) and neutral_listening_only(state)
        await c._decide('fixture-call',state,state.epoch,text);await drain(c)
        assert len(sent)==1 and state.phrase_id in {'neutral_ack','uh_huh_1','uh_huh_2'}
        state.caller=False;state.waiting_stopped=clock.now-.4;clock.now+=.1
        assert c.timing_reason(state)=='question_finished'
        await c.close()
    asyncio.run(run())

@pytest.mark.parametrize('text',[
    'Can you cancel my appointment for me please?',
    'Cancel it when I get there please',
    'Yes please book it and cancel the other appointment',
    'Actually I meant Wednesday not Tuesday for the appointment',
    'My credit card number is 1234567890123456 for the diagnostic',
])
def test_questions_do_not_relax_commands_corrections_or_private_details(text):
    assert listening_gate(text,'Tell me more.',SAFE)!='eligible'


def test_new_question_after_answer_keeps_its_own_listening_opportunities():
    async def run():
        c,clock,_,sent,_,event,_=setup();c.available_phrases.update(VARIATIONS)
        for turn,text in enumerate((TEXT,*QUESTIONS[:2])):
            if turn:
                event('speech-update','assistant',status='started')
                clock.now+=.02
                event('transcript','assistant',transcriptType='final',transcript='A diagnostic checks the vehicle concern. Tell me more.')
                clock.now+=.02
                event('speech-update','assistant',status='stopped')
                event('speech-update','user',status='started')
                clock.now+=8
            event('transcript','user',transcriptType='partial',transcript=text)
            await drain(c)
            assert len(sent)==turn+1
            event('speech-update','user',status='stopped');clock.now+=1
        await c.close()
    asyncio.run(run())
