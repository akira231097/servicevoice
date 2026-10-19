"""Readiness check; only --send-test sends one content-free LangSmith span.

This never changes .env, billing settings, application enablement or provider
configuration. Successful export means OTLP ingestion accepted, not independent
dashboard visibility, billing verification or an end-to-end voice acceptance.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
from typing import Mapping
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.observability import TelemetryConfig, create_telemetry

SETTINGS = frozenset({
    "LANGSMITH_API_KEY", "LANGSMITH_ENDPOINT", "LANGSMITH_PROJECT",
    "LANGSMITH_WORKSPACE_ID", "SERVICEVOICE_ENVIRONMENT",
    "SERVICEVOICE_LANGSMITH_ENABLED", "SERVICEVOICE_LANGSMITH_SAMPLE_RATE",
    "SERVICEVOICE_LANGSMITH_MONTHLY_SPAN_LIMIT", "SERVICEVOICE_LANGSMITH_BUDGET_PATH",
})
CONFIG_REASONS = frozenset({"ready", "missing_api_key", "configuration_error"})
SINK_REASONS = frozenset({
    "ready", "closed", "disabled", "missing_api_key", "configuration_error",
    "dependency_unavailable", "initialization_failed",
})
COUNTERS = (
    "recorded", "exported", "export_failed", "quota_dropped", "filtered",
    "sampled_out", "budget_errors",
)


def load_configuration(root: Path = ROOT, environ: Mapping[str, str] | None = None) -> TelemetryConfig:
    """Do not load unrelated secrets into environment or interpolate .env values."""
    from dotenv import dotenv_values
    raw = dotenv_values(root / ".env", interpolate=False)
    source = os.environ if environ is None else environ
    values = {key: str(value or "") for key, value in raw.items() if key in SETTINGS}
    values.update({key: str(source[key]) for key in SETTINGS if key in source})
    budget = values.get("SERVICEVOICE_LANGSMITH_BUDGET_PATH", "../../work/runtime/langsmith-budget.sqlite3")
    path = Path(budget)
    if budget and not path.is_absolute():
        values["SERVICEVOICE_LANGSMITH_BUDGET_PATH"] = str((root / path).resolve())
    elif budget:
        values["SERVICEVOICE_LANGSMITH_BUDGET_PATH"] = str(path)
    return TelemetryConfig.from_env(values)


def check_configuration(config: TelemetryConfig, *, send_test: bool = False, sink_factory=None) -> tuple[dict, int]:
    """Return an allowlisted report. Injectable sink is for offline tests only."""
    candidate = replace(config, enabled=True)
    reason = candidate.reason()
    report = {
        "schema_version": 1,
        "mode": "send_test" if send_test else "configuration_check",
        "key_present": bool(config.api_key),
        "workspace_present": bool(config.workspace_id),
        "agent_tracing_enabled": bool(config.enabled),
        "config_reason": reason if reason in CONFIG_REASONS else "configuration_error",
    }
    if reason != "ready":
        return report, 2
    if not send_test:
        return report, 0

    sink = None
    admitted, completed, failed = False, False, False
    report.update({key: 0 for key in COUNTERS})
    report.update(close_completed=False, ingest_accepted=False, export_reason="initialization_failed")
    try:
        sink = (create_telemetry if sink_factory is None else sink_factory)(replace(candidate, sample_rate=1.0))
        admitted = sink.record_event("setup.check", {"mode": "setup", "status": "ready"},
                                     correlation="synthetic-setup-" + uuid4().hex)
    except Exception:
        failed = True
    finally:
        if sink is not None:
            try:
                completed = sink.close(timeout_seconds=3.0)
            except Exception:
                failed = True
            try:
                status = sink.status()
                for key in COUNTERS:
                    value = status.get(key)
                    if type(value) is int and 0 <= value <= 100_000_000:
                        report[key] = value
                    elif key != "budget_errors":
                        failed = True
                value = status.get("reason")
                report["export_reason"] = value if value in SINK_REASONS else "check_failed"
                if value not in SINK_REASONS:
                    failed = True
            except Exception:
                failed = True
    report["close_completed"] = completed is True
    accepted = bool(
        not failed and admitted is True and completed is True
        and report["recorded"] == 1 and report["exported"] == 1
        and report["export_reason"] in {"ready", "closed"}
        and not any(report[key] for key in ("export_failed", "quota_dropped", "filtered", "sampled_out", "budget_errors"))
    )
    report["ingest_accepted"] = accepted
    if accepted:
        report["export_reason"] = "ingest_accepted"
    elif failed:
        report["export_reason"] = "check_failed"
    elif completed is not True:
        report["export_reason"] = "close_incomplete"
    elif report["budget_errors"]:
        report["export_reason"] = "budget_unavailable"
    elif report["quota_dropped"]:
        report["export_reason"] = "quota_dropped"
    elif report["export_failed"]:
        report["export_reason"] = "export_failed"
    elif report["export_reason"] in {"ready", "closed"}:
        report["export_reason"] = "not_exported"
    return report, 0 if accepted else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send-test", action="store_true", help="Send one synthetic, content-free trace; uses the local export budget.")
    parser.add_argument("--output", type=Path, help="Optional new JSON report. An existing file is never overwritten.")
    args = parser.parse_args(argv)
    output = None
    if args.output is not None:
        try:
            output = args.output.open("x", encoding="utf-8")
        except (OSError, ValueError):
            print(json.dumps({"schema_version": 1, "config_reason": "output_exists_or_unwritable"}))
            return 2
    try:
        try:
            config = load_configuration()
            report, code = check_configuration(config, send_test=args.send_test)
        except Exception:
            report, code = {"schema_version": 1, "config_reason": "configuration_load_failed"}, 2
        text = json.dumps(report, sort_keys=True) + "\n"
        if output is not None:
            try:
                output.write(text)
                output.flush()
                output.close()
            except (OSError, ValueError):
                print(json.dumps({"schema_version": 1, "config_reason": "output_write_failed"}))
                return 2
        print(text, end="")
        return code
    finally:
        if output is not None:
            try:
                output.close()
            except (OSError, ValueError):
                pass


if __name__ == "__main__":
    raise SystemExit(main())
