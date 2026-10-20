# Run ServiceVoice locally in Docker

Start the local sandbox at **http://127.0.0.1:8875** using the commands below. It needs no API keys and uses its own SQLite data volume. A separately started host demo uses port 8765 and a different database. Sessions and appointments do not move between them.

This package is for one process and one local data volume. It includes the application, local advisor inbox, provider-only listener, a patched SQLite runtime, and the backup/restore CLI. No AWS resources, carrier calls, CRM or DMS integration are created.

## Start, check and stop

From the `servicevoice` project directory in PowerShell:

```powershell
docker desktop start --timeout 120
docker compose -f deploy/compose.local.yaml up --build -d
docker compose -f deploy/compose.local.yaml ps
```

Open these local addresses:

| Address | Purpose |
| --- | --- |
| http://127.0.0.1:8875 | Rehearsal UI, labeled “Local Docker sandbox” |
| http://127.0.0.1:8875/advisor | Persistent local advisor inbox |
| http://127.0.0.1:8876/healthz | Process liveness: `status: ok` |
| http://127.0.0.1:8876/readyz | Local database/configuration readiness: `status: ready` |

Readiness does not validate provider credentials, a public tunnel, external availability or successful phone calls. Docker's health check probes liveness; inspect `/readyz` separately when configuring provider mode.

Stop while preserving all sandbox data:

```powershell
docker compose -f deploy/compose.local.yaml stop
```

Resume with `docker compose -f deploy/compose.local.yaml up -d`. To remove the stopped container/network while keeping the named volume, use `docker compose -f deploy/compose.local.yaml down` without `--volumes`. The volume is `servicevoice-local_sandbox-data`. Deleting that volume would delete its appointments and advisor history.

## Try the complete local workflow

1. Open port 8875 and leave **Rehearsal** selected. Enter `oil change`, `next available`, then `1`.
2. Enter `Alex Morgan, 5125550142, 2021 Toyota Camry` when asked for details.
3. Read the proposal. Enter `confirm` to save the fictional appointment, or `no` to decline. A proposal alone is not a booking.
4. Enter `advisor: please review my appointment`. Open **Local advisor inbox**, select the new case, enter a local operator label, claim it, and resolve it with a note.
5. Refresh or stop/start the container. The case, review audit and appointment remain in the sandbox volume. The browser's current conversation capability is memory-only; after a page refresh use a new session and the persistent advisor inbox to review saved work.

Operator labels are unverified local labels, not user accounts. Resolving a case records a review; it does not contact a person, arrange a callback or send data to a CRM. The inbox is accessible to processes/users on this computer. The keyless sandbox's live voice mode remains unavailable until a separate provider setup is deliberately configured.

## Backup and restore

The image includes `scripts/database_backup.py`. Choose a new destination name each time:

```powershell
docker compose -f deploy/compose.local.yaml exec servicevoice python scripts/database_backup.py backup --source /data/servicevoice.sqlite3 --destination /data/interview-backup-01.sqlite3 --timeout 30
docker compose -f deploy/compose.local.yaml exec servicevoice python scripts/database_backup.py verify --source /data/interview-backup-01.sqlite3 --timeout 30
docker compose -f deploy/compose.local.yaml exec servicevoice python scripts/database_backup.py restore --source /data/interview-backup-01.sqlite3 --destination /data/restored-01.sqlite3 --timeout 30
```

Backup uses SQLite's online backup API, so committed state is captured without copying a live WAL file by hand. Restore always creates a new file and does not replace the active database. Switching to a restored file is a separate step: stop the service, change its `SERVICEVOICE_DB` entry to `/data/restored-01.sqlite3`, then recreate it. Keep the original until the restored state has been reviewed.

A backup in the same volume is useful for testing but does not protect against loss of that volume. Copy a verified snapshot to separate storage when needed, for example:

```powershell
docker compose -f deploy/compose.local.yaml cp servicevoice:/data/interview-backup-01.sqlite3 ./interview-backup-01.sqlite3
```

These files can contain customer data. The Docker image itself contains neither the host runtime database nor its `.env`. The Linux verification recorded `directory_fsync: true`; the backup tool's generic Windows fsync limitation applies to Windows, not this Linux execution.

## Boundaries and configuration

The image defaults to **provider mode**. That mode binds only container port 8000 and exposes authenticated provider routes plus minimal health/readiness endpoints. It has no console socket or UI/API routes. The separate `compose.provider.yaml` is a future setup template requiring an explicitly supplied private environment file; it is not the keyless local start command.

