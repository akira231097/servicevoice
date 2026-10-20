"""Independent speech boundary checks; all provider responses are local fakes."""
import asyncio
import base64
import json

import httpx
import pytest
from starlette.requests import ClientDisconnect

from app.gemini_tts import GeminiTTS, PCMRateConverter, TTSError, request_body, voice_request


def envelope(text="Your requested appointment is ready for review.", rate=16000):
    return {"message": {"type": "voice-request", "text": text, "sampleRate": rate}}


def event(audio=b"\x01\x00\x02\x00", rate=16000, **extra):
    value = {"candidates": [{"content": {"parts": [{"inlineData": {
        "mimeType": f"audio/L16;codec=pcm;rate={rate}",
        "data": base64.b64encode(audio).decode()}}]}}], **extra}
    return b"data: " + json.dumps(value).encode() + b"\n\n"


def terminal():
    return b'data: {"candidates":[{"content":{"parts":[{"text":""}]},"finishReason":"STOP"}]}\n\n'


class Stream(httpx.AsyncByteStream):
    def __init__(self, blocks, gate=None, *, completed=True):
        self.blocks, self.gate, self.closed, self.completed = blocks, gate, False, completed

    async def __aiter__(self):
        for i, block in enumerate(self.blocks):
            if i and self.gate is not None:await self.gate.wait()
            yield block
        if self.completed:yield terminal()

    async def aclose(self):self.closed = True


async def body(response):
    return b"".join([chunk async for chunk in response.body_iterator])


def test_speech_request_uses_exact_text_and_requested_pcm_rate_without_language_whitelist():
    text = "Your booking is pending. మీ అభ్యర్థన. Su cita. الخميس."
    result = request_body(text, 8000)
    assert result["contents"] == [{"role": "user", "parts": [{"text": text}]}]
    assert result["generationConfig"]["responseFormat"]["audio"] == {"mimeType": "AUDIO_L16", "sampleRate": 8000}
    assert result["generationConfig"]["speechConfig"] == {"voiceConfig": {"voice": "Kore"}}
    assert "language" not in json.dumps(result)


def test_owned_upstream_pool_survives_normal_conversational_pauses_and_is_reused(monkeypatch):
    created=[]
    real_client=httpx.AsyncClient

    def client_factory(**kwargs):
        created.append(kwargs)
        return real_client(**kwargs)

    monkeypatch.setattr("app.gemini_tts.httpx.AsyncClient",client_factory)

    async def scenario():
        adapter=GeminiTTS(lambda:{"GEMINI_API_KEY":"synthetic-not-sent"})
        try:
            await adapter.start()
            owned=adapter.client
            await adapter.start()
            assert adapter.client is owned
            assert len(created)==1
            assert created[0]["limits"].keepalive_expiry==60
            assert created[0]["limits"].max_connections==8
            assert created[0]["limits"].max_keepalive_connections==8
            assert created[0]["follow_redirects"] is False
            assert created[0]["trust_env"] is False
        finally:
            await adapter.close()
        assert owned.is_closed and adapter.client is None

    asyncio.run(scenario())


@pytest.mark.parametrize("message", [None, {}, {"type":"other","text":"hello","sampleRate":16000},
    {"type":"voice-request","text":" ","sampleRate":16000},
    {"type":"voice-request","text":"a"*4001,"sampleRate":16000},
    {"type":"voice-request","text":{},"sampleRate":16000},
    {"type":"voice-request","text":"hello","sampleRate":True},
    {"type":"voice-request","text":"hello","sampleRate":12000},
    {"type":"voice-request","text":"hello","sampleRate":"16000"}])
def test_invalid_input_is_rejected_before_provider_access(message):
    with pytest.raises(TTSError) as error:voice_request({"message":message})
    assert error.value.status_code == 400


