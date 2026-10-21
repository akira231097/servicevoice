# Isolated owned phone media bridge

This implementation is opt-in. It never changes a Vapi number, saved assistant,
main phone routing, firewall, TLS certificate or scheduler configuration. The
existing number is Vapi-managed; no separate carrier credentials are assumed.

The proposed path is the authenticated Vapi `assistant-request` webhook, a
one-use SIP destination, Asterisk, private AudioSocket PCM, and the existing
isolated Vapi WebSocket assistant. `DuplexSession` and `DuplexCoordinator` provide
the same transcript, fresh policy, preparation, cancellation and booking
boundaries as the isolated browser. The bridge owns the only output PCM stream.

## Current evidence and activation limits

Offline tests exercise the actual local HTTP/TCP boundaries and PCM scheduling.
They do not prove that the Vapi-managed carrier forwards to this SIP endpoint,
negotiates TLS/SRTP, preserves caller audio, or stops audible remote media on
interruption. This is not an enabled production phone integration.

Vapi documents immediate SIP forwarding from `assistant-request`. Its raw
WebSocket transport replaces the call transport; `monitor.listenUrl` is a
separate listen-only channel. A remote `say` command cannot provide the same
sample ownership as this bridge.

The supplied public SIP template requires TLS1.2+ and SRTP(SDES), without a
cleartext fallback. Vapi explicitly documents `tls/srtp` for trunk gateways; its
managed-number transfer documentation does not establish that the same setting
is available for this exact forwarding path. Confirm this before activation.
If the carrier sends plain RTP, retain the failure rather than relaxing the
template. A separately configured, reviewed SIP trunk may be necessary.

The first network-none run exposed a real startup loss: default Asterisk
`Answer()` consumed the first20ms while waiting for media. The dialplan uses
`Answer(0,i)` to establish AudioSocket immediately. That initial failed receipt
is retained. The verifier uses Asterisk's exact default G.711 lookup quantizer;
Python's audioop encoder has different low-bit rounding and is not an exact
reference for outbound Asterisk PCM. The second run preserved all48000 caller PCM bytes and150 RTP packets,
and its complete cue/main sequence matched the exact codec reference. Both
legs drained. Its wrapper still exited1 because a Windows line ending remained
on a shell command; that failure is retained separately from the media pass.
Shell scripts now use LF explicitly; a clean wrapper rerun remains pending.

## Runtime boundaries

- The webhook and AudioSocket listeners bind only `127.0.0.1`, ports8790 and9019.
  A reverse proxy may expose only `POST /phone-bridge/ingress` over existing TLS.
- A separate phone webhook bearer authenticates admission. The bridge checks
  the configured organization and exact number, then reads that exact live call
  from Vapi before issuing a15-second random, one-use UUID. Caller ID grants no
  authority. Only then does the webhook prewarm one isolated inner AI session.
  Duplicate authenticated webhooks join that same reservation and allocation;
  an unknown allocation outcome is never retried. The response has a5-second
  local deadline within Vapi's7.5-second end-to-end assistant-request limit.
- The private AudioSocket peer consumes the UUID only after the inner session
  and continuous input encoder are ready. Outer and inner call IDs are bound
  once; telemetry contains hashes. No PCM is sent before media attaches. An
  unclaimed prewarm is stopped at the original15-second admission expiry.
  One active call is the default. The256-entry ledger fails closed when full;
  restart only after all calls have ended. A restart invalidates unused grants.
- Asterisk `AudioSocket()` supplies8kHz mono signed PCM16LE. Input uses the
  existing streaming16kHz encoder and energy detector. Output uses a stateful
  anti-alias lowpass before8kHz decimation. The optional16kHz channel-driver
  mode is not the default and needs an Asterisk version/module check.
- Output is paced in10ms frames. Complete cached cues must be at most1100ms.
  First main PCM fixes a cue-end barrier, later packets do not extend it, and
  one queue prevents simultaneous cue/main samples. Confirmed interruption
  clears owned queues. Already-written TCP/RTP/carrier audio cannot be recalled;
  the actual downstream stop latency needs a capture-based test.
- An output stall over100ms or TCP queue over40ms fails this isolated call.
  Prewarming holds the complete greeting in the bounded3-second output queue.
  Overflow fails and stops both legs rather than discarding greeting audio.
  The provider receives its SIP destination only when the input path is ready,
  so the first attached caller bytes reach the streaming encoder immediately.
  A late allocation response remains owned and is observed for exact cleanup.
- Caller hangup, provider end or shutdown closes owned media and the inner
  session, then verifies/stops the exact outer call if it remains active.
  A control acknowledgment is not proof that a remote carrier leg ended.
- DTMF is currently validated and ignored. It cannot authorize any action.

## Deployment preparation

