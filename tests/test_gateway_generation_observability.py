import copy
from datetime import datetime, timezone
import json
from uuid import uuid4

import pytest

from app.generation_observability import (GATEWAY_VERSION, GenerationEvidenceError,
    build_gateway_generation_runs, project_gateway_timings)


def row():
    generation = str(uuid4())
    return {'generation_id': generation, 'request_id': uuid4().hex, 'call_key': 'a'*16,
        'started_at_unix': 1792527910.629, 'milestones_ms': {'first_text': 125.25, 'total': 350.5},
        'generation_sequence': 17, 'generation_sequence_scope': 'process_local_http_arrival_only',
        'predecessor_generation_id': str(uuid4()), 'predecessor_relation': 'active_or_draining_owner',
        'superseded_by_generation_id': str(uuid4()), 'supersession_cause': 'newer_local_http_arrival',
        'superseded_at_ms': 300, 'payload_released_at_ms': 125.25,
        'payload_release_scope': 'gateway_yield_not_provider_receipt_or_playback',
        'downstream_disconnect_at_ms': None, 'checkpoint_rejections': 1,
        'generation_terminal_state': 'superseded_after_release',
        'sse_response_id': 'chatcmpl-' + uuid4().hex, 'neutral_sse_response_id': None,
        'route': 'gemini', 'outcome': 'superseded_after_release'}


def destination():
    root = str(uuid4())
    return dict(root_run_id=root, root_dotted_order='20261020T202510629000Z'+root, project_id=str(uuid4()))


def test_explicit_generation_and_sse_lineage_retained_without_inferred_join():
    source = row()
    result = project_gateway_timings([source])
    observed = result['generations'][0]
    assert observed['generation_id'] == source['generation_id']
    assert observed['predecessor_generation_id'] == source['predecessor_generation_id']
    assert observed['sse_response_id'] == source['sse_response_id']
    assert observed['generation_sequence_scope'] == 'process_local_http_arrival_only'
    assert observed['payload_release_scope'] == 'gateway_yield_not_provider_receipt_or_playback'
    assert observed['ended_at_unix'] == source['started_at_unix'] + 0.3505
    assert result['coverage']['provider_turn_or_tts_generation_join'] == 'unavailable_without_explicit_shared_wire_id'
    assert result['coverage']['audible_interruption_count'] is None


@pytest.mark.parametrize('field,code', [
    ('narration_reason','verified_receipt'),
    ('narration_reason','presentation_missing_or_changed'),
    ('jev_reason','not_first_user_turn'),
    ('jev_reason','action_or_complex_request'),
    ('eof_drain_outcome','eof'),
])
def test_recorded_fixed_decision_codes_survive_projection_and_span_export(field,code):
    source=row();source[field]=code
    projected=project_gateway_timings([source])
    child=build_gateway_generation_runs(projected,**destination())['runs'][0]
    assert projected['generations'][0][field]==code
    assert child['outputs']['observed_generation'][field]==code
    assert child['outputs']['phone_playback_verified'] is False


@pytest.mark.parametrize('value',['private_customer_name','Bearer PRIVATE KEY',{'code':'verified_receipt'},3,None])
def test_decision_codes_never_copy_unknown_or_private_values(value):
    source=row();source.update(narration_reason=value,jev_reason=value,eof_drain_outcome=value)
    result=build_gateway_generation_runs(project_gateway_timings([source]),**destination())
    observed=result['runs'][0]['outputs']['observed_generation']
    assert all(observed[key] is None for key in ('narration_reason','jev_reason','eof_drain_outcome'))
    assert 'private_customer_name' not in json.dumps(result)
    assert 'PRIVATE KEY' not in json.dumps(result)


def test_conflicting_decision_codes_do_not_silently_rewrite_one_generation():
    source=row();source['jev_reason']='not_first_user_turn'
    changed=copy.deepcopy(source);changed['jev_reason']='verified_static_faq'
    result=project_gateway_timings([source,changed])
    assert result['generations']==[]
    assert result['coverage']['skipped_row_counts']=={'conflicting_generation_identity':1}


