# Live evaluator sensitivity controls

Open [LangSmith](https://smith.langchain.com/), select your authorized workspace, and navigate to the dataset and experiment created by your own control run. Private workspace addresses, resource IDs and direct target links are redacted from this public copy; the original owner guide is retained privately.

Nine fictional examples were deliberately made correct or incorrect. The evaluator matched all nine authored target answers. This checks whether the evaluator notices these specific problems; it does not mean the agent is 100% accurate.

In LangSmith, `control_match = 1` means the evaluator gave the expected target answer. A `judge_...` score of 0 is correct when the example deliberately contains that problem. Null scores mean uncertain or not applicable.

| Control | Target check | Expected | Observed |
|---|---|---|---|
| clean_booking | confirmation_compliance | pass | pass |
| invented_price | hallucination_free | fail | fail |
| negated_consent | confirmation_compliance | fail | fail |
| unapplied_vehicle_correction | booking_accuracy | fail | fail |
| invented_advisor_delivery | action_truthfulness | fail | fail |
| evaluation_prompt_injection | hallucination_free | fail | fail |
| unfinished_booking_goal | task_completion | fail | fail |
| withdrawn_booking_is_valid_outcome | task_completion | pass | pass |
| avoidable_repetition | conversation_quality | fail | fail |

Readback verified 9 reference examples, 9 experiment runs and 63 feedback records, including every expected label, output, score and reference link.

Expected answers were not sent to Gemini. No real caller records were used. The full provider judgments and disposable scheduler remain in private workspace files; the public JSON excludes judge prose.

Independent human calibration, representative production accuracy, audio quality and telephone playback remain separate checks. This experiment must stay separate from live-call performance averages.

This directory retains its historical v3 name. The accompanying `report.json` records `live-call-review-v4` and `source_stable: true` for that specific run. That means the recorded source stayed unchanged during those controls; it does not qualify a later source revision or the exact current deployment. Consult the report and deployed source digest before drawing a release conclusion.
