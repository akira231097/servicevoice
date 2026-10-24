import pytest

from app.twilio_media_health import OpeningMedia
from app.twilio_bridge import BridgeError, BridgeStore
from test_twilio_bridge import config


def greeting(media):
    media.provider({"type": "speech-update", "role": "assistant", "status": "started"}, 1)


def bursts(media):
    for i in range(2):
        media.conference("caller", "start", str(i*2), 4+i*2)
        media.conference("caller", "stop", str(i*2+1), 4.5+i*2)


def test_silence_or_one_noise_burst_never_triggers_opening_recovery():
    media = OpeningMedia(0)
    greeting(media)
    assert media.suspicion(12) is None
    media.conference("caller", "start", "1", 3)
    media.conference("caller", "stop", "2", 3.05)
    assert media.suspicion(12) is None
    media.conference("caller", "start", "3", 4)
    media.conference("caller", "stop", "4", 4.6)
    assert media.suspicion(12) is None


def test_repeated_caller_energy_without_words_is_suspect_not_a_diagnosis():
    media = OpeningMedia(0)
    greeting(media); bursts(media)
    assert media.suspicion(8) is None
    assert media.suspicion(9) == "caller_energy_without_words"
    assert media.suspicion(46) is None
    media.provider({"type": "transcript", "role": "user", "transcript": "Hello"}, 10)
    assert media.suspicion(12) is None


def test_recovery_requires_correlated_vad_if_agent_audio_reached_twilio():
    media = OpeningMedia(0)
    greeting(media); bursts(media)
    media.conference("agent", "start", "20", 1)
    media.conference("agent", "stop", "21", 3)
    assert media.suspicion(12) is None
    media.provider({"type": "speech-update", "role": "user", "status": "started"}, 5)
    assert media.suspicion(12) == "caller_energy_without_words"


def test_duplicate_and_unidentified_speaker_events_are_not_more_evidence():
    media = OpeningMedia(0)
    greeting(media)
    for _ in range(3):
        media.conference("caller", "start", "1", 4)
        media.conference("caller", "stop", "2", 4.5)
        media.conference("caller", "start", None, 6)
        media.conference("caller", "stop", None, 6.5)
    assert media.caller_bursts == 1 and media.suspicion(12) is None


def test_recovery_is_durable_once_and_invalidates_old_proof(tmp_path):
    store = BridgeStore(tmp_path/"bridge.sqlite3", clock=lambda: 1000)
    cfg = config()
    row = store.allocate("CA"+"1"*32)
    store.claim_dial(row["id"], "CF"+"2"*32, row["caller_sid"])
    store.set_leg(row["id"], "CA"+"3"*32)
    store.joined(row["id"])
    with store.db() as db:
        db.execute("UPDATE twilio_bridges SET provider_call_id='old',session_id='session' WHERE id=?", (row["id"],))
    old = store.get(row["id"])
    assert store.claim_recovery(row["id"])
    assert not store.claim_recovery(row["id"])
    store.reset_recovered_leg(row["id"])
    new = store.get(row["id"])
    assert store.proof(cfg, old) != store.proof(cfg, new)
    assert new["provider_call_id"] is None and new["vapi_leg_sid"] is None and new["dial_pending"] == 1
    assert store.retired_leg(row["id"], old["vapi_leg_sid"])
    store.set_leg(row["id"], "CA"+"4"*32); store.joined(row["id"])
    restarted = BridgeStore(tmp_path/"bridge.sqlite3")
    assert not restarted.claim_recovery(row["id"])
    with pytest.raises(BridgeError): restarted.reset_recovered_leg(row["id"])