def test_legacy_frozen_projection_keeps_absent_optional_codes_on_export_retry():
    projected=project_gateway_timings([row()]);target=destination()
    original=build_gateway_generation_runs(projected,**target)
    cached=json.loads(json.dumps(projected))
    retried=build_gateway_generation_runs(cached,**target)
    assert retried==original
    optional={'narration_reason','jev_reason','eof_drain_outcome','backchannel'}
    assert not optional.intersection(cached['generations'][0])
    assert not optional.intersection(retried['runs'][0]['outputs']['observed_generation'])


def test_backchannel_evidence_survives_repeated_projection_and_langsmith_export():
    source=row()
    source['backchannel']={'phrase_id':'moment','reason':'emitted','emitted_ms':55,
        'classification_ms':28.5,'probability':.999,'confidence':.99,'requests':1,
        'policy_elapsed_ms':31.5,'language_choice':'cache_compatible',
        'language_probability':1.0,'language_confidence':.999,'audible_playback':'unverified'}
    projected=project_gateway_timings([source]);target=destination()
    observed=projected['generations'][0]['backchannel']
    assert observed['emitted_ms']==55
    assert observed['classification_ms']==28.5
    assert observed['requests']==1 and observed['probability']==.999
    assert observed['language_choice']=='cache_compatible'
    assert observed['evidence_scope']=='gateway_yield_not_audible_playback'
    assert 'audible_playback' not in observed
    assert project_gateway_timings(projected['generations'])==projected
    child=build_gateway_generation_runs(projected,**target)['runs'][0]
    assert child['outputs']['observed_generation']['backchannel']==observed
    assert child['outputs']['phone_playback_verified'] is False


def test_backchannel_projection_drops_unknown_text_and_invalid_typed_values():
    source=row();private='PRIVATE CUSTOMER CREDENTIAL'
    source['backchannel']={'phrase_id':private,'reason':private,'emitted_ms':True,
        'classification_ms':float('inf'),'probability':1.1,'confidence':-1,
        'requests':True,'language_choice':private,'text':private,'audio_url':private}
    exported=build_gateway_generation_runs(project_gateway_timings([source]),**destination())
    assert private not in json.dumps(exported)
    cue=exported['runs'][0]['outputs']['observed_generation']['backchannel']
    assert cue.get('phrase_id') is None and cue.get('emitted_ms') is None
    assert 'text' not in cue and 'audio_url' not in cue


@pytest.mark.parametrize('emitted',[350.501,-1,float('nan')])
def test_backchannel_emission_outside_observed_generation_is_not_an_inferred_instant(emitted):
    source=row();source['backchannel']={'phrase_id':'moment','reason':'emitted','emitted_ms':emitted}
    projected=project_gateway_timings([source])
    cue=projected['generations'][0]['backchannel']
    assert cue.get('emitted_ms') is None
    assert project_gateway_timings(projected['generations'])==projected


def test_conflicting_backchannel_evidence_excludes_generation_instead_of_silent_overwrite():
    source=row();source['backchannel']={'phrase_id':'moment','reason':'emitted','emitted_ms':55}
    changed=copy.deepcopy(source);changed['backchannel']['emitted_ms']=60
    result=project_gateway_timings([source,changed])
    assert not result['generations']
    assert result['coverage']['skipped_row_counts']=={'conflicting_generation_identity':1}


def test_old_request_id_never_becomes_a_generation_identity():
    source = row();source.pop('generation_id')
    result = project_gateway_timings([source])
    assert not result['generations'] and result['coverage']['state'] == 'unavailable'
    assert result['coverage']['skipped_row_counts'] == {'persisted_generation_identity_unavailable': 1}
    assert not build_gateway_generation_runs(result, **destination())['runs']


