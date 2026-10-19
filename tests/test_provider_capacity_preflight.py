"""Account inspection never buys, starts calls or returns response secrets."""
import json

import httpx

from scripts.provider_capacity_preflight import check, numeric_fields


def test_read_only_selective_redaction():
    calls = []
    sensitive = "do-not-echo-provider-secret"

    def handle(request):
        calls.append((request.method, request.url.path))
        assert request.method == "GET"
        if request.url.path == "/org":
            body = [{"id": sensitive, "subscriptionLimits": {"concurrencyLimit": 10}, "apiKey": sensitive}]
        elif request.url.path == "/phone-number":
            body = [{"provider": "vapi", "number": sensitive, "credentialId": sensitive}]
        elif request.url.path.startswith("/assistant/"):
            body = {"server": {"url": "https://secret.invalid/" + sensitive}, "apiKey": sensitive,
                    "model": {"tools": [{}] * 14}, "artifactPlan": {"recordingEnabled": False}}
        elif request.url.path == "/v1/user/subscription":
            body = {"max_concurrent_dialogue_sessions": 21, "token": sensitive}
        else:
            body = {"supportedGenerationMethods": ["generateContent"], "apiKey": sensitive}
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = check({"VAPI_API_KEY": sensitive, "ELEVENLABS_API_KEY": sensitive,
                        "GEMINI_API_KEY": sensitive,
                        "VAPI_ASSISTANT_ID": "11111111-1111-4111-8111-111111111111"}, client)
    assert len(calls) == 5
    assert sensitive not in json.dumps(result)
    assert result["vapi"]["organization"]["reported_limits"] == {"concurrencyLimit": 10}
    assert result["vapi"]["browser_assistant"]["tool_count"] == 14
    assert result["vapi"]["phone_numbers"]["vapi_managed_count"] == 1
    assert result["elevenlabs"]["reported_limits"]["max_concurrent_dialogue_sessions"] == 21
    assert result["ten_real_calls_verified"] is False
    assert result["calls_started"] == result["purchases"] == 0


def test_missing_keys_do_not_send_requests():
    def forbidden(request):
        raise AssertionError("Unexpected provider request")
    with httpx.Client(transport=httpx.MockTransport(forbidden)) as client:
        result = check({}, client)
    assert result["vapi"]["key_present"] is False


def test_failures_do_not_leak_provider_bodies():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
            403, text="sensitive-provider-diagnostic"))) as client:
        result = check({"VAPI_API_KEY": "private"}, client)
    assert result["vapi"]["organization"]["http_status"] == 403
    assert "sensitive-provider-diagnostic" not in json.dumps(result)


def test_malformed_or_unavailable_quota_is_unknown():
    assert numeric_fields({"limits": {"concurrencyLimit": True}}, ["concurrencyLimit"]) == {}
    assert numeric_fields({"limits": {"concurrencyLimit": "10"}}, ["concurrencyLimit"]) == {}
    assert numeric_fields(None, ["concurrencyLimit"]) == {}


def test_network_error_is_sanitized():
    def fail(request):
        raise httpx.ConnectError("do-not-echo-secret", request=request)
    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        result = check({"GEMINI_API_KEY": "private"}, client)
    assert result["gemini"]["model_lookup_http_status"] == "network_error"
    assert "do-not-echo" not in json.dumps(result)
