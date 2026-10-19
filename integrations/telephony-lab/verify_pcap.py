"""Bounded stdlib parser for this lab's classic PCAP/IPv4/UDP evidence only."""
import collections
import hashlib
import json
import re
import struct
from pathlib import Path


def packets(data):
    if len(data) > 8_000_000 or len(data) < 24:
        raise ValueError('pcap_size_invalid')
    magics = {b'\xd4\xc3\xb2\xa1': ('<', 1_000_000), b'\xa1\xb2\xc3\xd4': ('>', 1_000_000)}
    if data[:4] not in magics:
        raise ValueError('unsupported_pcap')
    endian, scale = magics[data[:4]]
    linktype = struct.unpack_from(endian + 'I', data, 20)[0]
    if linktype not in (1, 113, 276):
        raise ValueError('unsupported_linktype')
    offset = 24
    result = []
    while offset < len(data):
        if len(result) >= 5000 or len(data) - offset < 16:
            raise ValueError('pcap_record_bound')
        sec, usec, size, original = struct.unpack_from(endian + 'IIII', data, offset)
        offset += 16
        if size != original or size > 65535 or offset + size > len(data):
            raise ValueError('truncated_packet')
        frame = data[offset:offset + size]
        offset += size
        ipoffset = {1: 14, 113: 16, 276: 20}[linktype]
        protooffset = {1: 12, 113: 14, 276: 0}[linktype]
        if len(frame) < ipoffset or struct.unpack_from('!H', frame, protooffset)[0] != 0x0800:
            raise ValueError('non_ipv4_packet')
        ip = frame[ipoffset:]
        if len(ip) < 28 or ip[0] >> 4 != 4 or ip[9] != 17:
            raise ValueError('non_udp_packet')
        ihl = (ip[0] & 15) * 4
        total = struct.unpack_from('!H', ip, 2)[0]
        if ihl < 20 or total > len(ip) or struct.unpack_from('!H', ip, 6)[0] & 0x3fff:
            raise ValueError('fragmented_or_invalid_ip')
        src, dst = '.'.join(map(str, ip[12:16])), '.'.join(map(str, ip[16:20]))
        sport, dport, length, _ = struct.unpack_from('!HHHH', ip, ihl)
        if length < 8 or ihl + length != total:
            raise ValueError('udp_length_invalid')
        result.append({'time': sec + usec / scale, 'src': src, 'dst': dst,
                       'sport': sport, 'dport': dport, 'data': ip[ihl + 8:ihl + length]})
    return result


def sip(packet):
    text = packet['data'].decode('ascii')
    lines = text.replace('\r\n', '\n').split('\n')
    headers = {}
    for line in lines[1:]:
        if not line:
            break
        key, _, value = line.partition(':')
        headers[key.lower()] = value.strip()
    return {'line': lines[0], 'headers': headers, 'text': text, 'time': packet['time'],
            'sport': packet['sport'], 'dport': packet['dport']}


def sdp(message):
    text = message['text']
    address = re.search(r'^c=IN IP4 ([0-9.]+)\s*$', text, re.M)
    audio = re.search(r'^m=audio (\d+) RTP/AVP ([0-9 ]+)\s*$', text, re.M)
    if not address or not audio or address[1] != '127.0.0.1' or audio[2].strip() != '0':
        raise ValueError('pcmu_sdp_not_negotiated')
    if not re.search(r'^a=rtpmap:0 PCMU/8000\s*$', text, re.M):
        raise ValueError('pcmu_mapping_missing')
    return int(audio[1])


def rtp(packet):
    data = packet['data']
    if len(data) < 12 or data[0] >> 6 != 2 or data[1] & 127 != 0:
        raise ValueError('invalid_pcmu_rtp')
    offset = 12 + (data[0] & 15) * 4
    if data[0] & 16:
        if len(data) < offset + 4:
            raise ValueError('truncated_rtp_extension')
        offset += 4 + struct.unpack_from('!H', data, offset + 2)[0] * 4
    end = len(data) - (data[-1] if data[0] & 32 else 0)
    if end <= offset:
        raise ValueError('empty_rtp')
    seq, timestamp, ssrc = struct.unpack_from('!HII', data, 2)
    return {'sequence': seq, 'timestamp': timestamp, 'ssrc': ssrc, 'payload': data[offset:end]}


