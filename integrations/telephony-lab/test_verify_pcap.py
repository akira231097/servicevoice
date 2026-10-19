import struct
import unittest
from verify_pcap import analyze, packets


def fixture(*, missing_bye=False, corrupt_echo=False, sequence_gap=False, foreign=False, wrong_tag=False, tag_prefix=False, no_media=False, wrong_codec=False):
    records = []
    tone = bytes((i * 17 + i // 113) % 256 for i in range(24000))
    def packet(body, srcport, dstport, index):
        source = b'\x7f\x00\x00\x01'
        dest = b'\x08\x08\x08\x08' if foreign else source
        udp = struct.pack('!HHHH', srcport, dstport, 8 + len(body), 0) + body
        ip = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 20 + len(udp), 1, 0, 64, 17, 0, source, dest) + udp
        frame = bytes(12) + b'\x08\x00' + ip
        records.append(struct.pack('<IIII', 100 + index // 1000, index % 1000 * 1000, len(frame), len(frame)) + frame)
    def signal(first, cseq, index, response=False, sdpport=None):
        tag = ('bExtra' if tag_prefix else 'wrong') if (wrong_tag or tag_prefix) and first.startswith('ACK') else 'b'
        text = f'{first}\r\nFrom: <sip:sipp@127.0.0.1>;tag=a\r\nTo: <sip:600@127.0.0.1>;tag={tag}\r\nCall-ID: synthetic-fixture\r\nCSeq: {cseq}\r\n\r\n'
        if sdpport:
            codec = 'PCMA' if wrong_codec else 'PCMU'
            text += f'v=0\r\nc=IN IP4 127.0.0.1\r\nm=audio {sdpport} RTP/AVP 0\r\na=rtpmap:0 {codec}/8000\r\n'
        packet(text.encode(), 5060 if response else 5062, 5062 if response else 5060, index)
    signal('INVITE sip:600@127.0.0.1 SIP/2.0', '1 INVITE', 0, sdpport=6000)
    signal('SIP/2.0 200 OK', '1 INVITE', 1, True, 10000)
    signal('ACK sip:600@127.0.0.1 SIP/2.0', '1 ACK', 2)
    for i in range(0 if no_media else 150):
        body = tone[i * 160:(i + 1) * 160]
        for direction in range(2):
            seq = (65500 + i + (1 if sequence_gap and direction == 1 and i == 70 else 0)) % 65536
            content = body if not (corrupt_echo and direction == 1 and i == 70) else bytes(160)
            rtp = struct.pack('!BBHII', 0x80, 0, seq, (0xfffff000 + i * 160) % 2**32, 42 + direction) + content
            packet(rtp, 6000 if direction == 0 else 10000, 10000 if direction == 0 else 6000, 10 + i * 20 + direction)
    signal('BYE sip:600@127.0.0.1 SIP/2.0', '2 BYE', 4000)
    if not missing_bye:
        signal('SIP/2.0 200 OK', '2 BYE', 4001, True)
    header = struct.pack('<IHHIIII', 0xa1b2c3d4, 2, 4, 0, 0, 65535, 1)
    return header + b''.join(records), tone


class CaptureVerifierTests(unittest.TestCase):
    def test_full_sip_rtp_payload_proof_including_sequence_and_timestamp_wrap(self):
        report = analyze(*fixture())
        self.assertTrue(report['passed'])
        self.assertEqual(len(report['sip_dialog']), 5)
        self.assertEqual([s['packets'] for s in report['streams']], [150, 150])
        self.assertTrue(report['echo_payload_exact_subsequence'])

    def test_invite_200_cannot_substitute_for_bye_200(self):
        with self.assertRaisesRegex(ValueError, 'sip_dialog_incomplete'):
            analyze(*fixture(missing_bye=True))

    def test_received_packets_do_not_pass_when_echoed_payload_is_wrong(self):
        with self.assertRaisesRegex(ValueError, 'payload_mismatch'):
            analyze(*fixture(corrupt_echo=True))

    def test_sequence_duplicate_gap_or_reordering_fails(self):
        with self.assertRaisesRegex(ValueError, 'sequence_gap'):
            analyze(*fixture(sequence_gap=True))

    def test_non_loopback_packet_and_mismatched_dialog_tag_fail(self):
        with self.assertRaisesRegex(ValueError, 'non_loopback'):
            analyze(*fixture(foreign=True))
        with self.assertRaisesRegex(ValueError, 'tags_mismatch'):
            analyze(*fixture(wrong_tag=True))

    def test_truncated_capture_is_not_accepted(self):
        data, _ = fixture()
        with self.assertRaisesRegex(ValueError, 'truncated_packet'):
            packets(data[:-3])

    def test_tag_prefix_is_not_exact_dialog_identity(self):
        with self.assertRaisesRegex(ValueError, 'tags_mismatch'):
            analyze(*fixture(tag_prefix=True))

    def test_sip_success_alone_without_rtp_cannot_pass(self):
        with self.assertRaisesRegex(ValueError, 'two_rtp_streams'):
            analyze(*fixture(no_media=True))

    def test_wrong_negotiated_codec_cannot_pass_pcmu_data(self):
        with self.assertRaisesRegex(ValueError, 'pcmu_mapping_missing'):
            analyze(*fixture(wrong_codec=True))


if __name__ == '__main__':
    unittest.main()
