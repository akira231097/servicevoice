# ServiceVoice

A voice assistant for dealership service scheduling, built as an independent AI engineering interview project. The dealership, customers and reference evaluation cases are fictional. This project is not affiliated with BLiNK AI.

The problem is completing the caller's task accurately: find a valid appointment, capture corrections, read back the final details and save it only after confirmation. ServiceVoice separates conversational reasoning from the code that owns availability and appointment changes.

## Architecture

```mermaid
flowchart LR
    Caller[Phone or browser caller] <--> Vapi[Vapi: speech and turn taking]
    Vapi <--> Edge[Authenticated HTTPS routes]
    Edge <--> Gateway[Streaming Gemini gateway]
    Gateway <--> Model[Gemini 3.8 Flash]
    Gateway --> FAQ[Optional TypeSafe Jev static FAQ route]
    Edge --> Harness[Bounded tool harness]
    Harness --> Scheduler[Validated scheduling tools]
    Scheduler --> DB[(SQLite)]
    Scheduler --> Advisor[Local advisor request queue]
    Gateway -. content-free timings .-> Smith[LangSmith]
```

The v18 configuration uses Gemini `gemini-3.8-flash` with low reasoning, Soniox automatic speech recognition and ElevenLabs speech synthesis through Vapi. Provider access and model availability depend on your account. The application supports optional guarded narration from verified receipts and a narrow TypeSafe Jev FAQ classification route. Neither optimization can confirm a booking or invent availability.

## What is implemented

- Availability, proposals, booking, appointment lookup, rescheduling and cancellation in a local scheduler.
- Fresh, session-scoped proposals; readback and a later confirmation step before committing an appointment.
- Idempotent mutations, atomic capacity checks, issued-tool validation and verified result history.
- Bounded tool execution, pending/unknown operation states and recovery without blindly replaying uncertain work.
- Separate private operator console and authenticated public provider endpoints.
- Phone admission limits, an advisor request inbox and optional local MCP advisor integration.
- Content-free runtime tracing, synthetic reference datasets, native LangSmith experiments and hallucination checks.
- Docker and single-instance Lightsail deployment packages, backup/restore tools and fault/load tests.

An advisor request is a saved local task. It does not send a callback or transfer a live call. There is no real dealership CRM/DMS integration. Generated speech can still be wrong; these safeguards and evaluations reduce risk without guaranteeing zero hallucinations. The scheduler remains the authority for committed appointments.

## Run the local rehearsal

Use Python 3.12 and Node.js 22 or newer for frontend tests. No API key is needed for the scripted rehearsal.

On Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-lock.txt
python scripts/install_sqlite_runtime.py
Copy-Item .env.example .env
python run.py
```

Open http://127.0.0.1:8765 and choose the scripted rehearsal. Keep the console private. The second listener, port 8766, exposes only authenticated voice-provider routes. `.env` uses `.local/` for this installation's database and tracing budget; neither is committed.

The Windows installer verifies a pinned SQLite archive and installs a project-private DLL plus a virtual-environment startup hook. It does not change system Python. Other operating systems need SQLite 3.51.3 or newer; the [Docker package](deploy/README.md) builds a pinned compatible runtime.

## Enable live voice

Add credentials to your ignored `.env`, following the [Vapi setup guide](integrations/vapi-setup.md). Live voice requires a Vapi account, accessible Gemini and speech-provider integrations, separate webhook/gateway secrets and a reachable HTTPS provider endpoint. TypeSafe and LangSmith are optional. Never put private keys in browser JavaScript or commit working credentials.

The [Lightsail guide](deploy/lightsail/README.md) describes a phone-first single-VM deployment. The default capacity profile admits at most four combined live calls. That limit must fit your Vapi/provider quotas; it is not evidence of four simultaneous phone calls meeting an audio latency target. A phone number is provisioned separately in Vapi or through a supported carrier integration.

## Test and evaluate

Install optional MCP test dependencies for the complete Python suite:

```powershell
python -m pip install -r requirements-mcp-lock.txt
npm ci --ignore-scripts
python -m evals.offline
```

The offline runner executes the Python suite and Node tests against disposable fixtures, checks source stability, and saves local logs and JUnit output under ignored `docs/evals/runs/`. It does not start paid model or phone calls.

GitHub Actions partitions every Python test file into four disjoint shards and runs all Node tests separately. Checkout preserves the exact source bytes used by fixed-hash audio fixtures. The caller stress runner records initial terminal-event responses and permits at most three attempts for identical, idempotent hang-up events after a busy/deadline response; it never retries booking tools through that helper or changes the production deadline.

The Linux-specific deployment package tests run in a separate GitHub Actions job. On Linux, run `python -m pytest -q deploy/test_server.py deploy/langsmith/test_templates.py deploy/lightsail/test_package.py integrations/telephony-lab/test_verify_pcap.py`.

[Evaluation instructions and v17/v18 results](docs/evaluations.md) cover reference datasets, paired experiments, judge calibration and the remaining known issue. Model executions, LangSmith uploads and synthetic voice calls are separate opt-in operations and can incur provider charges. Human microphone, interruption, multilingual listening and carrier checks remain necessary.

## Repository layout

| Path | Purpose |
|---|---|
| `app/` | API, domain model, scheduling tools, gateway, harness, observability and browser UI |
| `evals/` | Reference datasets, contract checks, model runs, judges and native LangSmith tooling |
| `tests/` | Offline Python and Node regressions |
| `scripts/` | Provisioning, benchmarks, backups and explicit live checks |
| `deploy/` | Docker, Lightsail and observability configuration templates |
| `integrations/` | Vapi setup, optional advisor MCP and isolated telephony lab |

This public repository contains a clean source snapshot. Credentials, customer/caller records, audio, private experiment URLs, deployment archives and local operational history are excluded. Third-party browser dependencies retain their own licenses in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
