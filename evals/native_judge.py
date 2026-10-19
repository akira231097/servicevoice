"""Reusable, bounded batching for native synthetic experiment judging.

Plan-only by default. Records each batch and a bound merged review without
modifying source reports. A missing/failed judgment stays unavailable. Provider
keys are used locally and never uploaded as LangSmith workspace secrets.
"""
from __future__ import annotations
import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from .judge import prepare,execute
from .review import validate_review,summarize_review
from .schema import digest

ROOT=Path(__file__).resolve().parents[1]


def combine(report, reviews, results):
    if not reviews or len(reviews)!=len(results):raise ValueError('missing_judge_batches')
    merged=deepcopy(reviews[0]);by_id={(c['id'],c['attempt']):c for c in merged['cases']}
    expected_outputs={(c['id'],c['attempt']):c['output_sha256'] for c in report['cases']}
    states=[];seen=set()
    for review,result in zip(reviews,results,strict=True):
        validate_review(report,review)
        if (result['report_sha256']!=digest(report) or result['run_id']!=report['run_id']
                or result['dataset_sha256']!=report['dataset']['sha256']
                or result['model_requested']!='gemini-3.8-flash'):raise ValueError('judge_batch_source_mismatch')
        source={(c['id'],c['attempt']):c for c in review['cases']}
        for state in result['cases']:
            identity=state['id'],state['attempt']
            if identity in seen or identity not in by_id:raise ValueError('duplicate_or_unknown_judge_case')
            if state['output_sha256']!=expected_outputs[identity]:raise ValueError('judge_output_digest_mismatch')
            seen.add(identity);by_id[identity].clear();by_id[identity].update(deepcopy(source[identity]));states.append(deepcopy(state))
    if seen!=set(by_id):raise ValueError('incomplete_judge_batch_coverage')
    merged['reviewer']['reviewed_at']=datetime.now(timezone.utc).isoformat()
    validate_review(report,merged)
    result={'schema_version':1,'run_id':report['run_id'],'report_sha256':digest(report),
        'dataset_sha256':report['dataset']['sha256'],'model_requested':'gemini-3.8-flash',
        'requests_made':sum(r['requests_made'] for r in results),'max_requests':len(states),
        'status':'advisory','release_ready':False,'cases':states,'summary':summarize_review(report,merged),
        'limitations':results[0]['limitations'],'batch_result_sha256':[digest(r) for r in results]}
    return merged,result


async def run(args):
    report=json.loads(args.report.read_text(encoding='utf-8'))
    raw=args.dataset.read_bytes();dataset=json.loads(raw)
    ids=[c['id'] for c in report['cases']]
    if not 1<=len(ids)<=40 or len(ids)!=len(set(ids)):raise ValueError('select_one_to_forty_unique_cases')
    if not report.get('source',{}).get('before')==report.get('source',{}).get('after'):raise ValueError('source_changed')
    # Validate evidence bindings before credential loading or provider work.
    for offset in range(0,len(ids),20):prepare(report,dataset,raw,ids[offset:offset+20])
    if not args.execute:
        print(json.dumps({'mode':'plan_only','cases':len(ids),'maximum_requests':len(ids),'provider_calls':0}));return 0
    from dotenv import dotenv_values
    key=(os.environ.get('GEMINI_API_KEY') or dotenv_values(ROOT/'.env',interpolate=False).get('GEMINI_API_KEY') or '').strip()
    if not key:raise ValueError('gemini_key_missing')
    directory=args.output or args.report.parent/'native-judge'
    directory.mkdir(parents=True,exist_ok=False)
    reviews=[];results=[]
    for number,offset in enumerate(range(0,len(ids),20),1):
        review,cases=prepare(report,dataset,raw,ids[offset:offset+20])
        batch=directory/f'batch-{number}';batch.mkdir()
        result=await execute(report,review,cases,key,len(cases),batch)
        reviews.append(review);results.append(result)
        print(json.dumps({'batch':number,'requests':result['requests_made'],'judged':sum(c['status']=='judged_advisory' for c in result['cases'])}),flush=True)
    review,result=combine(report,reviews,results)
    for name,value in [('judge-review.json',review),('judge-result.json',result)]:
        with (directory/name).open('x',encoding='utf-8') as f:json.dump(value,f,indent=2,ensure_ascii=False)
    valid=sum(c['status']=='judged_advisory' for c in result['cases'])
    print(json.dumps({'output':str(directory.resolve()),'cases':len(ids),'valid_judgments':valid,'release_ready':False}))
    return 0 if valid==len(ids) else 1


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--report',type=Path,required=True)
    p.add_argument('--dataset',type=Path,default=ROOT/'evals/datasets/servicevoice-v18-grounded-v1.json')
    p.add_argument('--output',type=Path);p.add_argument('--execute',action='store_true')
    return asyncio.run(run(p.parse_args()))

if __name__=='__main__':raise SystemExit(main())
