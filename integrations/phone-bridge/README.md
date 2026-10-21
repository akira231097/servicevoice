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
The third run completed with exit0 and confirmed cleanup, preserving all caller
bytes and the full cue/main output. All existing containers stayed unchanged.
The fourth run repeated that media proof through the private Unix ingress,
including directory/socket permissions and bearer rejection.
The fifth run also released local speech259ms after the last real caller
packet and continued encoded silence for999ms without a zero RTP packet;
all48000 original bytes and the complete assistant sequence remained intact.
These are digital loopback results; provider SIP/TLS/SRTP remains unverified.

## Runtime boundaries

- The default webhook and AudioSocket listeners bind only `127.0.0.1`, ports8790
  and9019. For the existing Docker edge, the approved optional ingress uses only
  `/run/servicevoice-phone/ingress.sock`, in a same-owner0700 directory, with a
  0600 socket. No HTTP TCP port opens in that mode. Mount that dedicated directory
  into the edge's existing writable data volume as `/data/phone-bridge/`; its
  reverse proxy exposes only `POST /phone-bridge/ingress` over existing TLS.
  AudioSocket remains loopback-only in the shared host network namespace.
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
  One active call is the default. The256-entry admission ledger fails closed
  when full. Only exact provider-ended ownership can retire an ended row after
  its original grant expires. Every later webhook still verifies the provider
  call's live state, and retired UUIDs remain unusable.
- Asterisk `AudioSocket()` supplies8kHz mono signed PCM16LE. Input uses the
  existing streaming16kHz encoder and energy detector. Output uses a stateful
  anti-alias lowpass before8kHz decimation. The optional16kHz channel-driver
  mode is not the default and needs an Asterisk version/module check.
- Input advances on the owned10ms clock. It preserves queued caller bytes in
  order, allows at most120ms of pending jitter, and supplies silence on a
  missing-input interval. Carrier DTX therefore advances the ASR tail and local
  speech release without requiring a zero RTP packet. A burst is paced rather
  than compressed into immediate callbacks; excess input fails explicitly.
- Output is paced in10ms frames. Complete cached cues must be at most1100ms.
  First main PCM fixes a cue-end barrier, later packets do not extend it, and
  one queue prevents simultaneous cue/main samples. Confirmed interruption
  clears owned queues. Already-written TCP/RTP/carrier audio cannot be recalled;
  the actual downstream stop latency needs a capture-based test.
- An output stall over100ms or TCP queue over40ms fails this isolated call.
  Prewarming holds the complete greeting in the bounded3-second output queue.
  Overflow fails and stops both legs rather than discarding greeting audio.
  The provider receives its SIP destination only when the input path is ready,
  so the first attached caller bytes reach the next owned input clock tick.
  A late allocation response remains owned and is observed for exact cleanup.
- The Linux launcher requires a private durable ownership journal. It records
  the original leg before prewarm and allocation intent before the paid API
  request, binds a returned inner ID once, and clears a row only after both
  exact legs read back ended. A200 stop acknowledgment is insufficient.
  Unknown allocation or cleanup keeps admission closed and survives restart;
  scoped provider reconciliation is required before the process can reopen.
- Caller hangup, provider end or shutdown closes owned media and the inner
  session, then verifies/stops the exact outer call if it remains active.
  A control acknowledgment is not proof that a remote carrier leg ended.
- Health counts ending and unclaimed owners until late allocation cleanup has
  settled. Certificate rotation uses bearer-authenticated Unix-only drain and
  resume controls, bound to one random token, before checking complete idleness.
  The public proxy exposes neither control. Failed or unconfirmed rotation
  retains closed admission; it must not treat removed local sessions as idle.
- DTMF is currently validated and ignored. It cannot authorize any action.

## Deployment preparation

Use existing server capacity. Do not purchase a number or new infrastructure as
part of these files. The default isolated console uses its disposable candidate
database. Production admission is a separate default-off mode described below;
it has not yet established live phone acceptance. Node22+ is required.

