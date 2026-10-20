"""A component trace must preserve recorded timing/correlation without content."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4

import pytest

from app.evaluation_spans import build_component_runs


ROOT='4aa3c2fa-a6a2-4a3f-a2e0-f19b58dd7534'
PROJECT='aed7dd11-c235-46a1-982f-a1454c4fbf40'
ORDER='20261020T170000000000Z'+ROOT
START=datetime(2026,10,20,17,tzinfo=timezone.utc).timestamp()
KEY=hashlib.sha256(b'private-call-identity').hexdigest()
TURN='turn_'+'a'*24
OPERATION='op_'+'b'*24
REQUEST='32746941-9035-4b8e-9c45-f08c05acfba9'


def event(**changes):
    result={'event_id':str(uuid4()),'config_id':'c'*64,'call_key':KEY,'component':'gateway',
        'event':'request_finished','outcome':'success','turn_id':TURN,'request_id':REQUEST,
        'operation_id':None,'start_unix':START+1,'duration_ms':100,'attributes':{'http_status':200}}
    result.update(changes)
    return result


def build(events,**changes):
    settings={'root_run_id':ROOT,'root_dotted_order':ORDER,'project_id':PROJECT};settings.update(changes)
    return build_component_runs({'call_key':KEY,'events':events},**settings)


def event_runs(result):
    return [r for r in result['runs'] if r['extra']['metadata']['record_kind']=='component_event']


def group_runs(result,kind):
    return [r for r in result['runs'] if r['name']=='voice.'+kind+'.observed_events']


def test_actual_event_timing_and_stable_retry_payloads_are_preserved():
    rows=[event(component='llm',event='generation_finished',duration_ms=12.5)]
    first=build(rows)
    assert first==build(deepcopy(rows))
    run=event_runs(first)[0]
    assert run['start_time']=='2026-10-20T17:00:01.000000+00:00'
    assert run['end_time']=='2026-10-20T17:00:01.012500+00:00'
    assert run['run_type']=='llm' and run['trace_id']==ROOT
    assert run['extra']['metadata']['configuration_id']=='c'*64
    assert run['extra']['metadata']['provider_effective_configuration']=='unknown'


def test_parentage_uses_explicit_turn_request_and_operation_ids_in_parent_first_order():
    result=build([event(component='tool_harness',event='execution_finished',operation_id=OPERATION)])
    turn=group_runs(result,'turn')[0];request=group_runs(result,'request')[0];operation=group_runs(result,'operation')[0]
    actual=event_runs(result)[0]
    assert turn['parent_run_id']==ROOT
    assert request['parent_run_id']==turn['id']
    assert operation['parent_run_id']==request['id']
    assert actual['parent_run_id']==operation['id']
    positions={r['id']:i for i,r in enumerate(result['runs'])}
    for row in result['runs']:
        if row['parent_run_id']!=ROOT:
            parent=result['runs'][positions[row['parent_run_id']]]
            assert positions[parent['id']]<positions[row['id']]
            assert row['dotted_order'].startswith(parent['dotted_order']+'.')
        assert row['trace_id']==ROOT and row['session_id']==PROJECT


def test_turn_groups_are_observed_envelopes_not_invented_audio_or_execution_intervals():
    result=build([event(start_unix=START+1,duration_ms=10),event(start_unix=START+5,duration_ms=20)])
    turn=group_runs(result,'turn')[0]
    assert turn['start_time']=='2026-10-20T17:00:01.000000+00:00'
    assert turn['end_time']=='2026-10-20T17:00:05.020000+00:00'
    assert turn['extra']['metadata']['grouping_only'] is True
    assert turn['extra']['metadata']['timing_basis']=='observed_event_envelope_not_component_execution'
    assert 'not_audio_turn_boundary' in turn['extra']['metadata']['turn_basis']
    assert result['coverage']['complete_component_visibility'] is False


def test_late_authoritative_receipt_joins_only_recorded_operation_and_extends_past_call():
    pending=event(component='tool_harness',event='execution_finished',outcome='pending',operation_id=OPERATION,
        start_unix=START+2,duration_ms=250,attributes={'receipt_present':False})
    receipt=event(component='scheduler',event='receipt_observed',operation_id=OPERATION,request_id=None,
        start_unix=START+70,duration_ms=4,attributes={'receipt_present':True,'evidence_source':'tool_receipt'})
    result=build([pending,receipt]);operation=group_runs(result,'operation')[0]
    # The late receipt has no request id: no request is guessed from timing/tool name.
    assert operation['parent_run_id']==group_runs(result,'turn')[0]['id']
    assert operation['end_time']=='2026-10-20T17:01:10.004000+00:00'
    assert {r['parent_run_id'] for r in event_runs(result)}=={operation['id']}
    assert result['coverage']['root_time_containment_enforced'] is False
    assert any(r['outputs']['measurements'].get('receipt_present') is True for r in event_runs(result))


def test_conflicting_turn_correlations_do_not_invent_single_turn_parentage():
    result=build([event(operation_id=OPERATION),event(operation_id=OPERATION,turn_id='turn_'+'d'*24)])
    request=group_runs(result,'request')[0];operation=group_runs(result,'operation')[0]
    assert request['parent_run_id']==ROOT
    assert operation['parent_run_id']==request['id']
    assert len(group_runs(result,'turn'))==2


def test_no_correlation_is_guessed_for_unrelated_events_or_invalid_optional_identifiers():
    row=event(turn_id='private caller name',request_id='private url',operation_id='private phone')
    result=build([row]);assert len(result['runs'])==1
    assert event_runs(result)[0]['parent_run_id']==ROOT
    assert 'private' not in json.dumps(result)


def test_component_export_reprojects_private_arbitrary_payloads_and_fixed_failure_reason():
    row=event(component='tts',event='tts.failed',outcome='failure',attributes={
        'error_code':'tts_upstream_rate_limited','response_audio_bytes':0,'exception':'SECRET 5125550142',
        'transcript':'PRIVATE CUSTOMER','url':'https://private/?api_key=SECRET'})
    row['raw_prompt']='PRIVATE PROMPT';row['customer_name']='PRIVATE CUSTOMER'
    result=build([row]);serialized=json.dumps(result)
    for forbidden in ('SECRET','5125550142','PRIVATE CUSTOMER','PRIVATE PROMPT','https://private','private-call-identity'):
        assert forbidden not in serialized
    run=event_runs(result)[0]
    assert run['outputs']['measurements']['error_code']=='tts_upstream_rate_limited'
    assert run['error']=='recorded_component_event_failure'


@pytest.mark.parametrize('changes,reason',[
    ({'start_unix':None},'missing_or_invalid_start_time'),
    ({'start_unix':True},'missing_or_invalid_start_time'),
    ({'duration_ms':None},'missing_or_invalid_duration'),
    ({'duration_ms':-1},'missing_or_invalid_duration'),
    ({'duration_ms':float('nan')},'missing_or_invalid_duration'),
    ({'call_key':'e'*64},'different_call_or_invalid_call_key'),
    ({'component':{}},'invalid_event_contract'),
])
def test_missing_or_invalid_evidence_is_skipped_explicitly_without_fabricated_timestamps(changes,reason):
    result=build([event(**changes)])
    assert result['runs']==[] and result['coverage']['skipped_event_counts'][reason]==1


def test_recorded_zero_duration_is_valid_but_absent_duration_is_never_assumed_zero():
    result=build([event(duration_ms=0,turn_id=None,request_id=None)])
    row=event_runs(result)[0]
    assert row['start_time']==row['end_time'] and result['coverage']['exported_event_spans']==1


def test_duplicate_event_retry_deduplicates_and_conflicting_event_identity_is_quarantined():
    row=event();result=build([row,deepcopy(row)])
    assert result['coverage']['exported_event_spans']==1
    assert result['coverage']['exact_duplicate_events_ignored']==1
    changed={**row,'outcome':'failure'}
    result=build([row,changed])
    assert result['runs']==[] and result['coverage']['skipped_event_counts']['conflicting_event_identity']==1


def test_configuration_change_remains_recorded_identity_metadata_not_fake_execution_stage():
    result=build([event(),event(config_id='f'*64,start_unix=START+3)])
    assert group_runs(result,'turn')[0]['extra']['metadata']['configuration_ids']==['c'*64,'f'*64]
    assert {r['extra']['metadata']['configuration_id'] for r in event_runs(result)}=={'c'*64,'f'*64}
    assert not any(r['name'].startswith('voice.configuration') for r in result['runs'])


@pytest.mark.parametrize('changes',[{'root_run_id':'private'},{'project_id':'private'},
    {'root_dotted_order':'private'},{'root_dotted_order':'20261020T170000000000Z'+PROJECT}])
def test_invalid_or_different_root_scope_is_rejected(changes):
    with pytest.raises(ValueError):build([event()],**changes)
