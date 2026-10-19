"""Actual in-memory OTel spans: optional usage and distinct timing semantics."""
from app.observability import TelemetryConfig, create_telemetry
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
import pytest


def record(tmp_path, attributes):
    exporter = InMemorySpanExporter()
    sink = create_telemetry(TelemetryConfig(enabled=True,
        api_key="synthetic-not-a-real-secret", budget_path=str(tmp_path / "quota.sqlite3")), exporter=exporter)
    assert sink.record_event("gateway.request", attributes, correlation="synthetic-call")
    assert sink.close()
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert not spans[0].events and not spans[0].links
    return dict(spans[0].attributes)


def test_upstream_arrival_and_validated_release_are_distinct_metadata(tmp_path):
    attrs = record(tmp_path, {"route": "gemini", "model": "gemini-3.8-flash",
        "first_upstream_text_ms": 430.5, "first_text_ms": 900.25,
        "validated_text_hold_ms": 469.75, "input_tokens": 20, "output_tokens": 11,
        "total_tokens": 31, "cached_input_tokens": 8, "reasoning_tokens": 3,
        "prompt": "private content", "arguments": {"phone": "private content"}})
    assert attrs["langsmith.metadata.first_upstream_text_ms"] == 430.5
    assert attrs["langsmith.metadata.first_text_ms"] == 900.25
    assert attrs["langsmith.metadata.validated_text_hold_ms"] == 469.75
    assert attrs["gen_ai.usage.input_tokens"] == 20
    assert attrs["gen_ai.usage.output_tokens"] == 11
    assert attrs["gen_ai.usage.total_tokens"] == 31
    assert attrs["gen_ai.usage.cache_read.input_tokens"] == 8
    assert attrs["gen_ai.usage.reasoning.output_tokens"] == 3
    assert "private content" not in str(attrs)


def test_missing_usage_and_discarded_prose_hold_are_not_fabricated(tmp_path):
    attrs = record(tmp_path, {"route": "gemini", "first_upstream_text_ms": 500,
        "first_text_ms": None, "validated_text_hold_ms": None, "input_tokens": None,
        "output_tokens": True, "total_tokens": -1, "cached_input_tokens": float("nan"),
        "reasoning_tokens": "unreported"})
    assert not any(key.startswith("gen_ai.usage.") for key in attrs)
    assert "langsmith.metadata.first_text_ms" not in attrs
    assert "langsmith.metadata.validated_text_hold_ms" not in attrs
    assert attrs["langsmith.metadata.first_upstream_text_ms"] == 500


@pytest.mark.parametrize("route", ["deterministic_receipt", "jev_static_faq"])
def test_non_gemini_routes_cannot_report_gemini_cache_or_reasoning_usage(tmp_path, route):
    attrs = record(tmp_path, {"route": route, "model": "gemini-3.8-flash",
        "input_tokens": 10, "output_tokens": 4, "total_tokens": 14,
        "cached_input_tokens": 7, "reasoning_tokens": 2})
    assert attrs["langsmith.span.kind"] == "chain"
    assert not any(key.startswith("gen_ai.") for key in attrs)
    assert not any(key.endswith("_tokens") for key in attrs)
