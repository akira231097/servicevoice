from app.conversation_metrics import reply_lengths
from app.call_review_presentation import feedback_rows
import pytest

@pytest.mark.parametrize('tool',['prepare_booking','begin_service_request','search_availability'])
def test_greeting_and_hold_do_not_lower_reply_length_and_only_verified_readbacks_are_exempt(tool):
    data={'turn:1':{'sequence':0,'role':'assistant','text':'Hello, this is the demo.'},
        'turn:2':{'sequence':1,'role':'user','text':'What are your hours?'},
        'turn:3':{'sequence':2,'role':'assistant','text':'One moment.'},
        'turn:4':{'sequence':3,'role':'assistant','text':'We close at five.'},
        'turn:5':{'sequence':4,'role':'user','text':'Please prepare that.'},
        'tool:1':{'sequence':5,'name':tool,'gateway_issuance_verified':True,'server_result':{'ok':True}},
        'turn:6':{'sequence':6,'role':'assistant','text':'Detail '*40}}
    result=reply_lengths(data)
    assert result['ordinary_replies']==1 and result['median_words']==4
    assert result['readback_or_options_replies']==1 and result['within_20_words_fraction']==1
    data['tool:1']['gateway_issuance_verified']=False
    result=reply_lengths(data)
    assert result['ordinary_replies']==2 and result['maximum_words']==40 and result['within_20_words_fraction']==.5
    feedback=list(feedback_rows('fixture',{'reply_length':result}))
    assert next(r for r in feedback if r['key']=='reply_length_within_target')['score']==.5
    assert 'Detail' not in str(result) and 'Detail' not in str(feedback)

def test_missing_replies_are_unknown_not_perfect():
    assert reply_lengths({})['within_20_words_fraction'] is None

def test_idle_checkins_are_not_counted_as_additional_answer_words():
    evidence={'turn:1':{'role':'user','text':'Hello','sequence':0},
        'turn:2':{'role':'assistant','text':'How can I help?','sequence':1},
        'turn:3':{'role':'assistant','text':"Take your time, I'm still here.",'sequence':2},
        'turn:4':{'role':'assistant','text':'Are you still there?','sequence':3}}
    assert reply_lengths(evidence)['maximum_words']==4
