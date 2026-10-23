# Isolated Twilio listening prototype

Status: isolated deployment and synthetic phone testing in progress. The feature
is disabled by default. A separate Twilio number and Vapi SIP assistant are used;
the main Vapi number, assistant and production services remain unchanged. Live
listening acceptance is outstanding. This is not yet a verified simulated
full-duplex release.

The separate Twilio line puts the caller and a dedicated Vapi SIP assistant in
one conference. Jev classifies live partial transcripts; code may request a
caller-only participant announcement from the existing approved “Mm-hmm.” bank.
Waiting cues continue inside the existing Vapi stream with their separate
budget. No listening cue creates consent, tool work, a proposal or a booking.

## Implemented boundaries

- Twilio signature verification over the configured external URL and all form
  fields, including future fields; bounded form bodies; exact account and number.
- Durable ownership and two concurrent bridge slots. Duplicate joins cannot
  redial. A pending dial or incomplete cleanup continues to occupy its slot.
- A 15-second join watchdog, ten-minute Twilio call limit, terminal callback
  handling, and cleanup readbacks. Vapi's existing assistant duration is unchanged.
- Closing an owned bridge also retires its exact local call/session binding, so
  a missing Vapi terminal webhook cannot leave tools available after hang-up.
- A separate random SIP address and an expiring per-call HMAC proof. Retries for
  the same Vapi call reuse the admission; reuse by another call is refused.
- The SIP header object path is deliberately unconfigured. The first isolated
  capture must establish its actual location. Missing/unverified headers cannot
  start an assistant. Do not loosen the proof check to make a live probe pass.
  The October 23 live probe established `call.phoneCallProviderDetails.sip.headers`
  for the deployed SIP lane. Its default remains blank for new deployments.
- Authenticated partials use an in-memory admitted binding and do not take the
  scheduling webhook's worker slots or write each partial to SQLite.
- Jev uses independent typed content/language choices on already spoken words.
  Main speech, caller stop, stale/rewritten partials, task-state changes,
  corrections, questions, private details, approvals and readbacks suppress cues.
- Initial limits: at least five seconds and twelve words; partial age at dispatch
  at most 300 ms; seven seconds between attempts/cues; two cues per utterance and
  six per call; 1.5 seconds after observed assistant speech stops.
- A dispatched announcement is never retried/refunded on an ambiguous timeout.
  Runtime checks again immediately before requesting playback. Delayed provider
  delivery can still cause overlap; only live audio can establish acceptance.
- The 8 kHz cue is a WAV wrapping the approved cache's warmed telephone PCM,
  rather than a new TTS request. The source manifest is pinned and WAV hash is
  recorded in memory. It is approximately 0.67 seconds, not under half a second.
- Multi-assistant evaluation discovery and content-free listening timelines,
  trace children and dispatch/fetch metrics. Acoustic safety remains uncertain
  until there is independent caller audio evidence.
- The first prototype refuses enablement in `production` environments.

## Local checks

Run `python -m pytest -q tests/test_twilio_bridge.py tests/test_twilio_api.py
tests/test_listening_cues.py tests/test_twilio_review.py` (one command).
Run the full existing regression suite before an isolated deployment.

`scripts/benchmark_listening_cues.py --live --env-file <private.env>
--output <new-report.json>` makes explicit paid TypeSafe requests against a
fixed synthetic corpus; it makes no voice calls and performs no booking actions.

## What is needed for live testing

Use a separate Twilio Voice number. Put the five `TWILIO_*` values from
`.env.example` into the existing private environment file. A Standard API key
is correct: verify it against the phone-number resource, not `/Accounts`, which
requires the account Auth Token or a Main key. Keep the key out of chat and git.
Automatic recharge being disabled is not a guaranteed hard spending ceiling;
delayed billing and number rental can still accrue. Bound the test calls and
check actual usage, rather than relying on the balance as an exact cap.

Create a dedicated isolated Vapi assistant and SIP identity. Do not modify the
candidate verification assistant or production assistant. Configure the existing
dedicated telephony identity variables for this lane, and include both isolated
assistant IDs in `VAPI_EVAL_ASSISTANT_ID` before its first call. Keep the existing
reviewer on its own production configuration.

