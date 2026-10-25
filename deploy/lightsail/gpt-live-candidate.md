# Isolated GPT-Live (Vapi beta) test line

This is a separate test line on a separate Twilio number. Vapi runs OpenAI's `gpt-live-1` as the speaker and a GPT-5.6 reasoner. The reasoner calls this service's booking tools through the authenticated phone webhook.

It never changes:
- the main number, its assistant or its Compose project;
- the Twilio listening lane;
- any main volume or network.

## What is different from the main line

- **Tool provenance:** there is no Gemini gateway issuance. Phone admission pins `provider-native`, and only when all of these are set:
  - `SERVICEVOICE_PHONE_NATIVE_TOOLS=gpt-live-candidate`
  - `VAPI_MODEL_PROFILE=gpt-live-1`
  - `SERVICEVOICE_ENVIRONMENT` is `local`, `development` or `staging`

  Production refuses this configuration at startup.
- **Confirmations:** every `confirm_action` for a pending proposal passes `app/native_consent.py` first. The live call transcript that Vapi sends with the tool call must show:
  1. a readback after the proposal was prepared, covering the service, day, time and, for bookings, the name and the callback number's last four digits;
  2. then a later explicit yes that no later caller turn withdraws.

  Otherwise the result is `confirmation_not_verified` and nothing is written.
- **Admission caps:** `SERVICEVOICE_PHONE_DAILY_ADMISSIONS` and `SERVICEVOICE_PHONE_LIFETIME_ADMISSIONS`. An over-cap call receives Vapi's spoken `error` refusal; no session or model time is created.
- **Settings:** the assistant comes from `scripts/provision_gpt_live.py` and `app/gpt_live.py`. It has GPT-Live voices only, generated speech, idle check-ins at 8, 20 and 45 seconds, recording on and a 10-minute limit.

## Deploy (operator)

1. **Build the image.** Build `Dockerfile.gpt-live` as an app-only overlay on the reviewed base image. First confirm the base's `pip freeze` equals `requirements-lock.txt`. The build needs no network.
2. **Write the env file.** Create `/etc/servicevoice/gpt-live-business.env` (root, 0600) containing:
   - the settings above;
   - a fresh phone webhook secret, plus distinct browser and gateway secrets;
   - the Vapi org, assistant and phone-number IDs.

   The same phone secret becomes a Vapi `custom-credential` bearer credential, and that credential's ID must be in the assistant's `credentialIds`.
3. **Start the service.** Run `docker compose --project-name servicevoice-gpt-live-candidate --env-file <release>/settings.env -f <release>/compose.gpt-live.yaml up -d --no-build gpt-live-business`.
4. **Add the edge route.** Insert `gpt-live-candidate.caddy` into the host Caddyfile inside the HTTPS site, before its catch-all.

   **Warning:** the running Caddy container (started 2026-10-19) still has the *original inode* of `/etc/caddy/Caddyfile` mounted, because the host file was later replaced. So `caddy reload --config /etc/caddy/Caddyfile` reloads a stale file that lacks the `/speech-candidate/` routes the main number uses. That briefly took the main number's routes down on 2026-10-25; no calls were affected.

   Always reload from a fresh copy instead:
   1. `docker cp` the host Caddyfile into `/config/` and make it readable.
   2. Run `caddy validate`, then `caddy reload --config /config/<copy> --adapter caddyfile`.
   3. Check that each of these routes still answers 401 when unauthenticated: `/speech-candidate/webhooks/vapi/inbound`, `/webhooks/vapi/inbound`, `/backchannel-candidate/webhooks/vapi/inbound` and `/gpt-live/webhooks/vapi/inbound`.

   The pre-change config stays at `/config/restore-before-gpt-live.caddy` for rollback.
5. **Create the assistant.** Run `scripts/provision_gpt_live.py --apply`. It refuses without a connected OpenAI credential unless `--allow-vapi-billing` is passed.
6. **Import the number.** Run `scripts/import_twilio_number.py --apply`. It needs the owner's approval because it creates a dedicated, revocable Twilio API key and gives only that key to Vapi. The number gets no assistant, so every call reaches the authenticated `assistant-request`.

## Roll back

- `scripts/import_twilio_number.py --rollback --receipt <receipt>` deletes the Vapi number and the dedicated key, and restores the Twilio voice settings.
- Stop this Compose project.
- Remove the snippet between its BEGIN/END markers in the host Caddyfile, then load a fresh copy as in step 4. Or reload `/config/restore-before-gpt-live.caddy`, which is the exact pre-change config.

Keep the volume until the evidence has been reviewed.
