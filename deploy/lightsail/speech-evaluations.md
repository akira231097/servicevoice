# Candidate call review companion

This package reviews ended calls from the isolated Gemini speech candidate. It
does not change the phone number, assistant, voice settings, conversation owner,
booking tools, scheduler data, or original evaluator. The JSON file is a Compose
document, so it can be parsed and checked using Python's standard library.

Build and inspect `Dockerfile.speech-evaluator` separately using a reviewed local
tag for the accepted business image. The build argument is required; a bare
`sha256:...` image ID is not a valid BuildKit `FROM` reference. Verify that the
local tag resolves to the accepted ID immediately before building, with pulling
disabled:

```sh
SERVICEVOICE_REVIEW_BASE=servicevoice-accepted:speech-review-base
test "$(docker image inspect --format '{{.Id}}' "$SERVICEVOICE_REVIEW_BASE")" = 'sha256:e5c6f12d0f1fa2f2be3bb146337c50c6b433c4ddade9816d7484a00817556b3d' || exit 1
docker build --pull=false --build-arg SERVICEVOICE_BASE_IMAGE="$SERVICEVOICE_REVIEW_BASE" -f deploy/lightsail/Dockerfile.speech-evaluator -t servicevoice-speech-evaluator:reviewed .
```

Create that local tag from the accepted local image only if it is absent; inspect
it again rather than assuming an existing tag still points to the accepted ID.
Record the resulting evaluator image ID and set
`SERVICEVOICE_SPEECH_EVAL_IMAGE` to it. The Compose file never builds or pulls an
image implicitly. Before starting it, run the image's import smoke check:

```sh
docker run --rm --entrypoint python "$SERVICEVOICE_SPEECH_EVAL_IMAGE" -c 'from app.live_evaluations import Config, Worker; from app.evaluation_spans import build_component_runs; from app.diagnostics import read_call_diagnostics, read_review_diagnostics; from app.generation_observability import project_gateway_timings, build_gateway_generation_runs; print("review imports ready")'
```

Create a separate, mode-0600 environment file outside the checkout. Set
`SERVICEVOICE_SPEECH_EVAL_ENV_FILE` to its path. It needs:

```dotenv
VAPI_EVALUATION_API_KEY=<the replacement private Vapi key, not the old evaluator key>
GEMINI_API_KEY=<existing Google API key>
LANGSMITH_API_KEY=<existing LangSmith key>
LANGSMITH_WORKSPACE_ID=<workspace UUID>
VAPI_PHONE_ORG_ID=<Vapi organization UUID>
```

Optional provider-log capture uses `SERVICEVOICE_EVAL_PROVIDER_LOG_HOSTS`, a
comma-separated allowlist of at most two exact validated Vapi R2 storage hosts.
Approve the hosts independently; do not derive trust from an incoming artifact
URL or use a wildcard. Leave it empty to disable this source explicitly.
This setting requires the scoped Vapi organization and does not require another
credential. Storage requests use a separate unauthenticated client, no redirects
and no environment proxy. Signed storage URLs and raw log content are never
published to LangSmith.

Do not copy the old evaluator environment wholesale. The candidate assistant,
project, evidence paths and 200-attempt monthly budget are fixed in the Compose
document. The 24-hour initial capture window is persisted in its own outbox; it
is not a rolling promise to recover all historical calls. A new start must be
within that window to discover the listening test automatically.

Validate and start only this companion:

```sh
docker compose -f deploy/lightsail/compose.speech-evaluations.json config --quiet
docker compose -f deploy/lightsail/compose.speech-evaluations.json up -d --no-build --pull never
```

It mounts the existing candidate business and speech volumes read-only, with a
separate writable review outbox. It publishes no ports. These external volume
names must exist; Compose must fail rather than silently create empty evidence:

- `servicevoice-speech-candidate_candidate-application-data`
- `servicevoice-speech-candidate_candidate-speech-data`

The reviewer always reads the business diagnostic ledger derived from
`SERVICEVOICE_EVAL_SOURCE_DB`: `/evidence/servicevoice-diagnostics.sqlite3` in
this package. `SERVICEVOICE_EVAL_DIAGNOSTICS_PATH` adds the distinct speech
ledger, `/speech-evidence/tts-diagnostics.sqlite3`; it does not replace the
business ledger. Both are queried for the same call through the existing
read-only mounts. If both paths resolve to the same ledger, read it once.
Preserve both sets of configuration identities and source-level coverage in
the union. Missing or degraded speech evidence must not erase useful business
events or turn an incomplete report into a complete one, and vice versa.
The exported `diagnostics.sources` records those independent states and
configuration IDs. The primary `diagnostics.configuration` remains business-only,
including when only speech events are available. The coverage revision is
`call-diagnostic-sources-v1`; completed older reviews retain their frozen payloads.

