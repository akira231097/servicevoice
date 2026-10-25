# Isolated Twilio listening prototype

Status: isolated deployment and synthetic phone testing in progress. The feature
is disabled by default. A separate Twilio number and Vapi SIP assistant are used;
the main Vapi number, assistant and production services remain unchanged. Live
listening acceptance is outstanding. This is not yet a verified simulated
full-duplex release.

## Current concurrent capacity

The isolated bridge, listening registry and Jev listening pool admit **four**
callers. Application admission, main generation, speech generation and Twilio's
HTTP pool also have four slots. Each caller gets a private two-participant room
(caller and assistant); the conference participant count is not raised.
The user confirmed Vapi's shared account allowance is four, so the main and test
lines cannot assume four slots each at the provider. Startup refuses an application
call cap below four. The local readiness endpoint reports the relevant ceilings.

The independent lifetime prototype quota is now **100 calls**, raised from 60 at
the user's request to allow more testing. This total does not reset daily.
Admission history is retained; no counters are reset. These are configured and
offline-tested limits, not a four-caller live audio performance claim.

## October 24 handset regression repairs

The next conversation-policy candidate separates neutral holds from contextual
phrases. On the isolated assistant, a completed greeting, short answer, correction,
contact response or confirmation can receive "One moment", "One sec" or "Just a
sec". Those sounds confer no business authority. Stop/wait/goodbye, unfinished
speech, urgency, unrecognized input and meaningful non-English speech remain
excluded. Contextual lookup phrases retain the stricter content policy. Actual
caller-stop evidence, the quiet window, primary-answer priority, one hold per
logical turn and the spaced tool-result follow-up remain in force.

Later vehicle/service/scheduling descriptions can receive neutral listening
sounds with a pending proposal or operation. Active confirmation answers,
contact dictation and control/correction phrases remain excluded. A matching
finished-response classification suppresses a pause acknowledgment so it cannot
take the waiting cue's place; resumed speech invalidates that match. Fetch refusals
still return silent HTTP 200. This does not make already-fetched Twilio clips
instantly cancellable.

An additional live regression showed an inaudible speculative hold consuming the
logical-turn allowance. A cancelled generation with no released answer/tool can
now mark its reservation abandoned. Both budgets are reclaimed together only
after later caller speech and a stop, at least 2.1 seconds of event-lateness grace,
and no observed assistant speech or transcript since reservation. Heard or
interrupted cues, unknown/completed generations and stale reservation stamps keep
their allowance consumed. Reclaiming is not based on HTTP cancellation alone.
The private-address filter also avoids interpreting mileage followed by ordinary
driving prose as a street address; real contact/address dictation stays excluded.
The repeat still exposed a clause pause: Vapi briefly reported assistant speech
before cancelling it, so the conservative ledger correctly refused a refund.
A one-second quiet-window trial did not eliminate that live gap and was reverted;
the quiet window remains .75 seconds. A stopped caller's finalized clause now
revokes a listening cue before the waiting classifier finishes, including at
media fetch. A final alone never grants authority to emit a hold, and continuing
words reopen listening classification. Longer ambiguous pauses remain possible;
this is not prosodic endpointing or proof of pause-free conversation.

The owned-call acoustic checker retains its original unexplained-audio flags.
It can additionally align a provider speech interval using at least three nearby
correlation-matched waiting clips from two phrase IDs. Matching durations, unique
intervals, a 30-second neighborhood and at most 120 ms lag spread are required;
unsupported intervals retain the old 350 ms tolerance. This distinguishes audio
already in transit after an interruption from an unrelated announcement without
calibrating on the unexplained sound itself. Twilio notifications always fail the
gate. The original application-error capture still fails under this alignment.

An isolated-only delivery instruction targets one idea and about 20 words, with
complete readbacks, references and option lists taking priority. Automatic reviews
measure ordinary reply length separately, excluding greetings, approved hold
prefixes and turns backed by verified readback/options/result tools. This numeric
score is not a naturalness judgment.

The isolated Vapi assistant's duration was separately raised to 600 seconds;
Twilio's conference and SIP limits already use 600. Idle-message settings and
handset/audio results must be read from the deployment evidence, not inferred
from these policy tests. Main's assistant configuration remains separate.

