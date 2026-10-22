# Observability and evaluation review, 2026-10-22

A forensic look at what the deployed ServiceVoice stack records, how it judges
calls, and how LangSmith presented the result before this change. Numbers come
from the production LangSmith workspace, the production reviewer's status file
and the reviewer's own outbox, read without modifying anything. Caller content
was never read.

## What existed

| Layer | Mechanism | State on 2026-10-22 |
|---|---|---|
| Runtime tracing | Content-free OTLP spans per gateway request and tool execution from the voice process | On in production, exporting into the same LangSmith project as the call reviews; 361 of the 1,000-span monthly budget used by day 22 |
| Local ledgers | Timing sink (per generation), diagnostics sidecar (component events), scheduler journal | Healthy; the diagnostics reader capped each call at 2,000 events |
| Automatic reviews | Post-call worker: Vapi record + scheduler snapshot + ledgers, six advisory Gemini dimensions, code checks, export to LangSmith with feedback and child runs | Healthy, 10 completed reviews, average lag 6 min, worst 36 min |
| Synthetic evaluations | Authored datasets and experiments in LangSmith (`evals/`) | Unchanged by this work |
| Guides | `docs/evaluation-walkthrough.html` written for review contract v3 | Stale |

## Findings

1. **A call review was a 1.5 MB evidence dump.** The latest real call's root run
   carried 1,509,505 bytes of outputs, 1,245,877 of them a `diagnostics.events`
   list of exactly 2,000 rows. 1,640 of those rows were one entry per Vapi
   webhook delivery (`webhook http_finished` and `call_bound`); the cap had
   been hit, so later business events could have been cut off on a longer
   call. Nothing in the payload said what happened on the call in words.
2. **The trace had 2,689 child runs for a 5-minute call.** 927 request groups,
   821 webhook rows, 401 provider point events (speech start/stop, pipeline
   cleared, turn started), 30 gateway generations, 30 provider model spans and
   only 11 turn groups, all with dotted technical names such as
   `voice.component.webhook.http_finished`. Expanding the trace in the UI was
   the main reason it read as noise.
3. **Feedback mixed scores and measurements.** 32 rows per call: 1/0 checks,
   blanks, and raw numbers such as `latency_turn_p95_ms = 10706` and
   `latency_server_follow_up_acknowledgments = 3` sat side by side under one
   "score" column, with keys that needed the source code to decode.
4. **`needs_review` was always on.** Every one of the 10 reviews was tagged
   `needs-review` because the two latency checks became `uncertain` whenever a
   waiting cue played (`acknowledgment_may_mask_substantive_answer_delay`).
   Since cues now play on most turns, the headline flag had stopped meaning
   anything.
5. **Runtime spans buried the reviews.** 241 of the latest 300 root runs in the
   review project were `gateway.request` or `tool.execution` spans with no
   inputs or outputs, only metadata; the call reviews were 8 of 300.
6. **The runs table could not tell calls apart.** Every review was named
   `voice.call.review` with inputs holding only the call id and a URL; the
   runtime spans were unlinked to reviews (a pseudonymous thread id).
7. **Reviewer status rows arrived every 15 minutes** (`voice.review.coverage`),
   up to 96 rows a day in the same project.
8. **Useful evidence was recorded but never shown:** Vapi's per-turn stage
   latencies, caller and assistant speech durations, speech-recognition
   confidence per turn, the number of caller interruptions, and the provider
   cost breakdown (about $0.27 for a 5-minute call).
9. **Review lag** averaged 381 s and peaked at 2,146 s, consistent with posting
   and reading back thousands of children under API rate limits.
10. **Two dashboards existed** ("live-call supervision" and "Gemini candidate
    supervision") with tiles that averaged feedback scores correctly but
    included the always-on `needs_review`, filtered on v4/v5 only, and had no
    dashboard-level explanation of the measurements.
11. The legacy baseline evaluator container (`servicevoice-call-evaluations`)
    had been unhealthy for two days; it reviews the retired assistant and no
    longer receives calls.
12. The guide described contract v3 filters and child names that no longer
    existed.

## What changed

- **Review contract v6** (`app/call_review_presentation.py`, `app/live_evaluations.py`):
  a readable layer in front of the evidence. Root outputs now begin with
  `0 Report (markdown)`, `1 Summary` (headline, lines, what to check),
  `2 Scorecard`, `3 Turn-by-turn timeline`, `4 Latency`, `5 Waiting cues`,
  `6 Booking and actions` and `7 How to read this review`; the technical
  sections follow. The run's Input column carries the headline; tags and
  metadata carry the outcome, end reason, bookings, duration and cost.
- **Trace children rebuilt around caller turns:** one `turn NN · …` row per
  caller turn with its `cue`, `gemini` (LLM run with token usage) and `tool`
  rows, a `speech synthesis` group and a `vapi` group. The same 5-minute call
  now exports roughly 110 children instead of 2,689. Joins use explicit
  identifiers only; provider timings attach by ordinal only when both sources
  count the same caller turns, and `join_basis` says which case applied.
- **Smaller, complete evidence:** webhook delivery rows are summarized as
  counts; every other event is kept. The ledger reader's cap rose from 2,000
  to 6,000 events and drops webhook rows first on overflow.
- **Speed is two honest checks:** `first_response_within_target` (what the
  caller heard first, Vapi's stage sum) and `answer_within_target` (server
  time to the real reply or tool call, p95, default 3.0 s). `needs_review`
  now means a conversation, booking or evidence problem; `slow_call` is a
  separate flag.
- **Feedback convention:** checks score 1/0, flags, `coverage_*`, `metric_*`
  measurements (call seconds, caller turns, bookings, cost, first response,
  answer p95, cue p95, cues played, follow-ups, Vapi stage averages, tool
  calls, tokens, caller interruptions, lowest ASR confidence).
- **Human calibration:** calls flagged `needs_review` are queued once in a
  LangSmith annotation queue with a five-question listening rubric; the
  worker's status reports queue health.
- **Earlier reviews stay frozen:** LangSmith accepts a run only when its start
  time lies within a day of the upload, so completed v4/v5 reviews are not
  republished; only a review still pending at upgrade time is published in the
  v6 layout with its frozen grades. The new project fills with new calls.
- **Project hygiene:** the reviewer exports to `servicevoice-call-reviews`;
  runtime spans go to `servicevoice-runtime`; status rows are tagged
  `reviewer-status` and posted on change or every six hours.
- **Tools and docs:** `evals/langsmith_dashboard.py` provisions the
  "ServiceVoice · call quality" dashboard and the queue and archives the old
  sections; `evals/call_report.py` renders a review as one HTML page; the guide
  was rewritten for v6.

## What stays unchanged

The six advisory judge dimensions and prompt, the code checks on saved records,
the privacy boundary (no transcripts, customer details, tool arguments or judge
prose leave the host), the synthetic datasets and experiments, and the rule
that uncertain never counts as a pass.

## Still open

- The legacy baseline evaluator container should be stopped by an operator; it
  is outside the guarded promotion lane.
- Runtime tracing shares the 1,000-span monthly budget; at the current rate it
  will stop exporting late in a busy month. Lower the sample rate or raise the
  cap when the runtime project matters.
- Human calibration starts at zero: the queue needs listening sessions before
  the automatic verdicts can be compared with human ones.