def test_model_child_uses_actual_times_is_deterministic_and_never_a_fake_model_call():
    source = row();projected = project_gateway_timings([source]);target = destination()
    result = build_gateway_generation_runs(projected, **target)
    assert result == build_gateway_generation_runs(projected, **target)
    child = result['runs'][0]
    assert child['start_time'] == datetime.fromtimestamp(source['started_at_unix'], timezone.utc).isoformat(timespec='microseconds')
    assert child['end_time'] == datetime.fromtimestamp(source['started_at_unix']+0.3505, timezone.utc).isoformat(timespec='microseconds')
    assert child['parent_run_id'] == child['trace_id'] == target['root_run_id']
    assert child['run_type'] == 'chain' and child['error'] is None
    assert child['outputs']['provider_model_span_id'] is None and child['outputs']['tts_generation_id'] is None
    assert not child['outputs']['late_phone_packet_discard_verified']
    assert not child['outputs']['phone_playback_verified']


def test_projection_and_builder_drop_untrusted_private_text():
    source = row();private = 'PRIVATE CUSTOMER SECRET TEXT'
    source.update(headers={'Authorization': private}, transcript=private, audio=private,
        provider_model_span_id=private, tts_generation_id=private)
    source['milestones_ms']['private_text'] = private
    projected = project_gateway_timings([source])
    projected['generations'][0].update(transcript=private, identity_basis=private, timing_basis=private)
    projected['coverage']['interpretation'] = private
    result = build_gateway_generation_runs(projected, **destination())
    assert private not in json.dumps(result)


def test_invalid_optional_fields_are_unknown_and_not_exposed():
    source = row();source.update(predecessor_generation_id='a private name', sse_response_id='secret-url',
        generation_sequence=True, generation_sequence_scope='provider_semantic_order',
        generation_terminal_state='private text', payload_released_at_ms=351)
    value = project_gateway_timings([source])['generations'][0]
    for key in ('predecessor_generation_id', 'sse_response_id', 'generation_sequence',
                'generation_sequence_scope', 'generation_terminal_state', 'payload_released_at_ms'):
        assert value[key] is None


@pytest.mark.parametrize('started,total', [(None, 1), (float('nan'), 1), (True, 1), (1792527910, -1),
    (1792527910, float('inf')), (1792527910, None), (4102444800, 1)])
def test_missing_or_invalid_actual_intervals_are_not_replaced(started, total):
    source = row();source['started_at_unix'] = started;source['milestones_ms']['total'] = total
    result = project_gateway_timings([source])
    assert result['generations'] == []
    assert result['coverage']['skipped_row_counts'] == {'actual_gateway_interval_unavailable': 1}


def test_duplicates_are_deduplicated_but_conflicting_ids_are_excluded():
    source = row()
    result = project_gateway_timings([source, copy.deepcopy(source)])
    assert len(result['generations']) == 1 and result['coverage']['exact_duplicate_rows_ignored'] == 1
    conflict = copy.deepcopy(source);conflict['milestones_ms']['total'] = 600
    result = project_gateway_timings([source, conflict])
    assert not result['generations']
    assert result['coverage']['skipped_row_counts']['conflicting_generation_identity'] == 1


def test_mixed_call_scopes_are_unavailable_instead_of_silently_joined():
    first, second = row(), row();second['call_key'] = 'b'*16
    result = project_gateway_timings([first, second])
    assert not result['generations'] and result['coverage']['state'] == 'unavailable'
    assert result['coverage']['skipped_row_counts']['conflicting_gateway_call_scopes'] == 2


@pytest.mark.parametrize('rows', [None, {}, [None]*5001])
def test_input_is_bounded_and_errors_never_echo_private_content(rows):
    with pytest.raises(GenerationEvidenceError, match='invalid_or_oversized_gateway_timings'):
        project_gateway_timings(rows)


def test_invalid_destination_is_rejected_before_span_creation():
    target = destination();target['root_dotted_order'] = 'invented'
    with pytest.raises(GenerationEvidenceError, match='invalid_gateway_generation_destination'):
        build_gateway_generation_runs(project_gateway_timings([row()]), **target)