The later handset call ended with `pipeline-error-custom-llm-llm-failed`, not a
three-minute duration limit. The model issued a signed two-tool group, while
Vapi returned only its first command/result. The gateway rejected that incomplete
history. The isolated assistant now requests one action per generation. If the
model still produces a batch, the entire unissued candidate is discarded and
privately regenerated once. A second batch becomes a clarification with no tool
issuance. Signatures, observed-result checks and booking requirements stay strict;
other assistants retain their existing complete-group behavior. Retry usage and
outcomes remain observable, and cancellation releases the generation's slot.

Long informational questions about services, diagnostic coverage and billing may
receive neutral listening sounds while the caller speaks. A narrowly recognized
hypothetical on-site cancellation-policy question is distinct from an actual
cancellation command. Jev still excludes actual action approvals/corrections,
private details and sensitive task state. Questions use only the neutral
Mm-hmm/Uh-huh recordings; their listening opportunity ends at caller stop. They
do not receive an agreement, a diagnosis or a claim that work has started.

Current listening spacing varies by pause opportunity; caps remain 64 classifier
decisions and 36 cues per call. Those caps were not exhausted in the failed call.
Owned synthetic capture can now retain four minutes of evidence; ordinary caller
capture permissions and phone duration limits are unchanged. The earlier samples
and timing rules below describe the initial October 23 implementation, not current
acceptance evidence. Live results for these repairs must be recorded separately.

The separate Twilio line puts the caller and a dedicated Vapi SIP assistant in
one conference. Jev classifies live partial transcripts; code may request a
caller-only participant announcement from the approved “Mm-hmm.” / “I hear you.” bank.
Waiting cues continue inside the existing Vapi stream with their separate
budget. No listening cue creates consent, tool work, a proposal or a booking.

### Listening versus waiting

Listening permits only “Mm-hmm” and “I hear you,” on approved partial content
during caller speech, with a five-second cooldown. Waiting permits only the
separate hold/check phrase catalog after an observed caller stop and at least
750 ms of quiet following its receipt or the latest partial. A finalized ASR
clause or speculative model request does not establish that the caller finished.
The waiting stream may wait up to 1.25 seconds for that boundary while the main
answer runs concurrently; a ready main answer always wins. Resumed caller speech
invalidates the pending cue, including during the fresh business-state read.
Unknown speaker state fails closed, and a recently dispatched listening clip
also defers the waiting cue. The main Vapi transport is not changed by this guard.

This prevents the observed early waiting-cue emissions inside ongoing speech.
Webhooks still arrive asynchronously: the check is not an atomic handset audio
lock, and Vapi retains responsibility for interruption after a cue is released.
An extended pause inside a completed thought can still resemble a turn ending.

## Implemented boundaries

- Twilio signature verification over the configured external URL and all form
  fields, including future fields; bounded form bodies; exact account and number.
- Durable ownership and four concurrent bridge slots. Duplicate joins cannot
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
- Revised limits after the October 23 handset test: at least three seconds and
  six words; partial age at dispatch at most 300 ms; five seconds between cue
  reservations; three cues per explanation and twelve per call; 1.5 seconds
  after observed assistant speech stops.
  An uncertain growing partial may be reconsidered after 1.5 seconds, without
  spending the audible-cue cooldown. Duplicate text is not classified again;
  there are at most 64 decision attempts per call. Confidence thresholds remain
  unchanged. This separates frequent listening decisions from restrained speech.
- A caller pause of at most 1.5 seconds preserves the explanation's timer and
  three-cue budget when the assistant has not spoken. A longer pause, assistant
  speech or tool activity starts a fresh listening interval.
- Completed ASR clauses stay in bounded context for the next fresh partial;
  final transcripts never trigger listening playback themselves. Ordinary
  symptom uncertainty and casual conversational yes/no are classified by Jev;
  explicit control, final approval, sensitive context and private details veto.
- Only known ASR typography (curly quotes, dash, ellipsis, nonbreaking space) is
  normalized for cue decisions. The main transcript stays verbatim. Other
  non-ASCII text is not transliterated into an English-cue opportunity.
- After Jev approval, problem descriptions can use “I hear you.” (1.06 s),
  alternating with “Mm-hmm.” (0.67 s). Neither clip claims diagnosis, consent,
  accepted work or booking progress. Clip choice is a closed, signed query on
  the existing audio route; no production routing change is required.
- Native waiting classification can retry one malformed provider response
  inside the existing 350 ms deadline. Valid silence, language vetoes and low
  confidence are never resampled. Both attempts remain visible in diagnostics.
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
failed captures are retained. Later probes below establish listening playback.

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

