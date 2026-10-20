# Separate Gemini speech candidate

This is a controlled speech comparison, pending listening acceptance. It must not
change the main phone number, accepted assistant, Soniox recognition, Gemini LLM,
prompt, tool schema, booking safeguards or confirmations. It starts a second
business container from the exact accepted image
`sha256:e5c6f12d0f1fa2f2be3bb146337c50c6b433c4ddade9816d7484a00817556b3d`.
Its scheduling data is isolated in a new volume. Only its separate test assistant
uses the Gemini speech sidecar.

## Prepare private settings

Use owner-only files outside the checkout. Copy
`speech-candidate-settings.env.example` into an explicit candidate settings file
and `speech-candidate-tts.env.example` into the speech-only private file. Enter
the reviewed speech image ID, accepted public address and independent speech
bearer secret. Never print resolved Compose configuration with real credentials.

Create the business private file by securely copying the accepted deployment's
runtime settings. Preserve its model, recognition, routing and confirmation
settings exactly. Change only candidate organization/number/assistant identities,
the independent candidate phone/gateway credentials, and candidate URLs. Do not
copy a real booking database. The sidecar receives only its two speech credentials;
it receives no Vapi administrative or business credentials.

The saved test assistant must be a separate copy of the accepted assistant.
Compare the copy against the original before use: voice changes to custom Gemini
speech; phone/webhook/custom-LLM addresses and their independent credentials change
only to reach this isolated candidate. Everything governing conversation and
bookings remains the same. The main number keeps its existing assistant.

| Purpose | Candidate URL |
| --- | --- |
| Phone webhook | `https://PUBLIC_HOST/speech-candidate/webhooks/vapi/inbound` |
| Custom LLM base | `https://PUBLIC_HOST/speech-candidate/llm` |
| Custom voice server | `https://PUBLIC_HOST/speech-candidate/tts/vapi` |

The voice server's saved Vapi bearer credential must match the TTS private file.
Vapi never receives the Google API key. The speech model defaults to
`gemini-3.8-flash-lite-tts`, with voice `Kore`; choose a different supported speech
candidate explicitly if needed. Do not silently fall back to another vendor.

## Start only the candidate

An operator first verifies that the exact accepted business image and reviewed
speech image exist locally, and the existing external network
`servicevoice-lightsail_application` is available. The manifest does not build or
pull a business image. It publishes no ports.

On the VM, using the actual outside-checkout settings file:

```bash
sudo docker compose --project-name servicevoice-speech-candidate --env-file /etc/servicevoice/speech-candidate-settings.env -f deploy/lightsail/compose.speech-candidate.yaml config --quiet
sudo docker compose --project-name servicevoice-speech-candidate --env-file /etc/servicevoice/speech-candidate-settings.env -f deploy/lightsail/compose.speech-candidate.yaml up -d --no-build
sudo docker compose --project-name servicevoice-speech-candidate --env-file /etc/servicevoice/speech-candidate-settings.env -f deploy/lightsail/compose.speech-candidate.yaml ps
```

Never run these commands against the accepted main project's Compose manifest.
This companion adds at most 384 MiB / 0.4 CPU for its business process and 256 MiB / 0.2 CPU
for speech. Limits do not reserve free memory: check actual VM headroom and OOM
status before testing. The account's existing call ceiling still applies across
both assistants; this is not a four-plus-four concurrency allowance.

Add the reviewed `speech-candidate.caddy` snippet **inside** the existing HTTPS
site before its catch-all handler. Validate the resulting Caddy configuration
before a reload. Keep the existing main handlers byte-for-byte unchanged. The
snippet forwards only the three exact candidate POST paths and strips their
prefix. GET requests, console routes, diagnostic databases and arbitrary prefixed
paths must remain inaccessible. Each upstream still authenticates its request.
There are no proxy retries or forced negative flush intervals.

Local `/healthz` on the speech sidecar proves local startup/prewarm, not working
Google credentials, audible playback or a completed phone call. Check private
business readiness separately. After adding the paths, test wrong/missing bearer
credentials before making a controlled call.

## Evidence, backup and cleanup

The business volume is
`servicevoice-speech-candidate_candidate-application-data`; the speech evidence
volume is `servicevoice-speech-candidate_candidate-speech-data`. The latter holds
`/data/tts-diagnostics.sqlite3`. Speech diagnostic events do not prove audio was
heard, and candidate speech evidence does not automatically appear in the main
call evaluation project. Link a separate candidate reviewer explicitly if needed.

Before replacing or cleaning up a candidate, create a new business backup with
the existing read-only backup tool and verify it:

```bash
sudo docker compose --project-name servicevoice-speech-candidate --env-file /etc/servicevoice/speech-candidate-settings.env -f deploy/lightsail/compose.speech-candidate.yaml exec -T servicevoice-candidate python scripts/database_backup.py backup --source /data/servicevoice.sqlite3 --destination /data/candidate-before-change.sqlite3 --timeout 30
sudo docker compose --project-name servicevoice-speech-candidate --env-file /etc/servicevoice/speech-candidate-settings.env -f deploy/lightsail/compose.speech-candidate.yaml exec -T servicevoice-candidate python scripts/database_backup.py verify --source /data/candidate-before-change.sqlite3 --timeout 30
```

Use a fresh destination name each time. This creates a SQLite snapshot; it does
not back up the speech evidence. Copy verified backup and evidence snapshots
privately off the VM if they need to survive VM loss. Never copy a live SQLite
file without its consistency mechanism.

To stop, use this candidate project's `stop` command. Preserve volumes until the
comparison and evidence have been reviewed. Removing candidate routing or stopping
these containers is independent of the main phone service. Do not delete or prune
any main deployment volume or network.

## Listening acceptance before any promotion

Compare the candidate against the accepted assistant with the same controlled
script: greeting, factual question, booking proposal, corrected date/vehicle,
explicit confirmation, cancellation proposal, interruptions and number/reference
readbacks. Check pauses, clipping, repeated words, multilingual output and hangup
cleanup. Independently compare the saved receipt, approved text and actual heard
audio. Automated PCM and latency checks do not replace this listening test.

Promotion to the main number is a later, explicit step after candidate acceptance.
No command in this document changes the main number's assistant.
