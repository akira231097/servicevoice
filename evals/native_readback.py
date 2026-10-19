"""Read back only the exact datasets/experiments in native upload receipts."""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path

from .langsmith import upload_settings, write_new
from .schema import digest


def verify_rows(payload, receipt, examples):
    expected={r['row_id']:r for r in payload['results']}
    checks={'exact_example_coverage':len(examples)==len(expected) and {e['id'] for e in examples}==set(expected),
        'dataset_and_experiment_scope':True,'inputs_and_references_match':True,'actual_outputs_match':True,
        'all_feedback_scores_match':True,'feedback_comments_present':True}
    failures=[];counts=defaultdict(lambda:{'pass':0,'total':0,'uncertain':0,'unavailable':0})
    for example in examples:
        if example['id'] not in expected:
            failures.append('unexpected_example');continue
        row=expected[example['id']]
        runs=example.get('runs') or []
        scope=example['dataset_id']==receipt['dataset_id'] and len(runs)==1 and runs[0]['session_id']==receipt['experiment_id']
        checks['dataset_and_experiment_scope'] &= scope
        if not scope:
            failures.append(row['run_name']+':scope');continue
        run=runs[0]
        checks['inputs_and_references_match'] &= example['inputs']==row['inputs'] and example['outputs']==row['expected_outputs']
        checks['actual_outputs_match'] &= run.get('outputs')==row['actual_outputs']
        by_key=defaultdict(list)
        for score in row['evaluation_scores']:
            if score.get('score') is not None:by_key[score['key']].append(score)
        observed=run.get('feedback_stats') or {}
        for key, values in by_key.items():
            scores=[s['score'] for s in values]
            feedback=observed.get(key) or {}
            ok=feedback.get('n')==len(scores) and type(feedback.get('avg')) in (int,float) and math.isclose(feedback['avg'],sum(scores)/len(scores),abs_tol=1e-9)
            checks['all_feedback_scores_match'] &= ok
            comments=[s['comment'] for s in values if s.get('comment')]
            checks['feedback_comments_present'] &= not comments or bool(feedback.get('comments'))
            if not ok:failures.append(row['run_name']+':'+key)
            counts[key]['pass']+=sum(s==1 for s in scores);counts[key]['total']+=len(scores)
            counts[key]['uncertain']+=sum(s.get('value')=='uncertain' for s in values)
            counts[key]['unavailable']+=sum(s.get('value')=='unavailable' for s in values)
    return {'passed':all(checks.values()),'checks':checks,'failures':failures,'example_count':len(examples),
        'feedback_counts':dict(counts),'dataset_url':receipt['dataset_url'],'experiment_url':receipt['experiment_url'],
        'dataset_name':receipt['dataset_name'],'experiment_name':receipt['experiment_name']}


def load_payload(receipt_path,receipt):
    candidates=[]
    for file in receipt_path.parent.glob('native-langsmith-preview-*.json'):
        payload=json.loads(file.read_text(encoding='utf-8'))
        if digest(payload)==receipt['payload_sha256']:candidates.append(payload)
    if not candidates:raise ValueError('receipt_payload_missing')
    return candidates[0]


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--receipt',type=Path,action='append',required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--execute',action='store_true');args=p.parse_args()
    if not args.execute:
        print(json.dumps({'mode':'readback_plan','receipts':len(args.receipt),'network_requests':0}));return 0
    import httpx
    key,endpoint,workspace=upload_settings()
    result={'kind':'native_langsmith_exact_experiment_readback','checked_at':datetime.now(timezone.utc).isoformat(),
        'read_only':True,'billing_changed':False,'experiments':[]}
    with httpx.Client(base_url=endpoint,headers={'X-API-Key':key,'X-Tenant-Id':workspace,'Accept':'application/json'},timeout=25,follow_redirects=False) as client:
        def fetch(method,path,**kwargs):
            with client.stream(method,path,**kwargs) as response:
                if response.status_code!=200:raise ValueError('scoped_readback_http_error')
                raw=bytearray()
                for part in response.iter_bytes():
                    raw.extend(part)
                    if len(raw)>8*1024*1024:raise ValueError('readback_size_limit')
            return json.loads(raw)
        for path in args.receipt:
            receipt=json.loads(path.read_text(encoding='utf-8'));payload=load_payload(path,receipt)
            dataset=fetch('GET','/api/v1/datasets/'+receipt['dataset_id'])
            project=fetch('GET','/api/v1/sessions/'+receipt['experiment_id'],params={'include_stats':'true'})
            assert dataset['id']==receipt['dataset_id'] and dataset['tenant_id']==workspace and dataset['name']==receipt['dataset_name']
            assert project['id']==receipt['experiment_id'] and project['tenant_id']==workspace and project['name']==receipt['experiment_name'] and project['reference_dataset_id']==receipt['dataset_id']
            examples=fetch('POST','/api/v1/datasets/'+receipt['dataset_id']+'/runs',json={'session_ids':[receipt['experiment_id']],'offset':0,'limit':100,'preview':False})
            report=verify_rows(payload,receipt,examples)
            report['project_and_dataset_identity_verified']=True
            result['experiments'].append(report)
    result['passed']=all(r['passed'] for r in result['experiments'])
    write_new(args.output,result)
    print(json.dumps({'passed':result['passed'],'experiments':[{'name':r['experiment_name'],'rows':r['example_count'],'passed':r['passed']} for r in result['experiments']],'output':str(args.output)}))
    return 0 if result['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
