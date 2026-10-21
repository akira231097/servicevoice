"""Conservative edge slices preserve quiet sounds and every interior sample."""
import hashlib
import json
from pathlib import Path
import struct

import pytest

from app.backchannel_audio import BackchannelAudioCache
from app.backchannel_pcm import PCMProcessingError, PROCESSING_VERSION, processing_version, trim_edge_silence
from scripts.prepare_backchannel_audio import main, process_manifest

RATE = 16000


def silence(ms):
    return b"\x00\x00" * (RATE * ms // 1000)


def sound(ms, amplitude=1000):
    return struct.pack("<hh", amplitude, -amplitude) * (RATE * ms // 2000)


def test_edges_shorten_1200ms_to_670ms_with_100ms_padding_and_exact_interior():
    source = silence(350) + sound(470) + silence(380)
    output, proof = trim_edge_silence(source)
    assert output == silence(100) + sound(470) + silence(100)
    assert proof["original_duration_ms"] == 1200
    assert proof["processed_duration_ms"] == 670
    assert proof["removed_leading_ms"] == 250 and proof["removed_trailing_ms"] == 280
    assert proof["retained_leading_padding_ms"] == proof["retained_trailing_padding_ms"] == 100
    assert proof["processing_version"] == PROCESSING_VERSION
    assert proof["source_pcm_sha256"] == hashlib.sha256(source).hexdigest()
    assert proof["output_pcm_sha256"] == hashlib.sha256(output).hexdigest()
    assert proof["interior_samples"] == "unchanged" and proof["audible_content_verification"] == "not_performed"
    assert json.loads(json.dumps(proof, allow_nan=False)) == proof


def test_very_quiet_onsets_and_endings_remain_byte_exact():
    # RMS 6 and 7 are below -73dBFS, far below ordinary speech. Include an
    # interior pause so detection cannot trim to the loud central region.
    quiet_start, quiet_end = sound(100, 6), sound(80, 7)
    interior = quiet_start + silence(100) + sound(300) + quiet_end
    source = silence(400) + interior + silence(300)
    output, proof = trim_edge_silence(source)
    assert output == silence(100) + interior + silence(100)
    assert proof["removed_leading_ms"] == 300 and proof["removed_trailing_ms"] == 200
    assert quiet_start in output and quiet_end in output


def test_sparse_quiet_peak_below_frame_rms_threshold_is_still_preserved():
    sparse = bytearray(silence(10))
    sparse[40:42] = struct.pack("<h", 16)
    interior = bytes(sparse) + silence(100) + sound(200)
    source = silence(400) + interior + silence(300)
    output, proof = trim_edge_silence(source)
    assert output == silence(100) + interior + silence(100)
    assert proof["removed_leading_ms"] == 300


def test_interior_silence_is_never_removed_or_compressed():
    interior = sound(100) + silence(500) + sound(100)
    output, proof = trim_edge_silence(silence(300) + interior + silence(300))
    assert output == silence(100) + interior + silence(100)
    assert proof["processed_duration_ms"] == 900


def test_edges_with_less_than_100ms_removable_are_left_unchanged():
    source = silence(150) + sound(200) + silence(190)
    output, proof = trim_edge_silence(source)
    assert output == source
    assert proof["removed_leading_ms"] == proof["removed_trailing_ms"] == 0
    assert proof["changed"] is False


def test_edges_are_trimmed_independently_without_adding_unavailable_padding():
    source = sound(200) + silence(300)
    output, proof = trim_edge_silence(source)
    assert output == sound(200) + silence(100)
    assert proof["retained_leading_padding_ms"] == 0
    assert proof["retained_trailing_padding_ms"] == 100
    assert proof["removed_leading_ms"] == 0 and proof["removed_trailing_ms"] == 200


def test_processing_is_idempotent_and_never_changes_playback_sample_values():
    source = silence(300) + sound(470) + silence(300)
    once, proof = trim_edge_silence(source)
    twice, repeated = trim_edge_silence(once)
    begin, end = int(proof["removed_leading_ms"] * RATE / 1000) * 2, len(source) - int(proof["removed_trailing_ms"] * RATE / 1000) * 2
    assert source[begin:end] == once == twice
    assert repeated["changed"] is False
    assert proof["sample_rate"] == RATE and proof["channels"] == 1 and proof["sample_width"] == 2


def test_experimental_threshold_is_explicit_versioned_and_still_keeps_edge_padding():
    # A low noise floor makes the default decline to trim. Explicit candidate
    # mode may trim noise, but cannot claim that removed sound was non-speech.
    source = sound(350, 8) + sound(470) + sound(380, 8)
    original, default = trim_edge_silence(source)
    output, experiment = trim_edge_silence(source, rms_threshold=40)
    assert original == source and default["changed"] is False
    assert output == sound(100, 8) + sound(470) + sound(100, 8)
    assert experiment["processed_duration_ms"] == 670
    assert experiment["retained_leading_padding_ms"] == experiment["retained_trailing_padding_ms"] == 100
    assert experiment["processing_version"] == "edge-rms40-peak120-pad100-min100-v1"
    assert experiment["processing_version"] != default["processing_version"]
    assert experiment["processing_mode"] == "experimental_requires_listening"
    assert experiment["audible_content_verification"] == "not_performed"


@pytest.mark.parametrize("threshold", [True, 0, 6, 100, 40.0, "40"])
def test_threshold_cannot_change_silently_or_accept_arbitrary_values(threshold):
    with pytest.raises(PCMProcessingError, match="invalid_threshold"):
        trim_edge_silence(sound(200), rms_threshold=threshold)


@pytest.mark.parametrize("source", [b"", b"\x01", bytearray(b"\x01\x00"), b"\x01\x00" * 40001],
                         ids=["empty", "unaligned", "mutable", "overlong"])
def test_invalid_geometry_is_rejected(source):
    with pytest.raises(PCMProcessingError, match="backchannel_pcm_invalid"):
        trim_edge_silence(source)


@pytest.mark.parametrize("source", [silence(500), sound(500, 1), sound(500, 4)],
                         ids=["zero", "inaudible_one", "inaudible_four"])
def test_silent_or_subthreshold_clips_are_rejected(source):
    with pytest.raises(PCMProcessingError, match="backchannel_pcm_silent"):
        trim_edge_silence(source)


@pytest.mark.parametrize("rate", [True, 8000, 24000, 16000.0, "16000"])
def test_trim_never_relabels_sample_rate(rate):
    with pytest.raises(PCMProcessingError, match="backchannel_pcm_invalid"):
        trim_edge_silence(sound(200), rate)


def test_offline_manifest_processing_preserves_originals_and_links_provenance(tmp_path):
    directory = tmp_path / "bank"
    cache = BackchannelAudioCache(directory)
    original_pcm = silence(350) + sound(470) + silence(380)
    old_asset = cache.store("neutral_ack", original_pcm)
    old_manifest = cache.write_manifest([old_asset])
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    report = process_manifest(old_manifest, cache)
    assert report["provider_requests"] == 0 and report["listening_approval"] == "not_recorded"
    assert report["processing_version"] == PROCESSING_VERSION
    assert report["source_manifest_sha256"] == hashlib.sha256(before[old_manifest.name]).hexdigest()
    assert Path(report["manifest"]) != old_manifest
    assert all((directory / name).read_bytes() == data for name, data in before.items())
    row = report["phrases"][0]
    assert row["source_asset_id"] == old_asset["asset_id"]
    assert row["output_asset_id"] != old_asset["asset_id"]
    assert row["processing"]["source_pcm_sha256"] == old_asset["pcm_sha256"]
    assert row["processing"]["original_duration_ms"] == 1200
    assert row["duration_ms"] == row["processing"]["processed_duration_ms"] == 670
    assert row["new_synthesis_observed"] is False
    output = cache.lookup_pcm("Mm-hmm.", 16000)[0]
    assert row["processing"]["output_pcm_sha256"] == hashlib.sha256(output).hexdigest()


def test_offline_cli_does_not_read_credentials_or_make_provider_requests(tmp_path, monkeypatch, capsys):
    import dotenv
    import scripts.prepare_backchannel_audio as prepare
    cache = BackchannelAudioCache(tmp_path / "source")
    manifest = cache.write_manifest([cache.store("neutral_ack", silence(300) + sound(300) + silence(300))])
    monkeypatch.setattr(dotenv, "dotenv_values", lambda *_: pytest.fail("offline trim read a credential file"))
    monkeypatch.setattr(prepare, "GeminiTTS", lambda *_: pytest.fail("offline trim created a provider client"))
    assert main(["--trim-manifest", str(manifest), "--output-dir", str(tmp_path / "processed"),
                 "--env-file", str(tmp_path / "not-read.env")]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["provider_requests"] == 0 and report["phrases"][0]["duration_ms"] == 500


def test_offline_cli_explicit_experiment_keeps_original_bank_and_labels_unapproved(tmp_path, capsys):
    cache = BackchannelAudioCache(tmp_path / "source")
    manifest = cache.write_manifest([cache.store("neutral_ack", sound(350, 8) + sound(470) + sound(380, 8))])
    before = {path.name: path.read_bytes() for path in manifest.parent.iterdir()}
    assert main(["--trim-manifest", str(manifest), "--output-dir", str(tmp_path / "candidate40"), "--rms-threshold", "40"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["processing_version"] == processing_version(40)
    assert report["provider_requests"] == 0 and report["listening_approval"] == "not_recorded"
    assert report["phrases"][0]["processing"]["processing_mode"] == "experimental_requires_listening"
    assert report["phrases"][0]["duration_ms"] == 670
    assert all((manifest.parent / name).read_bytes() == data for name, data in before.items())
