# Vapi voice setup

Vapi handles phone/browser audio, speech recognition and speech synthesis. ServiceVoice owns business facts and scheduling. The v18 deployment profile uses `gemini-3.8-flash-custom`, `eleven-v4-turbo` and `soniox-v5-auto`; a new installation defaults to the baseline profile until explicitly configured.

## Local settings

Copy `.env.example` to `.env`. Keep all private keys local. Configure:

| Setting | Purpose |
|---|---|
| `VAPI_API_KEY` | Private account key for explicit provisioning |
| `VAPI_PUBLIC_KEY` | Browser SDK key; restrict it in the provider dashboard where supported |
| `VAPI_ASSISTANT_ID` | Saved browser assistant |
| `PUBLIC_BASE_URL` | HTTPS origin forwarding only the provider listener, port 8766 |
| `VAPI_WEBHOOK_SECRET` | Random webhook bearer secret, at least 32 characters |
| `VAPI_SERVER_CREDENTIAL_ID` | Saved Vapi custom credential for that secret |
| `GEMINI_API_KEY` | Backend-only Gemini key |
| `GEMINI_GATEWAY_SECRET` | Different random gateway bearer secret, at least 32 characters |
| `VAPI_CUSTOM_LLM_URL` | `https://YOUR_HOST/llm`, without `/chat/completions` |
| `VAPI_CUSTOM_LLM_CREDENTIAL_ID` | Saved custom-LLM credential containing the gateway secret |
| `ELEVENLABS_API_KEY` | Used to configure Vapi's saved voice-provider credential |
| `VAPI_ELEVENLABS_CREDENTIAL_ID` | Saved ElevenLabs credential ID |
| `VAPI_ELEVENLABS_VOICE_ID` | Voice accessible to your provider account |

Use separate saved credentials for webhook and gateway authentication. The assistant's `credentialIds` allowlist must include the webhook, gateway and voice credential IDs. Never put server secrets or transient server URLs into browser overrides. Private model keys stay on the server.

The optional TypeSafe FAQ path requires `TYPESAFE_API_KEY` and `SERVICEVOICE_JEV_FAQ=on`. Guarded deterministic receipt narration is enabled by `SERVICEVOICE_RECEIPT_NARRATION=on`; evaluate it against your own voice setup first.

## Preview and provision

Start the local server and expose **only** its provider listener at `127.0.0.1:8766` through your chosen HTTPS endpoint. Keep the console on port 8765 private. Preview reads local configuration; `--offline` instead uses placeholders and reads no local credentials:

```powershell
python scripts/provision_vapi.py --model-profile gemini-3.8-flash-custom --voice-profile eleven-v4-turbo --allow-experimental --offline
```

After configuring your saved credentials and reviewing the preview, remove `--offline` and explicitly apply:

```powershell
python scripts/provision_vapi.py --model-profile gemini-3.8-flash-custom --voice-profile eleven-v4-turbo --allow-experimental --apply
```

`--apply` creates or updates a saved assistant and can change your provider account. It does not buy a number or make a call. Restart the local application after updating its environment. Test hearing, transcription, correction, readback and later assent before accepting a booking.

## Phone deployment

Use a dedicated phone assistant and the separate inbound webhook configuration, following the [Lightsail package](../deploy/lightsail/README.md). Configure the phone number in Vapi or import it through a supported carrier. The application does not provision or purchase a telephone number automatically.

The app's admission ceiling must not exceed your Vapi concurrency and model/speech provider quotas. The default profile is four combined browser/phone sessions. No configured ceiling establishes acoustic latency or carrier quality; test real simultaneous calls separately.

Local advisor requests are saved tasks. No callback delivery, outbound dialer or live human transfer is implemented. The [transfer template](vapi-warm-transfer.template.json) is a reference for later integration and must not be described as a working transfer.
