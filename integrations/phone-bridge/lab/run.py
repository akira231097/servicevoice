"""One bounded synthetic call; container must run with --network none."""
import audioop
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import struct
import subprocess
import time
from verify_phone import analyze


OUT = Path('/out')


def command(args, seconds=5):
    return subprocess.run(args, capture_output=True, text=True, timeout=seconds)


def main():
    report = {'passed': False, 'synthetic_only': True, 'human_or_provider_calls': 0,
              'one_call_limit': 1, 'sipp_process_timeout_seconds': 25, 'channel_timeout_seconds': 15}
    processes = []
    handles = []
    started = time.monotonic()
    try:
        interfaces = sorted(os.listdir('/sys/class/net'))
        if interfaces != ['lo']:
            raise RuntimeError('container_network_is_not_isolated')
        report['interfaces'] = interfaces
        report['packages'] = command(['dpkg-query', '-W', '-f=${Package}=${Version}\n', 'asterisk', 'sip-tester', 'tcpdump', 'python3', 'nodejs']).stdout.splitlines()
        for part in ('lib', 'keys', 'agi', 'spool', 'run'):
            Path('/tmp/asterisk', part).mkdir(parents=True, exist_ok=True)
        samples = [round(10000 * math.sin(2 * math.pi * (440 * (i / 8000) + 70 * (i / 8000)**2))) for i in range(24000)]
        pcm = struct.pack('<' + 'h' * len(samples), *samples)
        tone = audioop.lin2ulaw(pcm, 2)
        (OUT / 'tone.pcmu').write_bytes(tone)
        report['tone'] = {'duration_seconds': 3, 'sample_rate': 8000, 'codec': 'PCMU', 'sha256': hashlib.sha256(tone).hexdigest()}
        node_log = (OUT / 'bridge.log').open('w'); handles.append(node_log)
        node = subprocess.Popen(['node', '/lab/adapter.mjs'], stdout=node_log, stderr=subprocess.STDOUT)
        processes.append(node)
        deadline = time.monotonic() + 8
        while not (OUT / 'bridge-ready.json').exists():
            if node.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError('bridge_startup_failed')
            time.sleep(.05)
        ready = json.loads((OUT / 'bridge-ready.json').read_text())
        import uuid
        token = str(uuid.UUID(ready['token']))
        if ready['sent_samples_before_attach'] != 0:
            raise RuntimeError('prewarm_sent_media_before_attachment')
        Path('/tmp/phone-extensions.conf').write_text(
            '[isolated-echo]\nexten => 600,1,Answer(0,i)\n'
            ' same => n,Set(TIMEOUT(absolute)=15)\n'
            f' same => n,AudioSocket({token},127.0.0.1:9019)\n same => n,Hangup()\n')
        astlog = (OUT / 'asterisk.log').open('w'); handles.append(astlog)
        ast = subprocess.Popen(['asterisk', '-f', '-C', '/etc/asterisk/asterisk.conf', '-vvv'], stdout=astlog, stderr=subprocess.STDOUT)
        processes.append(ast)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if ast.poll() is not None:
                raise RuntimeError('asterisk_exited_during_startup')
            probe = command(['asterisk', '-C', '/etc/asterisk/asterisk.conf', '-rx', 'pjsip show endpoints'])
            if probe.returncode == 0 and 'sipp' in probe.stdout:
                (OUT / 'asterisk-endpoints.txt').write_text(probe.stdout)
                break
            time.sleep(.2)
        else:
            raise RuntimeError('asterisk_startup_deadline')
        modules = command(['asterisk', '-C', '/etc/asterisk/asterisk.conf', '-rx', 'module show like audiosocket'])
        (OUT / 'asterisk-audiosocket-modules.txt').write_text(modules.stdout + modules.stderr)
        if 'app_audiosocket.so' not in modules.stdout or 'res_audiosocket.so' not in modules.stdout:
            raise RuntimeError('required_audiosocket_module_missing')
        report['listeners_before_call'] = command(['ss', '-lun']).stdout.splitlines()
        caplog = (OUT / 'capture.log').open('w'); handles.append(caplog)
        pcapfile = (OUT / 'call.pcap').open('wb'); handles.append(pcapfile)
        capture = subprocess.Popen(['tcpdump', '--immediate-mode', '-i', 'lo', '-p', '-Z', 'root', '-n', '-s', '0', '-U', '-w', '-', 'udp'], stdout=pcapfile, stderr=caplog)
        processes.append(capture)
        time.sleep(.4)
        if capture.poll() is not None:
            raise RuntimeError('packet_capture_start_failed')
        sipp = command(['sipp', '127.0.0.1:5060', '-sf', '/lab/scenario.xml', '-i', '127.0.0.1', '-p', '5062',
                        '-mi', '127.0.0.1', '-min_rtp_port', '6000', '-max_rtp_port', '6004', '-m', '1', '-l', '1', '-r', '1', '-nostdin',
                        '-timeout', '20s', '-timeout_error', '-trace_msg', '-trace_err', '-trace_stat',
                        '-message_file', '/out/sip-messages.log', '-error_file', '/out/sip-errors.log',
                        '-stf', '/out/sipp-stats.csv'], seconds=25)
        (OUT / 'sipp.log').write_text(sipp.stdout + sipp.stderr)
        report['sipp_exit_code'] = sipp.returncode
        time.sleep(.3)
        capture.send_signal(signal.SIGINT); capture.wait(timeout=3)
        report['capture_exit_code'] = capture.returncode
        command(['asterisk', '-C', '/etc/asterisk/asterisk.conf', '-rx', 'core stop gracefully'])
        ast.wait(timeout=5)
        report['asterisk_exit_code'] = ast.returncode
        if sipp.returncode != 0 or ast.returncode != 0 or capture.returncode != 0:
            raise RuntimeError('process_not_clean')
        node.send_signal(signal.SIGTERM); node.wait(timeout=8)
        report['bridge_exit_code'] = node.returncode
        report['bridge'] = json.loads((OUT / 'bridge-result.json').read_text())
        report['protocol'] = analyze((OUT / 'call.pcap').read_bytes(), tone,
                                     (OUT / 'expected-output.pcm').read_bytes(),
                                     (OUT / 'bridge-input.pcm').read_bytes())
        if node.returncode != 0 or report['bridge']['outer_state'] != 'ended' or report['bridge']['remaining_owned'] != 0:
            raise RuntimeError('owned_bridge_did_not_drain')
        if report['bridge']['ownership_cleanup'] != {'provider_ownership_verified': True, 'ownership_unknown': 0}:
            raise RuntimeError('synthetic_owned_ledger_did_not_verify_and_release')
        if not report['bridge']['inner_bound'] or report['bridge']['input_frames'] < 100:
            raise RuntimeError('caller_media_not_continuous')
        # SIPp emits3s RTP then pauses without zero RTP before BYE. The owned
        # clock must continue the ASR tail and release local speech on that gap.
        bridge_result = report['bridge']
        releases = [e for e in bridge_result['speech_events'] if not e['active']]
        if not any(e['active'] for e in bridge_result['speech_events']) or not releases:
            raise RuntimeError('carrier_dtx_did_not_release_local_speech')
        release_after_carrier_ms = releases[-1]['at_ms'] - bridge_result['last_carrier_at_ms']
        if not 0 < release_after_carrier_ms < 500:
            raise RuntimeError('carrier_dtx_release_outside_bound')
        if bridge_result['last_encoded_at_ms'] - bridge_result['last_carrier_at_ms'] < 500 or bridge_result['input_silence_samples'] < 4000:
            raise RuntimeError('carrier_dtx_asr_tail_did_not_advance')
        report['carrier_dtx'] = {'no_zero_rtp_required': True, 'local_stop_after_last_carrier_ms': round(release_after_carrier_ms, 3),
                                 'encoded_tail_ms': round(bridge_result['last_encoded_at_ms'] - bridge_result['last_carrier_at_ms'], 3),
                                 'silence_samples': bridge_result['input_silence_samples'], 'passed': True}
        if not bridge_result['drain_controls']:
            raise RuntimeError('private_admission_drain_not_proven')
        report['passed'] = report['protocol']['passed']
    except Exception as error:
        report['failure'] = str(error)[:200]
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=3)
                except OSError as error:
                    report.setdefault('cleanup_failures', []).append(type(error).__name__)
                    report['passed'] = False
        for handle in handles:
            handle.close()
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        report['artifacts'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in OUT.iterdir() if p.is_file() and p.name != 'result.json'}
        (OUT / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
