"""Caller-facing regressions from the third independent review."""
import asyncio
import pytest
import httpx
import io
import wave

from app.twilio_media_health import OpeningMedia
from app.twilio_bridge import TwilioClient, TwilioConfig, signature
from app.main import create_public_app
from test_twilio_api import lane_app, Provider


@pytest.mark.parametrize('recognition_gap', [False, True])
def test_suspected_media_failure_is_observation_only(lane_app, recognition_gap):
    async def run():
        lane = lane_app.state.twilio_lane
        class NeverReconnect(Provider):
            async def recovery_announcement(self, *args):
                raise AssertionError('Must not alter an uncertain working call')
            async def detach_agent(self, *args):
                raise AssertionError('Must not detach a working agent')
        lane.client = NeverReconnect()
        row = lane.store.allocate('CA'+'1'*32)
        lane.store.claim_dial(row['id'], 'CF'+'2'*32, row['caller_sid'])
        lane.store.set_leg(row['id'], 'CA'+'3'*32)
        lane.store.joined(row['id'])
        with lane.store.db() as db:
            db.execute("UPDATE twilio_bridges SET provider_call_id='fixture',session_id='session' WHERE id=?", (row['id'],))
        now = lane.coordinator.clock()
        media = OpeningMedia(now-12, greeting=now-11, caller_bursts=2, vapi_user_bursts=1)
        if recognition_gap:
            media.provider({'type':'speech-update','role':'user','status':'started'}, now)
        lane.media[row['id']] = media
        lane.recovery_safe = lambda *args: True
        await lane.recover_opening(row['id'])
        fresh = lane.store.get(row['id'])
        assert fresh['state'] == 'joined'
        assert fresh['recovery_count'] == 0
        await lane.close()
    asyncio.run(run())


@pytest.mark.parametrize('remaining', [.93, 1.32, 1.76])
def test_ready_answer_is_not_held_before_synthesis(lane_app, remaining):
    async def run():
        lane = lane_app.state.twilio_lane
        lane.coordinator.register('call', 'session')
        state = lane.coordinator.calls['call']
        state.clip_until = lane.coordinator.clock()+remaining
        state.clip_pending_until = lane.coordinator.clock()+.75
        lane.announcements['ticket'] = {'call_id':'call'}
        async with asyncio.timeout(.05):
            await lane.before_answer('call', 'session')
        assert state.answer_ready and state.clip_pending_until == 0
        assert not lane.announcements
        # Retain the playback estimate for waiting-cue gating/diagnostics.
        assert state.clip_until > lane.coordinator.clock()
        await lane.coordinator.close()
    asyncio.run(run())


def test_cleanup_retry_backs_off_per_bridge_and_resets_on_success(lane_app):
    async def run():
        lane = lane_app.state.twilio_lane
        lane.client = Provider()
        now = [100.0]
        lane.coordinator.clock = lambda: now[0]
        row = lane.store.allocate('CA'+'1'*32)
        lane.store.end(row['id'])
        calls = []
        async def unavailable(*args, **kwargs):
            calls.append(now[0])
            raise OSError('provider unavailable')
        lane._finish = unavailable
        for tick in range(60):
            now[0] = 100+tick
            await lane.sweep_once()
        assert calls == [100, 102, 106, 114, 130]
        now[0] = 160
        async def recovered(bridge, **kwargs): lane.store.end(bridge, verified=True)
        lane._finish = recovered
        await lane.sweep_once()
        assert not lane.cleanup_retry
        await lane.close()
    asyncio.run(run())


def test_pause_is_preferred_but_continuous_speech_has_bounded_opportunity():
    from test_listening_cues import setup, TEXT
    c, clock, _, _, _, _, _ = setup()
    s = c.calls['fixture-call']
    s.text = TEXT; s.last_cue = 1; s.cue_interval = 4.7
    clock.now = 5.8; s.received = clock.now
    assert c.timing_reason(s) == 'prefer_pause'
    s.caller = False; s.waiting_stopped = clock.now-.4
    assert c.timing_reason(s) == 'eligible'
    s.caller = True; clock.now = 7.21; s.received = clock.now
    assert c.timing_reason(s) == 'eligible'


def test_neutral_rotation_tracks_actual_fetch_and_alternates_takes(lane_app):
    lane = lane_app.state.twilio_lane
    lane.coordinator.register('call', 'session')
    state = lane.coordinator.calls['call']
    lane.clips = dict.fromkeys(('neutral_ack', 'uh_huh_1', 'uh_huh_2'))
    selected = []
    for _ in range(6):
        state.phrase_id = 'right_1'  # All requests need a pause substitution.
        choice = lane.neutral_clip(state)
        selected.append(choice)
        state.last_delivered_phrase = choice
    assert selected == ['neutral_ack', 'uh_huh_1', 'neutral_ack', 'uh_huh_2', 'neutral_ack', 'uh_huh_1']


def test_silent_refusals_have_reasons_and_a_rate_alert(lane_app):
    lane = lane_app.state.twilio_lane
    observed = []
    lane.app.state.diagnose = lambda *a, **k: observed.append((a, k))
    for _ in range(8): lane.clip_outcome('call')
    lane.clip_outcome('call', 'ticket_expired')
    assert not lane.clip_refusal_alert
    lane.clip_outcome('call', 'speaker_priority')
    assert lane.clip_refusal_alert
    assert lane.clip_refusals == {'ticket_expired':1, 'speaker_priority':1}
    assert len(observed) == 1 and observed[0][0][1:3] == ('clip_refusal_alert', 'failed')
    for _ in range(100): lane.clip_outcome('call')
    assert not lane.clip_refusal_alert


