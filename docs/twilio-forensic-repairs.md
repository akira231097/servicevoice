# Twilio forensic repair candidate — October 24, 2026

This is an isolated staging candidate for draft PR #8. Production remains
disabled by configuration. Passing local tests does not establish handset or
speakerphone acceptance. The permanent WebSocket whisper participant is not
implemented by this repair.

## Corrections and boundaries

1. **Waiting cue repeats:** a logical caller-turn ledger survives pauses,
   interrupted cues, partial cue captions, and Vapi history splitting. Sending a
   waiting cue consumes that turn even if the generation is cancelled. Actual
   substantive assistant words are required before the next caller turn can
   replenish it. The existing non-Twilio behavior is separately scoped.
2. **Early waiting cues:** the gateway still checks the complete bounded waiting
   window, observed speaker state, and current scheduler state. A 2.3-second
   pause after a complete question can still look like a finished turn. The old
   emission-instant replay overstated this fix; do not use it as evidence that
   this ambiguity disappeared. One cue per logical turn makes a false endpoint
   less disruptive, and Vapi remains responsible for interrupting waiting audio.
3. **Silent openings:** subscribe to signed conference speaker events. Multiple
   caller energy bursts without recognized words, corroborated by Vapi voice
   activity or missing agent energy, can trigger one recovery during the opening.
   Quiet callers and a single brief noise do not. Any recognized caller words or
   any business tool invocation disables automatic session replacement. Recovery
   announces the problem, verifies the old leg ended, closes its local authority,
   replaces its proof, and redials once. Old callbacks cannot attach the retired
   leg. This detects a suspected failure; it does not identify its root cause or
   prove handset audibility. Calls that end too quickly may escape detection.
4. **Stuck capacity:** definite create failures clear pending creation; ambiguous
   outcomes retain ownership until provider reconciliation. Cleanup serializes,
   retries on a sweeper, observes background errors, and checks both room and
   legs. No blind dial retries or broad termination of unrelated calls.
5. **Cut-off apology:** successful apology TwiML owns the caller until completion
   or a bounded timeout. Cleanup no longer immediately hangs up its playback.
6. **Late listening audio:** announcements have a 400ms request deadline, a
   single-use expiring signed fetch ticket, and a fresh speaker-state check.
   A ready answer invalidates unfetched clips. If a clip was fetched already,
   answer release waits for its bounded expected end. HTTP cancellation is not
   remote audio cancellation; actual telephone overlap still needs capture.
7. **Hang-up/admission races:** per-bridge serialization and fresh persisted
   readbacks retire the exact session. A full listening registry disables optional
   listening for that call instead of returning HTTP 500.
8. **Other phone identities:** optional `VAPI_TWILIO_*` settings add a separately
   authenticated identity alongside the primary phone ingress. Proof checks,
   closing state, waiting gates and model selection apply to the isolated
   identity only. The primary identity keeps its own secret and admission path.
   The production enable prohibition remains in place.

## Conversation changes

- Listening warm-up persists across pauses within the same explanation.
- Approved descriptions may receive a listening cue in a 300–1000ms pause;
  stale text and idle silence cannot create an opportunity.
- Listening cues remain at least five seconds apart, bounded to 12 per logical
  caller turn and 36 per call. Classifier decisions remain bounded separately.
- A clear new vehicle-description clause can release earlier question/private
  context; explicit control, safety and injection vetoes persist. Jev receives
  the current clause and a constrained prior-assistant context, not an instruction
  to guess consent or complete missing words.
- An optional immutable telephone audio bank adds two takes each of “Uh-huh,”
  “Yeah,” “Okay,” and “Right.” Rotation avoids immediate word repeats. These are
  attention cues for approved descriptions, never agreement with a pending action.
- The approved uncertain waiting fallback is exactly “One sec.” It needs a valid
  ordinary-content classification, verified English compatibility, no pending
  action/proposal/confirmation, an observed caller stop, and a recent final
  transcript matching the request. It excludes requested names and all provider
  errors. Background silence, unfinished ASR, and a final clause during ongoing
  speech are insufficient.