@pytest.mark.parametrize("rate", [8000,16000,22050,24000,44100])
def test_exact_sample_rate_pcm_passes_through_without_container_or_resampling(rate):
    async def scenario():
        calls, seen = [], []
        pcm = b"\x03\x00\x09\x00"
        stream = Stream([event(pcm,rate)])
        def handler(request):
            calls.append(request)
            return httpx.Response(200,headers={"content-type":"text/event-stream"},stream=stream)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter=GeminiTTS(lambda:{"GEMINI_API_KEY":"SYNTHETIC-KEY"},client=client)
            response=await adapter.response(envelope(rate=rate),observer=seen.append)
            assert await body(response)==pcm
            assert response.headers["content-type"]=="application/octet-stream"
            assert stream.closed
        assert len(calls)==1
        request=calls[0]
        assert request.url.host=="generativelanguage.googleapis.com"
        assert request.url.path.endswith("gemini-3.8-flash-lite-tts:streamGenerateContent")
        assert dict(request.url.params)=={"alt":"sse"}
        assert "SYNTHETIC-KEY" not in str(request.url)
        assert request.headers["x-goog-api-key"]=="SYNTHETIC-KEY"
        assert json.loads(request.content)["generationConfig"]["responseFormat"]["audio"]["sampleRate"]==rate
        assert seen[-1]["event"]=="tts.completed"
        assert seen[-1]["audio_bytes"]==len(pcm)
        assert seen[-1]["observation_scope"]=="server_bytes_only_not_audible_playback"
        assert all(row["provider_call_id"] is None and row["turn_id"] is None for row in seen)
        encoded=json.dumps(seen)
        assert "SYNTHETIC-KEY" not in encoded and "requested appointment" not in encoded
    asyncio.run(scenario())


def test_first_audio_does_not_wait_for_later_sse_chunks_or_entire_utterance():
    async def scenario():
        gate=asyncio.Event();stream=Stream([event(b"\x01\x00"),event(b"\x02\x00")],gate)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,headers={"content-type":"text/event-stream"},stream=stream))) as client:
            adapter=GeminiTTS(lambda:{"GEMINI_API_KEY":"test"},client=client)
            response=await asyncio.wait_for(adapter.response(envelope()),.5)
            assert await anext(response.body_iterator)==b"\x01\x00"
            assert not gate.is_set()
            gate.set()
            assert await body(response)==b"\x02\x00"
            assert stream.closed
    asyncio.run(scenario())


@pytest.mark.parametrize("code,expected", [(401,"tts_upstream_rejected"),(429,"tts_upstream_rate_limited"),(500,"tts_upstream_rejected")])
def test_upstream_http_errors_are_safe_before_audio_and_never_retry(code,expected):
    async def scenario():
        calls, seen = [], []
        def handler(request):
            calls.append(request)
            return httpx.Response(code,text="PRIVATE-UPSTREAM-DETAILS")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter=GeminiTTS(lambda:{"GEMINI_API_KEY":"test"},client=client)
            for _ in range(2):
                with pytest.raises(TTSError,match=expected):await adapter.response(envelope(),observer=seen.append)
            assert len(calls)==2  # One request per caller attempt, no hidden retry.
            assert all(row["failure_phase"]=="before_first_audio" for row in seen if row["event"]=="tts.failed")
            assert "PRIVATE-UPSTREAM-DETAILS" not in json.dumps(seen)
    asyncio.run(scenario())


@pytest.mark.parametrize("data,error", [
    (event(rate=12000),"wrong_sample_rate"),
    (b"data: {}\n\n","empty_audio"),
    (b"data: not json\n\n","invalid_json"),
    (b'data: {"error":{"message":"PRIVATE"}}\n\n',"error_event"),
    (b'data: {"candidates":[{"content":{"parts":[{"text":"unexpected"}]}}]}\n\n',"unexpected_text"),
    (event(b"RIFFxxxx"),"unexpected_wav_header"),
    (event(b"\x01"),"incomplete_pcm_sample"),
    (event().replace(b"audio/L16",b"audio/wav"),"wrong_audio_format"),
])
def test_wrong_or_missing_audio_is_not_returned_as_success(data,error):
    async def scenario():
        seen=[];stream=Stream([data],completed=False)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,headers={"content-type":"text/event-stream"},stream=stream))) as client:
            adapter=GeminiTTS(lambda:{"GEMINI_API_KEY":"test"},client=client)
            with pytest.raises(TTSError,match=error):await adapter.response(envelope(),observer=seen.append)
            assert stream.closed
            assert seen[-1]["event"]=="tts.failed" and seen[-1]["failure_phase"]=="before_first_audio"
            assert all(row["event"]!="tts.completed" for row in seen)
    asyncio.run(scenario())


