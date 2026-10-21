"""Digital synthetic loopback evidence. No provider, TLS/SRTP or PSTN claim."""
import audioop
import hashlib
import struct
from verify_pcap import packets, sip, sdp, rtp


def encode_asterisk_ulaw(pcm):
    """Asterisk20 default AST_LIN2MU table, including its4-low-bit bucket edge.

    Primary contract: asterisk/asterisk20 main/ulaw.c and include/asterisk/ulaw.h.
    This is an exact codec reference, not a waveform tolerance or fuzzy match.
    """
    values = []
    for (sample,) in struct.iter_unpack('<h', pcm):
        sample = (sample & 0xfffc) | 3
        if sample >= 32768:
            sample -= 65536
        sign = 128 if sample < 0 else 0
        magnitude = min(abs(sample), 32635) + 132
        exponent = max(0, magnitude.bit_length() - 8)
        values.append((~(sign | (exponent << 4) | ((magnitude >> (exponent + 3)) & 15))) & 255)
    return bytes(values)


def assert_pcm_media(caller_pcmu, bridge_input, expected_pcm, assistant_pcmu):
    decoded = audioop.ulaw2lin(caller_pcmu, 2)
    if bridge_input.find(decoded) < 0:
        raise ValueError('caller_pcm_not_preserved_into_real_node_bridge')
    expected = encode_asterisk_ulaw(expected_pcm)
    offset = assistant_pcmu.find(expected)
    if offset < 0:
        raise ValueError('complete_cue_and_main_pcmu_not_preserved')
    return offset


def analyze(data, caller_tone, expected_pcm, bridge_input):
    rows = packets(data)
    if not rows or any(p['src'] != '127.0.0.1' or p['dst'] != '127.0.0.1' for p in rows):
        raise ValueError('non_loopback_capture')
    signals = [sip(p) for p in rows if 5060 in (p['sport'], p['dport'])]
    if len({s['headers'].get('call-id') for s in signals}) != 1:
        raise ValueError('call_scope')
    invite = next(s for s in signals if s['line'].startswith('INVITE '))
    answer = next(s for s in signals if s['line'].startswith('SIP/2.0 200 ') and s['headers'].get('cseq') == '1 INVITE')
    for prefix, cseq in [('ACK ', '1 ACK'), ('BYE ', '2 BYE'), ('SIP/2.0 200 ', '2 BYE')]:
        if not any(s['line'].startswith(prefix) and s['headers'].get('cseq') == cseq for s in signals):
            raise ValueError('sip_dialog_incomplete')
    caller, pbx = sdp(invite), sdp(answer)
    streams = {'caller': [], 'assistant': []}
    for p in rows:
        direction = 'caller' if (p['sport'], p['dport']) == (caller, pbx) else 'assistant' if (p['sport'], p['dport']) == (pbx, caller) else None
        if direction:
            streams[direction].append(rtp(p))
    evidence = {}
    bodies = {}
    for direction, frames in streams.items():
        if len(frames) < 100:
            raise ValueError('insufficient_rtp')
        if any((b['sequence'] - a['sequence']) % 65536 != 1 or (b['timestamp'] - a['timestamp']) % 2**32 != len(a['payload']) for a, b in zip(frames, frames[1:])):
            raise ValueError('rtp_gap_reorder_or_timestamp')
        bodies[direction] = b''.join(f['payload'] for f in frames)
        evidence[direction] = {'packets': len(frames), 'bytes': len(bodies[direction]), 'sha256': hashlib.sha256(bodies[direction]).hexdigest()}
    if caller_tone.find(bodies['caller']) < 0:
        raise ValueError('unexpected_caller_pcmu')
    offset = assert_pcm_media(bodies['caller'], bridge_input, expected_pcm, bodies['assistant'])
    return {'passed': True, 'streams': evidence, 'caller_pcm_exact_subsequence': True,
            'complete_670ms_cue_then_1000ms_main_exact_pcmu': True,
            'assistant_exact_sequence_byte_offset': offset, 'reference_codec': 'asterisk20_default_lookup_quantizer', 'all_destinations_loopback': True,
            'limitations': ['No carrier/provider calls, public SIP, TLS/SRTP, NAT or acoustic test.',
                            'Uses production Node HTTP/admission/AudioSocket/PCM classes with a synthetic injected AI session.']}
