# Phone ingress and optional human transfer

Inbound phone admission is implemented with a dedicated phone assistant, configured organization/number identity and a separate authenticated webhook. `app/telephony_ingress.py` binds provider calls to local sessions and enforces capacity. Follow the [Lightsail deployment guide](../deploy/lightsail/README.md) for configuration. Caller ID is not customer authentication.

The working advisor feature saves a local request and its context. It does not dial an advisor, send a callback or transfer a call. The application must describe that distinction honestly.

`vapi-warm-transfer.template.json` is an optional configuration reference with a placeholder destination. It is deliberately not auto-applied. Before implementing transfer, allowlist a consenting test destination, verify carrier compatibility, connect the native provider transfer tool, and test answered, busy, no-answer and voicemail behavior. Verify that both people can speak and that the summary is accurate; an API acknowledgment alone is insufficient.

Use your own approved test destinations. Additional carrier legs and voice-provider usage can incur charges. See [Vapi transfer documentation](https://docs.vapi.ai/tools/transfer-call) for the current provider contract.
