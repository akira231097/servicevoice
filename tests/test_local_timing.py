"""Durable content-free timings and slow/failing storage boundaries; no network."""
import json
import sqlite3
import threading
import time
from uuid import uuid4

import pytest

from app import observability as module
from app.observability import LocalTimingSink, NullLocalTimingSink, create_local_timing_sink, safe_timing_row


def row(**updates):
    value = {"request_id": uuid4().hex, "call_key": "a"*16, "started_at_unix": time.time(),
             "route": "gemini", "outcome": "completed", "narration_reason": "presentation_missing_or_changed",
             "narrator_version": "receipt-en-2026-10-18-6", "jev_faq_version": "jev-static-faq-2026-10-18-2",
             "jev_model": "jev-1.13.0", "system_prompt_sha256": "b"*64,
             "milestones_ms": {"upstream_headers": 1400, "first_upstream_event": 1450,
                               "first_upstream_text": 1500, "first_text": 2100, "total": 2150},
             "durations_ms": {"validated_text_hold": 600}, "counts": {"text_chunks": 1},
             "usage": {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150}}
    value.update(updates)
    return value


def test_restart_preserves_content_free_timings_and_distinct_unknown_usage(tmp_path):
    path = tmp_path / "timings.sqlite3"
    sink = LocalTimingSink(path)
    value = row()
    value.update(prompt="PRIVATE NAME 2025550146", arguments={"phone": "PRIVATE"},
                 session_id="private-session", provider_call_id="private-call", thought_signature="private-signature")
    assert sink.record(value)
    assert sink.close()
    second = LocalTimingSink(path)
    try:
        rows = second.snapshot("a"*16)
        assert len(rows) == 1 and rows[0]["request_id"] == value["request_id"]
        assert rows[0]["milestones_ms"]["first_upstream_text"] == 1500
        assert rows[0]["milestones_ms"]["first_text"] == 2100
        assert rows[0]["durations_ms"]["validated_text_hold"] == 600
        assert rows[0]["usage"]["cached_input_tokens"] is None
        assert rows[0]["usage"]["reasoning_tokens"] is None
        assert rows[0]["jev_model"] == "jev-1.13.0"
        serialized = json.dumps(rows)
        for forbidden in ("PRIVATE", "2025550146", "private-session", "private-call", "private-signature"):
            assert forbidden not in serialized
            assert forbidden.encode() not in path.read_bytes()
    finally:
        second.close()


def test_scoped_read_and_immutable_request_identity(tmp_path):
    sink = LocalTimingSink(tmp_path / "timings.sqlite3")
    first = row()
    assert sink.record(first)
    assert sink.record({**first, "call_key": "c"*16, "route": "deterministic_receipt"})
    assert sink.record(row(call_key="d"*16))
    assert sink.close()
    assert len(sink.snapshot("a"*16)) == 1
    assert sink.snapshot("c"*16) == []
    assert len(sink.snapshot("d"*16)) == 1
    assert sink.snapshot("a' OR 1=1") == []
    assert sink.snapshot(limit=257) == []
    assert not sink.record(row())


def test_row_bound_keeps_newest_and_expiration_prunes_on_restart(tmp_path, monkeypatch):
    path = tmp_path / "timings.sqlite3"
    sink = LocalTimingSink(path, max_rows=3, ttl_seconds=60)
    moment = time.time()
    values = [row(started_at_unix=moment-10+i) for i in range(5)]
    for value in values: assert sink.record(value)
    assert sink.close()
    assert [item["request_id"] for item in sink.snapshot()] == [item["request_id"] for item in values[-3:]]
    monkeypatch.setattr(module.time, "time", lambda: moment+61)
    assert sink.snapshot() == []
    restarted = LocalTimingSink(path, max_rows=3, ttl_seconds=60)
    assert restarted.close()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM gateway_timings").fetchone()[0] == 0


def test_slow_storage_never_blocks_record_and_queue_is_bounded(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = LocalTimingSink._write
    caller_thread = threading.get_ident()
    def blocked(self, rows):
        assert threading.get_ident() != caller_thread
        if rows:
            entered.set()
            assert release.wait(3)
        return original(self, rows)
    monkeypatch.setattr(LocalTimingSink, "_write", blocked)
    sink = LocalTimingSink(tmp_path / "timings.sqlite3", queue_size=2, batch_size=1)
    try:
        assert sink.record(row())
        assert entered.wait(1)
        assert sink.record(row())
        assert sink.record(row())
        assert not sink.record(row())
        assert sink.status()["queued"] == 2 and sink.status()["queue_dropped"] == 1
        assert sink.close(timeout_seconds=0) is False
        assert not sink.record(row())
    finally:
        release.set()
        assert sink.close()
    assert len(sink.snapshot()) == 3


def test_busy_companion_drops_without_waiting_on_business_database(tmp_path):
    path = tmp_path / "timings.sqlite3"
    sink = LocalTimingSink(path)
    assert sink.record(row())
    assert sink.close()
    business = tmp_path / "business.sqlite3"
    with sqlite3.connect(business) as db: db.execute("CREATE TABLE marker(value INTEGER)"); db.execute("INSERT INTO marker VALUES(7)")
    lock = sqlite3.connect(path)
    lock.execute("BEGIN EXCLUSIVE")
    next_sink = LocalTimingSink(path)
    try:
        assert next_sink.record(row())
        assert next_sink.close()
        assert next_sink.status()["storage_dropped"] == 1
        with sqlite3.connect(business) as db:
            assert db.execute("SELECT value FROM marker").fetchone()[0] == 7
    finally:
        lock.rollback();lock.close();next_sink.close()


def test_unwritable_parent_is_observable_without_caller_exception(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("unchanged")
    sink = create_local_timing_sink(blocked / "timings.sqlite3")
    assert sink.record(row())
    assert sink.close()
    assert sink.status()["storage_dropped"] == 1
    assert blocked.read_text() == "unchanged"
    assert sink.snapshot() == [] and sink.status()["read_errors"] == 1


@pytest.mark.parametrize("field,value", [("request_id", "private name"), ("call_key", "provider-uuid"),
    ("route", ["gemini"]), ("outcome", "customer-text"), ("started_at_unix", float("nan")),
    ("milestones_ms", {"total": -1})])
def test_invalid_identity_or_required_timing_is_rejected(field, value):
    assert safe_timing_row(row(**{field:value})) is None


def test_unknown_attributes_bad_numbers_and_bad_shapes_are_not_persisted():
    value = row(usage={"input_tokens": True, "output_tokens": -1, "total_tokens": "secret", "reasoning_tokens": 0},
                output_text_source=["private"], narration_reason="customer-name", jev_probability=float("inf"),
                output_text_sha256="not-a-hash", private="do not retain")
    safe = safe_timing_row(value)
    assert safe["usage"] == {"input_tokens":None,"output_tokens":None,"total_tokens":None,"cached_input_tokens":None,"reasoning_tokens":0}
    assert safe["output_text_source"] is None and safe["narration_reason"] is None
    assert safe["jev_probability"] is None and safe["output_text_sha256"] is None
    assert "private" not in json.dumps(safe)


def test_disabled_or_invalid_sink_does_not_create_files(tmp_path):
    null = NullLocalTimingSink()
    assert not null.record(row()) and null.snapshot() == [] and null.close()
    for args in ({"queue_size":257}, {"batch_size":33}, {"max_rows":4097}, {"ttl_seconds":7*86400+1}):
        with pytest.raises(ValueError):LocalTimingSink(tmp_path / "never.sqlite3", **args)
    assert not (tmp_path / "never.sqlite3").exists()
