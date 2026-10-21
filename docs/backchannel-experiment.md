# Jev acknowledgment experiment

This experiment reduces the silence before an answer by selecting one short,
approved acknowledgment while the existing reasoner continues. It does not make
a four-second substantive answer instantaneous. Measure the first acknowledgment
and the first useful answer separately, and reject a candidate that makes the
useful answer slower or degrades corrections and explicit confirmation.

The accepted assistant and its phone number stay on their existing configuration.
The experiment uses a separate assistant, gateway, speech endpoint and booking
state. Its feature flag belongs in the isolated backend's environment, not in the
reasoning prompt. A branch or passing text test is not evidence that a caller
heard the right audio.

## Why one coordinated voice path

[Vapi's live control API](https://docs.vapi.ai/calls/call-features) can play a short
WAV or MP3 from a public HTTPS URL. It caches files by URL, downloads the entire
file before playback, and does not add the file's words to conversation artifacts.
A successful control response does not establish successful download or playback.
Its normal `say` behavior can queue behind current speech; enabling assistant
interruption can instead discard an answer. The documented controls do not offer
a selective generation-ID cancellation contract for queued filler files.

The candidate therefore keeps acknowledgments in the existing assistant's output
stream. A coordinator may emit one allowlisted phrase, followed by `<flush />`,
before the validated answer or tool command. The independent classifier cannot
write arbitrary speech or modify the booking. All model-generated prose keeps
the existing validation and pre-tool speech gate.

[Vapi's flush documentation](https://docs.vapi.ai/assistants/flush-syntax) requires
`voice.chunkPlan.enabled: true`. With chunking disabled, flush tags can reach
speech output. Flush should occur at a complete phrase boundary and should be
used sparingly; excessive use can fragment speech. Exact-match cached PCM at the
custom voice endpoint avoids a live synthesis request for approved phrases.

Do not add the obsolete `fillerInjectionEnabled` setting: Vapi's
[November 24 change notice](https://docs.vapi.ai/whats-new/2024/11/24) removed it.
`stopSpeakingPlan.acknowledgementPhrases` concerns caller acknowledgments that
should not interrupt the assistant; it is not the assistant's filler dictionary.

## Jev's bounded responsibility

[TypeSafe's quickstart](https://docs.typesafe.ai/introduction/quickstart) describes
typed decisions. Jev chooses a label and confidence; it does not generate a name
or arbitrary sentence. The application maps an accepted label to approved text.
Low confidence, classifier failure, unsupported context or a missed deadline
means silence. The classifier runs alongside reasoning, never in front of it.

The initial candidate should use short neutral acknowledgments. Phrases that
claim a booking, a successful update, an advisor contact or a completed check are
excluded. "Checking availability" is appropriate only when an authoritative
availability read is actually underway, not merely because a classifier guesses
that one might be needed. Repeated "um" and "uh" are not a latency strategy.

Names remain in the existing verified conversation and booking readbacks.
Personalizing an acknowledgment needs a verified name plus synthesis of the
complete phrase. Splicing a prerecorded name onto another recording can create
awkward prosody and costs additional latency; it is excluded from the first
cached-audio experiment.

The coordinator must suppress an acknowledgment when an answer wins the race,
the caller resumes, a generation becomes obsolete, the call ends, a correction or
confirmation is being handled, a tool continuation is pending, or the cooldown
has not elapsed. At most one acknowledgment is allowed for an eligible turn.
An unverified or unsupported context should remain silent.

## Configuration and rollback review

`scripts/prepare_backchannel_assistant.py` is a pure offline preparation helper.
It accepts a private saved assistant snapshot and candidate endpoint URLs. It
does not read environment keys, make requests, create calls or change number
mapping. The default output directory is ignored by Git.

```powershell
python scripts/prepare_backchannel_assistant.py --source PRIVATE_ASSISTANT_SNAPSHOT.json --gateway-url https://candidate.example/ack/llm --tts-url https://candidate.example/ack/tts/vapi --webhook-url https://candidate.example/ack/webhooks/vapi/inbound
```

The three resulting files are the original rollback snapshot, a candidate create
plan and a content-free change audit. Do not commit private snapshots or plans.
The create DTO uses the verified official
[Vapi SDK schema snapshot](https://github.com/VapiAI/server-sdk-typescript/blob/ce0b348412130447f18e77363de7a97ca12f244b/src/api/types/CreateAssistantDto.ts).
Unknown fields require review instead of being silently dropped. Inline secrets
are rejected; existing saved credential references remain unchanged.

Only the candidate name, three endpoint URLs, chunking enable flag and additive
experiment metadata tag may differ. The reasoner, prompt, Soniox configuration,
tools, credential references, interruption settings, recording settings and
native end-call safeguards are preserved exactly. A separately authenticated
candidate deployment may need distinct credential references; that extra change
requires its own explicit audit before remote creation.

After creating a separate assistant, fetch it and run `verify_saved` against the
plan. Additional provider defaults or any changed protected field require review.
Rollback means retaining the accepted assistant and unchanged number route;
reverting only the prompt is insufficient if endpoints or audio change.

## Evidence and acceptance gates

For each attempted acknowledgment, record the decision outcome and confidence,
phrase ID, generation ID, decision time, suppression reason, first emitted text,
cache hit/miss, audio byte count and declared sample rate. Track candidate version
and voice/model hashes. Retain a distinction between selection, backend bytes,
provider speech events and actual heard speech.

[Vapi's server event reference](https://docs.vapi.ai/server-url/events) documents
`speech-update`, `assistant.speechStarted` and `user-interrupted`. Raw PCM custom
voices provide text-only playback events rather than word-level alignment.
Optional provider `turnId` values can be joined only where an explicit shared ID
exists; matching timestamps are not proof of identity or late-packet discard.

Automated checks must include silence/timeout fallback, fast-answer races,
classifier cancellation, stale generations, correction and confirmation turns,
interleaved callers, exact cached text, corrupt/missing cache entries, full audio
durations and correct sample rates. A response labeled 16 kHz must contain audio
converted to 16 kHz; relabeling 24 kHz audio reproduces slow playback.

Listening checks must assess natural pace, sentence endings, interruption,
acknowledgment frequency, duplicate speech, name/reference pronunciation,
corrected complete readbacks, explicit confirmation and the full farewell before
hang-up. Compare baseline and candidate using representative calls. Promotion is
an independent decision after those results, not an automatic consequence of
lower time to first sound.

## Current branch behavior and measured sample

The implemented candidate waits 550 ms before considering a cue. Its Jev request
has a 350 ms deadline and runs alongside the unchanged primary request. Completed
primary output wins the race. Only the exact phrase `One moment.` can currently
be selected; the other catalog clips are preview assets. There is a 15-second
cooldown and a maximum of four cues per call. Classification errors, uncertainty,
unverified cached-language compatibility and sensitive turns remain silent.

The live synthetic benchmark in `docs/backchannel-policy-benchmark.json` used
16 cases and seven provider requests with zero retries. It selected four of six
ordinary requests and kept all ten sensitive cases silent. The median request
elapsed time was 157.619 ms, with a maximum of 241.301 ms. This is a small
classification sample, not a calibrated accuracy claim or end-to-end voice
latency measurement. Earlier overlapping phrase labels split probability among
synonyms; a binary acknowledge/silence decision avoids that unnecessary choice.

The main reasoning prompt has no new filler or language rules. Cached clip
selection has its own compatibility gate because a fixed audio recording cannot
change language. Dynamic names and claims that an action has started are excluded.

`SERVICEVOICE_BACKCHANNELS` defaults off. The gateway additionally requires the
exact isolated `VAPI_BACKCHANNEL_ASSISTANT_ID` and a validated, warmed immutable
`SERVICEVOICE_BACKCHANNEL_AUDIO_MANIFEST`. The custom speech service uses the same
model, voice and manifest. It returns a cache hit only for exact text, voice,
model and sample geometry; normal sentences retain the existing synthesis path.
Vapi may append ASCII edge whitespace to a flushed phrase. The lookup removes
only outer ASCII spaces, tabs and line endings, and records
`cache_text_normalized`; it does not change case, punctuation, internal spacing,
Unicode characters or additional words. Input hashes still describe the actual
provider request, while asset hashes describe the immutable approved recording.
Keep generated audio, provider snapshots and runtime settings outside Git.

## Reading the candidate's evaluation

The candidate's reviewer uses a separate LangSmith project and assistant
allowlist. Inspect `voice.gateway.generation` children for the optional
`backchannel` object: decision and suppression reason, classifier timing and
requests, confidence, cached-language evidence and server emission time.
This typed projection survives database persistence and repeated export without
adding caller words or credentials. Older generations without this evidence
remain absent rather than being assigned an inferred silent decision.

`latency_server_first_ack_p95_ms` measures gateway cue emission.
`latency_server_substantive_text_p95_ms` measures validated useful text readiness;
the original per-generation `milestones_ms.first_text` keeps this meaning.
Neither measures heard sound. A `tts.cache_hit` event and PCM hashes establish
returned bytes, not playback. When any explicit call-scoped cue emission is
observed, general Vapi response-latency checks become uncertain because the
provider number may include the acknowledgment. A missing timing row cannot
erase a separately captured component event or turn a filler into an answer.

The candidate is not promoted automatically. Compare actual listening and useful
answer latency with the accepted assistant, including whether a queued short
clip makes an otherwise-ready answer arrive later. Keep provider playback,
audible interruption and late carrier packet discard as unverified until an
appropriate audio test establishes them.

## Full-duplex boundary

The current candidate is an after-turn acknowledgment experiment. Vapi continues
listening during speech and supports barge-in, but this selector runs when Vapi
requests the next model response. It does not yet select or play listening cues
while the caller is still talking.

[Partial transcript and speech events](https://docs.vapi.ai/server-url/events)
can support speculative Jev decisions during caller speech. Such a selector
should choose a fixed phrase ID or silence from the current context, rather than
generate or guess arbitrary next words. Every partial revision needs an epoch;
late decisions must become obsolete, and partial text must never authorize a
booking or confirmation.

Audible overlap needs stronger coordination. Vapi's documented live `say`
controls lack cue-specific cancellation, expiry and an atomic speaker-state
check. Disabling interruption can ignore caller speech; enabling assistant
interruption can discard the main response. Neither global mute nor clearing a
local queue proves that an accepted remote cue is cancelled.

An owned browser playback coordinator around
[Vapi WebSocket transport](https://docs.vapi.ai/calls/websocket-transport) is a
possible next prototype: keep forwarding caller PCM to Soniox, receive the
existing main PCM, and schedule cached listening cues with cancellable local
epochs. This changes the transport/playback path and needs its own audio,
interruption and timing evaluation. Phone support would additionally need a
media bridge. It is not implemented or approved by the current candidate's tests.
Vapi's [GPT-Live](https://docs.vapi.ai/gpt-live/overview) takes a different path
that replaces separate transcription/speech and does not retain this exact
custom-audio stack, so it is not used here.
