"""Conservative edge-only processing for pre-generated 16kHz mono PCM clips.

Sample values, playback rate, and every interior pause remain unchanged. The
threshold detects very quiet sound; it is not semantic speech recognition or
proof that a phoneme was preserved. Listening approval remains separate.
"""
from __future__ import annotations

import hashlib
import struct

PROCESSING_VERSION = "edge-rms5-peak16-pad100-min100-v1"
SAMPLE_RATE = 16000
FRAME_MS = 10
PADDING_MS = 100
MINIMUM_REMOVAL_MS = 100
RMS_THRESHOLD = 5
PEAK_THRESHOLD = 16
MAX_SOURCE_SECONDS = 2.5


class PCMProcessingError(ValueError):
    """Fixed processing codes, without audio, user text, or external messages."""


def processing_version(rms_threshold=RMS_THRESHOLD):
    if type(rms_threshold) is not int or rms_threshold not in {5, 40}:
        raise PCMProcessingError("backchannel_pcm_invalid_threshold")
    peak = max(PEAK_THRESHOLD, rms_threshold * 3)
    return f"edge-rms{rms_threshold}-peak{peak}-pad100-min100-v1"


def trim_edge_silence(pcm, sample_rate=SAMPLE_RATE, *, rms_threshold=RMS_THRESHOLD):
    """Return exact PCM slice and deterministic, content-free provenance.

    Default: a 10ms frame is active at RMS >=5 OR absolute peak >=16 on PCM16's
    full scale of 32768. Retain at least 100ms around first/last active frames;
    remove an edge only if at least 100ms can be removed there. Low-amplitude
    detail in the retained region is copied byte for byte, with no fades,
    normalization, time stretching, concatenation, or interior silence edits.
    Explicit threshold40 is an experimental candidate processing mode with a
    peak threshold of120; it may remove very quiet sounds and requires listening
    before promotion. It is never selected automatically from the waveform.
    """
    version = processing_version(rms_threshold)
    peak_threshold = max(PEAK_THRESHOLD, rms_threshold * 3)
    if (type(sample_rate) is not int or sample_rate != SAMPLE_RATE
        or not isinstance(pcm, bytes) or not pcm or len(pcm) % 2
        or len(pcm) > int(sample_rate * 2 * MAX_SOURCE_SECONDS)):
        raise PCMProcessingError("backchannel_pcm_invalid")
    samples = [sample[0] for sample in struct.iter_unpack("<h", pcm)]
    frame_size = sample_rate * FRAME_MS // 1000
    first, last = None, None
    for offset in range(0, len(samples), frame_size):
        frame = samples[offset:offset + frame_size]
        active = (sum(value * value for value in frame) >= rms_threshold ** 2 * len(frame)
            or max(abs(value) for value in frame) >= peak_threshold)
        if active:
            if first is None:
                first = offset
            last = offset + len(frame)
    if first is None:
        raise PCMProcessingError("backchannel_pcm_silent")
    padding = sample_rate * PADDING_MS // 1000
    minimum = sample_rate * MINIMUM_REMOVAL_MS // 1000
    leading = max(0, first - padding)
    trailing = max(0, len(samples) - last - padding)
    leading = leading if leading >= minimum else 0
    trailing = trailing if trailing >= minimum else 0
    stop = len(samples) - trailing
    processed = pcm[leading * 2:stop * 2]
    milliseconds = lambda count: round(count / sample_rate * 1000, 6)
    provenance = {"processing_version": version,
        "processing_mode": "conservative_default" if rms_threshold == RMS_THRESHOLD else "experimental_requires_listening",
        "source_pcm_sha256": hashlib.sha256(pcm).hexdigest(),
        "output_pcm_sha256": hashlib.sha256(processed).hexdigest(),
        "sample_rate": sample_rate, "channels": 1, "sample_width": 2,
        "original_duration_ms": milliseconds(len(samples)),
        "processed_duration_ms": milliseconds(len(processed) // 2),
        "removed_leading_ms": milliseconds(leading), "removed_trailing_ms": milliseconds(trailing),
        "configured_edge_padding_ms": PADDING_MS,
        "retained_leading_padding_ms": milliseconds(first - leading),
        "retained_trailing_padding_ms": milliseconds(len(samples) - trailing - last),
        "minimum_edge_removal_ms": MINIMUM_REMOVAL_MS,
        "frame_ms": FRAME_MS, "rms_threshold_pcm16": rms_threshold,
        "peak_threshold_pcm16": peak_threshold, "changed": bool(leading or trailing),
        "interior_samples": "unchanged", "audible_content_verification": "not_performed"}
    return processed, provenance