def test_reconnect_cannot_accidentally_be_enabled(lane_app):
    import os
    with pytest.raises(ValueError, match='Automatic opening recovery is unavailable'):
        TwilioConfig.from_mapping({**os.environ, 'SERVICEVOICE_TWILIO_OPENING_RECOVERY':'on'})


def test_twilio_connections_survive_normal_gaps_between_cues(lane_app, monkeypatch):
    limits = []
    class Client:
        def __init__(self, **kwargs): limits.append(kwargs['limits'])
    monkeypatch.setattr('app.twilio_bridge.httpx.AsyncClient', Client)
    TwilioClient(lane_app.state.twilio_lane.config)
    assert limits[0].keepalive_expiry == 30


def test_signed_fetch_rotates_actual_pause_audio_and_reports_expiry(lane_app):
    async def run():
        lane = lane_app.state.twilio_lane; lane.client = Provider()
        row = lane.store.allocate('CA'+'1'*32)
        lane.store.claim_dial(row['id'], 'CF'+'2'*32, row['caller_sid'])
        with lane.store.db() as db:
            db.execute("UPDATE twilio_bridges SET state='joined',provider_call_id='fixture' WHERE id=?", (row['id'],))
        lane.bindings['fixture'] = (row['id'], 'session')
        lane.coordinator.register('fixture', 'session')
        state = lane.coordinator.calls['fixture']; state.assistant = False; state.caller = False
        payloads = {}
        for i, key in enumerate(('neutral_ack','uh_huh_1','uh_huh_2','right_1'), 1):
            buf = io.BytesIO()
            with wave.open(buf,'wb') as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000); w.writeframes(bytes((i,0))*4000)
            payloads[key] = buf.getvalue()
        lane.wav = payloads['neutral_ack']; lane.clips = {k:(v,'a'*64) for k,v in payloads.items()}
        actual = []
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_public_app(lane_app)), base_url='https://test') as client:
            for n in range(5):
                ticket = 'ticket'+str(n)
                lane.announcements[ticket] = {'call_id':'fixture','bridge_id':row['id'],'phrase_id':'right_1',
                    'expires':lane.coordinator.clock()+(.75 if n<4 else -1)}
                suffix = 'clip/neutral_ack.wav?bridge='+row['id']+'&ticket='+ticket+'&cue=right_1'
                response = await client.get('/twilio/'+suffix, headers={'x-twilio-signature':signature(lane.config.auth_token,lane.config.url(suffix),[])})
                assert response.status_code == 200
                if n<4:
                    actual.append(next(k for k,v in payloads.items() if v==response.content))
                else:
                    from test_twilio_silent_announcements import assert_silence
                    assert_silence(response)
            assert actual == ['neutral_ack','uh_huh_1','neutral_ack','uh_huh_2']
            assert lane.clip_refusals == {'ticket_expired':1}
            assert list(lane.clip_outcomes) == [False,False,False,False,True]
        await lane.close()
    asyncio.run(run())


@pytest.mark.parametrize('local_veto', [True, False])
def test_only_proven_pre_request_veto_restores_listening_budget(local_veto):
    from app.listening_cues import CueNotSent
    from app.twilio_bridge import BridgeError
    from test_listening_cues import setup, TEXT, drain
    async def run():
        c, clock, _, _, events, _, _ = setup()
        state = c.calls['fixture-call'];state.text = TEXT;state.received = clock.now
        async def dispatch(*args):
            raise CueNotSent('local veto') if local_veto else BridgeError('announcement_outcome_unknown')
        c.dispatch = dispatch
        await c._decide('fixture-call', state, state.epoch, TEXT)
        await drain(c)
        assert state.count == (0 if local_veto else 1)
        assert state.turn_count == (0 if local_veto else 1)
        assert (state.last_cue < 0) is local_veto
        assert any(e[-1]=='not_dispatched' for e in events) is local_veto
        await c.close()
    asyncio.run(run())


def test_cancelled_decision_with_local_dispatch_veto_does_not_burn_cooldown():
    from app.listening_cues import CueNotSent
    from test_listening_cues import setup, TEXT, drain
    async def run():
        c, clock, _, _, _, event, _ = setup()
        started, release = asyncio.Event(), asyncio.Event()
        async def dispatch(*args):
            started.set(); await release.wait(); raise CueNotSent('local veto')
        c.dispatch = dispatch
        event('transcript', 'user', transcriptType='partial', transcript=TEXT)
        await started.wait()
        event('user-interrupted')
        release.set(); await drain(c)
        state = c.calls['fixture-call']
        assert state.count == 0 and state.turn_count == 0 and state.last_cue < 0
        await c.close()
    asyncio.run(run())


def test_interruption_clears_old_classification_context_without_refilling_waiting_budget():
    from app.listening_cues import listening_gate
    from test_listening_cues import setup, TEXT, SAFE
    c, clock, _, _, _, event, _ = setup()
    s = c.calls['fixture-call'];s.completed = s.text = TEXT;s.waiting_count = 2
    s.waiting_first = 1
    event('user-interrupted')
    assert s.text == s.completed == s.segment == ''
    assert s.waiting_count == 2 and s.waiting_first == 1
    assert s.caller_started == clock.now
    for prefix in ('Actually, I meant Wednesday', 'Actually, I meant Wednesday, not Tuesday.'):
        assert listening_gate(TEXT+' '+prefix, 'Tell me about your car.', SAFE) == 'control_content'
