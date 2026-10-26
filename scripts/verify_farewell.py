"""Check that the native farewell's speaking interval precedes disconnect.

Content-free provider timing evidence, not proof of handset audio or semantics.
This complements caller-side acoustic inspection; a successful endCall alone
must never pass this check.
"""
import argparse
import json
import math
from pathlib import Path


def verify(logs):
    rows=sorted((r for r in logs if type(r.get('time')) in (int,float) and math.isfinite(r['time'])),key=lambda r:r['time'])
    inputs=[r['time'] for r in rows if r.get('body')=='Voice input' and
        (r.get('attributes',{}).get('text') or r.get('attributes',{}).get('input'))=='Thank you for calling. Goodbye.']
    result={'passed':False,'scope':'Provider farewell timing; caller-side audio is verified separately.'}
    if not inputs:
        # Current provider logs omit Voice input. A typed native endCall start
        # can anchor teardown timing, but cannot establish farewell content.
        inputs=[r['time'] for r in rows if r.get('body')=='Tool execution started'
            and r.get('attributes',{}).get('toolType')=='endCall'
            and r.get('attributes',{}).get('toolNames')==['endCall']]
        result['anchor']='native_end_call_execution'
    else:
        result['anchor']='exact_farewell_voice_input'
    if not inputs:return {**result,'reason':'farewell_input_missing'}
    start=inputs[-1];ends=[r['time'] for r in rows if r.get('body')=='call.ended' and r['time']>=start]
    if not ends:return {**result,'reason':'disconnect_evidence_missing'}
    end=ends[0];opened=None;spans=[];late=[]
    for r in rows:
        if r['time']<start:continue
        if r.get('body')=='Bot started speaking':
            if r['time']>end:late.append(r['time'])
            if opened is None:opened=r['time']
        elif r.get('body')=='Bot stopped speaking' and opened is not None:
            spans.append((opened,r['time']));opened=None
    spoken=max((b-a for a,b in spans if b<=end),default=0)
    passed=spoken>=1000 and not late and opened is None
    return {**result,'passed':passed,'reason':'speaking_completed_before_disconnect' if passed else 'farewell_not_completed_before_disconnect',
        'longest_speaking_before_disconnect_ms':spoken,'speech_starts_after_disconnect':len(late)}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('folder',type=Path);a=p.parse_args()
    result=verify(json.loads((a.folder/'provider-log.private.json').read_text(encoding='utf-8')))
    (a.folder/'farewell-verification.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result));raise SystemExit(0 if result['passed'] else 1)
