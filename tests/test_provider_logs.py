"""Authored offline storage fixtures; no actual HTTP, SSH, call or model use."""
import asyncio
from copy import deepcopy
import gzip
import json
import logging
from urllib.parse import urlencode
from uuid import uuid4
import zlib

import httpx
import pytest

from app import provider_logs as m
BASE='a'*32+'.r2.cloudflarestorage.com';SIGNED='hipaa-calllogs.'+BASE
HOSTS=(BASE,SIGNED)
CALL=str(uuid4());ORG=str(uuid4());ASSISTANT=str(uuid4());OBJECT=str(uuid4())
QUERY={'X-Amz-Algorithm':'AWS4-HMAC-SHA256','X-Amz-Content-Sha256':'UNSIGNED-PAYLOAD',
    'X-Amz-Credential':'fictional-storage-key/20261020/auto/s3/aws4_request','X-Amz-Date':'20261020T220000Z',
    'X-Amz-Expires':'3600','X-Amz-Signature':'b'*64,'X-Amz-SignedHeaders':'host','x-id':'GetObject'}
PATH=f'/{ORG}/{CALL}-{OBJECT}-1792533005405.jsonl.gz'


def call(*,signed=True):
    field='presignedLogUrl' if signed else 'logUrl'
    url='https://'+SIGNED+PATH+'?'+urlencode(QUERY) if signed else 'https://'+BASE+'/hipaa-calllogs'+PATH
    return {'id':CALL,'orgId':ORG,'assistantId':ASSISTANT,'status':'ended','artifact':{field:url}}


def event(body='LLM request started',*,time=1792533005405,**attrs):
    return {'body':body,'time':time,'attributes':{'callId':CALL,'traceId':CALL,'spanId':OBJECT,'turnId':'2',
        'headers':{'Authorization':'PRIVATE ADMIN KEY'},'transcript':'PRIVATE CUSTOMER',**attrs}}


class Bytes(httpx.AsyncByteStream):
    def __init__(self,value):self.value=value
    async def __aiter__(self):
        for index in range(0,len(self.value),37):yield self.value[index:index+37]


def invoke(c=None,*,raw=None,rows=None,status=200,headers=None,limits=None,trusted_hosts=HOSTS,**kwargs):
    requests=[]
    if raw is None:raw=gzip.compress(('\n'.join(json.dumps(row) for row in (rows or [event()]))+'\n').encode())
    async def handler(request):
        requests.append(request)
        return httpx.Response(status,headers=headers or {},stream=Bytes(raw))
    result=asyncio.run(m.capture_provider_log(call() if c is None else c,expected_call_id=CALL,expected_org_id=ORG,
        expected_assistant_id=ASSISTANT,trusted_hosts=trusted_hosts,limits=limits,
        _transport=httpx.MockTransport(handler),**kwargs))
    return result,requests


