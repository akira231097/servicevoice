"""A successful upload is insufficient: native rows and feedback must reconcile."""
from copy import deepcopy
import pytest
from evals.native_readback import verify_rows


def sample():
    receipt={'dataset_id':'dataset','experiment_id':'experiment','dataset_url':'https://example.test/dataset',
        'experiment_url':'https://example.test/experiment','dataset_name':'synthetic','experiment_name':'v18'}
    expected={'row_id':'example','run_name':'CASE-001','inputs':{'question':'Synthetic'},
        'expected_outputs':{'answer':'Verified'},'actual_outputs':{'answer':'Verified'},
        'evaluation_scores':[{'key':'judge_grounding','score':0,'value':'uncertain','comment':'Insufficient evidence.'}]}
    payload={'results':[expected]}
    example={'id':'example','dataset_id':'dataset','inputs':expected['inputs'],'outputs':expected['expected_outputs'],
        'runs':[{'session_id':'experiment','outputs':expected['actual_outputs'],
            'feedback_stats':{'judge_grounding':{'n':1,'avg':0.0,'comments':['Insufficient evidence.']}}}]}
    return payload,receipt,[example]


def test_readback_reconciles_uncertain_feedback_without_upgrading_it():
    result=verify_rows(*sample())
    assert result['passed']
    assert result['feedback_counts']['judge_grounding']=={'pass':0,'total':1,'uncertain':1,'unavailable':0}


@pytest.mark.parametrize('mutation',[
    lambda rows:rows.clear(),
    lambda rows:rows[0].update(dataset_id='unrelated'),
    lambda rows:rows[0]['runs'][0].update(session_id='unrelated'),
    lambda rows:rows[0].update(inputs={'question':'Different'}),
    lambda rows:rows[0]['runs'][0].update(outputs={'answer':'Different'}),
    lambda rows:rows[0]['runs'][0]['feedback_stats']['judge_grounding'].update(avg=1.0),
    lambda rows:rows[0]['runs'][0]['feedback_stats']['judge_grounding'].update(n=0),
    lambda rows:rows[0]['runs'][0]['feedback_stats']['judge_grounding'].update(comments=[]),
])
def test_wrong_missing_or_greenwashed_remote_rows_fail_verification(mutation):
    payload,receipt,rows=sample();mutation(rows)
    assert verify_rows(payload,receipt,rows)['passed'] is False
