# Evaluations and observability

## Layers

1. Offline unit, contract, browser lifecycle, fault and concurrency tests validate code behavior with disposable data and mocked providers.
2. Synthetic model evaluations exercise actual tool choices against reference expectations in isolated SQLite databases. The `gateway` transport includes the streaming gateway, optional FAQ route and receipt renderer.
3. Native LangSmith datasets and experiments store synthetic inputs, references, outputs and per-example feedback for comparison.
4. Human voice acceptance checks the microphone, recognition, audible replies, interruption and real caller experience.

Passing one layer does not establish the next. Text response time is not acoustic latency. A matching tool receipt does not independently prove spoken consent.

## Recorded v17/v18 comparison — 2026-10-19

The private LangSmith workspace received two datasets, three experiments and 74 result rows: 31 matched synthetic conversations per version, plus 12 judge controls. The runs shared the same reference cases, effective date and review rubric. These are one-run regression comparisons, not statistically representative benchmarks. The public repository includes the synthetic dataset and evaluation tooling; private operational logs and workspace links are excluded.

| Check | v17 | v18 |
|---|---:|---:|
| Mechanical workflow checks | 31/31 | 30/31 |
| Advisory judge: factual grounding | 31/31 | 31/31 |
| Advisory judge: action truthfulness | 31/31 | 31/31 |
| Advisory judge: task completion | 30/31 | 30/31 |
| Advisory judge: conversation quality | 31/31 | 30/31 |
| Advisory judge: consent/corrections | 31/31 | 31/31 |

Known issue **SV-R003**: after a caller changes diagnostic service to a brake inspection and supplies the requested details, v18 offers correct slots but stops before preparing the requested proposal. No appointment or proposal was created and it did not claim a booking. The v17 judge's task-completion failure was a different case, SV-N005, which omitted explicit advisor-verification guidance. These failures are retained rather than excluded from the comparison.

The same Gemini family supplied the advisory semantic judge. It matched the expected labels on 12/12 known-fact positive and negative controls, including invented prices, unsupported callback delivery, false business facts, evaluator injection and a Spanish case. This small calibration does not establish an independent or universal hallucination detector. Most cases do not reach confirmation; only three per version perform a confirmation and three speak a literal booking reference. All dataset/example inputs, reference outputs, actual outputs and feedback scores were read back from LangSmith for verification.

The evaluation tooling checkpoint passed 280 focused tests. The public source copy is retested before publication; see its GitHub Actions result for the reproducible offline suites.

## Reproduce offline checks

```powershell
python -m evals.run validate
python -m evals.run contracts
python -m evals.offline
```

For an explicitly paid gateway evaluation in disposable data:

```powershell
python -m evals.run model --dataset evals/datasets/servicevoice-v18-grounded-v1.json --transport gateway --max-requests 120 --execute
```

Inspect CLI help before running the judge, uploading or comparing experiments:

```powershell
python -m evals.native_judge --help
python -m evals.calibrate --help
python -m evals.native_upload --help
python -m evals.native_readback --help
python -m evals.compare --help
```

The native uploader defaults to a preview. Explicit `--upload --allow-extended-retention` is required to write synthetic examples/results to your LangSmith workspace. Never use real call records as synthetic examples. The readback tool checks the expected dataset, experiment, outputs and feedback against its scoped upload receipt. Use the same dataset and reference expectations when comparing versions, preserve failures, and do not publish a run's private workspace URLs accidentally.

## Runtime observability

Set `SERVICEVOICE_LANGSMITH_ENABLED=on`, provide `LANGSMITH_API_KEY`, select your account's `LANGSMITH_ENDPOINT`, and optionally supply `LANGSMITH_WORKSPACE_ID`. Runtime export is asynchronous, uses a bounded queue and a local span budget, and does not block booking completion. It excludes prompts, transcripts, customer details, tool arguments and provider signatures. Native synthetic evaluations deliberately store richer synthetic content separately.

Trace timing distinguishes gateway phases and tool execution. Local content-free timing persistence can be enabled with `SERVICEVOICE_TIMING_PERSISTENCE=on`. Neither metric includes the entire audible caller-to-reply delay. See [LangSmith configuration](../deploy/langsmith/README.md).
