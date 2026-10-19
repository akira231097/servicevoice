# Vapi account setup API reference

Historical API-schema research, verified from official documentation and the official Web SDK on October 17, 2026. The research itself was read-only; later project provisioning configured the saved assistant and credentials described in [the current setup guide](vapi-setup.md). This reference does not imply that every documented endpoint was exercised.

## Webhook credential

Send `POST https://api.vapi.ai/credential` using the private API key as an Authorization Bearer token. The request body is:

```json
{
  "provider": "custom-credential",
  "name": "servicevoice-webhook",
  "authenticationPlan": {
    "type": "bearer",
    "token": "LOCAL_RANDOM_WEBHOOK_SECRET",
    "headerName": "Authorization",
    "bearerPrefixEnabled": true
  }
}
```

Save the returned credential ID into `VAPI_SERVER_CREDENTIAL_ID`. The saved assistant references it under `server.credentialId`. When the assistant also sets top-level `credentialIds`, include this webhook credential there alongside the custom LLM and ElevenLabs credentials. That explicit list limits the credentials available to the call. The first synthetic audio attempt exposed an omitted webhook ID; the provisioner now includes and deduplicates it, and the repeated FAQ audio check passed. Keep the token itself in local secret configuration. [Authentication documentation](https://docs.vapi.ai/server-url/server-authentication), [official assistant credential schema](https://github.com/VapiAI/client-sdk-web/blob/main/api.ts).

The Gemini gateway uses a separate saved credential with `{"provider":"custom-llm","apiKey":"GATEWAY_BEARER_SECRET"}`. Its bearer secret is distinct from `GEMINI_API_KEY` and `VAPI_WEBHOOK_SECRET`. The provider key stays in the gateway's local configuration. Register the supplied ElevenLabs key as a separate saved `11labs` credential. See [the full model/voice contract](model-comparison.md) for the current exact Gemini 3.8 configuration.

## Public token API

The official SDK declares authenticated `GET /token`, `GET /token/{id}`, `POST /token`, and `PATCH /token/{id}` routes. Listing returns token objects, whose `tag` distinguishes public/private. The optional `value` field is documented as the token key; `id` identifies the token resource. Do not log the list response, which can include private credentials, and do not infer that an absent value can be reconstructed.

For a dedicated public token, `POST https://api.vapi.ai/token` accepts:

```json
{
  "tag": "public",
  "name": "servicevoice-web",
  "restrictions": {
    "enabled": true,
    "allowedOrigins": ["http://127.0.0.1:REPLACE_PORT", "http://localhost:REPLACE_PORT"],
    "allowedAssistantIds": ["REPLACE_WITH_SAVED_ASSISTANT_UUID"],
    "allowTransientAssistant": false
  }
}
```

Replace the port and assistant ID with the application's actual values. If the user opens a hosted UI, include its exact HTTPS origin. Prefer a dedicated project token to modifying unrelated existing keys.

Source: [official SDK API types and routes](https://raw.githubusercontent.com/VapiAI/client-sdk-web/main/api.ts). Relevant definitions are `CreateCustomCredentialDTO`, `BearerAuthenticationPlan`, `CreateTokenDTO`, `Token`, `TokenRestrictions`, `tokenControllerFindAll`, `tokenControllerCreate`, and `credentialControllerCreate`.

## Restriction semantics

Only public keys support origins and assistant restrictions. Origins compare scheme, host, and port; paths are ignored. Empty allowed-origins means any origin. Restricting assistant IDs does not prohibit `assistantOverrides`, so these key restrictions do not protect an allowed assistant's prompt or server settings. Keep business authorization and session checks in our backend. A successful resource response must still be verified with a browser call using the actual configured origin. [Vapi API keys](https://docs.vapi.ai/security-and-privacy/api-keys)

The current gateway additionally replaces incoming system messages with the server-owned prompt, restores call-scoped Google signatures, and verifies tool results against the webhook's scoped observation ledger. These checks are necessary because Vapi's browser messaging capability can submit additional history; authenticating a Vapi request alone does not establish the authority of every message.

This file describes configuration shapes, not a verification result for your account. Check current provider schemas before provisioning and use the offline preview first. Operational call evidence is intentionally excluded from the public source.