@pytest.mark.parametrize('signed',[True,False])
def test_exact_bound_official_shape_fetch_is_unauthenticated_and_only_projection_survives(signed,monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY','http://127.0.0.1:1')
    monkeypatch.setenv('VAPI_API_KEY','PRIVATE ADMIN KEY')
    rows=[event(),event('LLM first token received',time=1792533005505),event('LLM response completed',time=1792533005605),
          event('LLM request aborted before completion',time=1792533005705)]
    result,requests=invoke(call(signed=signed),rows=rows)
    assert result['status']=='observed' and result['network_requests']==len(requests)==1
    request=requests[0]
    assert request.method=='GET' and request.headers['accept-encoding']=='identity'
    assert not {'authorization','proxy-authorization','x-api-key','cookie'}&set(request.headers)
    expected=m.project_provider_generations(rows,CALL)
    assert result['projection']==expected
    assert result['projection']['counts']['completed_then_cleanup_abort_label']==1
    assert result['projection']['coverage']['audible_caller_interruption_count'] is None
    text=json.dumps(result)
    for forbidden in ('PRIVATE ADMIN KEY','PRIVATE CUSTOMER',ORG,BASE,'X-Amz-',QUERY['X-Amz-Signature']):assert forbidden not in text


def test_empty_allowlist_is_explicitly_disabled_without_fetch():
    result,requests=invoke(trusted_hosts=())
    assert result['status']=='disabled' and result['reason_code']=='provider_log_hosts_unconfigured'
    assert requests==[]


@pytest.mark.parametrize('field,value',[('id',str(uuid4())),('orgId',str(uuid4())),('assistantId',str(uuid4())),('status','in-progress')])
def test_exact_authenticated_call_binding_required(field,value):
    c=call();c[field]=value
    result,requests=invoke(c)
    assert result['reason_code']=='provider_call_binding_required' and requests==[]


@pytest.mark.parametrize('change',['suffix','untrusted_account','userinfo','fragment','empty_fragment','http','port','loopback','percent_path',
    'dot_path','double_slash','different_org','different_call','query_duplicate','query_unknown','query_headers','query_signature',
    'query_missing','control','backslash','oversize'])
def test_url_attack_controls_reject_before_network(change):
    c=call();url=c['artifact']['presignedLogUrl']
    if change=='suffix':url=url.replace(SIGNED,SIGNED+'.evil.invalid')
    elif change=='untrusted_account':url=url.replace('a'*32,'c'*32)
    elif change=='userinfo':url=url.replace('https://','https://user:PRIVATE@')
    elif change=='fragment':url+='\u0023PRIVATE'
    elif change=='empty_fragment':url+='\u0023'
    elif change=='http':url=url.replace('https:','http:')
    elif change=='port':url=url.replace(SIGNED,SIGNED+':8443')
    elif change=='loopback':url=url.replace(SIGNED,'127.0.0.1')
    elif change=='percent_path':url=url.replace('/'+ORG,'/%'+ORG)
    elif change=='dot_path':url=url.replace('/'+ORG,'/../'+ORG)
    elif change=='double_slash':url=url.replace('/'+ORG,'//'+ORG)
    elif change=='different_org':url=url.replace(ORG,str(uuid4()))
    elif change=='different_call':url=url.replace(CALL,str(uuid4()))
    elif change=='query_duplicate':url+='&X-Amz-Signature='+'b'*64
    elif change=='query_unknown':url+='&redirect=https://evil.invalid'
    elif change=='query_headers':url=url.replace('SignedHeaders=host','SignedHeaders=authorization')
    elif change=='query_signature':url=url.replace('Signature='+'b'*64,'Signature=PRIVATE')
    elif change=='query_missing':url=url.split('?')[0]
    elif change=='control':url+='\n'
    elif change=='backslash':url=url.replace('/'+ORG,'/\\'+ORG)
    elif change=='oversize':url+='x'*24001
    c['artifact']['presignedLogUrl']=url
    result,requests=invoke(c)
    assert result['status']=='unavailable' and result['reason_code']=='provider_log_url_rejected' and requests==[]
    assert 'PRIVATE' not in json.dumps(result)


def test_explicit_443_allowed_but_unsigned_shape_cannot_masquerade_as_signed():
    c=call();c['artifact']['presignedLogUrl']=c['artifact']['presignedLogUrl'].replace(SIGNED,SIGNED+':443')
    assert invoke(c)[0]['status']=='observed'
    c=call(signed=False);c['artifact']['presignedLogUrl']=c['artifact'].pop('logUrl')
    assert invoke(c)[1]==[]


@pytest.mark.parametrize('hosts',[('evil.invalid',),(SIGNED+'.evil.invalid',),(SIGNED,SIGNED),[SIGNED]*3,'https://'+SIGNED])
def test_allowlist_is_explicit_strict_and_not_derived_from_artifact(hosts):
    result,requests=invoke(trusted_hosts=hosts)
    assert result['reason_code']=='provider_log_host_configuration_invalid' and requests==[]


@pytest.mark.parametrize('status',[301,302,307,308,401,403,404,429,500])
def test_redirects_and_failed_storage_status_never_forward_or_retry(status):
    result,requests=invoke(status=status,headers={'location':'http://127.0.0.1/private'})
    assert result['status']=='unavailable' and len(requests)==1
    assert result['http_status']==status
    assert result['reason_code']==('provider_log_redirect_rejected' if status<400 else 'provider_log_http_error')


@pytest.mark.parametrize('case,code',[('wire','provider_log_wire_limit_exceeded'),('expanded','provider_log_expanded_limit_exceeded'),
    ('line','provider_log_line_limit_exceeded'),('events','provider_log_event_limit_exceeded'),('incomplete','provider_log_incomplete_gzip'),
    ('trailing','provider_log_trailing_compressed_data'),('invalidgzip','provider_log_invalid_gzip'),('invalidjson','provider_log_invalid_json'),
    ('duplicatejson','provider_log_ambiguous_json'),('array','provider_log_invalid_row'),('empty','provider_log_empty'),
    ('contentlength','provider_log_wire_limit_exceeded'),('encoding','provider_log_encoding_rejected')])
def test_hard_bounds_and_malformed_content_never_publish_partial_success(case,code):
    raw=None;rows=[event()];limits=m.Limits();headers={}
    if case=='wire':limits=m.Limits(compressed_bytes=1)
    elif case=='expanded':limits=m.Limits(expanded_bytes=1)
    elif case=='line':limits=m.Limits(line_bytes=1)
    elif case=='events':limits=m.Limits(events=1);rows=[event(),event()]
    elif case=='incomplete':raw=gzip.compress(json.dumps(event()).encode())[:-8]
    elif case=='trailing':raw=gzip.compress(json.dumps(event()).encode())+gzip.compress(b'{}')
    elif case=='invalidgzip':raw=b'PRIVATE malformed gzip'
    elif case=='invalidjson':raw=gzip.compress(b'PRIVATE malformed json\n')
    elif case=='duplicatejson':raw=gzip.compress(b'{"attributes":{},"attributes":{}}\n')
    elif case=='array':raw=gzip.compress(b'[]\n')
    elif case=='empty':raw=gzip.compress(b'\n\n')
    elif case=='contentlength':headers={'content-length':'9'*5000}
    elif case=='encoding':headers={'content-encoding':'br'}
    result,_=invoke(raw=raw,rows=rows,limits=limits,headers=headers)
    assert result['status']=='unavailable' and result['reason_code']==code and result['projection'] is None
    assert 'PRIVATE' not in json.dumps(result)


def test_foreign_and_unbound_rows_are_not_joined_by_timestamp_or_file():
    rows=[event(),event('Bot stopped speaking',callId=str(uuid4()),traceId=str(uuid4())),
        {'body':'Pipeline cleared','time':1792533005405,'attributes':{'wasInterruption':True}}]
    result,_=invoke(rows=rows)
    assert result['projection']['skipped']=={'conflicting_call_identity':1,'unbound_call_identity':1}
    assert result['projection']['counts']['provider_assistant_speech_stops']==0


@pytest.mark.parametrize('cached,completed',[(True,False),(False,True),(True,True)])
def test_frozen_completed_history_never_fetches_rejudges_or_replaces(cached,completed):
    frozen={'already_graded':'immutable'} if cached else None
    before=deepcopy(frozen)
    result,requests=invoke(frozen_report=frozen,completed=completed)
    assert result['status']=='preserve_frozen_review' and result['network_requests']==0 and requests==[]
    assert result['replace_existing_report'] is False and frozen==before


def test_absolute_deadline_stops_a_dripping_body_and_closes_stream():
    class Slow(httpx.AsyncByteStream):
        closed=False
        async def __aiter__(self):
            while True:
                await asyncio.sleep(.02)
                yield b'\x1f'
        async def aclose(self):self.closed=True
    stream=Slow();requests=[]
    async def handler(request):requests.append(request);return httpx.Response(200,stream=stream)
    result=asyncio.run(m.capture_provider_log(call(),expected_call_id=CALL,expected_org_id=ORG,
        trusted_hosts=HOSTS,limits=m.Limits(deadline_seconds=.03),_transport=httpx.MockTransport(handler)))
    assert result['reason_code']=='provider_log_deadline_exceeded' and len(requests)==1 and stream.closed


def test_transport_exception_is_fixed_and_never_contains_signed_url():
    async def handler(request):raise httpx.ConnectError('PRIVATE '+str(request.url))
    result=asyncio.run(m.capture_provider_log(call(),expected_call_id=CALL,expected_org_id=ORG,
        trusted_hosts=HOSTS,_transport=httpx.MockTransport(handler)))
    assert result['reason_code']=='provider_log_network_unavailable' and 'PRIVATE' not in json.dumps(result)


def test_oversized_json_integer_is_sanitized_instead_of_escaping_optional_capture():
    result,_=invoke(raw=gzip.compress(b'{"oversized":'+b'9'*5000+b'}\n'))
    assert result['reason_code']=='provider_log_invalid_json' and result['projection'] is None


def test_transport_logging_redacts_only_current_capture_context(caplog):
    caplog.set_level(logging.DEBUG)
    raw=gzip.compress((json.dumps(event())+'\n').encode())
    async def handler(request):
        logging.getLogger('httpcore.http11').debug('PRIVATE URL %s PRIVATE HEADER',str(request.url))
        # A separate pre-existing task keeps its original context and logging.
        await outsider
        return httpx.Response(200,stream=Bytes(raw))
    async def run():
        nonlocal outsider
        async def independent():
            await asyncio.sleep(0)
            logging.getLogger('httpx').info('Unrelated task logging retained')
        outsider=asyncio.create_task(independent())
        return await m.capture_provider_log(call(),expected_call_id=CALL,expected_org_id=ORG,trusted_hosts=HOSTS,
            _transport=httpx.MockTransport(handler))
    outsider=None
    result=asyncio.run(run())
    logging.getLogger('httpx').info('Outside capture logging retained')
    assert result['status']=='observed'
    assert 'Provider storage transport details redacted' in caplog.text
    assert 'Unrelated task logging retained' in caplog.text and 'Outside capture logging retained' in caplog.text
    for forbidden in ('PRIVATE URL','PRIVATE HEADER',BASE,'X-Amz-',QUERY['X-Amz-Signature']):assert forbidden not in caplog.text


def test_sync_wrapper_runs_only_without_an_active_event_loop():
    raw=gzip.compress((json.dumps(event())+'\n').encode());requests=[]
    async def handler(request):requests.append(request);return httpx.Response(200,stream=Bytes(raw))
    args={'expected_call_id':CALL,'expected_org_id':ORG,'trusted_hosts':HOSTS,'_transport':httpx.MockTransport(handler)}
    result=m.capture_provider_log_sync(call(),**args)
    assert result['status']=='observed' and len(requests)==1
    async def inside():return m.capture_provider_log_sync(call(),**args)
    result=asyncio.run(inside())
    assert result['reason_code']=='provider_log_active_async_context' and result['network_requests']==0
    assert len(requests)==1


def test_sync_wrapper_preserves_frozen_history_even_inside_async_context():
    frozen={'immutable':'legacy'}
    async def inside():return m.capture_provider_log_sync(call(),frozen_report=frozen)
    assert asyncio.run(inside())['status']=='preserve_frozen_review'
    assert frozen=={'immutable':'legacy'}


def test_public_host_validator_never_fills_or_derives_a_default():
    assert m.validate_trusted_hosts(())==frozenset()
    assert m.validate_trusted_hosts(HOSTS)==frozenset(HOSTS)
    with pytest.raises(ValueError,match='provider_log_host_configuration_invalid'):
        m.validate_trusted_hosts(('evil.invalid',))


def test_public_capture_boundary_revalidates_projection_and_rebuilds_only_fixed_fields():
    original,_=invoke(rows=[event(),event('LLM response completed',time=1792533005505)])
    original['unknown_url']='https://PRIVATE.invalid/?secret=PRIVATE'
    original['scope']='PRIVATE instructions and claims'
    original['projection']['headers']={'Authorization':'PRIVATE ADMIN'}
    original['projection']['model_generations']=[{'text':'PRIVATE MODEL PROSE'}]
    safe=m.safe_provider_capture(original,CALL)
    assert safe['status']=='observed' and safe['projection']['call_id']==CALL
    assert safe['scope']==m._CAPTURE_SCOPE
    assert safe['projection']['coverage']['audible_caller_interruption_count'] is None
    assert 'PRIVATE' not in json.dumps(safe) and 'headers' not in safe['projection']


@pytest.mark.parametrize('field,value',[
    ('capture_revision','PRIVATE'),('status','pass'),('reason_code','PRIVATE'),('network_requests',True),
    ('http_status',True),('http_status',201),('storage_profile','PRIVATE'),('artifact_field','PRIVATE'),
    ('call_key','f'*64),('wire_bytes',-1),('expanded_bytes',1.5),('source_rows',10001),
    ('wire_sha256','PRIVATE'),('projection',[]),('replace_existing_report',True)])
def test_invalid_capture_metadata_becomes_unknown_without_leaking_or_throwing(field,value):
    result,_=invoke();result[field]=value
    safe=m.safe_provider_capture(result,CALL)
    assert safe['status']=='unavailable' and safe['reason_code']=='provider_log_capture_invalid'
    assert safe['projection'] is None and safe['network_requests'] is None
    assert 'PRIVATE' not in json.dumps(safe)


@pytest.mark.parametrize('change',['call','reported_rows_too_small'])
def test_projection_identity_or_lineage_contradiction_never_becomes_observed(change):
    result,_=invoke(rows=[event(),event('LLM response completed',time=1792533005505)])
    if change=='call':result['projection']['call_id']=str(uuid4())
    else:result['source_rows']=1
    safe=m.safe_provider_capture(result,CALL)
    assert safe['status']=='unavailable' and safe['projection'] is None


@pytest.mark.parametrize('change',['negative_event_time','oversized_event_index'])
def test_malformed_optional_event_fields_do_not_discard_valid_siblings_or_invent_timing(change):
    result,_=invoke(rows=[event(),event('LLM response completed',time=1792533005505)])
    if change=='negative_event_time':result['projection']['events'][0]['observed_unix_ms']=-1
    else:result['projection']['events'][0]['source_entry_index']=10001
    safe=m.safe_provider_capture(result,CALL)
    assert safe['status']=='observed' and safe['projection']['coverage']['state']=='provider_reported_partial'
    assert safe['projection']['coverage']['complete'] is False
    if change=='negative_event_time':assert safe['projection']['events'][0]['observed_unix_ms'] is None
    else:
        assert len(safe['projection']['events'])==1
        assert safe['projection']['summary']['rejected_field_counts']['invalid_provider_event']==1


@pytest.mark.parametrize('value',[False,True,{},[],17,'PRIVATE'])
def test_arbitrary_persisted_capture_values_are_not_a_trusted_export_boundary(value):
    safe=m.safe_provider_capture(value,CALL)
    assert safe['status']=='unavailable' and safe['projection'] is None
    assert 'PRIVATE' not in json.dumps(safe)


def test_missing_historical_capture_and_known_failures_remain_explicit_unknowns():
    safe=m.safe_provider_capture(None,CALL)
    assert safe['reason_code']=='provider_log_not_captured' and safe['network_requests'] is None
    failure,_=invoke(status=403)
    failure['url']='https://PRIVATE.invalid'
    safe=m.safe_provider_capture(failure,CALL)
    assert safe['status']=='unavailable' and safe['reason_code']=='provider_log_http_error'
    assert safe['http_status']==403 and 'PRIVATE' not in json.dumps(safe)
    failure['reason_code']='PRIVATE'
    assert m.safe_provider_capture(failure,CALL)['reason_code']=='provider_log_capture_invalid'


@pytest.mark.parametrize('state,reason',[('disabled','provider_log_hosts_unconfigured'),
    ('preserve_frozen_review','existing_review_not_recaptured')])
def test_no_fetch_statuses_cannot_claim_projection_or_success(state,reason):
    capture={'capture_revision':m.REVISION,'status':state,'reason_code':reason,'network_requests':0,
        'projection':None,'replace_existing_report':False,'private':'PRIVATE'}
    safe=m.safe_provider_capture(capture,CALL)
    assert safe['status']==state and safe['projection'] is None and 'PRIVATE' not in json.dumps(safe)
    capture['projection']={'fake':'PRIVATE'}
    assert m.safe_provider_capture(capture,CALL)['status']=='unavailable'


def test_repeated_capture_boundary_preserves_known_source_rejections_without_refetch():
    original,_=invoke(rows=[event(),event('LLM response completed',time=1792533005505),
        event('Bot stopped speaking',callId=str(uuid4()),traceId=str(uuid4()))])
    original['projection']['events'][0]['source_entry_index']=10001
    once=m.safe_provider_capture(original,CALL)
    twice=m.safe_provider_capture(json.loads(json.dumps(once)),CALL)
    assert once==twice and once['status']=='observed'
    assert once['projection']['summary']['rejected_field_counts']['invalid_provider_event']==1
    assert once['projection']['summary']['source_projection_skipped_counts']['conflicting_call_identity']==1


def test_default_and_hard_capacity_bounds_allow_normal_large_calls_without_relaxing_line_or_event_limits():
    limits=m.Limits();limits.validate()
    assert limits.compressed_bytes==32*1024*1024
    assert limits.expanded_bytes==256*1024*1024 and limits.deadline_seconds==45.0
    assert limits.line_bytes==4*1024*1024 and limits.events==10000


@pytest.mark.parametrize('field,value',[
    ('compressed_bytes',32*1024*1024+1),('expanded_bytes',256*1024*1024+1),
    ('deadline_seconds',45.001),('line_bytes',4*1024*1024+1),('events',10001),
])
def test_capacity_hard_maxima_still_reject_before_any_network(field,value):
    limits=m.Limits(**{field:value})
    result,requests=invoke(limits=limits)
    assert result['reason_code']=='invalid_capture_limits' and result['projection'] is None and requests==[]


def test_streamed_21mib_storage_object_is_captured_and_sanitized_without_materializing_a_large_fixture():
    class LargeStream(httpx.AsyncByteStream):
        closed=False
        wire_bytes=0
        maximum_emitted_chunk=0
        async def __aiter__(self):
            # Stored DEFLATE blocks make wire size representative rather than
            # collapsing repetitive padding. Only one <=64KiB piece is yielded
            # at once; no giant compressed body or file fixture is constructed.
            compressor=zlib.compressobj(level=0,wbits=16+zlib.MAX_WBITS)
            for index in range(42):
                prefix=(json.dumps(event(time=1792533005405+index))[:-1]+',"ignored_padding":"').encode()
                for piece in (prefix,)+(b'x'*m.CHUNK,)*8+(b'"}\n',):
                    encoded=compressor.compress(piece)
                    for start in range(0,len(encoded),m.CHUNK):
                        chunk=encoded[start:start+m.CHUNK]
                        self.wire_bytes+=len(chunk);self.maximum_emitted_chunk=max(self.maximum_emitted_chunk,len(chunk))
                        yield chunk
            encoded=compressor.flush()
            for start in range(0,len(encoded),m.CHUNK):
                chunk=encoded[start:start+m.CHUNK]
                self.wire_bytes+=len(chunk);self.maximum_emitted_chunk=max(self.maximum_emitted_chunk,len(chunk))
                yield chunk
        async def aclose(self):self.closed=True
    stream=LargeStream();requests=[]
    async def handler(request):
        requests.append(request)
        return httpx.Response(200,stream=stream)
    result=asyncio.run(m.capture_provider_log(call(),expected_call_id=CALL,expected_org_id=ORG,
        expected_assistant_id=ASSISTANT,trusted_hosts=HOSTS,_transport=httpx.MockTransport(handler)))
    assert result['status']=='observed' and result['network_requests']==len(requests)==1
    assert 21*1024*1024<result['wire_bytes']==stream.wire_bytes<32*1024*1024
    assert 21*1024*1024<result['expanded_bytes']<256*1024*1024
    assert result['source_rows']==42 and len(result['projection']['events'])==42
    assert stream.closed and stream.maximum_emitted_chunk<=m.CHUNK
    assert m.safe_provider_capture(result,CALL)['status']=='observed'
    text=json.dumps(result)
    for forbidden in ('ignored_padding','PRIVATE',BASE,'X-Amz-'):assert forbidden not in text


@pytest.mark.parametrize('field,maximum',[('wire_bytes',32*1024*1024),('expanded_bytes',256*1024*1024)])
def test_public_metadata_accepts_updated_capacity_ceiling_but_rejects_above_it(field,maximum):
    original,_=invoke();original[field]=maximum
    assert m.safe_provider_capture(original,CALL)['status']=='observed'
    original[field]=maximum+1
    safe=m.safe_provider_capture(original,CALL)
    assert safe['status']=='unavailable' and safe['reason_code']=='provider_log_capture_invalid'
