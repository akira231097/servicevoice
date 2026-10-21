# Hybrid duplex browser experiment

This branch adds an isolated browser transport that can play a short cached
listening cue while the caller is still speaking and a neutral hold during the
wait after a short reply. Caller audio keeps streaming
to Vapi/Soniox, and Gemini continues to own the main conversation and guarded
booking tools. This is separate from the earlier after-turn acknowledgment.

The operator opens a localhost console. A server-side allocator creates a
WebSocket call for the existing isolated assistant; its administration key never
enters the browser or the AWS business container. The browser sends continuous
20 ms, mono PCM16 frames at 16 kHz, including silence. One WebAudio clock owns
both cached cues and received main audio. Neither path mutes microphone capture.
The main target is the two-to-three-second answer gap after a caller finishes;
listening cues remain occasional rather than filling every conversation pause.

## Cue decisions and speaker priority

Partial transcripts are data for a bounded Jev content decision: ordinary
vehicle/service conversation or excluded content. Code maps an accepted listening
decision to `Mm-hmm.`. Waiting uses a separate disjoint content-kind question and
code selects from the validated neutral/prospective bank described in
[the current experiment contract](backchannel-experiment.md). Jev
classifies the content already present without predicting the next words or
requiring the caller to finish a request. This binary question
avoids splitting confidence between synonymous filler phrases. An independent
language decision is required for these fixed English recordings. Other
languages continue through the original model without fixed-language fillers.
Listening requires three stable words. Waiting accepts one-word input for
classification and can resolve a name against the server's preceding assistant
question. An explicit name introduction can also qualify. Names never enter
cached speech, and uncertain short replies still produce silence. Supplied
contact/payment details and consent remain excluded. A pending intake proposal
at the details stage blocks listening cues, while a neutral waiting hold can
remain eligible if no confirmation is active. A scoped nonempty pending set
whose rows are all queued/running can permit waiting only; unknown operations
remain blocked. The server independently verifies this proof before and after
selection. Only that accepted-work proof permits `Working on it.`.

Jev runs alongside the conversation. The primary reasoner never waits for it.
Selection is read-only and has no access to booking actions. Listening remains
strict about corrections; a fresh neutral waiting hold can follow a clear ordinary
factual correction without affirming it. Rejection of an agent action, sensitive
details, confirmation, urgency, prompt instructions,
unverified state, incompatible language and missed deadlines remain silent.
The server reads authoritative scheduler state before and after classification.
Routine vehicle symptoms, oil changes, vehicle model years and waiting-time
questions are not control commands. Narrow deterministic exceptions preserve
these facts while Jev still receives the original text.

One caller utterance has separate listening and waiting allowances: at most one
listening cue and two waiting cues. Listening has a 15-second cooldown and waiting
has a one-second cooldown across utterances,
with at most four reservations and 64 classification attempts in a rolling
15-second window, plus eight classification attempts per caller utterance.
Accepted reservations are not refunded after uncertain delivery or cancellation.
A waiting
cue starts no earlier than 550 ms after confirmed caller yield and at least
300 ms after the previous clip ends. After any active listening clip finishes,
selection starts in parallel with the remaining playback guard; an early result
retains its original expiry while it waits. Selection does not consume that
expiry waiting behind a full listening clip.
It is optional when the main reply is fast. Actual voiced main
audio revokes in-flight and held selection, so a late result cannot add speech
after the real answer begins. No additional selection is made simply because
an accepted result expired before playback.
Listening decisions use a complete-word transcript
prefix, leaving the current unstable word out. Compatible transcript extensions
preserve that pending decision; a rewrite inside its prefix cancels it. A
350 ms request cadence coalesces rapid updates instead of exhausting the request
budget. Raw sensitive changes cancel immediately, including in the omitted tail.
New caller onsets cancel queued or playing cues. Actual main audio cancels
future selections and held decisions; a started approved whole cue follows the
bounded completion rule below.
Normal transcript refinements and a caller yielding do not cut an
already-started acknowledgment. Selection expiry is a deadline to start, not a
timer that truncates a word. A separate duration guard bounds runaway playback.

