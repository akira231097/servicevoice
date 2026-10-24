# Third-review reliability repairs

Scope: the isolated Twilio conference lane, following the review of `0559ce3`.
The main Vapi assistant is outside this deployment. Main model, booking policy,
waiting quiet threshold, tool-follow-up allowance and voice bank are unchanged.

## Opening media health

Automatic reconnect is disabled, rather than claiming that energy without ASR
proves a media failure. The former destructive reconnect implementation has been
removed from the opening watchdog. A suspicion generates one diagnostic per
bridge, with no announcement, detach, redial or hang-up. Caller/agent events and
recognized-word evidence continue to be collected.

`SERVICEVOICE_TWILIO_OPENING_RECOVERY=off` is explicit in the isolated deployment.
Setting it to `on` is rejected at startup until a safe replacement is implemented.
Readiness reports `automatic_opening_recovery: false` and
`media_health_mode: observe_only`. Old recovery announcement URLs return silence.
This avoids misfires but does **not** automatically repair a genuinely silent call.

## Ready-answer handoff

The gateway no longer sleeps behind the estimated end of a fetched listening
clip before releasing answer text for speech generation. It atomically marks the
answer ready and invalidates unfetched cue tickets. The playback estimate remains
available to the separate waiting-cue gate.

This removes the review's 0.93/1.32/1.76-second artificial holds. It does not make
Twilio REST announcements cancellable after fetching: a short clip already in
flight can still finish. `answer_released / fetched_clip_active` explicitly records
that case. It is not evidence that acoustic overlap was prevented. Strict audio
preemption still requires a controllable streaming channel; a successful ordinary
test call cannot prove all fast-answer races safe.

## Cleanup, delivery and naturalness

- Per-bridge cleanup retries wait 2, 4, 8, 16, then at most 30 seconds, resetting
  after confirmed cleanup. A failed bridge does not skip cleanup of other bridges.
  A failure reading the bridge list also increases the sweeper delay up to 30 seconds.
- Twilio HTTP keepalive expiry is 30 seconds rather than the client's five-second
  default. This permits reuse between cues; it is not a guarantee the remote end
  keeps the connection open.
- After a cue, short pauses become eligible at a sampled 4–5.5 seconds. Continuous
  speech waits another 1.5 seconds to prefer a pause. Safety, freshness, classifier
  and per-call budgets still apply; this is an opportunity window, not a promised
  playback cadence. The first explanation warm-up remains three seconds.
- Actual fetched pause/request audio alternates Mm-hmm and the two Uh-huh takes.
  Rotation follows the last fetched clip, including substitutions, rather than
  the originally requested phrase. Fetching is not proof of handset audibility.
- Authenticated silent fetches retain a closed-set reason (expired ticket, missing
  ticket, speaker priority, unavailable bridge, internal error, etc.). Invalid
  signatures still get silent media but cannot pollute the authenticated metrics.
- A process-local rolling window of 100 authenticated fetches raises a failed
  diagnostic on entering an elevated refusal rate: at least 10 observations,
  at least two refusals, and more than 5%. Readiness exposes the window, reason
  totals and alert state. Counters reset on restart; persisted diagnostics remain.
  This is an operational diagnostic, not an external notification subscription.

## Verification contract

`tests/test_twilio_third_review.py` reproduces uncertain/noisy openings with a
failing reconnect provider, all three reported answer holds, cleanup backoff,
pause preference, actual signed clip fetch rotation, refusal reasons/alerting,
and the disabled recovery configuration. Existing tests retain refusal-as-silent
200 behavior, the separately spaced tool follow-up and production admission scope.

Every live capture must still pass the existing hard checks: all Twilio call-leg
notifications clear and no unexplained caller-side speech-like audio outside
approved clips and Vapi speaking intervals. The separate LangSmith project remains
`servicevoice-twilio-duplex`. Handset and speakerphone checks remain necessary.

Model/prompt speed work is intentionally a separate next step. These repairs do
not promise pause-free speech or reduce the model's measured generation time.
