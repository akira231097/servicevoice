# Phone-first Lightsail deployment package

This package runs one ServiceVoice process and Caddy on one Ubuntu 24.04 LTS Lightsail VM. It publishes only HTTPS phone-provider routes and minimal liveness. **It does not publish the browser console or advisor inbox.** A call to the configured Vapi number can run independently of your laptop once the VM, HTTPS and dedicated phone assistant are configured and accepted.

These files are reusable configuration templates. They do not include a working server address, phone identity, credentials or database. Create and verify your own provider and AWS configuration before enabling phone admission.

## Cost and capacity

The intended starting plan is Linux, public IPv4, **2 GB RAM / 2 vCPU / 60 GB SSD / $12 per month**, in **us-west-2 (Oregon)**. This is VM pricing, not the total voice-agent bill: Vapi, phone service, Gemini, ElevenLabs, optional TypeSafe/JEV, backups and excess transfer can cost more. Confirm the selected bundle in the AWS checkout. [Lightsail pricing](https://aws.amazon.com/lightsail/pricing/).

The `default` profile configures at most **four** combined live sessions, four gateway requests, four phone HTTP admissions and four phone workers, matching the current Vapi allowance. It retains four tool writers and a bounded queue. These are admission ceilings, **not evidence of four simultaneous phone conversations meeting a latency target**. The app is limited to 1 GiB and 1 CPU; Caddy to 192 MiB and 0.25 CPUs, leaving roughly 800 MiB for Linux and Docker. No Vapi upgrade is required for this selected target. The application retains an optional `demo10` profile for a separately reviewed future upgrade; it is not enabled here. Never add Uvicorn workers or a second application process against this volume.

## Public IPv4 HTTPS: a domain purchase is optional

Attach a Lightsail **static public IPv4** before setting provider URLs. `SERVICEVOICE_PUBLIC_HOST` accepts that IP or an owned DNS name. Caddy 2.11.7 is pinned by image digest and requests Let's Encrypt's `shortlived` certificate profile. IP certificates last about 160 hours, so continuous automatic renewal, persistent Caddy data and reachable ports 80/443 matter. The global `default_sni` supports clients that omit SNI for an IP address.

Local tests use an isolated local CA; they do **not** prove a public ACME certificate was issued or that Vapi accepts this particular IP endpoint. Actual AWS reachability, issuance, renewal and Vapi TLS must pass before phone use. [Let's Encrypt IP certificates](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability), [Caddy TLS](https://caddyserver.com/docs/caddyfile/directives/tls), [Caddy 2.11.7](https://github.com/caddyserver/caddy/releases/tag/v2.11.7).

## Prepare the VM and private configuration

1. Create the selected Ubuntu VM and attach its static IPv4. In the Lightsail firewall, allow TCP 80 and 443 publicly; restrict SSH TCP 22 to your trusted IP. Do not open 8000, 8001, 2019, 8765 or 8875. Apply equivalent restrictions to IPv6 if enabled. Docker's published ports can bypass host UFW rules; the Lightsail firewall and the absence of an application port mapping are intentional boundaries. [Lightsail firewall](https://docs.aws.amazon.com/lightsail/latest/userguide/understanding-firewall-and-port-mappings-in-amazon-lightsail.html), [Docker firewall behavior](https://docs.docker.com/engine/network/packet-filtering-firewalls/).
2. Install Docker Engine and the Compose plugin using the [official Ubuntu instructions](https://docs.docker.com/engine/install/ubuntu/). Compose 2.30 or newer is needed for raw env files. Enable Docker at boot with `sudo systemctl enable --now docker`.
3. Copy the project source to `/opt/servicevoice`. Exclude `.env`, databases, `docs`, `work`, `.venv`, `node_modules` and private runtime files. The application build uses the existing allowlisted `.dockerignore`, pinned Python/dependencies and verified SQLite build; it fails closed below SQLite 3.51.3.
4. Create settings outside the checkout. These commands copy **templates**, not working credentials:

```bash
cd /opt/servicevoice
sudo install -d -m 700 /etc/servicevoice
sudo install -m 600 deploy/lightsail/settings.env.example /etc/servicevoice/settings.env
sudo install -m 600 deploy/lightsail/private.env.example /etc/servicevoice/private.env
sudoedit /etc/servicevoice/settings.env
sudoedit /etc/servicevoice/private.env
```

Use raw `KEY=value`, no quotes, spaces, shell commands or variable references. Replace every placeholder. Generate separate random secrets for the phone webhook and Gemini gateway; do not reuse them. No Vapi admin API key, browser public key or ElevenLabs key is required by this container: Vapi owns the saved voice-provider credential. Do not paste secrets into terminal commands, issue trackers, reports or Compose output. Never run `docker compose config` without `--quiet` on the real configuration.

The application retains bounded, content-free gateway timing in a separate SQLite file beside the booking database. A background worker writes those diagnostics without sharing the booking writer lock. Optional LangSmith export is disabled by default; set `SERVICEVOICE_LANGSMITH_ENABLED=on` and supply its API key (and workspace ID when needed) only to export numeric timings, fixed route labels and pseudonymous correlation. Prompts, transcripts, customer details, tool arguments and provider signatures are excluded. Export uses a background batch worker and a local 1,000-span monthly cap; it never gates booking completion.

`SERVICEVOICE_JEV_FAQ=on` preserves the accepted optional FAQ fast path and requires `TYPESAFE_API_KEY`. To deliberately use Gemini fallback instead, set it to `off` and remove the TypeSafe placeholder line. This is a feature choice, not a promised latency difference.

The phone organization, number and **dedicated phone assistant** UUIDs must exist before startup. The assistant must use the current prompt/tool schema and the same exact Gemini 3.8 custom gateway profile. Configure the number's assistant-request routing and the assistant's authenticated phone webhook to `https://PUBLIC_HOST/webhooks/vapi/inbound`; configure the custom LLM base URL to `https://PUBLIC_HOST/llm` with the distinct gateway credential. Provider credential configuration happens outside this package. The app derives both public URLs from one settings value to prevent stale-tunnel URL mismatches. Do not use the local browser assistant as the dedicated phone assistant.

The preflight rejects missing phone IDs or secrets. There is no fake-ready bootstrap with telephony disabled. A provider administrator can create/configure these resources before bringing the VM app up, then finish acceptance after HTTPS is reachable.

## Build, start and inspect

```bash
cd /opt/servicevoice
sudo python3 deploy/lightsail/manage.py --settings /etc/servicevoice/settings.env check
sudo python3 deploy/lightsail/manage.py --settings /etc/servicevoice/settings.env build
sudo python3 deploy/lightsail/manage.py --settings /etc/servicevoice/settings.env validate-edge
sudo python3 deploy/lightsail/manage.py --settings /etc/servicevoice/settings.env up
sudo python3 deploy/lightsail/manage.py --settings /etc/servicevoice/settings.env status
sudo python3 deploy/lightsail/manage.py --settings /etc/servicevoice/settings.env ready
```

`check` validates file permissions, configuration shape and VM memory/CPU; it does not authenticate provider credentials. `ready` checks local configuration and SQLite through the private application listener. `GET https://PUBLIC_HOST/healthz` is only liveness. Readiness, API docs, static files, sessions, operator capabilities and the browser webhook remain inaccessible through Caddy.

Only `POST /webhooks/vapi/inbound` and `POST /llm/chat/completions` are forwarded as provider operations; the app still authenticates and binds them. Caddy caps request bodies and headers. SSE is immediately flushed by Caddy's content-type handling; there is no forced negative flush interval, which would disable upstream cancellation on a downstream disconnect. No proxy retries replay tool-bearing requests. [Caddy reverse proxy behavior](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy).

Inspect logs over SSH with `manage.py ... logs`. Treat logs as private operational data. There is no public operator UI in this package and no SSH command that marks cases resolved. The packaged backup verifier provides private aggregate integrity/count checks; actual advisor follow-up still needs an authorized operator workflow or later CRM integration.

## Backup, off-disk recovery and stopping

Use a new label each time. Online SQLite backup produces a verified, self-contained file without deleting or checkpointing the running source:

```bash
sudo python3 deploy/lightsail/manage.py --settings /etc/servicevoice/settings.env backup --label before-update-20261018
sudo python3 deploy/lightsail/manage.py --settings /etc/servicevoice/settings.env verify-backup --label before-update-20261018
```

This writes `/data/backups/before-update-20261018.sqlite3` in the app volume. **A copy on the same VM is not disaster recovery.** Copy it over your authenticated SSH session to separate encrypted storage, with access restricted to you. One explicit extraction workflow on the VM is:

```bash
sudo install -d -m 700 /var/backups/servicevoice
cid=$(sudo docker compose --project-name servicevoice-lightsail --env-file /etc/servicevoice/settings.env -f /opt/servicevoice/deploy/lightsail/compose.yaml ps -q servicevoice)
sudo docker cp "$cid:/data/backups/before-update-20261018.sqlite3" /var/backups/servicevoice/before-update-20261018.sqlite3
sudo chmod 600 /var/backups/servicevoice/before-update-20261018.sqlite3
```

Use a trusted administrator's SSH/SFTP channel to transfer that file off the VM; do not make it web-accessible. Test restore on disposable data before depending on a backup. The CLI always restores to a **new** database path:

```bash
sudo python3 deploy/lightsail/manage.py --settings /etc/servicevoice/settings.env restore --backup before-update-20261018 --label restored-20261018
sudo python3 deploy/lightsail/manage.py --settings /etc/servicevoice/settings.env stop
sudoedit /etc/servicevoice/settings.env
# Set SERVICEVOICE_DATABASE_PATH=/data/restores/restored-20261018.sqlite3
sudo python3 deploy/lightsail/manage.py --settings /etc/servicevoice/settings.env up
sudo python3 deploy/lightsail/manage.py --settings /etc/servicevoice/settings.env ready
```

The example restores a backup already in the volume. For a replacement VM, securely import the verified backup into that new volume first; do not overwrite an active database. Restore preserves historical receipts and uncertain operations rather than replaying them. Keep the original database until recovery is accepted.

`stop` preserves all three named volumes. Never use `down -v` or prune these volumes as routine shutdown. Compose allows 40 seconds for app drain and 45 for Caddy; blocked OS I/O is not guaranteed to finish. A native SIGTERM exit 143 can be normal if shutdown completed and data is intact; 137/forced kills require investigation.

`restart: unless-stopped` resumes previously running containers after daemon/VM restart; explicitly stopped containers stay stopped. Docker health does not itself restart an unhealthy container. After reboot, run `status` and `ready`, inspect Caddy renewal errors, and check authenticated provider acceptance. For updates, arrange a call-free maintenance window, take and export a backup, build the reviewed source, then run `up` and repeat checks. This package is a single-instance deployment, not high availability.

## Acceptance still required on AWS

- Static-IP HTTPS chain and automatic renewal; HTTP redirects to public HTTPS port 443.
- Public console/readyz/docs rejection, authenticated provider routes and exact phone identity.
- Provider account concurrency/credits, real inbound phone routing and call termination.
- One booking only after full readback and later confirmation; correction, interruption, withdrawal and multilingual checks.
- Real simultaneous calls within the configured ceiling with measured audio latency, throttling and cleanup. Local mocked stress proves bounded behavior, not carrier/audio quality.
- Reboot and off-disk restore on the actual VM. Local package tests do not perform AWS provisioning, billable phone calls or public certificate issuance.

Retain private verification logs, including failures and source/image hashes, for your own deployment. Operational logs and real call data do not belong in this public repository.
