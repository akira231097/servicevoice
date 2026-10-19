"""Bounded optional JEV benchmark on synthetic labeled fixtures. No live routing."""
from __future__ import annotations
import argparse
import asyncio
from collections import Counter
from dataclasses import asdict
from datetime import datetime,timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import time
from dotenv import dotenv_values

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.jev_classifier import JevClassifier,ClassificationUnavailable,JEV_MODEL,LABELS,request_payload,route_candidate

async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset',required=True,type=Path)
    parser.add_argument('--label',required=True)
    parser.add_argument('--max-requests',type=int,default=80)
    args=parser.parse_args()
    if not re.fullmatch(r'[a-z0-9-]{1,60}',args.label) or not 1<=args.max_requests<=100:parser.error('Invalid label or bound')
    raw=args.dataset.read_bytes();dataset=json.loads(raw)
    cases=dataset if isinstance(dataset,list) else dataset['cases']
    if not cases or len(cases)>args.max_requests:parser.error('Fixture count exceeds request cap')
    for case in cases:
        request_payload(case['state'])
        if (not isinstance(case.get('id'),str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}',case['id'])
            or case.get('expected_label') not in LABELS or case.get('expected_handler') not in {'static_faq','gemini'}
            or case.get('expected_label')=='other' and case.get('expected_handler')=='static_faq'):
            parser.error('Invalid fixture label, handler or ID')
    if len({c['id'] for c in cases})!=len(cases):parser.error('Duplicate case IDs')
    key=dotenv_values(ROOT/'.env').get('TYPESAFE_API_KEY')
    if not key:parser.error('TYPESAFE_API_KEY is not configured locally')
    output=ROOT/'docs'/f'jev-benchmark-{args.label}.json'
    with output.open('x',encoding='utf-8') as f:f.write('{}\n')
    report={'started_at':datetime.now(timezone.utc).isoformat(),'status':'running','model':JEV_MODEL,
        'dataset_sha256':hashlib.sha256(raw).hexdigest(),'client_sha256':hashlib.sha256((ROOT/'app/jev_classifier.py').read_bytes()).hexdigest(),
        'questions':request_payload(cases[0]['state'])['questions'],'maximum_requests':args.max_requests,'actual_requests':0,
        'thresholds':{'intent_top_probability':.99,'intent_margin':.98,'english_probability':.99,'candidate_deadline_ms':350,'measurement_timeout_ms':2000},
        'live_routing_enabled':False,'cases':[],
        'limitations':['Synthetic development corpus, not a representative accuracy benchmark or production certification.',
                      'Scores are not independent proof of truth, consent, safety or calibration.',
                      'Sequential warmed-client measurements include network; no retry, no Gemini fallback calls or voice calls.',
                      'The 350ms candidate routing budget is evaluated against a 2s measurement timeout; this is not a live deadline implementation.']}
    def checkpoint():output.write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    client=JevClassifier(key,timeout_seconds=2)
    try:
        for case in cases:
            began=time.monotonic();report['actual_requests']+=1
            row={'id':case['id'],'state':request_payload(case['state'])['state'],'expected_label':case['expected_label'],'expected_handler':case['expected_handler'],
                 'handler':'gemini','false_static_acceptance':False,'status':'attempting'}
            report['cases'].append(row);checkpoint()
            try:
                decision=await client.classify(case['state'])
                handler,reason=route_candidate(case['state'],decision)
                row.update(status='completed',decision=asdict(decision),handler=handler,route_reason=reason,
                    label_correct=decision.label==case['expected_label'],
                    false_static_acceptance=handler=='static_faq' and (case['expected_handler']!='static_faq' or decision.label!=case['expected_label']))
            except ClassificationUnavailable as error:
                row.update(status='unavailable',error=error.code)
            except asyncio.CancelledError:
                row.update(status='interrupted',error='interrupted');report['status']='interrupted'
                raise
            except Exception:
                row.update(status='failed',error='unexpected_error');report['status']='failed'
                raise
            finally:
                row['elapsed_ms']=round((time.monotonic()-began)*1000,3);checkpoint()
            print(json.dumps({'case':row['id'],'handler':row['handler'],'error':row.get('error'),'false_static_acceptance':row['false_static_acceptance']}),flush=True)
            if row.get('error') in {'provider_http_401','provider_http_403','provider_http_429','provider_http_529'}:
                report['status']='stopped_provider_error';break
        else:report['status']='completed'
    finally:
        try:
            await asyncio.wait_for(client.close(),timeout=2)
        except (Exception,asyncio.CancelledError):
            report['cleanup']='not_confirmed'
        finally:
            if report['status']=='running':report['status']='incomplete'
            values=sorted(c['decision']['elapsed_ms'] for c in report['cases'] if 'decision' in c)
            all_values=sorted(c['elapsed_ms'] for c in report['cases'] if 'elapsed_ms' in c)
            def distribution(values):
                pct=lambda q:values[max(0,math.ceil(len(values)*q)-1)] if values else None
                return {'p50':pct(.5),'p95':pct(.95),'max':max(values) if values else None}
            accepted=[c for c in report['cases'] if c['handler']=='static_faq']
            report['summary']={'valid_responses':len(values),'errors':sum('error' in c for c in report['cases']),
                'timeouts':sum(c.get('error')=='timeout' for c in report['cases']),
                'label_correct':sum(c.get('label_correct',False) for c in report['cases']),
                'static_accepted':len(accepted),'false_static_acceptances':sum(c['false_static_acceptance'] for c in report['cases']),
                'successful_latency_ms':distribution(values),'all_attempt_latency_ms':distribution(all_values),
                'fallback_reasons':dict(Counter(c.get('route_reason',c.get('error','incomplete')) for c in report['cases'] if c['handler']=='gemini'))}
            report['finished_at']=datetime.now(timezone.utc).isoformat();checkpoint()
    print(json.dumps({'output':str(output),'status':report['status'],'summary':report['summary']}))

if __name__=='__main__':asyncio.run(main())
