"""Preview or explicitly upload native synthetic LangSmith experiments.

Default is offline. --upload --allow-extended-retention acknowledges the native
experiment retention path. Reservations prevent blind retry of ambiguous POSTs.
Runtime tracing, live caller capture and business databases are never changed.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

from .langsmith import upload_payload, upload_settings, write_new
from .native import build_native_payload, build_control_payload
from .schema import digest

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--report',type=Path)
    group.add_argument('--controls',type=Path,help='Directory containing controls dataset.json and result.json')
    parser.add_argument('--dataset',type=Path,default=ROOT/'evals/datasets/servicevoice-v18-grounded-v1.json')
    parser.add_argument('--judge-directory',type=Path)
    parser.add_argument('--label',required=True)
    parser.add_argument('--upload',action='store_true')
    parser.add_argument('--allow-extended-retention',action='store_true')
    args=parser.parse_args()
    if args.upload and not args.allow_extended_retention:
        parser.error('Native experiments can use billed extended retention; explicitly acknowledge it.')
    if args.report:
        report=json.loads(args.report.read_text(encoding='utf-8'))
        judge=args.judge_directory or args.report.parent/'native-judge'
        review=json.loads((judge/'judge-review.json').read_text(encoding='utf-8'))
        result=json.loads((judge/'judge-result.json').read_text(encoding='utf-8'))
        raw=args.dataset.read_bytes()
        payload=build_native_payload(json.loads(raw),report,review,result,
            dataset_sha256=hashlib.sha256(raw).hexdigest(),label=args.label)
        directory=args.report.parent
    else:
        dataset=json.loads((args.controls/'dataset.json').read_text(encoding='utf-8'))
        result=json.loads((args.controls/'result.json').read_text(encoding='utf-8'))
        payload=build_control_payload(dataset,result,label=args.label)
        directory=args.controls
    preview=directory/('native-langsmith-preview-'+uuid.uuid4().hex+'.json')
    write_new(preview,payload)
    if not args.upload:
        print(json.dumps({'mode':'offline_preview','cases':len(payload['results']),'preview':str(preview),'network_requests':0}));return 0
    # User authorization applies before this opt-in path; no key is stored at
    # LangSmith for the judge. Judging already ran locally via the existing key.
    key,endpoint,workspace=upload_settings()
    journal=directory/'native-langsmith-upload-reservation.json'
    write_new(journal,{'experiment_name':args.label,'payload_sha256':digest(payload),
        'status':'reserved_outcome_unknown_until_receipt','at':datetime.now(timezone.utc).isoformat(),
        'warning':'Do not blindly retry an ambiguous POST. Inspect the exact experiment name first.'})
    outcome=asyncio.run(upload_payload(payload,key=key,endpoint=endpoint,workspace=workspace))
    receipt={**outcome,'experiment_name':args.label,'dataset_name':payload['dataset_name'],
        'rows':len(payload['results']),'payload_sha256':digest(payload),'synthetic_only':True,
        'extended_retention_acknowledged':True,'provider_key_uploaded':False,'public_share_created':False,
        'dataset_url':f"https://smith.langchain.com/o/{workspace}/datasets/{outcome['dataset_id']}",
        'experiment_url':f"https://smith.langchain.com/o/{workspace}/datasets/{outcome['dataset_id']}/compare?selectedSessions={outcome['experiment_id']}",
        'readback_verified':False}
    write_new(directory/'native-langsmith-receipt.json',receipt)
    print(json.dumps(receipt))
    return 0

if __name__=='__main__':raise SystemExit(main())
