"""Contact-correction acceptance oracles exercised against real disposable tools.

The mock model controls utterance boundaries; no credential or provider is used.
Failures show that a plausible reply/successful tool cannot hide wrong stored
contacts, collateral changes, or confirmation in the correction turn.
"""
import asyncio
from copy import deepcopy
from datetime import date
import json
from pathlib import Path

import httpx
import pytest

from evals import run, schema
from scripts.evaluate_gemini import MODEL

DATASET = Path(__file__).resolve().parents[1] / 'evals/datasets/servicevoice-golden-v2.json'


def dataset():
    return schema.validate_dataset(json.loads(DATASET.read_text(encoding='utf-8')))


def case(cid):
    return next(c for c in dataset()['cases'] if c['id'] == cid)


def response(*, text=None, tool=None, arguments=None, index=1):
    message = {'role': 'assistant', 'content': text}
    if tool:
        message['tool_calls'] = [{'id': f'fixture-call-{index}', 'type': 'function',
                                  'function': {'name': tool, 'arguments': json.dumps(arguments)}}]
    return httpx.Response(200, json={'model': MODEL, 'choices': [
        {'message': message, 'finish_reason': 'tool_calls' if tool else 'stop'}]})


def execute(tmp_path, cid, *, corrected_phone='2025551076', corrected_vehicle='2021 Toyota Camry', premature=False):
    rendered = schema.render_case(case(cid), run.variables_for(date.today()))
    request_count = 0

    def provider(request):
        nonlocal request_count
        request_count += 1
        messages = json.loads(request.content)['messages']
        user_turn = sum(m['role'] == 'user' for m in messages)
        results = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
        if messages[-1]['role'] == 'tool':
            if premature and user_turn == 2 and results[-1].get('proposal_id'):
                return response(tool='confirm_action', arguments={'proposal_id': results[-1]['proposal_id'], 'confirmed': True}, index=request_count)
            return response(text='The verified local result is ready; the full details are in the tool evidence.')
        if user_turn == 1:
            return response(tool='prepare_first_available_booking', arguments={
                'strategy': 'first_available', 'service': 'oil_change', 'customer_name': 'Taylor Reed',
                'phone': '2025550706', 'vehicle': '2021 Toyota Camry'}, index=request_count)
        if user_turn == 2:
            initial = next(result for result in results if result.get('proposal_id'))
            return response(tool='prepare_booking', arguments={
                'service': 'oil_change', 'slot_id': initial['slot_id'], 'customer_name': 'Taylor Reed',
                'phone': corrected_phone, 'vehicle': corrected_vehicle}, index=request_count)
        current = next(result for result in reversed(results) if result.get('proposal_id'))
        return response(tool='confirm_action', arguments={'proposal_id': current['proposal_id'], 'confirmed': True}, index=request_count)

    async def invoke():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            return await run.execute_case(rendered, 1, client, 'SYNTHETIC-NOT-A-KEY', [10], tmp_path, {'cases': []})

    return asyncio.run(invoke())


def test_v2_preserves_every_historical_case_without_relabeling():
    parent = json.loads((DATASET.parent / 'servicevoice-golden-v1.json').read_text(encoding='utf-8'))
    current = dataset()
    index = {row['id']: row for row in current['cases']}
    assert current['dataset_id'] != parent['dataset_id'] and current['version'] != parent['version']
    assert all(index[old['id']] == old for old in parent['cases'])
    assert 'pending' in current['review_status']


@pytest.mark.parametrize('cid', ['SV-R011', 'SV-R012', 'SV-R013'])
def test_correct_phone_replacement_and_later_assent_satisfy_mechanical_oracles(tmp_path, cid):
    row = execute(tmp_path, cid)
    assert row['status'] == 'completed' and all(check['passed'] for check in row['checks'])
    states = row['outputs']['turn_states']
    assert states[0]['phone'] == '+12025550706' and states[1]['phone'] == '+12025551076'
    assert states[0]['appointments'] == states[1]['appointments'] == 0
    assert row['outputs']['state']['appointments'] == (1 if cid == 'SV-R012' else 0)
    assert row['semantic_review']['status'] == 'pending'
    # A matching database value and these placeholder mock replies do not grant
    # a semantic readback, human fluency or acoustic pass.


@pytest.mark.parametrize('cid', ['SV-R011', 'SV-R012', 'SV-R013'])
def test_transposed_corrected_phone_cannot_pass_even_when_tools_succeed(tmp_path, cid):
    row = execute(tmp_path, cid, corrected_phone='2025551706')
    assert all(tool['ok'] for tool in row['outputs']['tools'])
    failures = [check['name'] for check in row['checks'] if not check['passed']]
    assert 'turn_2.phone' in failures and 'final.phone' in failures


def test_phone_only_change_cannot_hide_collateral_vehicle_change(tmp_path):
    row = execute(tmp_path, 'SV-R011', corrected_vehicle='2022 Toyota Camry')
    assert all(tool['ok'] for tool in row['outputs']['tools'])
    assert not next(check['passed'] for check in row['checks'] if check['name'] == 'turn_2.vehicle')


@pytest.mark.parametrize('cid', ['SV-R011', 'SV-R012', 'SV-R013'])
def test_successful_confirmation_during_correction_fails_before_later_assent(tmp_path, cid):
    row = execute(tmp_path, cid, premature=True)
    assert row['outputs']['turn_states'][1]['appointments'] == 1
    assert not next(check['passed'] for check in row['checks'] if check['name'] == 'turn_2.appointments')
    assert not next(check['passed'] for check in row['checks'] if check['name'] == 'confirmation_requires_prior_turn_proposal.turn_2')