The final isolated runtime tested on October 23 is `7d83141`. Both GitHub
workflows passed: 2,648 Python tests, 155 JavaScript tests, and 57 deployment
tests with 19 subtests. Three test services are healthy; the main assistant,
number and protected runtime remain unchanged. The prototype is restored to
conference mode with listening enabled and no active test bridges.

Further live evidence:

- Consecutive 23.2-second explanations produced two full listening clips and
  preserved all 75 source words in order. A multi-turn call used four distinct
  waiting phrases plus two listening clips, with no appointment or proposal.
- Shared-clock synthetic caller capture measured 492.7 and 567.7 ms from cue
  approval to first voiced audio arrival (median 530.2 ms; only two samples).
  All 5,568 media messages were contiguous. This is not handset acoustic latency.
  The provider advertised protocol version `0.2.0`, unlike the documentation's
  example; the diagnostic receiver records that metadata and separately validates
  the authenticated call, capability, media format and every frame.
- Three FAQ calls per topology measured median caller-end-to-answer gaps of
  1.48 seconds in conference mode and 1.38 seconds in direct-SIP mode. The observed
  100 ms difference includes backend/network variability and does not isolate
  pure transport overhead. One scripted caller overlapped the greeting tail;
  the measured answer gap starts after the caller's question.
- One recorded interruption stopped old speech in approximately 720 ms. A
  goodbye call ended with `assistant-ended-call` after 18 seconds, before its
  75-second test cap. Known Twilio legs, Vapi calls and local sessions closed.
- A live malformed waiting-classifier response recovered on its second attempt;
  both requests were retained in diagnostics. Automatic reviews completed for
  the main tested scenarios. Waveform evidence is distinct from advisory audio
  model ratings, which sometimes missed cues present in the recording.

Still needed before production acceptance: handset/speakerphone echo and
listening tests, broader timing/race samples, and settled incremental billing
evidence. One repeat call substituted connecting words; exact transcription is
not guaranteed. Dispatched participant announcements are not an atomic audio
lock. No production merge/deployment follows from these samples alone; this
task explicitly preserves the main agent and asks for user testing first.

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

### Fifth-review repairs (October 24)

The isolated lane replaces the logical-turn hold lock with observed spacing:
four seconds from the last dispatch/observed start and at least one second of
subsequent caller speech. Sustained, growing fresh ASR can establish that
continuation when Vapi omits a new speech-start event; repeated/revised text,
old transcripts and widely spaced fragments cannot. Repeated holds are neutral and never immediately use
the same phrase. One tool-result follow-up remains available after 2.5 seconds.
Cancelled, measured speech shorter than 300ms can reclaim a reservation after
the event-lateness grace; even a blip retains its observed spacing anchor.
Missing or inconsistent speech boundaries do not prove unheard audio.

The isolated prompt explicitly requests the existing typed presentation metadata
for standalone availability offers and complete preparation readbacks. The same
signed receipt, current-state, language, timezone and operation checks apply.
Phone offers consolidate a shared date/timezone; complete consent readbacks are
unchanged. Extra questions, unsupported languages and stale results use the
ordinary model path. Metadata is never supplied or altered after tool issuance.

A narrow explicit English goodbye can issue the existing signed native endCall
without a model pass when fresh recognized closing words match, the caller has
observably stopped and stayed quiet, and no proposal or operation is pending.
A final-transcript marker is not required. A verified proposal dismissal can reach that path; the
shortcut itself performs no scheduler action. Bare stop, quoted/conditional
goodbyes, consent, new requests, uncertainty and other languages keep the model
path. The isolated live repeat reached this path after verified withdrawal;
its goodbye was audible after 3.20 seconds instead of 5.88 seconds. That still
includes withdrawal work, and is not an instant-response claim.

Vapi can send an assistant partial after conference cleanup. The lane retains
at most the prototype lifetime quota of exact ended call/session identities.
Authenticated fast events for those identities are acknowledged without
restoring listening, admission or tool work. Unknown or mismatched identities
remain rejected; the in-memory receipts do not survive a process restart.

These changes do not switch models, shorten booking checks, or enable the
prototype on the main number. Endpointing, prompt reduction, speakerphone echo,
and four simultaneous live callers require separate measurement.
## Handset regression safeguards (October 24)