def test_midstream_error_aborts_without_repeating_or_claiming_completed_audio():
    async def scenario():
        calls,seen=[],[];stream=Stream([event(),b'data: {"error":{"message":"PRIVATE"}}\n\n'])
        def handler(request):
            calls.append(request)
            return httpx.Response(200,headers={"content-type":"text/event-stream"},stream=stream)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter=GeminiTTS(lambda:{"GEMINI_API_KEY":"test"},client=client)
            response=await adapter.response(envelope(),observer=seen.append)
            assert await anext(response.body_iterator)==b"\x01\x00\x02\x00"
            with pytest.raises(TTSError,match="error_event"):await anext(response.body_iterator)
            assert len(calls)==1 and stream.closed
            assert seen[-1]["event"]=="tts.failed" and seen[-1]["failure_phase"]=="after_first_audio"
            assert all(row["event"]!="tts.completed" for row in seen)
    asyncio.run(scenario())


def test_cancellation_closes_upstream_and_releases_capacity():
    async def scenario():
        gate=asyncio.Event();streams=[];seen=[]
        def handler(request):
            stream=Stream([event(),event()],gate);streams.append(stream)
            return httpx.Response(200,headers={"content-type":"text/event-stream"},stream=stream)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter=GeminiTTS(lambda:{"GEMINI_API_KEY":"test"},client=client,max_concurrency=1)
            response=await adapter.response(envelope(),observer=seen.append)
            assert await anext(response.body_iterator)
            with pytest.raises(TTSError,match="capacity_exhausted"):await adapter.response(envelope())
            task=asyncio.create_task(anext(response.body_iterator));await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
            assert streams[0].closed
            assert seen[-1]["event"]=="tts.cancelled"
            other=await adapter.response(envelope())
            gate.set();assert await body(other)
    asyncio.run(scenario())


def test_observer_failure_never_changes_audio_or_exports_unrequested_text():
    async def scenario():
        def broken(_):raise RuntimeError("observer unavailable")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,headers={"content-type":"text/event-stream"},content=event()+terminal()))) as client:
            adapter=GeminiTTS(lambda:{"GEMINI_API_KEY":"test"},client=client)
            assert await body(await adapter.response(envelope(),observer=broken))
    asyncio.run(scenario())


def test_prebuilt_voice_and_model_are_explicit_and_key_never_enters_body():
    async def scenario():
        calls=[]
        def handler(request):
            calls.append(request)
            return httpx.Response(200,headers={"content-type":"text/event-stream"},content=event()+terminal())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter=GeminiTTS(lambda:{"GEMINI_API_KEY":"PRIVATE-KEY","GEMINI_TTS_MODEL":"gemini-3.8-flash-tts","GEMINI_TTS_VOICE":"Puck"},client=client)
            assert await body(await adapter.response(envelope()))
            assert calls[0].url.path.endswith("gemini-3.8-flash-tts:streamGenerateContent")
            assert "PRIVATE-KEY" not in calls[0].content.decode()
            assert json.loads(calls[0].content)["generationConfig"]["speechConfig"]["voiceConfig"]["voice"]=="Puck"
    asyncio.run(scenario())


@pytest.mark.parametrize("settings", [{},{"GEMINI_API_KEY":"test","GEMINI_TTS_MODEL":"invented"},{"GEMINI_API_KEY":"test","GEMINI_TTS_VOICE":"https://example.com/key"}])
def test_bad_configuration_never_opens_an_upstream_connection(settings):
    async def scenario():
        def forbidden(_):raise AssertionError("must reject without network")
        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
            adapter=GeminiTTS(lambda:settings,client=client)
            with pytest.raises(TTSError) as error:await adapter.response(envelope())
            assert error.value.status_code==503
    asyncio.run(scenario())


