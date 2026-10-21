"""Exact codec-reference controls for the isolated synthetic phone media lab."""
import importlib.util
from pathlib import Path
import struct
import sys

import pytest

LAB = Path(__file__).resolve().parents[1] / 'integrations/phone-bridge/lab'
spec = importlib.util.spec_from_file_location('phone_lab_verify_pcap', LAB / 'verify_pcap.py')
pcap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pcap)
sys.modules.setdefault('verify_pcap', pcap)
spec = importlib.util.spec_from_file_location('phone_lab_verify', LAB / 'verify_phone.py')
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


def test_asterisk_lookup_reference_retains_exact_signed_low_bit_quantization():
    pcm = struct.pack('<10h', 0, -2, -3, -3, -6, -1, -9, 4, -10, 8)
    assert list(verify.encode_asterisk_ulaw(pcm)) == [255, 127, 127, 127, 126, 127, 126, 254, 126, 254]


def test_full_duplex_reference_accepts_complete_exact_media_with_transport_padding():
    caller = bytes(range(256)) * 3
    incoming = verify.audioop.ulaw2lin(caller, 2)
    expected = struct.pack('<800h', *range(-400, 400))
    output = verify.encode_asterisk_ulaw(expected)
    assert verify.assert_pcm_media(caller, bytes(160) + incoming + bytes(160), expected, b'\xff' * 80 + output + b'\xff' * 80) == 80


@pytest.mark.parametrize('kind', ['first_input_frame', 'internal_input_frame', 'input_corruption', 'cue_prefix', 'main_suffix', 'output_reorder', 'output_corruption'])
def test_complete_media_gate_rejects_frame_loss_corruption_and_reordering(kind):
    caller = bytes(range(256)) * 3
    incoming = verify.audioop.ulaw2lin(caller, 2)
    expected = struct.pack('<800h', *range(-400, 400))
    output = verify.encode_asterisk_ulaw(expected)
    if kind == 'first_input_frame':
        incoming = incoming[320:]
    elif kind == 'internal_input_frame':
        incoming = incoming[:320] + incoming[640:]
    elif kind == 'input_corruption':
        incoming = incoming[:101] + bytes([incoming[101] ^ 128]) + incoming[102:]
    elif kind == 'cue_prefix':
        output = output[80:]
    elif kind == 'main_suffix':
        output = output[:-80]
    elif kind == 'output_reorder':
        output = output[80:160] + output[:80] + output[160:]
    else:
        output = output[:101] + bytes([output[101] ^ 128]) + output[102:]
    with pytest.raises(ValueError):
        verify.assert_pcm_media(caller, incoming, expected, output)


def test_phone_dialplan_skips_default_answer_media_consumption():
    assert 'Answer(0,i)' in (LAB.parent / 'config/extensions.conf').read_text()
    assert 'Answer(0,i)' in (LAB / 'run.py').read_text()