The private environment must provide `SERVICEVOICE_PHONE_BRIDGE=on`,
`VAPI_API_KEY`, a separate `VAPI_PHONE_WEBHOOK_SECRET` of at least32 characters,
`VAPI_PHONE_ORG_ID`, the exact `VAPI_PHONE_NUMBER_ID`, and a reviewed
`SERVICEVOICE_PHONE_SIP_HOST`. No values belong in source control.

Start the foreground process with:

```sh
node scripts/serve_phone_bridge.mjs --env-file /run/secrets/phone-bridge.env --ownership-file /run/phone-state/ownership.json --console-url http://127.0.0.1:8785
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

The retained final network-isolated lab also verifies Unix ingress, exact first
caller PCM, microphone release during a no-RTP pause, continued encoder tail,
admission drain controls and durable ownership cleanup against injected terminal
provider responses. Actual encrypted provider/carrier acceptance is separate.

A temporary random Vapi SIP-URI resource can supply a provider test entrypoint
without changing the main saved number. An owned SIP client calls that URI,
whose authenticated assistant-request forwards to the isolated secure bridge.
This needs explicit resource configuration and live capture; no test entrypoint
is created by these files. Vapi-managed numbers cannot originate outbound calls
without an imported number or trunk, so a direct outbound test cannot be assumed
from the currently available account. SIP ingress alone does not establish PSTN
carrier ingress behavior.

For the existing short-lived IP certificate, mount its entire owned certificate
DIRECTORY read-only into Asterisk, so atomic renewal does not leave a stale file
bind. A transport reload strategy must validate certificate/key matching, SAN,
expiry and the newly served fingerprint. Reload only the owned isolated Asterisk
when it and the bridge are idle; reject new admissions/stop the isolated service
if renewal cannot be confirmed before the expiry safety window. The short-lived
certificate is not a reason to ship a static long-running configuration. Public
activation remains separate from the candidate image upgrade and requires a
reviewed rollback for the edge fragment, signaling firewall and owned containers.

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

The exact accepted main assistant explicitly permits300 seconds, matching this
isolated bridge's duration. Its account concurrency is not exposed by the
assistant readback. Capacity1 and a256-entry admission ledger remain bounded
limits. Verified-ended retirement is implemented; account capacity still needs
matching before broader main routing.

## Canonical backend admission (default off)

Setting `SERVICEVOICE_PHONE_BRIDGE_ADMISSION=on` additionally requires a private
loopback `SERVICEVOICE_PHONE_BRIDGE_ADMISSION_URL` and a distinct
`SERVICEVOICE_PHONE_BRIDGE_CONTROL_SECRET`. The matching backend flag must also
be enabled. The bridge then obtains one canonical live session from the shared
phone admission and booking database, marks allocation once before its paid
provider request, and binds the exact returned inner call. It retains the outer
call's original300-second limit, subtracting admission network time.

Local journal retirement requires independent terminal readbacks for both legs
and confirmed backend closure. Delayed known terminal states are reconciled
with bounded read-only retries. Unknown allocation outcomes are never replayed
and keep admission closed. A marker request whose outcome is unknown can
conservatively retain ownership even when no provider allocation is observed;
scoped reconciliation is required rather than assuming free capacity.

## Primary contracts

- [Vapi server events and SIP destinations](https://docs.vapi.ai/server-url/events)
- [Vapi WebSocket versus listen-only monitor](https://docs.vapi.ai/calls/websocket-transport)
- [Vapi SIP networking and TLS/SRTP](https://docs.vapi.ai/advanced/sip/sip-networking)
- [Asterisk AudioSocket protocol](https://docs.asterisk.org/Configuration/Channel-Drivers/AudioSocket/)
- [Asterisk AudioSocket application](https://docs.asterisk.org/Asterisk_20_Documentation/API_Documentation/Dialplan_Applications/AudioSocket/)
- [Asterisk secure calling](https://docs.asterisk.org/Deployment/Secure-Calling/Secure-Calling-Tutorial/)