The isolated speech service can enable `SERVICEVOICE_TTS_ATOMIC_FAREWELL=on`.
Only the exact native farewell, “Thank you for calling. Goodbye.”, is buffered
and validated completely before any audio bytes are returned. A one-second
silent tail allows the final voiced frames to cross phone/conference buffers
before teardown. The eight-second total audio bound and existing request deadline
still apply. Ordinary replies
continue streaming; the main speech service is not opted in. This addresses an
observed Vapi endCall failure where a five-millisecond first segment was mistaken
for farewell completion and the real audio began after disconnect.

Fresh growing partial words can restore listening after an omitted VAD start.
They revoke waiting-cue permission and never establish an end of turn. Repeated
text, same-length revisions, stale events and elapsed silence cannot do this.
An observed stop is still required before a waiting cue.

A descriptive passage over twelve words needs 1.5 seconds of observed quiet
before a hold, including early pauses and after an interruption resets the timer.
Short replies and a final direct question/request retain the 0.75-second floor.
This reduces cut-off “Just…”/“One…” fragments during one-second thinking pauses;
it does not infer an end of turn from silence or delay a ready main answer.

Jev distinguishes routine worry, uncertainty about a vehicle issue and earliest
appointment requests from emergency urgency and commands to stop. The isolated
neutral-hold fallback also accepts a weak exclusion (below 0.90) only with an
independently recognized routine subject, compatible language, fresh matching
final caller text and observed quiet. Strong exclusions, missing/invalid model
responses, explicit stop/end requests and unfinished thoughts remain silent.

## Caller-ID name step (October 25)

Optional and off unless both `SERVICEVOICE_TWILIO_CALLER_CONTEXT_POLICY` and
`SERVICEVOICE_TWILIO_CALLER_CONTEXT_KEY` are set, and then only on the conference
lane with listening cues (other settings refuse to start). The private policy
file holds the mode (`off`, `shadow`, `personalize`) and HMAC-SHA256 digests of
allowlisted numbers, never the numbers. It is reread every two seconds off the
call path; a missing, unreadable or malformed file fails closed to `off`,
including for calls already in progress.

For an allowlisted caller, or every eligible US caller when the policy sets
`"callers": "everyone"`, the signed incoming-call webhook starts one background
task. It first reads this number's latest earlier booking (name, vehicle and
visit count; read-only, no schema change); only without one does it make a
Twilio Lookup caller-name request (`Fields=caller_name`, about one cent) on its
own four-connection pool with a single total deadline, no retries and no queue.
Nothing in call admission, the greeting or an answer waits for it. Results live
in memory for that call and are discarded at hang-up. In allowlist mode other
callers are never looked up; withheld and toll-free numbers never are. A
labelled synthetic name is accepted only for owned test calls.

A returning caller hears "Should I put this under [name] again, at the number
you're calling from?". The earlier booking's vehicle is withheld from the
assistant until that question has been asked, and is offered ("Is this for the
[vehicle] again?") only if the caller confirms the name. If they decline, the
earlier name, booking and vehicle are never mentioned. No earlier appointment
becomes accessible: the existing session-only appointment rules are unchanged.

CNAM listings are at most 15 characters and often placeholders. The cleaner
rejects business, placeholder, place-name, joint, single-word, initial-only,
possibly truncated (15 characters) and instruction-like values, and drops titles
and suffixes. Only an explicit comma establishes surname-first order; otherwise
the listed order is kept and never guessed, so the assistant uses a confirmed
name in full or not at all.

In `personalize`, the phone assistant receives unverified caller-ID text after
the policy hash. It replaces the existing name-and-number question with "Should
I put this under [name], at the number you're calling from?", or, without a
usable name, "May I have your name? I can use the number you're calling from."
The question state comes from the call history Vapi reports, so a cancelled
draft never counts as asked; after the question, the name is withdrawn and only
a name the caller confirmed or gave is used. A lookup that finishes after the
number was already offered never reopens the name step. Caller ID is never identity,
consent or authentication: the readback and explicit later assent still govern
every booking. `shadow` performs the lookup without changing speech.

Diagnostics record fixed codes only (`caller_lookup`, `caller_hint`,
`caller_policy` on `twilio_bridge`): outcome, mode, STIR/SHAKEN category and
duration, never the number, name or provider response. The reviewer reads the
same codes to add a content-free `caller_id` evidence item, so an accepted
caller-ID name or calling number is not graded as an invented detail. The
booking database schema is unchanged.

The owner's number returned no CNAM name ("UNDETERMINED"), so live calls from
it use the number-only question; the name question is exercised with the owned
test line's labelled synthetic name.
