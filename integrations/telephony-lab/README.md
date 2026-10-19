# Isolated SIP and RTP lab

This is an executable protocol lab beside ServiceVoice. It uses Ubuntu distro packages for Asterisk 20 and SIPp 3.7 to send a three-second synthetic PCMU tone through an answered SIP call and Asterisk's `Echo()` application. It does not connect to the AI agent, a telephone carrier, or a human.

The runner records signaling, RTP, generated tone, packet capture and verification results under a fresh local evidence directory. Captures and operational logs are not included in this public source. The parser has offline regression coverage.

## Run on Windows

Requires Docker Desktop with its Linux engine. From the ServiceVoice project directory:

```powershell
docker desktop start --timeout 120
.\integrations\telephony-lab\run_lab.ps1 -Label interview-01
```

Choose a fresh label each time. Existing evidence is never overwritten. The first image build downloads the pinned official Ubuntu base and distro packages; runtime has `--network none`, no published ports, a read-only root filesystem, and only a fresh evidence directory mounted. The image is amd64-specific because Ubuntu's installed Asterisk module directory is architecture-specific. Package versions and the resulting image ID are recorded; apt repository updates can change subsequent builds even though the base image digest is pinned.

Asterisk listens for SIP on container loopback `127.0.0.1:5060`; SIPp uses `5062`, and SDP negotiates the RTP ports. No host port is bound. The app's `8765` and `8766` listeners are independent. There are no secrets, registrations, trunks, public destinations, or application/database mounts. Each invocation makes at most one synthetic call. Asterisk has a 15-second channel timeout, SIPp has a 20-second global timeout, and its parent waits at most 25 seconds. All child processes are reaped; the stopped container remains for inspection.

Capabilities are limited to packet capture and tcpdump's user/group initialization. The container has no network interface other than loopback, even during capture. The lab verifies this before starting Asterisk. Asterisk's other internal UDP sockets are confined to that same isolated namespace.

## What constitutes a pass

- SIP messages share one nonempty Call-ID, correct directional ports, exact dialog tags, and the expected CSeq/method sequence. A 200 for INVITE cannot substitute for a 200 for BYE.
- Offer and answer explicitly negotiate PCMU/8000, payload type 0, with loopback media addresses. The verifier reads the actual negotiated RTP ports.
- Two separate directional RTP streams have valid version-2 headers, at least 100 packets each, 160-byte payloads, consecutive sequence numbers (including wrap), and 160-sample timestamp steps.
- Captured outbound bytes match the generated tone, and the returned payload is an exact contiguous portion of the outbound bytes. The successful run returned every byte with offset zero. RTCP is not counted as RTP.
- SIPp, Asterisk and tcpdump exit successfully, and the runtime container is stopped with no published ports.

Inspect the capture and verifier result together. A valid local signaling/media exchange does not establish carrier connectivity or AI conversation quality.

## Inspect or test offline

Open `call.pcap` in Wireshark and filter `sip || rtp`. All audio is a synthetic chirp; no caller audio or credentials are present.

With Python 3.12 from the project environment, run:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s integrations\telephony-lab -p test_verify_pcap.py -v
.\.venv\Scripts\python.exe integrations\telephony-lab\verify_pcap.py PATH_TO_YOUR_CAPTURE\call.pcap PATH_TO_YOUR_CAPTURE\tone.pcmu
```

The verifier uses only the standard library. Tests reject incomplete signaling, wrong audio, missing RTP, wrong codec, sequence discontinuity, foreign addresses, mismatched tags and truncated captures. Runtime tone encoding uses Python 3.12's `audioop`; upgrading the image to Python 3.13 requires replacing that encoder.

## Limits

This is a one-call loopback protocol lab. It does not establish carrier/PSTN access, SIP authentication, NAT traversal, SRTP/TLS, transfer, internet jitter tolerance, concurrent-call capacity or AI telephony integration. The runtime is isolated and uses no provider account. The runner retains its stopped container for inspection; review its exact name before cleanup.

## Primary references

- [Ubuntu Asterisk package](https://packages.ubuntu.com/noble/asterisk) and [SIPp package](https://packages.ubuntu.com/noble/sip-tester): official distro package sources.
- [Asterisk Echo application](https://docs.asterisk.org/Asterisk_20_Documentation/API_Documentation/Dialplan_Applications/Echo/): echoes channel media and requires answering first.
- [Asterisk PJSIP configuration](https://docs.asterisk.org/Configuration/Channel-Drivers/SIP/Configuring-res_pjsip/PJSIP-Configuration-Sections-and-Relationships/): endpoint and transport configuration.
- [SIPp media support](https://sipp.readthedocs.io/en/latest/media.html) and [RTP streaming actions](https://sipp.readthedocs.io/en/latest/scenarios/actions.html): raw PCMU, payload type 0, 160 bytes per 20 ms frame.
- [Docker network isolation](https://docs.docker.com/engine/network/drivers/none/): the `none` driver leaves only loopback.
