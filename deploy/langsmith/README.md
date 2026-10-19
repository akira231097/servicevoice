# LangSmith configuration

Runtime tracing is optional and disabled in `.env.example`. It uses an isolated OpenTelemetry provider and an asynchronous bounded export queue. Native synthetic evaluation datasets/experiments are separate from content-free runtime spans. See [evaluation instructions](../../docs/evaluations.md).

Set these in your ignored `.env` or private deployment settings:

```dotenv
LANGSMITH_API_KEY=REPLACE_WITH_YOUR_KEY
LANGSMITH_ENDPOINT=https://aws.api.smith.langchain.com
LANGSMITH_PROJECT=servicevoice-local
LANGSMITH_WORKSPACE_ID=
SERVICEVOICE_LANGSMITH_ENABLED=on
SERVICEVOICE_LANGSMITH_SAMPLE_RATE=1.0
SERVICEVOICE_LANGSMITH_MONTHLY_SPAN_LIMIT=1000
SERVICEVOICE_LANGSMITH_BUDGET_PATH=.local/langsmith-budget.sqlite3
```

The endpoint must match your account region. Supported account endpoints are `https://api.smith.langchain.com`, `https://aws.api.smith.langchain.com`, `https://eu.api.smith.langchain.com` and `https://apac.api.smith.langchain.com`. Do not append the OTLP signal path. A workspace ID is its UUID, not its display name.

Runtime spans contain numeric timings, fixed route labels and pseudonymous correlation, not prompts, transcripts, customer contact data, tool arguments or provider signatures. The local monthly span cap is not an account-wide billing guarantee. Export failure drops diagnostics without gating the booking path. Synthetic evaluation upload can have different retention and requires explicit opt-in.

The JSON files here are example ECS/IAM fragments for a possible future deployment, not a complete infrastructure plan. They use placeholders and do not provision AWS resources. The simpler single-VM package is documented under [Lightsail](../lightsail/README.md).

```powershell
python deploy/langsmith/validate_templates.py
python -m pytest -q deploy/langsmith/test_templates.py
python scripts/check_langsmith.py --help
```