The current worker contract is `live-call-review-v5`. It retains v4's six semantic
dimensions, conservative reference-text uncertainty and separate acoustic and
termination coverage. Completed v4 reviews remain valid and are not automatically
requeued: the native platform capability was absent from their calls. Completed
v1–v3 reviews are queued for fresh assessment using their existing paid-attempt
counters and budget. Exhausted attempts produce unknown semantics, not invented
passes. Finish any pending v4 export before upgrading, since a pending older
contract is not relabeled as v5.

v5 narrowly recognizes a single empty-argument `endCall` whose exact fingerprint
and current caller anchor match a stored issuance scoped to this call and session.
It records a **platform attempt with unknown execution outcome**, not a scheduler
receipt. Unsigned, forged, repeated, mixed-batch or nonempty-argument attempts
remain incomplete evidence. Provider termination is coverage only and does not
prove caller intent or farewell playback. Native action evidence is not added to
the fourteen business tools or their confirmation rules.

Future gateway generation children belong under the same call-review root and
use explicit persisted generation identities and measured start/duration only.
Legacy request IDs must not be promoted into generation IDs. An older timing
row without the new stamp remains unavailable lineage coverage. Gateway events
are not joined to vendor audio by timing, text, matching counts or adjacency.
Copy all eight source modules listed by the separate evaluator Dockerfile.
The evidence reader now imports `normalize_services` from `app/domain.py` to
check every service in a combined visit; copying only the six review/projection
modules leaves that import unavailable in an older base image. Diagnostics also
depends on the observability generation-field allowlist, even though this worker
does not start a voice tracer or call controller. Provider capture adds
`app/provider_logs.py`; omitting it prevents the current worker from importing.

After starting, verify the resolved image ID, both read-only mount flags, worker
health, call ID, review version, six score rows, latency metrics and LangSmith
readback. Verify that automatic call reviews contain the available business and
speech observations together, with honest source-level coverage. A separately
published synthetic business trace does not establish that the automatic
worker read both ledgers. A healthy worker alone does not establish that a particular call was
reviewed. Judge requests are bounded to three attempts per call and 200 model
attempts per month; upload retries reuse the frozen review. Stop this companion
to roll back review processing; the voice and booking services remain running.

## Evidence limits that must remain visible

The original accepted business image predates the new component diagnostics.
The isolated candidate can add the passive overlay described in
[`component-observability.md`](../../docs/component-observability.md); the
reviewer image alone does not add observations to the voice process. Calls made
before that overlay keep their original missing business-component coverage.
The separate speech ledger can export a component event only when it contains
the exact call ID and valid recorded timing. A custom TTS request without that
identity cannot be attached to a call using text, approximate timestamps or
guesswork. The worker must show missing coverage rather than invent a complete
trace.

For new unfrozen reviews, the bounded provider reader can add explicit
provider turn/model/speech observations. Its status, fixed unavailable reason,
source hash, partial coverage and source-row counts appear in
`provider_observations`; child export counts appear in
`provider_observation_export`. `voice.provider.*` event spans are timestamp
instants, while model spans require recorded start/end boundaries and a shared
explicit provider span ID. They are not gateway/TTS joins or acoustic proof.
The reader has a 15-second deadline and fixed compressed/expanded/line/row
limits. Export caps are explicit. Frozen pending exports retain their original
payload, and completed historical reviews are not requeued for this addition.
Readback checks include the recorded IDs, values, times, metadata and tag set;
successful ingestion alone is not the verification result.

The six dimensions review text and authoritative scheduler receipts. They do
not certify what a person heard, reference pronunciation, barge-in, natural
speed, or automatic hangup. The candidate's first listening call did not test
vehicle correction, a declined proposal or reference repetition. Keep those
acceptance checks pending even if the text judge returns high scores. A
customer-ended call after goodbye establishes caller hangup, not automatic
termination.

The current literal reference check recognizes complete `CSV-XXXXXXXX` tokens.
It does not reliably recognize spaced, spoken letters or missing leading zeros.
Retain the transcript discrepancy as a manual-review flag; do not relabel it an
acoustically proven failure. Multi-service intent needs a separate task-coverage
check: one correct saved diagnostic is not evidence that a requested brake
inspection was also booked. A truthful agreed handoff may satisfy that remaining
request, but the actual handoff receipt and reason must be inspected.