Provider speech announcements can precede audible TTS by seconds and do not
cancel waiting work. Both the remaining server TTL at response construction and
an absolute 800 ms request-age limit bound decisions; network/classification
elapsed time is charged once. A new selection is permitted after an
authoritative hard-block-to-safe transition only when the previous attempt
failed context preflight without reserving a cue. Stable model uncertainty,
accepted or expired decisions and unknown operations cannot trigger retries.
The server may return a bounded `retry_after_ms` only for preclassification
request throttling or cue spacing. One fresh waiting retry can follow that hint,
within the same eight-attempt utterance budget and fresh authoritative gates.
No classifier failure, uncertainty or accepted reservation qualifies.
The first voiced main packet cancels future cue work. An approved cue already
playing can finish its complete bytes behind one fixed audio-clock barrier of
at most 1,100 ms; later main packets cannot extend that barrier. Main output can
therefore wait for the remaining complete phrase. Confirmed caller interruption,
mute or invalidated generation cancels immediately with a bounded 8 ms fade.
A late Jev result cannot resurrect a cue after main readiness. Output overflow
fails the isolated session explicitly instead of silently dropping valid PCM.

One optional long-wait follow-up requires a naturally completed first waiting
cue, fresh classification/context and a different phrase. Selection begins no
earlier than 600 ms after completion and the one-second phase cooldown; playback
cannot begin before 1,250 ms of post-cue silence. Its unchanged absolute 800 ms
request-age bound includes transport and holding. It cannot use preparation,
create a third waiting cue or retry uncertainty/expiry. Fast main readiness
cancels this future work.
Local microphone energy can provisionally pause owned main playback before the
provider's interruption event arrives. Pending audio and the already-scheduled
word suffix are preserved for up to 350 ms. A provider speech event or partial
transcript confirms the interruption and discards the old answer; unconfirmed
noise resumes the same answer and buffered audio. At most two speculative
pauses per answer and increasing false-onset cooldowns bound noise disruption.
Unconfirmed energy during a cached cue does not cut that clip or delay the next
main packet; confirmed provider speech or a transcript still cancels it.
Idle energy does not manufacture a caller turn. The energy detector is a
heuristic; echo, background noise and quiet speech still need representative
listening evaluation.

ASR final clauses accumulate within the caller's turn, with bounded text and
deduplication of cumulative refinements. Temporary stop/start events before any
main voice preserve that turn and its cue allowance unless a waiting cue has
already audibly started. Caller resumption after voiced waiting owns a new
epoch while retaining supplied local facts; padding-only playback does not
manufacture a new turn. Actual main voice, a
completed answer or a bounded abandoned-turn gap closes the retained context.

For browser-owned calls, the gateway recognizes the exact
`servicevoice_duplex_playback=browser-owned-v1` variable and suppresses its older
after-turn cue stream. It applies only to the explicitly isolated backchannel
assistant. Other assistants and calls keep the existing behavior.

## Run the private candidate

The existing candidate console must be reachable through its loopback SSH
forward at `http://127.0.0.1:8775`. Use the existing private credential file and
reviewed immutable local audio manifest; never paste credentials into a command.

```powershell
python scripts/serve_duplex.py --env-file PRIVATE_ENV_FILE --candidate-id ISOLATED_ASSISTANT_ID --audio-manifest PRIVATE_MANIFEST_PATH
```

Open `http://127.0.0.1:8785/duplex` and choose **Start conversation**. The page
requests microphone permission before allocating a paid call. The full call is
limited to five minutes. Stop closes local output/capture and requests provider
termination through its scoped control endpoint. A short owned WebSocket
termination is the fallback; without an acknowledgment, the termination outcome
remains unknown until provider readback. Allocation reservations survive
operator-process restarts in an ignored local
SQLite ledger. Unknown allocation outcomes cannot be retried with the same
session. The call allocator never edits an assistant or phone number.

## Evidence and acceptance