def test_total_deadline_covers_first_audio_and_releases_slot():
    async def scenario():
        gate=asyncio.Event();seen=[]
        async def handler(request):
            await gate.wait()
            return httpx.Response(200,headers={"content-type":"text/event-stream"},content=event()+terminal())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter=GeminiTTS(lambda:{"GEMINI_API_KEY":"test"},client=client,max_concurrency=1,timeout_seconds=1)
            with pytest.raises(TTSError,match="deadline_exceeded"):
                await adapter.response(envelope(),observer=seen.append)
            assert seen[-1]["failure_phase"]=="before_first_audio"
            gate.set();assert await body(await adapter.response(envelope()))
    asyncio.run(scenario())


def test_odd_provider_chunks_are_joined_into_complete_pcm_samples():
    async def scenario():
        stream=Stream([event(b"\x01"),event(b"\x00\x02\x00")])
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,headers={"content-type":"text/event-stream"},stream=stream))) as client:
            adapter=GeminiTTS(lambda:{"GEMINI_API_KEY":"test"},client=client)
            assert await body(await adapter.response(envelope()))==b"\x01\x00\x02\x00"
    asyncio.run(scenario())


def test_explicit_provider_call_id_is_preserved_without_guessing_turns():
    async def scenario():
        seen=[];value=envelope()
        value["message"]["call"]={"id":"b3e52f63-ef71-40c5-97d8-adf8a472b0e2"}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,headers={"content-type":"text/event-stream"},content=event()+terminal()))) as client:
            adapter=GeminiTTS(lambda:{"GEMINI_API_KEY":"test"},client=client)
            assert await body(await adapter.response(value,observer=seen.append))
        assert all(row["provider_call_id"]==value["message"]["call"]["id"] for row in seen)
        assert all(row["provider_call_id_status"]=="provider_reported" and row["turn_id"] is None for row in seen)
    asyncio.run(scenario())


def test_stream_resource_is_closed_even_if_asgi_send_fails_before_body_starts():
    async def scenario():
        seen=[];streams=[]
        def handler(request):
            stream=Stream([event()]);streams.append(stream)
            return httpx.Response(200,headers={"content-type":"text/event-stream"},stream=stream)
        async def send(_):raise OSError("socket closed")
        async def receive():return {"type":"http.disconnect"}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter=GeminiTTS(lambda:{"GEMINI_API_KEY":"test"},client=client,max_concurrency=1)
            response=await adapter.response(envelope(),observer=seen.append)
            with pytest.raises(ClientDisconnect):await response({"type":"http","asgi":{"spec_version":"2.4"}},receive,send)
            assert streams[0].closed and not adapter.capacity.locked()
            assert seen[-1]["event"]=="tts.cancelled" and seen[-1]["failure_phase"]=="before_first_audio"
    asyncio.run(scenario())


@pytest.mark.parametrize("rate", [8000, 16000, 22050, 44100])
def test_real_google_24k_pcm_is_resampled_with_correct_duration_and_frequency(rate):
    import numpy as np

    async def scenario():
        source = np.rint(12000 * np.sin(2 * np.pi * 1000 * np.arange(24000) / 24000)).astype("<i2")
        # Odd byte boundaries exercise sample reassembly before rate conversion.
        raw = source.tobytes()
        pieces = [raw[i:i+1921] for i in range(0, len(raw), 1921)]
        stream = Stream([event(piece, 24000) for piece in pieces])
        seen=[]
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,headers={"content-type":"text/event-stream"},stream=stream))) as client:
            adapter=GeminiTTS(lambda:{"GEMINI_API_KEY":"test"},client=client)
            output=np.frombuffer(await body(await adapter.response(envelope(rate=rate),observer=seen.append)),dtype="<i2")
        assert abs(len(output)-rate)<=1
        peak=np.argmax(abs(np.fft.rfft(output))) * rate / len(output)
        assert abs(peak-1000)<2
        # Correctly converted speech keeps pitch and amplitude rather than playing
        # 24k samples as a lower rate, which triples duration and lowers pitch.
        assert 7000 < float(np.sqrt(np.mean(output.astype(float)**2))) < 9000
        assert seen[-1]["upstream_sample_rate"]==24000
        assert seen[-1]["sample_rate"]==rate and seen[-1]["resampled"] is True
        assert seen[-1]["resampler"]=="soxr_hq"
        assert seen[-1]["audio_duration_ms"]==pytest.approx(1000,abs=.13)
    asyncio.run(scenario())


