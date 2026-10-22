# Prerecorded waiting cues through Vapi

This release keeps the existing Vapi phone number, speech recognition, assistant,
and phone transport. It adds a short prerecorded phrase while an answer is being
prepared. It does not add a second audio connection or acknowledgments during
the caller's speech.

The reasoning request and the TypeSafe Jev classification start in parallel.
The gateway gives the answer a 550 ms head start. If the answer is ready, it
passes through without a cue. Otherwise, a timely, confident classification may
select one approved phrase. The phrase and answer use the same Vapi response
stream, with a complete-phrase `<flush />` boundary and the existing interruption
handling. A caller interruption invalidates the old generation.

Jev independently classifies eligibility, reply language, and content category.
The application selects an exact cached phrase appropriate for that category,
avoiding the previous phrase where possible. Short names and factual corrections
receive neutral pauses. Information requests can receive prospective phrases
such as "Let me check that." Names are not inserted into prerecorded audio.
"Working on it" and verbal agreement are not part of this waiting-cue selection.

The first production phone review exposed missed cues on ordinary capability
questions, initial booking requests and casual "yeah" replies. The policy now
recognizes those conversational forms. Standalone approval, cancellation and
booking-context checks remain separate; "Yeah, it is a 2016 sedan" does not
authorize an action. Explicit thinking pauses and unfinished conjunctions stay
silent so the caller can continue.

A routine utterance with fit probability at least 0.90 and margin at least 0.80
may receive only a neutral pause when the independent subject classification is
also strong (probability at least 0.95 and confidence at least 0.93). Lookup-style
phrases and confirmation holds retain the stricter 0.98 fit requirement; language
compatibility still requires 0.99. This separates phrase-selection risk from
business authority, following TypeSafe's guidance to
[validate thresholds for the consequences of each action](https://docs.typesafe.ai/confidence).
The reproducible synthetic corpus in `scripts/benchmark_phone_waiting.py` covers
47 ordinary and excluded examples, including the phone review's failure patterns
without retaining caller contact details.

The cue can never confirm a booking or authorize a tool. Conditional approval,
stop requests, urgent situations, unsupported language, stale context, and failed
classification suppress it. Any allowed confirmation hold is restricted to a
neutral pause; business validation and confirmation requirements remain separate.
Signed tool-follow-up requests reuse only the caller context for classification,
not tool arguments or results, and share the same one-cue-per-turn limit.

The budget permits at most one cue per caller turn, separated by at least one
second and capped at four within 15 seconds. It does not shut off after four
cues over an entire conversation. Completed or expired sessions can retire their
budget entries without evicting active calls.

Vapi starts speculative model requests while a caller is still speaking and
cancels or replaces them as speech continues; it discards whatever those
requests produced. A cue emitted into such a request is refunded to the turn
budget when the generation ends cancelled or superseded before any answer text
or tool command was released, so the real post-turn request can still receive
its cue. The refund keeps the one-second spacing from the unplayed emission,
never permits back-to-back speech, and does not apply to completed answers or
upstream failures. The timing row records `refunded` with its cause, and the
`backchannel.refunded` diagnostic lets the evaluator ignore that emission.

## Runtime configuration

Enable `SERVICEVOICE_BACKCHANNELS=on` for the gateway and speech service. Set
`VAPI_BACKCHANNEL_ASSISTANT_ID` to the exact assistant allowed to receive cues,
and mount the verified audio manifest at
`SERVICEVOICE_BACKCHANNEL_AUDIO_MANIFEST`. Jev uses the existing TypeSafe
credential. The saved Vapi assistant requires `voice.chunkPlan.enabled=true`.
Cache integrity and readiness are checked before serving enabled traffic.
The original 16 kHz recordings remain immutable. Startup warms bandlimited PCM
derivatives for all supported Vapi rates (8, 16, 22.05, 24 and 44.1 kHz), so a
phone-format request needs neither speech synthesis nor live conversion. Cache
asset identity refers to the original verified recording; diagnostics record
the delivered rate and whether resampling was used.

Keep the feature disabled until the exact release has passed regression tests,
direct Vapi audio checks, and cache-hit verification. Retain the previous image,
environment values and assistant chunking configuration for rollback. Production
uses its existing database and phone mapping; candidate booking state is never
copied into production.

## Evidence

Measure time to the cue separately from time to useful speech. Gateway emission,
speech-cache delivery and received Vapi audio are different measurements. A cue
does not make the main model faster, and a cue already playing can briefly defer
the answer through the same output queue. Tests must check fast-answer
suppression, complete clips, interruptions, same-turn tool follow-ups, phrase
variety and suppression in excluded contexts.

The initial single-phrase experiments are documented in
[the historical experiment notes](backchannel-experiment.md). They are not
acceptance evidence for subsequent releases. The separate browser audio
controller and SIP bridge are outside this release.

Vapi documents the required [flush configuration](https://docs.vapi.ai/assistants/flush-syntax)
and [interruption handling](https://docs.vapi.ai/customization/voice-pipeline-configuration).
