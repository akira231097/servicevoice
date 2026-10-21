# Hybrid Jev cue experiment

This branch adds listening acknowledgments and short waiting holds to the existing
STT → reasoner → TTS assistant. The isolated browser owns both cached cues and
main-response PCM on one output clock while caller audio keeps streaming. Jev
classifies bounded transcript snapshots alongside the reasoner; it cannot book,
confirm, change records, produce arbitrary speech or authorize an outcome.

The main assistant and number retain their existing configuration. This is an
experiment in an isolated assistant, database and private operator console.
Earlier live calls exposed missing waiting cues and long answer gaps. Those
failures remain evidence; the new offline results do not establish that the
revised conversation is production ready.

## Meaning and phrase selection

[TypeSafe Choice](https://docs.typesafe.ai/primitives/choice) and
[structured state](https://docs.typesafe.ai/concepts/state) support separate,
atomic questions against the same bounded state. The selector asks independently
whether existing content is eligible, whether the fixed English cache matches its
language, and, for waiting only, which content kind is present. Content kinds are
correction, information request, intake detail and other, with explicit precedence.
Code maps the result to available approved audio. Synonymous filler phrases do
not compete as semantic labels or split the eligibility probability.

[TypeSafe confidence](https://docs.typesafe.ai/confidence) publishes the Choice
normalization `(p_max - 1/n) / (1 - 1/n)`. It is derived from the probability,
not independent corroboration. Main cue eligibility retains probability ≥0.98,
margin ≥0.96, language probability ≥0.99 and the 350 ms provider deadline.
Optional style selection requires both probability and reported confidence ≥0.90;
uncertain style falls back to a neutral phrase. These thresholds are unchanged.

| ID | Exact cached text | Permitted meaning |
| --- | --- | --- |
| `neutral_ack` | Mm-hmm. | Occasional listening signal for ordinary content |
| `moment` | One moment. | Neutral waiting hold |
| `one_sec` | One sec. | Neutral waiting hold |
| `just_a_sec` | Just a sec. | Neutral waiting hold |
| `let_me_see` | Let me see. | Neutral waiting hold, including a clear ordinary correction |
| `checking` | Let me check that. | Prospective intent for a confidently classified information or scheduling question |
| `taking_a_look` | Let me take a look. | Prospective intent for a confidently classified information or scheduling question |
| `working_on_it` | Working on it. | Only fresh, scoped, nonempty accepted work whose pending rows are all queued/running |

The last two prospective phrases claim no completed lookup, booking or result.
Present-tense progress is never inferred from the classifier. Unknown work,
ended/handoff state, active confirmation and protected readbacks remain silent.
The public `accepted_work_pending` flag is only a scheduling hint; the cue API
independently derives it from authoritative pending rows before and after selection.
A client-supplied flag grants no authority.

Listening remains conservative about corrections, names and consent. Waiting can
follow a clear factual correction without affirming it: the raw correction first
invalidates the old selection, then a fresh neutral-content decision is required.
Bare approval/rejection, stop/cancel commands, action consent, conditional approval,
final booking readbacks, supplied contact/payment/credential details, urgency,
instructions and unsupported language remain excluded. Routine vehicle negation,
model years, oil changes and waiting-time questions are not control commands.
Names never enter a cached phrase. No audio splicing is used.

## Timing, preparation and speaker ownership

Each caller utterance permits at most one listening cue and two waiting cues.
Listening cooldown is 15 seconds; waiting cooldown is one second. The combined
budget stays at four accepted reservations and 64 classifications per rolling
15 seconds, with eight attempts per utterance. Accepted reservations are never
refunded after cancellation or uncertain delivery.

The first waiting cue cannot play before 550 ms after confirmed yield or within
300 ms of another cue ending. One optional long-wait follow-up uses a fresh
classification and fresh authoritative state, chooses a different phrase and
cannot play before 1,250 ms of post-cue silence. It starts selection after the
first cue has completed and the phase cooldown/request cadence permit. It has no
third attempt or preparation reuse. A fast main reply cancels future cue work.

At most two nonplaying preparations per utterance can classify a stable exact
waiting snapshot. Preparation returns silence and reserves no audio slot. Only a
completed receipt for the exact text, epoch, prior assistant context and safe state
can be reused within two seconds. Activation reads authoritative state again and
creates a new decision with the unchanged 800 ms total request-age limit. An
in-flight preparation may be joined only within the original bounded selector
budget; main speech, caller resumption, changed text or sensitive state cancels it.
Preparation shares the request budget and retains an actual-waiting allowance.

Approved duplex clips must be at most 1,100 ms. When actual voiced main PCM arrives,
in-flight and held cue selections are canceled. If a validated cue has already
started, its complete remaining bytes may finish before the main response. The
first packet fixes that audio-clock barrier; later packets cannot extend it.
Thus main audio may wait up to 1,100 ms for word completion. It is incorrect to
claim zero handoff delay or instantaneous useful answers.

Confirmed caller speech immediately revokes cached and main output; the browser
uses a bounded 8 ms fade. Unconfirmed local energy can preserve/pause the current
main suffix for up to 350 ms while waiting for provider/transcript confirmation.
Caller capture continues throughout. This is an acoustic heuristic, so echo,
quiet speech, packet delay and room/telephone interruption still need real tests.
Output overflow fails the isolated session explicitly rather than silently dropping
valid main PCM. One owned output queue prevents cue/main overlap.

For the exact isolated assistant, the authenticated
`servicevoice_duplex_playback=browser-owned-v1` marker suppresses the earlier
server-emitted after-turn filler. Other calls retain their existing behavior.
The earlier `<flush />` path is historical context, not a second speaker in the
owned browser transport. See [browser run instructions](duplex-browser.md).

## Main-response prompt scope

The versioned reasoner prompt distinguishes a caller volunteering an intake fact
from asking to schedule. A standalone vehicle detail should not trigger an
unsolicited availability lookup. Genuine booking offers retain explicit
presentation metadata so the existing verified receipt renderer can be used when
eligible. No booking tool schema, confirmation requirement, operation authority,
production timeout or receipt validation is weakened by cue selection.

Bounded synthetic prompt comparisons supported this clarification, but an offer
or invitation can still occur. The live continuity tests must measure actual
useful-answer latency and tool behavior; adding filler does not make model
inference or synthesis faster.

## Current evidence and remaining acceptance

The new frozen 64-case synthetic policy development sample used 30 Jev requests,
zero retries and no voice calls. Waiting selected 19 of 21 ordinary examples and
stayed silent on all 31 excluded examples. Listening selected three of six ordinary
examples and stayed silent on all six correction examples. The first three
ordinary listening requests exceeded the unchanged 350 ms deadline. Across both
phases, 22 of 27 ordinary examples selected a cue and all 37 excluded examples
remained silent.

The two waiting misses were a service-replacement correction and a requested name
in a simulated accepted-work context; their content probabilities remained below
0.98. Seven approved phrase IDs appeared. `working_on_it` was reachable in scoped
API/session tests but was not selected in this bounded model sample. Simulated
pending work does not prove that a remote operation actually ran. These are
retained development outcomes, not calibrated production accuracy.

The reviewed local audio bank contains eight approved duplex clips, all at most
1,080 ms. Format/hash checks and a separate model listening advisory found complete,
clear phrases. This does not replace human listening or new end-to-end playback.
421 focused Python checks passed for the policy/API/gateway/lifecycle/console and
unchanged legacy policy before later integration checks. Exact final commit,
complete suite/CI results and new live captures must be recorded separately.

Earlier browser captures proved continuous caller input, owned cue playback and
interruption in some scenarios, but also preserved failed waiting coverage and
long gaps after a cue. One six-turn capture covered every waiting turn while the
main reply still took up to about 8.5 seconds; that result is not smoothness proof.
Subsequent preparation captures also retained continuity failures. Do not rename
failed receipts as passes or replace their original acceptance rules with a
post-hoc fast-answer criterion.

New acceptance must bind exact source, manifest, raw receipt and recorded PCM.
Measure caller source end, confirmed yield, cue voice start/end, packet readiness,
actual useful main voice and post-cue silence separately. Verify complete cue
endings or explicit caller cancellation, the fixed ≤1,100 ms main handoff, no
assistant overlap, bounded follow-up frequency and no booking mutation in safe
FAQ/intake tests. A short sound is not a useful answer. Repeated tests must include
ordinary corrections, short turns, long speech, language changes and fast-main races.

## Phone bridge boundary

The branch now includes an opt-in phone media bridge: authenticated Vapi inbound
admission, a one-use SIP destination, private Asterisk AudioSocket PCM and the same
owned duplex session. It prewarms one inner isolated call, binds it to the verified
outer call and owns one paced output queue. This implementation does not change a
saved number or assistant. See [phone bridge scope and runbook](../integrations/phone-bridge/README.md).

Local HTTP/TCP/media tests and a network-isolated lab can validate code paths and
digital geometry. They do not establish that the managed carrier forwards to the
chosen endpoint, negotiates the required TLS/SRTP, preserves real caller audio or
stops downstream phone audio within the local cancellation bound. Live phone
validation, both-leg ended readback, representative acoustic listening and
continuity evidence are still required before main phone routing or promotion.

## Configuration, evidence and rollback

`SERVICEVOICE_BACKCHANNELS` defaults off. The gateway additionally requires the
exact isolated `VAPI_BACKCHANNEL_ASSISTANT_ID`; the audio cache validates its
immutable manifest, exact text/model/voice/sample geometry and every PCM/WAV hash.
Only harmless outer ASCII whitespace can be normalized for a cache lookup. Other
sentences continue through ordinary synthesis.

Generated audio, API keys, snapshots, caller transcripts and private deployment
settings stay outside Git. Content-free cue telemetry records fixed IDs/reasons,
confidence, phase, bounded timing, preparation reuse and suppression. Join provider,
model and TTS evidence only through explicit shared IDs, never time proximity.
Model text readiness, returned PCM and graph playback have different meanings.

Candidate deployment must bind the tested worktree and exact manifest. An additive
bank upgrade preserves all old audio bytes, changes only the candidate business
and speech manifest setting, and restores exact old settings before old-image
startup on rollback. Main/provider/edge configuration and data mounts stay outside
that transaction. End active calls and automatic evaluations before rollout;
unknown outcomes require reconciliation. Passing offline checks alone does not
permit a claim that the candidate is ready for production.