def test_phone_resampler_rejects_aliasing_above_telephone_nyquist():
    import numpy as np

    def rms(frequency):
        source=np.rint(12000*np.sin(2*np.pi*frequency*np.arange(24000)/24000)).astype("<i2")
        converter=PCMRateConverter(24000,8000)
        converted=converter.convert(source.tobytes())+converter.convert(b"",last=True)
        samples=np.frombuffer(converted,dtype="<i2").astype(float)[400:-400]
        return float(np.sqrt(np.mean(samples**2)))
    assert rms(6000)<rms(1000)*.001


def test_resampler_keeps_filter_state_across_chunks_and_flushes_tail_once():
    import numpy as np
    source=np.rint(12000*np.sin(2*np.pi*919*np.arange(24000)/24000)).astype("<i2").tobytes()
    whole=PCMRateConverter(24000,8000)
    expected=whole.convert(source)+whole.convert(b"",last=True)
    split=PCMRateConverter(24000,8000)
    actual=b"".join(split.convert(source[i:i+1920]) for i in range(0,len(source),1920))+split.convert(b"",last=True)
    # Integer quantization and libsoxr dither can differ by a few least
    # significant bits across independent converter instances.
    assert len(actual)==len(expected)==16000
    assert abs(np.frombuffer(actual,dtype="<i2").astype(int)-np.frombuffer(expected,dtype="<i2").astype(int)).max()<=3


def test_resampled_first_audio_is_ready_before_later_network_chunks():
    async def scenario():
        gate=asyncio.Event()
        # One 80ms real Google first packet is enough for the HQ filter to emit.
        first=b"\x20\x00"*1920
        stream=Stream([event(first,24000),event(first,24000)],gate)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,headers={"content-type":"text/event-stream"},stream=stream))) as client:
            adapter=GeminiTTS(lambda:{"GEMINI_API_KEY":"test"},client=client)
            response=await asyncio.wait_for(adapter.response(envelope(rate=8000)),.5)
            assert await anext(response.body_iterator)
            assert not gate.is_set()
            gate.set()
            assert await body(response)
    asyncio.run(scenario())


@pytest.mark.parametrize("stop_text", ["Unexpected speech.", " ", None, 0])
def test_terminal_text_is_never_treated_as_spoken_pcm(stop_text):
    async def scenario():
        bad=b"data: "+json.dumps({"candidates":[{"content":{"parts":[{"text":stop_text}]},"finishReason":"STOP"}]}).encode()+b"\n\n"
        stream=Stream([event(),bad],completed=False)
        seen=[]
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,headers={"content-type":"text/event-stream"},stream=stream))) as client:
            adapter=GeminiTTS(lambda:{"GEMINI_API_KEY":"test"},client=client)
            response=await adapter.response(envelope(),observer=seen.append)
            assert await anext(response.body_iterator)
            with pytest.raises(TTSError,match="unexpected_text"):await body(response)
            assert seen[-1]["event"]=="tts.failed"
    asyncio.run(scenario())


def test_empty_text_before_audio_does_not_pass_as_terminal_metadata():
    async def scenario():
        stream=Stream([terminal(),event()],completed=False)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,headers={"content-type":"text/event-stream"},stream=stream))) as client:
            adapter=GeminiTTS(lambda:{"GEMINI_API_KEY":"test"},client=client)
            with pytest.raises(TTSError,match="unexpected_text"):await adapter.response(envelope())
    asyncio.run(scenario())