def analyze(data, tone):
    rows = packets(data)
    if not rows or any(p['src'] != '127.0.0.1' or p['dst'] != '127.0.0.1' for p in rows):
        raise ValueError('non_loopback_or_empty_capture')
    signals = [sip(p) for p in rows if 5060 in (p['sport'], p['dport'])]
    if not signals or not signals[0]['headers'].get('call-id') or len({s['headers'].get('call-id') for s in signals}) != 1:
        raise ValueError('call_id_scope_invalid')
    expected = [('INVITE ', '1 INVITE'), ('SIP/2.0 200 ', '1 INVITE'),
                ('ACK ', '1 ACK'), ('BYE ', '2 BYE'), ('SIP/2.0 200 ', '2 BYE')]
    selected = []
    cursor = 0
    for prefix, cseq in expected:
        found = next((i for i in range(cursor, len(signals))
                      if signals[i]['line'].startswith(prefix) and signals[i]['headers'].get('cseq') == cseq), None)
        if found is None:
            raise ValueError('sip_dialog_incomplete')
        selected.append(signals[found])
        cursor = found + 1
    invite, answer, ack, bye, final = selected
    caller_tag = re.search(r';tag=([^;\s]+)', invite['headers'].get('from', ''))
    callee_tag = re.search(r';tag=([^;\s]+)', answer['headers'].get('to', ''))
    if not caller_tag or not callee_tag:
        raise ValueError('dialog_tags_missing')
    for message in selected[1:]:
        actual_from = re.search(r';tag=([^;\s]+)', message['headers'].get('from', ''))
        actual_to = re.search(r';tag=([^;\s]+)', message['headers'].get('to', ''))
        if not actual_from or not actual_to or actual_from[1] != caller_tag[1] or actual_to[1] != callee_tag[1]:
            raise ValueError('dialog_tags_mismatch')
    for i, message in enumerate(selected):
        expected_ports = (5060, 5062) if i in (1, 4) else (5062, 5060)
        if (message['sport'], message['dport']) != expected_ports:
            raise ValueError('sip_direction_mismatch')
    caller_port, pbx_port = sdp(invite), sdp(answer)
    streams = collections.defaultdict(list)
    for packet in rows:
        if 5060 in (packet['sport'], packet['dport']):
            continue
        if (packet['sport'], packet['dport']) not in ((caller_port, pbx_port), (pbx_port, caller_port)):
            # RTCP is separately identified, never counted as RTP.
            if (packet['sport'], packet['dport']) in ((caller_port + 1, pbx_port + 1), (pbx_port + 1, caller_port + 1)):
                continue
            raise ValueError('unexpected_media_ports')
        media = rtp(packet)
        streams[(packet['sport'], packet['dport'], media['ssrc'])].append(media)
    if len(streams) != 2:
        raise ValueError('expected_two_rtp_streams')
    evidence = []
    payloads = {}
    for (sport, dport, ssrc), frames in streams.items():
        if len(frames) < 100 or any(len(f['payload']) != 160 for f in frames):
            raise ValueError('insufficient_or_wrong_frame_size')
        if any((b['sequence'] - a['sequence']) % 65536 != 1 for a, b in zip(frames, frames[1:])):
            raise ValueError('rtp_sequence_gap_duplicate_or_reorder')
        if any((b['timestamp'] - a['timestamp']) % (2**32) != 160 for a, b in zip(frames, frames[1:])):
            raise ValueError('rtp_timestamp_step_invalid')
        direction = 'caller_to_asterisk' if sport == caller_port else 'asterisk_to_caller'
        body = b''.join(f['payload'] for f in frames)
        payloads[direction] = body
        evidence.append({'direction': direction, 'source_port': sport, 'destination_port': dport,
                         'ssrc': ssrc, 'packets': len(frames), 'payload_type': 0, 'payload_bytes': len(body),
                         'sequence_first': frames[0]['sequence'], 'sequence_last': frames[-1]['sequence'],
                         'sequence_contiguous_modulo_65536': True, 'timestamp_step': 160,
                         'payload_sha256': hashlib.sha256(body).hexdigest()})
    sent = payloads['caller_to_asterisk']
    echoed = payloads['asterisk_to_caller']
    offset = sent.find(echoed)
    if offset < 0 or tone.find(sent) < 0 or len(set(sent)) < 32:
        raise ValueError('echo_or_synthetic_tone_payload_mismatch')
    return {'passed': True, 'packet_count': len(rows), 'all_destinations_loopback': True,
            'sip_call_id': invite['headers']['call-id'],
            'sip_dialog': [{'start_line': s['line'], 'cseq': s['headers']['cseq'],
                            'relative_ms': round((s['time'] - invite['time']) * 1000, 3)} for s in selected],
            'negotiated_codec': 'PCMU/8000', 'negotiated_payload_type': 0,
            'caller_rtp_port': caller_port, 'asterisk_rtp_port': pbx_port, 'streams': evidence,
            'echo_payload_exact_subsequence': True, 'echo_byte_offset': offset,
            'transmitted_payload_matches_generated_tone': True,
            'limitations': ['A local one-call no-loss fixture; not an internet latency or load benchmark.',
                            'Does not test PSTN, a SIP trunk, SRTP/TLS, NAT, transfer, or the AI voice application.']}


if __name__ == '__main__':
    import sys
    result = analyze(Path(sys.argv[1]).read_bytes(), Path(sys.argv[2]).read_bytes())
    print(json.dumps(result, indent=2))
