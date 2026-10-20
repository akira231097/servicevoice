"""Behavior-changing flags stay visible without exposing runtime secrets."""
import json

import pytest

from app.diagnostics import Diagnostics, configuration_manifest, read_call_diagnostics


@pytest.mark.parametrize("flag", ["SERVICEVOICE_NATIVE_END_CALL", "SERVICEVOICE_DIAGNOSTICS_ENABLED"])
def test_behavior_flag_changes_configuration_identity_and_stays_requested_only(tmp_path, flag):
    enabled = configuration_manifest({flag: "on"}, tmp_path)
    disabled = configuration_manifest({flag: "off"}, tmp_path)
    assert enabled["values"][flag] == "on"
    assert disabled["values"][flag] == "off"
    assert enabled["id"] != disabled["id"]
    assert enabled["source_sha256"] == disabled["source_sha256"]
    assert enabled["provider_effective_configuration"] == "unknown"
    assert enabled["provenance"] == "local_requested_settings_and_owned_source"


@pytest.mark.parametrize("flag", ["SERVICEVOICE_NATIVE_END_CALL", "SERVICEVOICE_DIAGNOSTICS_ENABLED"])
def test_invalid_flag_values_and_unrelated_credentials_never_reach_manifest(tmp_path, flag):
    secret = "Bearer synthetic-private-key person@example.invalid"
    baseline = configuration_manifest({flag: "on"}, tmp_path)
    assert baseline == configuration_manifest({flag: "on", "GEMINI_API_KEY": secret,
        "VAPI_API_KEY": secret, "LANGSMITH_API_KEY": secret, "PASSWORD": secret}, tmp_path)
    bad = configuration_manifest({flag: secret, "GEMINI_API_KEY": secret}, tmp_path)
    assert bad["values"][flag] == "unknown"
    assert bad["capture_issues"] == ["unrecognized_" + flag.lower()]
    assert secret not in json.dumps(bad)


def test_persisted_manifest_reprojection_preserves_both_safe_flags_and_no_secrets(tmp_path):
    private = "synthetic-private-value-never-export"
    settings = {"SERVICEVOICE_NATIVE_END_CALL": "on", "SERVICEVOICE_DIAGNOSTICS_ENABLED": "on",
                "GEMINI_API_KEY": private, "VAPI_API_KEY": private}
    sink = Diagnostics(tmp_path / "observations.sqlite3", settings, source_root=tmp_path)
    assert sink.record("gateway", "request_finished", "success", call_id="synthetic-call")
    assert sink.close()
    report = read_call_diagnostics(sink.path, "synthetic-call")
    assert report["coverage"]["state"] == "observed"
    projected = report["configuration"]
    assert projected["id"] == sink.manifest["id"]
    assert projected["values"]["SERVICEVOICE_NATIVE_END_CALL"] == "on"
    assert projected["values"]["SERVICEVOICE_DIAGNOSTICS_ENABLED"] == "on"
    assert projected["provider_effective_configuration"] == "unknown"
    assert private not in json.dumps(report)
    assert private.encode() not in sink.path.read_bytes()
