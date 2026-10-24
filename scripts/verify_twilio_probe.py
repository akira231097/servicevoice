"""Fail closed on provider errors or unexplained caller-side audio in owned tests.

This is an acoustic attribution gate, not a semantic or human-naturalness grade.
Only correlation-matched approved clips and bounded Vapi speaking intervals can
explain audible output. Missing capture/provider evidence cannot produce a pass.
"""
import argparse
import hashlib
import json
import math
import time
from pathlib import Path


def notifications(client, base, calls):
    records=[]
    for role, call in calls:
        prefix=base+'/Calls/'+call+'/Notifications.json'
        url=prefix+'?PageSize=100';seen=set()
        for _ in range(10):
            if url in seen: raise ValueError('notification_pagination_cycle')
            seen.add(url)
            response=client.get(url);response.raise_for_status()
            data=response.json()
            if not isinstance(data.get('notifications'),list):raise ValueError('notifications_unavailable')
            for row in data['notifications']:
                if row.get('call_sid') not in (None,call):raise ValueError('notification_call_mismatch')
                records.append({'role':role,'call_sid':call,'notification':row})
            next_uri=data.get('next_page_uri')
            if not next_uri:break
            origin='https://api.twilio.com'
            url=origin+next_uri if next_uri.startswith('/') else next_uri
            if not url.startswith(prefix+'?'):raise ValueError('notification_scope_changed')
        else:raise ValueError('notification_pagination_limit')
    return {'complete':True,'legs':len(calls),'records':records,'collected_unix':time.time()}


def mulaw(raw):
    result=[]
    for byte in raw:
        value=(~byte)&255
        amplitude=(((value&15)<<3)+132)<<((value&112)>>4)
        result.append(132-amplitude if value&128 else amplitude-132)
    return result


def unexplained_audio(samples, packets, clips, logs):
    # Explicit event intervals, not transcript length or model request windows.
    intervals=[];opened=None;incomplete=False
    for row in sorted(logs,key=lambda r:r['time']):
        if row.get('body')=='Bot started speaking':
            if opened is None:opened=row['time']/1000
        elif row.get('body')=='Bot stopped speaking' and opened is not None:
            intervals.append((opened-.35,row['time']/1000+.35));opened=None
    if opened is not None:incomplete=True
    approved=[]
    for clip in clips:
        for match in clip.get('matches',[]):
            if match.get('track')=='inbound' and match.get('correlation',0)>=.65:
                approved.append((max(0,match['sample']-320),match['sample']+int(match['duration_ms']*8)+320))
    frame_count=len(samples)//160
    stamps=[None]*frame_count
    for packet in packets:
        if packet.get('track')!='inbound':continue
        first=packet['byte_offset']//160;last=min(frame_count,(packet['byte_offset']+packet['bytes']+159)//160)
        for index in range(first,last):stamps[index]=packet['received_unix']
    active=[];missing=0
    for index in range(frame_count):
        begin=index*160;frame=samples[begin:begin+160]
        if math.sqrt(sum(x*x for x in frame)/160)<300:continue
        if any(left<=begin+80<=right for left,right in approved):continue
        stamp=stamps[index]
        if stamp is None:missing+=1
        if stamp is not None and any(left<=stamp<=right for left,right in intervals):continue
        active.append(index)
    groups=[]
    for index in active:
        if not groups or index-groups[-1][-1]>10:groups.append([])
        groups[-1].append(index)
    unexplained=[{'start_ms':g[0]*20,'end_ms':(g[-1]+1)*20,'voiced_ms':len(g)*20}
                 for g in groups if len(g)>=6]
    return {'unexplained_intervals':unexplained,'missing_clock_voiced_frames':missing,
            'provider_interval_incomplete':incomplete,'provider_speaking_intervals':len(intervals),
            'scope':'20 ms frames, RMS >=300, >=120 ms unexplained energy; gaps <=200 ms grouped. Approved clips have 40 ms padding; provider speaking intervals have 350 ms transport tolerance. Flags require review; a pass is not proof of naturalness.'}


def verify(folder):
    capture=json.loads((folder/'media/capture.private.json').read_text())
    raw=(folder/'media/inbound.mulaw').read_bytes()
    if hashlib.sha256(raw).hexdigest()!=capture['audio']['inbound']['sha256']:raise ValueError('capture_hash_mismatch')
    note=json.loads((folder/'twilio-notifications.private.json').read_text())
    if note.get('complete') is not True or note.get('legs',0)<2:raise ValueError('notification_coverage_incomplete')
    # Notification resources describe provider problems. Treat every returned
    # notification as a failure, including a warning, rather than grade it away.
    errors=[{'role':r['role'],'code':r['notification'].get('error_code')} for r in note['records']]
    audit=json.loads((folder/'media-audit.json').read_text())
    result=unexplained_audio(mulaw(raw),capture['packets'],audit['clips'],
        json.loads((folder/'provider-log.private.json').read_text()))
    result['twilio_notifications']=errors
    result['passed']=(not errors and not result['unexplained_intervals']
        and not result['missing_clock_voiced_frames'] and not result['provider_interval_incomplete']
        and capture.get('outcome')=='completed' and not audit['sequence_gaps'])
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('folder',type=Path);a=p.parse_args()
    try:result=verify(a.folder)
    except Exception as exc:result={'passed':False,'evidence_error':type(exc).__name__}
    (a.folder/'call-verification.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result));raise SystemExit(0 if result['passed'] else 1)
