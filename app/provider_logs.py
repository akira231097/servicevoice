"""Bounded unauthenticated Vapi storage-log capture for new post-call reviews.

The library function accepts an already authenticated terminal Vapi call record
and independently required expected identities. It never reads keys, sends an
administration header, writes a cache, calls a model or publishes an evaluation.

Worker placement: only inside creation of a NEW, unfrozen review, before
Outbox.save_report. Persist the sanitized projection in that same frozen report.
Cached/in-flight exports reuse the frozen report; complete historical jobs are
never requeued for this additive capture. Backfills require a separate explicit
append-only evidence export, not another judgment or an old run replacement.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import logging
import math
import re
import time
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID
import zlib

import httpx

from .generation_observability import MAX_EVENTS, GenerationEvidenceError, project_provider_generations

REVISION='provider-storage-capture-v1'
STORAGE_PROFILE='vapi_r2_calllogs_v1'
_CAPTURE_SCOPE='Provider-reported fixed events only; no proof of audible interruptions, packet discard or TTS-to-generation lineage.'
_FAILURE_CODES=frozenset({'invalid_capture_limits','provider_log_host_configuration_invalid','provider_call_binding_required',
    'provider_log_url_unavailable','provider_log_url_rejected','provider_log_invalid_row','provider_log_ambiguous_json',
    'provider_log_invalid_json','provider_log_deadline_exceeded','provider_log_line_limit_exceeded',
    'provider_log_event_limit_exceeded','provider_log_expanded_limit_exceeded','provider_log_empty',
    'provider_log_redirect_rejected','provider_log_http_error','provider_log_wire_limit_exceeded',
    'provider_log_encoding_rejected','provider_log_trailing_compressed_data','provider_log_incomplete_gzip',
    'provider_log_network_unavailable','provider_log_invalid_gzip','provider_log_projection_unavailable',
    'provider_log_active_async_context'})
CHUNK=64*1024
_UUID=r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}'
_PATH=re.compile(r'^/(?P<org>'+_UUID+r')/(?P<call>'+_UUID+r')-(?P<object>'+_UUID+r')-(?P<stamp>[0-9]{13})\.jsonl\.gz$')
_TURN=re.compile(r'^(?:0|[1-9][0-9]{0,8})$')
_HOST=re.compile(r'^(?:hipaa-calllogs\.)?[a-f0-9]{32}\.r2\.cloudflarestorage\.com$')
_QUERY_KEYS=frozenset({'X-Amz-Algorithm','X-Amz-Content-Sha256','X-Amz-Credential','X-Amz-Date',
    'X-Amz-Expires','X-Amz-Signature','X-Amz-SignedHeaders','x-id'})
_EVENT_NAMES=frozenset({'Turn started','Pipeline cleared','LLM request started','LLM first token received',
    'LLM response completed','LLM request aborted before completion','Bot started speaking','Bot stopped speaking',
    'User started speaking','User stopped speaking','Custom LLM HTTP request'})
_STORAGE_LOGGING=ContextVar('servicevoice_provider_storage_logging',default=False)


class _StorageLogRedaction(logging.Filter):
    """Optional transport debugging cannot print a signed URL or raw headers.

    The filter is installed once on fixed transport loggers, but only redacts
    records from this capture's async context. Other worker logging is retained.
    """
    def filter(self,record):
        if _STORAGE_LOGGING.get():
            record.msg='Provider storage transport details redacted'
            record.args=()
            record.exc_info=record.exc_text=record.stack_info=None
        return True


def _install_storage_log_redaction():
    for name in ('httpx','httpcore.connection','httpcore.http11','httpcore.http2','httpcore.proxy','httpcore.socks'):
        logger=logging.getLogger(name)
        if not any(isinstance(item,_StorageLogRedaction) for item in logger.filters):
            logger.addFilter(_StorageLogRedaction())


class CaptureError(ValueError):
    """Only fixed codes, never a URL, log content or network exception."""


@dataclass(frozen=True)
class Limits:
    compressed_bytes:int=32*1024*1024
    expanded_bytes:int=256*1024*1024
    line_bytes:int=4*1024*1024
    events:int=MAX_EVENTS
    deadline_seconds:float=45.0

    def validate(self):
        for value,maximum in ((self.compressed_bytes,32*1024*1024),(self.expanded_bytes,256*1024*1024),
                              (self.line_bytes,4*1024*1024),(self.events,MAX_EVENTS)):
            if type(value) is not int or not 1<=value<=maximum:raise CaptureError('invalid_capture_limits')
        if type(self.deadline_seconds) not in (int,float) or not math.isfinite(self.deadline_seconds) or not 0<self.deadline_seconds<=45:
            raise CaptureError('invalid_capture_limits')


def _uuid(value):
    try:return type(value) is str and str(UUID(value))==value
    except (ValueError,AttributeError):return False


def validate_trusted_hosts(values):
    """Validate an explicit private allowlist; an empty collection disables I/O."""
    if type(values) not in (tuple,list,set,frozenset) or len(values)>2:
        raise CaptureError('provider_log_host_configuration_invalid')
    if any(type(value) is not str or not _HOST.fullmatch(value) for value in values) or len(set(values))!=len(values):
        raise CaptureError('provider_log_host_configuration_invalid')
    # These are explicit private configuration, never inferred from a call URL.
    return frozenset(values)


def _url(call,call_id,org_id,assistant_id,trusted_hosts):
    if (not _uuid(call_id) or not _uuid(org_id) or type(call) is not dict or call.get('id')!=call_id
            or call.get('orgId')!=org_id or call.get('status')!='ended'
            or assistant_id is not None and (not _uuid(assistant_id) or call.get('assistantId')!=assistant_id)):
        raise CaptureError('provider_call_binding_required')
    artifact=call.get('artifact')
    if type(artifact) is not dict:raise CaptureError('provider_log_url_unavailable')
    field='presignedLogUrl' if artifact.get('presignedLogUrl') is not None else 'logUrl'
    url=artifact.get(field)
    if type(url) is not str or not url or len(url)>24_000 or any(ord(ch)<33 or ord(ch)>126 for ch in url) or '#' in url:
        raise CaptureError('provider_log_url_rejected')
    try:
        parts=urlsplit(url)
        port=parts.port
    except ValueError:raise CaptureError('provider_log_url_rejected') from None
    if (parts.scheme!='https' or port not in (None,443) or parts.username is not None or parts.password is not None
            or parts.hostname not in trusted_hosts
            or parts.netloc not in {parts.hostname,parts.hostname+':443'}):
        raise CaptureError('provider_log_url_rejected')
    path=parts.path
    if not parts.hostname.startswith('hipaa-calllogs.'):
        if not path.startswith('/hipaa-calllogs/') or parts.query or field!='logUrl':raise CaptureError('provider_log_url_rejected')
        path=path[len('/hipaa-calllogs'):]
    else:
        if field!='presignedLogUrl' or not parts.query or len(parts.query)>16_000:raise CaptureError('provider_log_url_rejected')
        try:items=parse_qsl(parts.query,keep_blank_values=True,strict_parsing=True,max_num_fields=9)
        except ValueError:raise CaptureError('provider_log_url_rejected') from None
        query=dict(items)
        if (len(items)!=len(query) or set(query)!=_QUERY_KEYS or any(not value for value in query.values())
                or query['X-Amz-Algorithm']!='AWS4-HMAC-SHA256' or query['X-Amz-SignedHeaders']!='host'
                or query['X-Amz-Content-Sha256']!='UNSIGNED-PAYLOAD' or query['x-id']!='GetObject'
                or not re.fullmatch(r'[a-f0-9]{64}',query['X-Amz-Signature'])
                or not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z',query['X-Amz-Date'])
                or not re.fullmatch(r'[0-9]{1,6}',query['X-Amz-Expires'])
                or not 1<=int(query['X-Amz-Expires'])<=604800):
            raise CaptureError('provider_log_url_rejected')
    match=_PATH.fullmatch(path)
    if (not match or match['org']!=org_id or match['call']!=call_id
            or not 1_577_836_800_000<=int(match['stamp'])<4_102_444_800_000):
        raise CaptureError('provider_log_url_rejected')
    return url,field


def _minimal_row(row,call_id):
    """Discard headers/text/arguments immediately, retaining source row indices.

    Existing projector remains the authority for field validation and event
    semantics. Foreign IDs become a fixed conflict marker, not copied content.
    """
    if type(row) is not dict:raise CaptureError('provider_log_invalid_row')
    attrs=row.get('attributes')
    if type(attrs) is not dict:return {}
    safe={}
    for key in ('callId','traceId'):
        if key in attrs:safe[key]=attrs[key] if attrs[key] in (None,call_id) else 'conflicting'
    if 'wasInterruption' in attrs:safe['wasInterruption']=attrs['wasInterruption'] if type(attrs['wasInterruption']) is bool else None
    for key in ('turnId','previousTurnId'):
        value=attrs.get(key)
        if type(value) is str and _TURN.fullmatch(value):safe[key]=value
    if _uuid(attrs.get('spanId')):safe['spanId']=attrs['spanId']
    request=attrs.get('request')
    if type(request) is dict:
        metadata=request.get('metadata')
        if type(metadata) is dict:
            values={}
            for key in ('numAssistantTurns','numUserTurns','numModelRequestInTurn'):
                value=metadata.get(key)
                values[key]=value if type(value) is int and 0<=value<=1_000_000 else None
            value=metadata.get('assistantTurnInterrupted')
            values['assistantTurnInterrupted']=value if type(value) is bool else None
            stamp=request.get('timestamp')
            safe['request']={'timestamp':stamp if type(stamp) is int else None,'metadata':values}
    body=row.get('body');stamp=row.get('time')
    return {'attributes':safe,'body':body if type(body) is str and body in _EVENT_NAMES else None,
            'time':stamp if type(stamp) is int else None}


def _object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise CaptureError('provider_log_ambiguous_json')
        result[key]=value
    return result


def _reject_constant(value):raise CaptureError('provider_log_invalid_json')


class _Rows:
    def __init__(self,limits,call_id,end,clock):
        self.limits,self.call_id,self.end,self.clock=limits,call_id,end,clock
        self.pending=bytearray();self.rows=[];self.expanded=0
    def deadline(self):
        if self.clock()>=self.end:raise CaptureError('provider_log_deadline_exceeded')
    def parse(self,line):
        self.deadline()
        if len(line)>self.limits.line_bytes:raise CaptureError('provider_log_line_limit_exceeded')
        if not line.strip():return
        if len(self.rows)>=self.limits.events:raise CaptureError('provider_log_event_limit_exceeded')
        try:row=json.loads(line.decode('utf-8'),object_pairs_hook=_object,parse_constant=_reject_constant)
        except CaptureError:raise
        except (UnicodeDecodeError,ValueError,RecursionError):raise CaptureError('provider_log_invalid_json') from None
        self.rows.append(_minimal_row(row,self.call_id));self.deadline()
    async def feed(self,chunk):
        self.deadline();self.expanded+=len(chunk)
        if self.expanded>self.limits.expanded_bytes:raise CaptureError('provider_log_expanded_limit_exceeded')
        self.pending.extend(chunk)
        while True:
            position=self.pending.find(b'\n')
            if position<0:break
            line=bytes(self.pending[:position]);del self.pending[:position+1]
            self.parse(line)
            await asyncio.sleep(0)
        if len(self.pending)>self.limits.line_bytes:raise CaptureError('provider_log_line_limit_exceeded')
    def finish(self):
        if self.pending:self.parse(bytes(self.pending))
        if not self.rows:raise CaptureError('provider_log_empty')


async def capture_provider_log(call,*,expected_call_id,expected_org_id,expected_assistant_id=None,
        trusted_hosts=(),frozen_report=None,completed=False,limits=None,_transport=None,_clock=time.monotonic):
    """Return sanitized advisory evidence for a NEW report; never export itself.

    An existing frozen report or completed job is a hard no-fetch decision. The
    caller must preserve that report unchanged, rather than merging this result.
    The optional transport/clock arguments exist only for offline draft tests.
    """
    if frozen_report is not None or completed:
        return {'capture_revision':REVISION,'status':'preserve_frozen_review','reason_code':'existing_review_not_recaptured',
                'network_requests':0,'projection':None,'replace_existing_report':False}
    attempted=False;status=None;logging_token=None
    try:
        limits=Limits() if limits is None else limits
        if type(limits) is not Limits:raise CaptureError('invalid_capture_limits')
        limits.validate()
        trusted=validate_trusted_hosts(trusted_hosts)
        if not trusted:
            return {'capture_revision':REVISION,'status':'disabled','reason_code':'provider_log_hosts_unconfigured',
                'network_requests':0,'projection':None,'replace_existing_report':False}
        url,field=_url(call,expected_call_id,expected_org_id,expected_assistant_id,trusted)
        _install_storage_log_redaction()
        logging_token=_STORAGE_LOGGING.set(True)
        end=_clock()+limits.deadline_seconds
        rows=_Rows(limits,expected_call_id,end,_clock)
        wire_bytes=0;wire_hash=hashlib.sha256();inflator=zlib.decompressobj(16+zlib.MAX_WBITS)
        async with asyncio.timeout(limits.deadline_seconds):
            async with httpx.AsyncClient(follow_redirects=False,trust_env=False,auth=None,cookies={},
                    headers={'Accept':'application/gzip, application/x-ndjson, application/octet-stream','Accept-Encoding':'identity'},
                    timeout=httpx.Timeout(limits.deadline_seconds,connect=min(3.0,limits.deadline_seconds)),
                    limits=httpx.Limits(max_connections=1,max_keepalive_connections=0),transport=_transport) as client:
                attempted=True
                async with client.stream('GET',url) as response:
                    status=response.status_code if 100<=response.status_code<=599 else None
                    if status!=200:raise CaptureError('provider_log_redirect_rejected' if status is not None and 300<=status<400 else 'provider_log_http_error')
                    length=response.headers.get('content-length')
                    if length is not None and (len(length)>12 or not length.isdigit() or int(length)>limits.compressed_bytes):raise CaptureError('provider_log_wire_limit_exceeded')
                    if response.headers.get('content-encoding','identity').lower() not in {'identity','gzip'}:raise CaptureError('provider_log_encoding_rejected')
                    async for chunk in response.aiter_raw(chunk_size=CHUNK):
                        rows.deadline();wire_bytes+=len(chunk);wire_hash.update(chunk)
                        if wire_bytes>limits.compressed_bytes:raise CaptureError('provider_log_wire_limit_exceeded')
                        if inflator.eof and chunk:raise CaptureError('provider_log_trailing_compressed_data')
                        pending=chunk
                        while True:
                            expanded=inflator.decompress(pending,CHUNK)
                            if inflator.unused_data:raise CaptureError('provider_log_trailing_compressed_data')
                            await rows.feed(expanded)
                            pending=inflator.unconsumed_tail
                            if not pending and len(expanded)<CHUNK:break
                    if not inflator.eof:raise CaptureError('provider_log_incomplete_gzip')
                    rows.finish();rows.deadline()
        rows.deadline()
        projection=project_provider_generations(rows.rows,expected_call_id)
        rows.deadline()
        # The validated call UUID is retained for the exact-call export guard.
        # No storage URL, signature, administration header or raw log is returned.
        return {'capture_revision':REVISION,'status':'observed','reason_code':'provider_log_projected',
            'network_requests':1,'http_status':200,'storage_profile':STORAGE_PROFILE,'artifact_field':field,
            'call_key':hashlib.sha256(expected_call_id.encode()).hexdigest(),'wire_bytes':wire_bytes,
            'expanded_bytes':rows.expanded,'source_rows':len(rows.rows),'wire_sha256':wire_hash.hexdigest(),
            'projection':projection,'replace_existing_report':False,
            'scope':_CAPTURE_SCOPE}
    except (asyncio.TimeoutError,httpx.TimeoutException):code='provider_log_deadline_exceeded'
    except CaptureError as exc:code=str(exc)
    except httpx.HTTPError:code='provider_log_network_unavailable'
    except zlib.error:code='provider_log_invalid_gzip'
    except GenerationEvidenceError:code='provider_log_projection_unavailable'
    finally:
        if logging_token is not None:_STORAGE_LOGGING.reset(logging_token)
    return {'capture_revision':REVISION,'status':'unavailable','reason_code':code,'network_requests':int(attempted),
            'http_status':status,'projection':None,'replace_existing_report':False}


def capture_provider_log_sync(call,**kwargs):
    """Synchronous worker adapter; never nest an event loop or refetch history."""
    if kwargs.get('frozen_report') is not None or kwargs.get('completed'):
        return {'capture_revision':REVISION,'status':'preserve_frozen_review','reason_code':'existing_review_not_recaptured',
                'network_requests':0,'projection':None,'replace_existing_report':False}
    try:asyncio.get_running_loop()
    except RuntimeError:return asyncio.run(capture_provider_log(call,**kwargs))
    return {'capture_revision':REVISION,'status':'unavailable','reason_code':'provider_log_active_async_context',
            'network_requests':0,'http_status':None,'projection':None,'replace_existing_report':False}


def safe_provider_capture(capture,expected_call_id):
    """Rebuild the SaaS boundary from fixed metadata and a revalidated timeline.

    Persisted reports and network results are evidence, not trusted dictionaries.
    Unknown keys, URLs and supplied explanations never reach this return value.
    Invalid/missing evidence yields an explicit unknown, never a successful grade.
    This pure function performs no fetch, retry, judgment, export or cache write.
    """
    def unavailable(code='provider_log_capture_invalid'):
        return {'capture_revision':REVISION,'status':'unavailable','reason_code':code,
            'network_requests':None,'http_status':None,'projection':None,'replace_existing_report':False}
    if capture is None:return unavailable('provider_log_not_captured')
    try:
        if (not _uuid(expected_call_id) or type(capture) is not dict or capture.get('capture_revision')!=REVISION
                or capture.get('replace_existing_report') is not False):return unavailable()
        state=capture.get('status');reason=capture.get('reason_code');requests=capture.get('network_requests')
        if type(requests) is not int or requests not in (0,1):return unavailable()
        status=capture.get('http_status')
        if status is not None and (type(status) is not int or not 100<=status<=599):return unavailable()
        if state in {'disabled','preserve_frozen_review'}:
            expected_reason='provider_log_hosts_unconfigured' if state=='disabled' else 'existing_review_not_recaptured'
            if reason!=expected_reason or requests!=0 or status is not None or capture.get('projection') is not None:return unavailable()
            return {'capture_revision':REVISION,'status':state,'reason_code':expected_reason,'network_requests':0,
                'http_status':None,'projection':None,'replace_existing_report':False}
        if state=='unavailable':
            if reason not in _FAILURE_CODES or capture.get('projection') is not None:return unavailable()
            return {'capture_revision':REVISION,'status':'unavailable','reason_code':reason,'network_requests':requests,
                'http_status':status,'projection':None,'replace_existing_report':False}
        if (state!='observed' or reason!='provider_log_projected' or requests!=1 or status!=200
                or capture.get('storage_profile')!=STORAGE_PROFILE or capture.get('artifact_field') not in {'presignedLogUrl','logUrl'}
                or capture.get('call_key')!=hashlib.sha256(expected_call_id.encode()).hexdigest()):return unavailable()
        for name,maximum in (('wire_bytes',32*1024*1024),('expanded_bytes',256*1024*1024),('source_rows',MAX_EVENTS)):
            value=capture.get(name)
            if type(value) is not int or not 1<=value<=maximum:return unavailable()
        digest=capture.get('wire_sha256')
        if type(digest) is not str or not re.fullmatch(r'[a-f0-9]{64}',digest):return unavailable()
        from .generation_observability import sanitize_provider_projection
        projection=sanitize_provider_projection(capture.get('projection'),expected_call_id)
        indices=[row['source_entry_index'] for row in projection['events']+projection['provider_request_metadata']]
        if any(index>=capture['source_rows'] for index in indices):return unavailable()
        return {'capture_revision':REVISION,'status':'observed','reason_code':'provider_log_projected',
            'network_requests':1,'http_status':200,'storage_profile':STORAGE_PROFILE,'artifact_field':capture['artifact_field'],
            'call_key':capture['call_key'],'wire_bytes':capture['wire_bytes'],'expanded_bytes':capture['expanded_bytes'],
            'source_rows':capture['source_rows'],'wire_sha256':digest,'projection':projection,
            'replace_existing_report':False,'scope':_CAPTURE_SCOPE}
    except (ValueError,TypeError,KeyError,AttributeError,ImportError,OverflowError,RecursionError):
        return unavailable()