## Model comparison

The available lightweight reasoning model was `gemini-3.5-flash-lite`.
`gemini-3.8-flash-lite-tts` is a speech model, not the answer-generation replacement.

Two bounded evaluations used synthetic callers and disposable scheduling databases:

- Interleaved provider streaming samples: eight per model. Median complete
  response was approximately 2.37s for 3.8 Flash and 1.10s for 3.5 Flash-Lite.
  The models selected different text/tool paths; these are not paired handset
  timings or production percentiles.
- The real gateway evaluation completed all six scenarios with 3.8 Flash.
  Flash-Lite completed five; after correctly dismissing a declined proposal it
  returned an empty reply. Its text-only release median was approximately 1.19s
  versus 2.08s for Flash, on small, differing samples.

**Keep 3.8 Flash on the phone candidate.** The faster model has not met the user's
reliability condition. Its opt-in staging configuration and actual-model trace
metadata are available for further investigation. Empty upstream replies now
produce a failure outcome rather than an empty successful answer.

## Review evidence and outstanding acceptance

Regression coverage includes durable cleanup, concurrent finish/dial, registry
capacity, recovery success/abort/failure, caller hang-up, proof replacement,
cross-identity credentials, partial cue captions, uncertain fallback, short pauses,
audio integrity and model provenance. Live Jev classification selected 28/28
ordinary fixtures and 0/32 excluded fixtures. A blinded advisory audio review
transcribed the eight new clips exactly and rated them clear with complete endings.

The full test result, exact deployed commit, acoustic self-call captures, and
LangSmith readback belong in the local handoff after deployment verification.
Human handset and speakerphone testing remain separate acceptance steps.

New trace/evaluation destination: `servicevoice-twilio-duplex`. Historical reviews
remain in `servicevoice-backchannel-experiment`; do not relabel or overwrite them.
The old semantic-review cap was exhausted at 50 attempts. The repair deployment
plans a bounded allowance of 20 additional attempts, without resetting counters.
Existing trace allowance remains 1,000; 381 were consumed at inspection.

Review metrics distinguish actual saved phrase IDs, clip fetches, caller energy,
waiting dispatches during caller energy, duplicate dispatches by gateway turn,
guarded fallbacks and suspected media failures. Dispatch/fetch and speaker events
are not proof of what a person heard. The reviewer never treats ungraded calls as
passing calls.
# Second review: rejected announcement audio and tool follow-up

The 961f08c interruption capture was not a clean acceptance result. Twilio fetched
an invalidated Okay ticket, received HTTP 410 and spoke its application-error
goodbye. A prior advisory audio review missed it. The revised verification gate
reproduces both the caller-leg 11200 notification and unexplained output audio.

Announcement refusals now return HTTP 200 with an 80 ms silent WAV and no caching.
Authentication still gates every real clip and mutation. Expiry, ownership,
single-use, closed-session and main-speaker priority remain enforced; ordinary
ASR revisions and pauses do not expire an approved clip. Pause/request delivery
uses only Mm-hmm/Uh-huh, including a neutral substitution if a pause begins during
fetch. Each call samples a starting rotation offset.

The logical turn permits one initial waiting cue and one verified-tool-result
follow-up, separated by at least 2.5 seconds. Neither is refunded when generation
is cancelled. The metric distinguishes this deliberate follow-up from duplicates.

`scripts/verify_twilio_probe.py` fails on any Twilio notification or speech-like
output attributable to neither an approved clip match nor Vapi speaking intervals.
Missing evidence fails closed. It records detector thresholds and transport
tolerances; it is not a naturalness score. Owned-call collection and waveform
auditing invoke these checks automatically. The separate retired-ticket live probe
must verify silent handling before handset acceptance. Main remains unchanged.