def test_provider_eof_without_stop_is_a_failed_truncated_utterance():
    async def scenario():
        seen=[]
        stream=Stream([event()],completed=False)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,headers={"content-type":"text/event-stream"},stream=stream))) as client:
            adapter=GeminiTTS(lambda:{"GEMINI_API_KEY":"test"},client=client)
            response=await adapter.response(envelope(),observer=seen.append)
            assert await anext(response.body_iterator)
            with pytest.raises(TTSError,match="incomplete_generation"):await body(response)
        assert seen[-1]["event"]=="tts.failed" and seen[-1]["failure_phase"]=="after_first_audio"
    asyncio.run(scenario())


@pytest.mark.parametrize("modified,code", [
    (lambda b:b.replace(b"rate=16000",b"rate=999999"),"wrong_sample_rate"),
    (lambda b:b.replace(b";rate=16000",b""),"missing_sample_rate"),
    (lambda b:b.replace(b"rate=16000",b"rate=16000;channels=2"),"wrong_channel_count"),
])
def test_ambiguous_or_unsupported_source_pcm_metadata_fails_closed(modified,code):
    async def scenario():
        stream=Stream([modified(event())])
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,headers={"content-type":"text/event-stream"},stream=stream))) as client:
            adapter=GeminiTTS(lambda:{"GEMINI_API_KEY":"test"},client=client)
            with pytest.raises(TTSError,match=code):await adapter.response(envelope())
    asyncio.run(scenario())


def test_provider_cannot_change_sample_rate_mid_utterance():
    async def scenario():
        stream=Stream([event(),event(rate=24000)])
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,headers={"content-type":"text/event-stream"},stream=stream))) as client:
            adapter=GeminiTTS(lambda:{"GEMINI_API_KEY":"test"},client=client)
            response=await adapter.response(envelope())
            assert await anext(response.body_iterator)
            with pytest.raises(TTSError,match="sample_rate_changed"):await body(response)
    asyncio.run(scenario())


def test_resampler_dependency_failure_rejects_instead_of_mislabeling_24k_bytes(monkeypatch):
    import builtins
    original=builtins.__import__
    def unavailable(name,*args,**kwargs):
        if name=="soxr":raise ImportError("synthetic missing optional module")
        return original(name,*args,**kwargs)
    monkeypatch.setattr(builtins,"__import__",unavailable)
    async def scenario():
        seen=[];requests=[]
        def handler(request):
            requests.append(request)
            raise AssertionError("readiness must reject before upstream access")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter=GeminiTTS(lambda:{"GEMINI_API_KEY":"test"},client=client,max_concurrency=1)
            with pytest.raises(TTSError,match="resampler_unavailable"):await adapter.response(envelope(rate=8000),observer=seen.append)
            assert not requests and not adapter.capacity.locked() and not adapter.resampler_ready
            assert seen[-1]["event"]=="tts.failed" and seen[-1]["failure_phase"]=="before_first_audio"
    asyncio.run(scenario())


def test_cancellation_during_resampler_buffering_releases_connection_and_slot():
    async def scenario():
        gate=asyncio.Event()
        # Two samples are valid PCM but too short for the HQ filter to emit.
        stream=Stream([event(rate=24000),event(rate=24000)],gate)
        seen=[]
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,headers={"content-type":"text/event-stream"},stream=stream))) as client:
            adapter=GeminiTTS(lambda:{"GEMINI_API_KEY":"test"},client=client,max_concurrency=1)
            pending=asyncio.create_task(adapter.response(envelope(rate=8000),observer=seen.append))
            await asyncio.sleep(.01)
            assert adapter.capacity.locked()
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):await pending
            assert stream.closed and not adapter.capacity.locked()
            assert seen[-1]["event"]=="tts.cancelled" and seen[-1]["failure_phase"]=="before_first_audio"
    asyncio.run(scenario())
