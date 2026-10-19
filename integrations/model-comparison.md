# Model profiles and comparison boundaries

`app/voice_profiles.py` defines provider configurations. Previewing a profile does not change a saved assistant or establish a latency winner.

```powershell
python scripts/provision_vapi.py --list-profiles
python scripts/provision_vapi.py --offline --model-profile baseline --output .local/baseline.preview.json
python scripts/provision_vapi.py --offline --model-profile gemini-3.8-flash-custom --voice-profile eleven-v4-turbo --allow-experimental --output .local/gemini.preview.json
```

The v18 configuration uses the exact Gemini 3.8 Flash custom gateway, low reasoning and a 2,048-token output cap. Native Gemini support is a separate profile and is not silently substituted. A fresh installation retains the baseline fallback until configured. Speech recognition uses the configured transcriber profile; v18 uses Soniox automatic recognition. Provider model identifiers and access must be verified against your own account.

## Gateway trust boundary

The gateway accepts authenticated, call-bound Chat Completions requests on `/llm/chat/completions`. It replaces incoming system messages with the server-owned prompt, forwards only allowlisted messages/tools, restores call-scoped provider thought signatures and verifies tool results against previously emitted observations. Tool execution checks the model-issued ID, name and original arguments before allowing effects. Exact retries can recover already recorded results; they cannot turn changed arguments into a new authorized mutation.

Requests have bounded body sizes, generation deadlines, concurrency and response sizes. A canceled HTTP waiter does not reverse an accepted or committed appointment. Pending operations use a durable operation ID and status lookup. Provenance checks establish that a command/result came through the expected path; they do not independently prove spoken consent or perfect language understanding.

Optional verified receipt narration and the TypeSafe static FAQ route can avoid another generative response only when their eligibility checks pass. They do not supply consent or create availability. The application falls back to normal reasoning when those paths are ineligible.

## Compare fairly

Keep the dataset, reference expectations, effective date, prompt, tools, provider settings and input conditions fixed when possible. Retain unsuccessful calls, cancellations, token usage and provider attempts. Compare factual grounding, task completion, confirmation correctness and total cost along with timing. A model's first text token, an SDK audio event and first audible speech are different measurements.

The [v17/v18 comparison](../docs/evaluations.md) is a matched synthetic gateway regression, with a same-model advisory judge and explicit limitations. It does not rank all available models or prove a production latency target. Live voice comparisons need repeated caller audio and independent listening checks in addition to database verification.