Set `SERVICEVOICE_TWILIO_PUBLIC_BASE_URL` to the isolated HTTPS base. Forward only
the exact `/twilio/` routes in `app/twilio_api.py`; include `/twilio/caller-status`
as the number's completed-call callback, so hang-up before conference join is
observed. Start with listening disabled and collect the SIP header object shape
without recording credential values. Pin `SERVICEVOICE_TWILIO_SIP_HEADERS_PATH`
only after that evidence is verified. A blank path refuses the call.

The direct-SIP topology is the conference latency baseline. It admits through
the same proof and scheduler boundary but never sends listening announcements.
It is not a substitute for measured conference acceptance.

## Remaining live gates and tooling

Completed on October 23: isolated resource/edge provisioning with rollback
receipts, owned Twilio self-calls, SIP proof validation, authenticated synthetic
audio delivery, and dual-channel recordings. The first call exposed missing
authentication on subsequent Vapi messages; the next exposed the same issue on
custom voice requests. Explicit headers on the dedicated saved assistant fixed
both without weakening endpoint authentication. Those private header values
must never enter source control, reports or the publication snapshot helper.
The third audio probe delivered the main response and ended cleanly. Earlier
failed captures are retained. No live listening cue has passed audio acceptance.

The fourth probe recorded a complete caller-only “Mm-hmm” during the synthetic
23.2-second utterance, followed by “Let me check that” and “One moment” in the
waiting period. All 75 caller words were retained in order, the listening cue
did not appear as an assistant turn, no proposal/appointment was created, and
all three Twilio legs and Vapi ended. This is one successful mechanism capture,
not completed interruption, latency, echo or human-listening acceptance.

The opt-in tester includes a signed WebSocket receiver for both audio tracks of
an owned synthetic self-call. It additionally requires a short-lived single-use
capability, checks the call's exact account-owned To/From and outbound direction,
and bounds duration, bytes, connections and stored captures. Ordinary inbound
caller audio cannot enter this path. Its packet-arrival timestamps share the
host clock with Jev diagnostics; they do not measure handset acoustic latency.

The live partial benchmark exposed an unrelated greeting influencing Jev's
classification. The prompt now identifies the caller partial as the label target,
and uses prior assistant speech only to identify sensitive reply context. With
unchanged thresholds, 14/14 ordinary cases and 0/20 exclusions passed across a
normal prior question and an interrupted greeting. These are classifier results.

Still needed: shared-clock synthetic caller capture, partial webhook delivery
timing, live Jev plus acknowledgment audio captures, conference versus
direct-SIP timing, speakerphone echo test, and the user's listening acceptance.
These are pending work, not passed checks. No production merge/deployment is
authorized by a local test result alone; the current task explicitly preserves
the main agent and asks for user testing first.

Acceptance must demonstrate that each cue is audible only to the caller, no
caller words are lost or split by it, no cues overlap the useful answer, and
both providers terminate after hang-up. Measure approval-to-audio median <=600ms
and worst <=1000ms, conference overhead median <=300ms, barge-in <=800ms, greeting
<=3.5s and cleanup <=10s. Preserve failed captures. Do not present a REST success,
clip fetch, STT transcript or offline test as proof that a phone user heard it.

Deployment checks must test HTTP methods as well as paths: callbacks use POST;
the two approved WAV routes use GET. The waiting-cue assistant identity must
match the new isolated assistant, independently of the listening-lane flag.
Twilio's US/Canada low-risk outbound permission is enabled with the user's
approval for continued use of the test number; high-risk ranges remain disabled.

If participant announcements mask the conference or cannot meet overlap/timing
gates, test the coaching participant fallback separately. No coaching fallback,
caller-audio fork or number migration is enabled by this implementation.

References: [Twilio participants](https://www.twilio.com/docs/voice/api/conference-participant-resource),
[webhook signatures](https://www.twilio.com/docs/usage/security),
[API key scopes](https://www.twilio.com/docs/iam/api-keys),
[Vapi SIP](https://docs.vapi.ai/advanced/sip),
[Vapi server events](https://docs.vapi.ai/server-url/events).