Local mode is explicitly selected by `compose.local.yaml`. One Uvicorn process shares one application store and one lifespan across two sockets. Host port 8875 maps to the local console, and 8876 maps to the provider routes; both bindings are restricted to host `127.0.0.1`. The local adapter accepts only the dedicated bridge gateway or loopback, validates a local Host and same-origin browser metadata, strips forwarded headers, then supplies the loopback peer expected by the existing operator API. It does not trust arbitrary proxy headers or grant access through the provider socket.

The dedicated bridge **permits outbound networking**. No provider credentials are supplied to the local sandbox, and startup performs no provider requests. An initial `internal: true` bridge prevented Docker 29 from publishing the loopback ports; that configuration was replaced rather than described as usable network isolation. If subnet `172.30.88.0/24` conflicts with another local network, choose a free subnet and update both its gateway and `SERVICEVOICE_LOCAL_PEER` together.

The container runs as UID/GID 10001 with a read-only root filesystem, dropped capabilities, no-new-privileges, a 16 MiB temporary filesystem, 512 MiB memory, one CPU and 128-process limit. Only `/data` persists. A nonblocking filesystem lock prevents a second application process from owning the same volume. Multiple workers or replicas would invalidate some in-memory limits and are unsupported here.

SIGTERM uses native Uvicorn handling. In the repaired-image run, shutdown completed in 1.298 seconds, Docker reported exit **143**, and persisted state survived restart. Uvicorn re-raises the received signal after draining; exit 143 alone is not evidence of a failure. The test required shutdown-complete logs, no OOM/forced kill, and successful restart verification. Exit 137 is not accepted. Compose allows 40 seconds for the server and application drain; blocked operating-system I/O still has no hard completion guarantee.

## Runtime integrity

The official Python base is pinned by digest. Its stock SQLite 3.40.1 was rejected by the existing `ensure_sqlite()` guard, which remains unchanged and requires at least 3.51.3. The image instead compiles SQLite 3.54.1 from the official amalgamation after checking the published SHA3-256. The resulting library loads through a fixed `/opt/sqlite/lib` path. Source/build hashes and installed dependency versions are recorded in the verification artifact.

All Python requirements are exact versions from the existing `requirements-lock.txt`; `pip check` passes. This pins package versions, not every future wheel or compiler package byte. The recorded image ID is the exact tested build. The build context is allowlisted: `.env`, databases, docs, work directories, Windows DLLs, other scripts and a synthetic exclusion canary are absent from the image. Only the backup helper is copied from `scripts/`.

## Future live provider use

Your own live container setup needs allowed egress, private runtime credentials, matching saved provider assistant/server credentials, and an HTTPS endpoint targeting the container's **provider** port. The [Lightsail package](lightsail/README.md) provides a phone-first template. Do not point a public tunnel at the console port.

For inbound-phone mode, use a dedicated assistant/organization/number identity and the independent phone webhook credential, enable `SERVICEVOICE_TELEPHONY_ENABLED=on`, configure the custom Gemini profile/key/gateway credential, then validate the real provider contract. Readiness only checks the local configuration and database; it does not prove the number, assistant or credentials work remotely.

The native host dashboard cannot issue a session for the isolated container database. Any future browser-based live use must create and bind its session through the local console that owns the same container database. Private environment values are runtime inputs, never Docker build arguments. `*_FILE` is supported for the allowlisted secret keys, including `GEMINI_TTS_BEARER_SECRET`, when files are mounted read-only under `/run/secrets`; direct environment values and file values for the same key are rejected as ambiguous. Nothing in this package deploys to AWS.

## Verification

Run `python -m pytest -q deploy/test_server.py` for package unit tests. The synthetic integration verifier, `node deploy/verify_deployment.mjs unique-label`, intentionally creates fictional records and stops/restarts the named Docker sandbox. Run it only when that sandbox is free for testing. It does not target the native host database or ports 8765/8766. Save operational evidence privately and verify source/image hashes before applying results to another build.

The [isolated SIP/RTP lab](../integrations/telephony-lab/README.md) exercises real loopback signaling/media separately from this application. It is not a carrier connection.

Primary references: [official Python image](https://hub.docker.com/_/python), [SQLite source and checksums](https://sqlite.org/download.html), [Docker secrets](https://docs.docker.com/build/building/secrets/).
