"""Judge batch merging must not lose failures, missing cases, or bindings."""
from copy import deepcopy
import pytest
from evals.native_judge import combine
from evals.schema import digest
from test_native_evaluations import bundle


def records():
    _,report,review,result,_=bundle()
    # Use the strict review contract, including completed criterion evidence.
    review['reviewer']={'kind':'judge','name':'Synthetic unit judge','reviewed_at':'2026-10-19T12:00:00+00:00'}
    for c in review['cases']:
        for criterion in c['criteria']:criterion.update(verdict='pass',evidence='turn:1: Synthetic evidence.')
    result.update(requests_made=1,limitations=['Unit control.'])
    return report,review,result


def test_valid_merge_keeps_source_and_advisory_authority():
    report,review,result=records()
    merged,summary=combine(report,[review],[result])
    assert merged['reviewer']['kind']=='judge' and summary['release_ready'] is False
    assert summary['report_sha256']==digest(report)
    assert summary['cases'][0]['output_sha256']==report['cases'][0]['output_sha256']


def test_duplicate_batch_cannot_double_count_success():
    report,review,result=records()
    with pytest.raises(ValueError,match='duplicate'):combine(report,[review,review],[result,result])


def test_missing_or_wrong_source_batches_fail_closed():
    report,review,result=records()
    missing=deepcopy(result);missing['cases']=[]
    with pytest.raises(ValueError,match='incomplete'):combine(report,[review],[missing])
    wrong=deepcopy(result);wrong['report_sha256']='a'*64
    with pytest.raises(ValueError,match='source_mismatch'):combine(report,[review],[wrong])