The results below are retained historical development evidence, including failed
continuity calls. The newly frozen eight-phrase/correction/follow-up candidate
has separate offline/model evidence in [the current experiment report](backchannel-experiment.md).
Do not infer that older live recordings validated the new full-clip handoff or
that new offline passes repair those recordings. New exact-commit end-to-end
captures, response-budget checks and acoustic review remain required.

`scripts/benchmark_duplex_cues.py` defaults to an offline plan. Live mode uses
synthetic partial text and saves content-free decision results under `.local`.
The first three-choice design selected no listening cues. Its retained rerun
with the binary decision selected seven of eight ordinary listening clauses,
four of eight waiting examples, and silence for all 34 sensitive examples.
Live speech recognition exposed a second gap: completed short clause fragments
were less useful than the benchmark's long descriptions. The current semantic
classifier recognizes ordinary vehicle ownership and usage background without
requiring a whole service request. With the same thresholds, its retained
segmented-clause comparison selected all 12 ordinary examples and stayed silent
on all 15 sensitive, unclear, unrelated or language-mismatched examples. A rescore
of the original cases selected all 16 ordinary examples and stayed silent on all
34 sensitive examples. These development samples are behavioral evidence, not a
production accuracy estimate.

Short waiting replies need a different content question from listening clauses.
The retained initial short-turn probe selected none of eight ordinary examples;
a comparison selected three of eight. The final waiting question uses disjoint
ordinary/excluded semantic labels, omits the candidate clip from model state,
and leaves the existing probability, margin and language gates unchanged. Its
retained 16-request comparison selected eight of ten ordinary examples, including
the exact short service question and name introduction used in the live test,
and stayed silent on all 21 excluded examples (some excluded before a provider
request). Two terse FAQ fragments remained below the content threshold. These
are development cases, not evidence of perfect short-reply coverage.

`scripts/duplex_browser_smoke.mjs --run` uses the real browser modules,
AudioContext and AudioWorklet with a local provider replay. It tests concurrent
microphone capture/cue playback, full clip geometry, main audio priority,
correction cancellation, local interruption and late work. The explicit
`--run --live` mode uses an isolated paid Vapi call and prerecorded synthetic
caller PCM. Replay success does not establish Jev quality or external service
compatibility; both live checks are needed.

An actual isolated WebSocket call subsequently passed the listening and
interruption gates: the cached cue overlapped voiced synthetic caller input,
34 microphone frames continued during that cue, and the main answer remained
audible. A second synthetic utterance interrupted the main reply. Local output
paused before the provider interruption event, the confirmed caller turn played
no old main audio, and the agent answered the new question. Both answer gaps
were recorded independently from the cue. These are browser graph and provider
integration results, with synthetic input; they do not establish room acoustics
or human preference.

The short-turn acceptance also covers both timing branches. A 2.20-second
service question received main audio 751 ms after synthetic input ended, so its
waiting selection was canceled and no cue played. The probe had explicitly
required a waiting cue, so that raw result remains failed under its original
expectation; a separate recorded-graph audit identifies it as a fast-answer
control rather than rewriting the receipt. A 1.92-second name introduction
received a complete cached `One moment.` starting 1.322 seconds after input
ended. Its main audio was ready at 2.781 seconds, after the 1.050-second cue had
finished, with no simultaneous assistant sources. The answer gap is still
measured separately from the hold. Advisory review of the name recording heard
clear, natural playback and complete endings; representative human microphone
and telephone listening remain separate acceptance checks.

The page exports content-free browser events and aggregate counters, including
microphone frames sent during cues and during main output. Cue timing and main
answer timing stay separate. Browser-origin events are labeled as such, rather
than being promoted to provider or human listening evidence. LangSmith's
existing main-call review remains a separate source of booking/generation facts.
The server also retains at most 256 content-free decision dispositions per
session, including phase, reason, probability, elapsed time, expiry and budgets.
These records contain no transcript, credential or provider URL, and are not
proof that a selected clip was heard.