Use existing server capacity. Do not purchase a number or new infrastructure as
part of these files. Run the existing isolated console against its disposable
candidate database, never the production booking database. Node22+ is required.

The private environment must provide `SERVICEVOICE_PHONE_BRIDGE=on`,
`VAPI_API_KEY`, a separate `VAPI_PHONE_WEBHOOK_SECRET` of at least32 characters,
`VAPI_PHONE_ORG_ID`, the exact `VAPI_PHONE_NUMBER_ID`, and a reviewed
`SERVICEVOICE_PHONE_SIP_HOST`. No values belong in source control.

Start the foreground process with:

```sh
node scripts/serve_phone_bridge.mjs --env-file /run/secrets/phone-bridge.env --console-url http://127.0.0.1:8785
```

The Asterisk template needs a public hostname with a valid certificate/private
key, the server's correct external SDP/NAT address, and access to its existing
private Node listener. Resolve the template placeholders before loading it.
Load only the required modules, including `res_srtp`, `res_audiosocket` and
`app_audiosocket`. Restrict SIP5061 to the current documented Vapi US signaling
sources. Confirm actual transfer source addresses; do not expand the allowlist
to arbitrary internet clients after a failed test. The template uses local
RTP40000–40019; the provider's remote media ports can span40000–60000.

Keep the ingress path disconnected from the saved main number until all checks
below pass. The existing main webhook remains the rollback. Do not activate by
merely changing an assistant's `phone_enabled` field.

## Isolated media lab

Build from the repository root with `integrations/phone-bridge/lab/Dockerfile`,
then run `sh integrations/phone-bridge/lab/run-isolated.sh` in a fresh dedicated
extraction directory on the existing server. The script enforces a memory
headroom check,384MiB build/256MiB runtime caps and bounded wall time. It runs
with `--network none`, no published ports, no keys and a read-only root.
The injected synthetic AI session exercises the production Node HTTP/admission,
AudioSocket, encoder and playback code through actual Asterisk and SIPp. Its
PCAP checks the complete670ms cue followed by1000ms main output and exact caller
PCM ingress. It does not invoke the external Vapi transport or policy model.
The lab image uses Ubuntu's packaged Node for the injected session only; the
real provider bridge requires Node22+ with its native WebSocket.

A temporary random Vapi SIP-URI resource can supply a provider test entrypoint
without changing the main saved number. An owned SIP client calls that URI,
whose authenticated assistant-request forwards to the isolated secure bridge.
This needs explicit resource configuration and live capture; no test entrypoint
is created by these files. Vapi-managed numbers cannot originate outbound calls
without an imported number or trunk, so a direct outbound test cannot be assumed
from the currently available account. SIP ingress alone does not establish PSTN
carrier ingress behavior.

## Required acceptance before main routing

1. Build/import the exact Asterisk modules and inspect the resolved config.
   Confirm TLS certificate validity, encrypted SDP/media, source allowlisting,
   no public AudioSocket listener, no open dial-out context and one-use replay
   rejection. Verify actual negotiated codec/NAT addresses in a private capture.
2. Use an isolated SIP entrypoint and an owned synthetic caller endpoint, with
   no unrelated phone destination. Prove caller PCM reaches the AI while a cue
   and a main response play, cue bytes complete, no assistant overlap, bounded
   cue-to-main handoff, and unchanged fresh policy exclusions.
3. Test both hangup directions, unknown grant, repeated webhook, late startup
   close, no-answer, expiry, packet delay, output stall and provider loss. Read
   both provider legs back as ended; do not infer this from local socket close.
4. Repeat a six-turn safe FAQ/intake scenario plus genuine interruption. Measure
   source-end, voiced cue start, cue end, useful main start and post-cue silence
   separately. Preserve failed continuity cases and interrupted-cue evidence.
   Verify the candidate scheduler created no appointment or booking mutation.
5. Use the owned endpoint capture to assess phrase completeness, audible gaps,
   intelligibility and actual media cancellation. Digital loopback evidence
   cannot establish physical room echo or every handset acoustic condition;
   report that limit without presenting browser evidence as phone acceptance.

## Primary contracts

- [Vapi server events and SIP destinations](https://docs.vapi.ai/server-url/events)
- [Vapi WebSocket versus listen-only monitor](https://docs.vapi.ai/calls/websocket-transport)
- [Vapi SIP networking and TLS/SRTP](https://docs.vapi.ai/advanced/sip/sip-networking)
- [Asterisk AudioSocket protocol](https://docs.asterisk.org/Configuration/Channel-Drivers/AudioSocket/)
- [Asterisk AudioSocket application](https://docs.asterisk.org/Asterisk_20_Documentation/API_Documentation/Dialplan_Applications/AudioSocket/)
- [Asterisk secure calling](https://docs.asterisk.org/Deployment/Secure-Calling/Secure-Calling-Tutorial/)
