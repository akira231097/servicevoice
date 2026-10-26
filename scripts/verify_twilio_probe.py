"""Fail closed on provider errors or unexplained caller-side audio in owned tests.

This is an acoustic attribution gate, not a semantic or human-naturalness grade.
Only correlation-matched approved clips and bounded Vapi speaking intervals can
explain audible output. Missing capture/provider evidence cannot produce a pass.
"""
import argparse
import hashlib
import json
import math
import statistics
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


def transport_alignment(packets, clips, intervals):
    """Measure this call's transport from known waiting clips, never unknown audio.

    At least three distinct provider intervals and two approved phrase IDs must
    agree. Listening announcements are independent of Vapi and cannot calibrate
    its clock. Missing, ambiguous or inconsistent evidence leaves the old strict
    tolerance unchanged.
    """
    waiting={'moment','one_sec','just_a_sec','checking','taking_a_look'}
    measurements={}
    inbound=[p for p in packets if p.get('track')=='inbound']
    for clip in clips:
        if clip.get('clip_id') not in waiting:continue
        for match in clip.get('matches',[]):
            if match.get('track')!='inbound' or match.get('correlation',0)<.9:continue
            sample=match['sample'];duration=match['duration_ms']/1000
            found=[p for p in inbound if p['byte_offset']<=sample<p['byte_offset']+p['bytes']]
            if len(found)!=1:continue
            packet=found[0]
            received=packet['received_unix']+(sample-packet['byte_offset'])/8000
            candidates=[(i,received-start) for i,(start,end) in enumerate(intervals)
                if abs(end-start-duration)<=.12 and 0<=received-start<=1]
            if len(candidates)!=1:continue
            index,lag=candidates[0]
            measurements.setdefault(index,[]).append((lag,clip['clip_id']))
    rows=[{'lag_seconds':items[0][0],'phrase_id':items[0][1],'provider_interval':index}
          for index,items in measurements.items() if len(items)==1]
    aligned=[]
    # Transport drifts within a call. Each source interval requires its own
    # nearby independent anchors; one early clip cannot shift the whole call.
    for index,(start,_) in enumerate(intervals):
        nearby=[r for r in rows if abs(intervals[r['provider_interval']][0]-start)<=30]
        lags=[r['lag_seconds'] for r in nearby]
        if len(nearby)>=3 and len({r['phrase_id'] for r in nearby})>=2 and max(lags)-min(lags)<=.12:
            aligned.append({'provider_interval':index,'lag_seconds':round(statistics.median(lags),6),
                'spread_seconds':round(max(lags)-min(lags),6),
                'anchor_intervals':[r['provider_interval'] for r in nearby]})
    return {'used':bool(aligned),'samples':len(rows),
        'phrase_ids':sorted({r['phrase_id'] for r in rows}),
        'calibrated_intervals':aligned,'residual_tolerance_seconds':.12,
        'scope':'Each aligned Vapi interval needs >=3 unique approved waiting-clip anchors within 30 s across >=2 phrase IDs, correlation >=.9, duration error <=120 ms and lag spread <=120 ms. Others retain 350 ms tolerance. Unknown audio never calibrates the clock.'}


def unexplained_audio(samples, packets, clips, logs, *, calibrate=True):
    # Explicit event intervals, not transcript length or model request windows.
    intervals=[];opened=None;incomplete=False
    for row in sorted(logs,key=lambda r:r['time']):
        if row.get('body')=='Bot started speaking':
            if opened is None:opened=row['time']/1000
        elif row.get('body')=='Bot stopped speaking' and opened is not None:
            intervals.append((opened,row['time']/1000));opened=None
    if opened is not None:incomplete=True
    alignment=transport_alignment(packets,clips,intervals) if calibrate else {'used':False}
    if alignment['used']:
        shifts={r['provider_interval']:r['lag_seconds'] for r in alignment['calibrated_intervals']}
        pad=alignment['residual_tolerance_seconds']
        intervals=[(start+shifts[i]-pad,end+shifts[i]+pad) if i in shifts
                   else (start-.35,end+.35) for i,(start,end) in enumerate(intervals)]
    else:
        intervals=[(start-.35,end+.35) for start,end in intervals]
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
    result={'unexplained_intervals':unexplained,'missing_clock_voiced_frames':missing,
            'provider_interval_incomplete':incomplete,'provider_speaking_intervals':len(intervals),
            'transport_alignment':alignment,
            'scope':'20 ms frames, RMS >=300, >=120 ms unexplained energy; gaps <=200 ms grouped. Approved clips have 40 ms padding. Provider intervals use measured waiting-clip transport with 120 ms residual tolerance when independently supported; otherwise fixed 350 ms tolerance. Flags require review; a pass is not proof of naturalness.'}
    if alignment['used']:
        result['uncalibrated_unexplained_intervals']=unexplained_audio(samples,packets,clips,logs,calibrate=False)['unexplained_intervals']
    return result


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