A subsequent real microphone conversation exposed a coverage failure despite
the earlier synthetic results: one listening clip lasted about 151 ms before
interruption, and no waiting cue played across seven caller turns. Main answer
gaps after browser turn release ranged from about 1.56 to 3.91 seconds. Historical
generic suppression events cannot establish each exact classifier reason. The
recovery changes above address observed ASR context loss and noise behavior,
plus independently reproduced expiry, budget and announcement races. Their
32-request synthetic development probe selected 18 of 20 ordinary cases and
stayed silent for all 32 excluded cases, some rejected before a provider call.
One ordinary example timed out and one remained below the unchanged confidence
threshold. This does not establish reliable cue coverage in real conversations.

The first six-turn synthetic recovery call retained a failed coverage verdict:
three waiting and three listening clips played in full, but three ordinary
turns had no waiting clip despite 2.67â€“6.49-second source-end-to-main gaps.
The recorded PCM matched all six approved cached clips and showed zero
cue/main overlap. Exact new dispositions identified a canceled listening
request reaching backend pacing late, a waiting decision expiring behind a
listening clip, and a preclassification spacing refusal. These findings
motivated the bounded pacing recovery and delayed selection described above;
they are not regraded as a passing live call. No appointment or handoff was
saved in that synthetic conversation.

The WebSocket audio stream has no documented per-frame generation IDs. Local
playback epochs identify owned work; they do not prove the lineage or rejection
of every late provider/carrier packet. Missing speaker-state evidence and
unmatched output must remain visible. A recorded synthetic test does not prove
that an arbitrary microphone, room or phone sounds natural.

This implementation is a browser candidate. The main telephone route has not
been switched. An opt-in owned phone media bridge is now implemented under
`integrations/phone-bridge`, with private AudioSocket transport and exact owned
call admission/cleanup. It still needs live carrier, encryption, interruption
and telephone acoustic acceptance; the browser results do not provide that evidence. Promotion requires representative audio
comparison, not just a fast first sound or a green unit test.

### Partial waiting preparation

The next waiting candidate can be classified from stable partial speech without
playing audio or spending a cue reservation. Preparation remains bounded by the
same classification budget, with at most two preparations per caller utterance.
A prepared result is explicitly nonplaying. After the caller yields, activation
requires its matching receipt, exact text, prior assistant question and scheduler
context, fresh authoritative checks, and a preparation age below two seconds.
Changed content or context falls back to a new classification. Preparation does
not authorize booking or weaken the content and language thresholds. Main audio,
resumed caller speech, corrections and closure retain cancellation priority.

A later real microphone call ended normally with a confirmed appointment and
five successful booking-tool events. Its ten main-response gaps after browser
turn release averaged 2.95 seconds, with eight exceeding two seconds and a
maximum of 4.95 seconds. One listening and one waiting cue were recorded. The
waiting cue began 0.56 seconds after final browser yield; another 2.41 seconds
elapsed between its end event and main audio. These browser events establish
remaining coverage and continuity problems, not sample-level acoustic quality.
The classifier itself took 108â€“227 ms, while whole selection requests took
287â€“586 ms. Silence, uncertain content and language decisions caused most misses;
no main-arrival cancellation or cooldown rejection was recorded in that call.

A frozen 24-case synthetic comparison tested a narrow clarification that an
offered-time selection is a scheduling preference, while approval after a full
booking readback remains excluded. It selected 12 of 12 ordinary examples versus
10 of 12 for the baseline, with all 12 excluded examples remaining silent. A
broader prompt rewrite regressed to 8 of 12 and was rejected. This development
comparison does not establish production accuracy, naturalness or phone support.

Primary references: [Vapi WebSocket transport](https://docs.vapi.ai/calls/websocket-transport),
[Vapi API keys](https://docs.vapi.ai/security-and-privacy/api-keys),
[Vapi server authentication](https://docs.vapi.ai/server-url/server-authentication),
[TypeSafe choice](https://docs.typesafe.ai/primitives/choice),
[TypeSafe confidence](https://docs.typesafe.ai/confidence).
