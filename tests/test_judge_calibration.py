"""Judge negative-control reporting without network/model execution."""
import asyncio
import json
import pytest
from evals.calibrate import controls, execute
from evals.native import build_control_payload
from evals.review import DIMENSIONS


def test_expected_labels_are_not_leaked_to_judge_input():
    data=controls({'hours':'Weekdays only','physical_address':None})
    assert len(data['cases'])==12
    assert {c['expected_verdict'] for c in data['cases']}=={'pass','fail'}
    for case in data['cases']:
        raw=json.dumps(case['package'])
        assert 'expected_verdict' not in raw and 'target_dimension' not in raw


def test_always_pass_judge_fails_negative_controls_and_native_scores_keep_failures(tmp_path):
    data=controls({'hours':'Weekdays only','physical_address':None})
    async def always_pass(client,key,package):
        return {'dimensions':{d:{'verdict':'pass','evidence_refs':['turn:1'],'reason':'Mock always passes.'} for d in DIMENSIONS},
                'criteria':{'criterion_01':{'verdict':'pass','evidence_refs':['turn:1'],'reason':'Mock.'}}}
    result=asyncio.run(execute(data,'synthetic-key',tmp_path,request=always_pass))
    assert result['matched']==5 and result['total']==12 and not result['all_controls_matched']
    payload=build_control_payload(data,result,label='servicevoice-judge-unit-test')
    assert sum(row['evaluation_scores'][0]['score'] for row in payload['results'])==5
    result['cases'][0]['matched']=True
    with pytest.raises(ValueError):build_control_payload(data,result,label='servicevoice-judge-unit-test')


def test_judge_transport_failure_is_not_a_passing_control(tmp_path):
    data=controls({'hours':'Weekdays only','physical_address':None})
    async def fails(*args):raise TimeoutError()
    result=asyncio.run(execute(data,'synthetic-key',tmp_path,request=fails))
    assert result['matched']==0 and result['requests_made']==12
    assert all(c['actual_verdict']=='unavailable' for c in result['cases'])
