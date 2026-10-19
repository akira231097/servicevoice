"""Local operator boundaries and workflow; no external account or voice calls."""
import importlib
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from test_api import client, headers, new_session, tool, TEST_SECRET


def operator_headers(client):
    response = client.get("/api/operator/capability")
    assert response.status_code == 200
    return {"x-operator-token": response.json()["token"]}


def make_case(client):
    session = new_session(client)
    result = tool(client, session, "request_handoff", {"reason": "Needs a price verified by an advisor."})
    assert result.status_code == 200 and result.json()["ok"]
    return session, result.json()["id"]


def change(revision=0, status="in_progress", **values):
    return {"expected_revision": revision, "status": status, "operator": "Demo operator", "note": "", **values}


def test_caller_withdrawal_disappears_from_open_inbox_and_operator_cannot_reopen(client):
    caller,case_id=make_case(client)
    access=operator_headers(client)
    result=tool(client,caller,"withdraw_handoff",{"handoff_id":case_id},call_id="withdraw-request").json()
    assert result["ok"] and result["status"]=="withdrawn",result
    assert client.get("/api/operator/cases",headers=access).json()["items"]==[]
    case=client.get(f"/api/operator/cases/{case_id}",headers=access).json()
    assert case["status"]=="resolved" and case["handoff_status"]=="withdrawn"
    response=client.post(f"/api/operator/cases/{case_id}/status",headers=access,json=change(status="open"))
    assert response.status_code==409 and response.json()["error"]=="advisor_case_withdrawn"
    assert client.get(f"/api/sessions/{caller['id']}",headers=headers(caller)).json()["handoff"]["status"]=="withdrawn"


def test_case_survives_session_end_and_operator_work_does_not_mutate_scheduler(client):
    session, case_id = make_case(client)
    caller_receipt = client.get(f"/api/sessions/{session['id']}", headers=headers(session)).json()["handoff"]
    assert client.post(f"/api/sessions/{session['id']}/end", headers=headers(session)).status_code == 200
    operator = operator_headers(client)
    listing = client.get("/api/operator/cases", headers=operator).json()
    assert [row["id"] for row in listing["items"]] == [case_id]
    assert listing["items"][0]["status"] == "open"
    detail = client.get(f"/api/operator/cases/{case_id}", headers=operator).json()
    assert detail["context"]["session"]["ended"] is True
    assert session["token"] not in str(detail)
    assert "token_hash" not in str(detail)
    claim = client.post(f"/api/operator/cases/{case_id}/status", headers=operator, json=change())
    assert claim.status_code == 200
    assert claim.json()["revision"] == 1
    closed = client.post(f"/api/operator/cases/{case_id}/status", headers=operator,
                         json=change(1, "resolved", note="Reviewed locally. No call was made."))
    assert closed.status_code == 200
    assert closed.json()["status"] == "resolved"
    assert len(closed.json()["audit"]) == 2
    assert client.get("/api/operator/cases", headers=operator).json()["items"] == []
    assert len(client.get("/api/operator/cases?status=resolved", headers=operator).json()["items"]) == 1
    assert client.get(f"/api/sessions/{session['id']}", headers=headers(session)).json()["handoff"] == caller_receipt
    with client.app.state.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0


@pytest.mark.parametrize("credential", ["none", "caller_header", "caller_as_operator", "provider"])
def test_caller_and_provider_credentials_do_not_authorize_operator_access(client, credential):
    session, case_id = make_case(client)
    supplied = {"none": {}, "caller_header": headers(session),
                "caller_as_operator": {"x-operator-token": session["token"]},
                "provider": {"authorization": "Bearer " + TEST_SECRET}}[credential]
    assert client.get("/api/operator/cases", headers=supplied).status_code == 401
    assert client.get(f"/api/operator/cases/{case_id}", headers=supplied).status_code == 401
    assert client.post(f"/api/operator/cases/{case_id}/status", headers=supplied, json=change()).status_code == 401


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "evil.example.invalid"])
def test_public_listener_never_has_operator_routes_even_with_credential_or_host_spoof(client, host):
    module = importlib.import_module("app.main")
    operator = {**operator_headers(client), "host": host}
    with TestClient(module.create_public_app(client.app)) as public:
        for path in ("/advisor", "/static/advisor.html", "/api/operator/capability", "/api/operator/cases", "/api/operator/cases/fake"):
            assert public.get(path, headers=operator).status_code == 404
        assert public.post("/api/operator/cases/fake/status", headers=operator, json=change()).status_code == 404


@pytest.mark.parametrize("unsafe", [
    {"origin": "https://evil.example.invalid"}, {"origin": "null"},
    {"origin": "http://testserver:8767"}, {"sec-fetch-site": "cross-site"},
    {"sec-fetch-site": "same-site"}, {"host": "rebind.example.invalid"},
])
def test_capability_and_cases_reject_cross_origin_and_cross_site(client, unsafe):
    operator = {**operator_headers(client), **unsafe}
    assert client.get("/api/operator/capability", headers=unsafe).status_code == 403
    assert client.get("/api/operator/cases", headers=operator).status_code == 403
    assert client.post("/api/operator/cases/fake/status", headers=operator, json=change()).status_code == 403


def test_capability_not_cacheable_or_frameable_and_cross_origin_preflight_denied(client):
    response = client.get("/api/operator/capability", headers={"origin": "http://testserver", "sec-fetch-site": "same-origin"})
    assert response.status_code == 200
    assert len(response.json()["token"]) >= 40
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "set-cookie" not in response.headers
    preflight = client.options("/api/operator/cases", headers={"origin": "https://evil.example.invalid", "access-control-request-method": "GET"})
    assert preflight.status_code == 403
    assert "access-control-allow-origin" not in preflight.headers


def test_capability_rotates_between_app_instances_and_cases_persist(client):
    _, case_id = make_case(client)
    old = operator_headers(client)
    module = importlib.import_module("app.main")
    app2 = module.create_app(client.app.state.store.path)
    with TestClient(app2) as restarted:
        assert restarted.get("/api/operator/cases", headers=old).status_code == 401
        current = operator_headers(restarted)
        assert current != old
        assert restarted.get(f"/api/operator/cases/{case_id}", headers=current).status_code == 200


def test_duplicate_and_non_ascii_operator_headers_rejected_without_server_error(client):
    operator = operator_headers(client)
    assert client.get("/api/operator/cases", headers=list(operator.items()) * 2).status_code == 401
    assert client.get("/api/operator/cases", headers=[(b"x-operator-token", b"\xff")]).status_code == 401


def test_missing_case_has_clear_not_found_response_after_authorization(client):
    operator = operator_headers(client)
    response = client.get("/api/operator/cases/missing", headers=operator)
    assert response.status_code == 404
    assert response.json()["error"] == "advisor_case_not_found"
    assert client.post("/api/operator/cases/missing/status", headers=operator, json=change()).status_code == 404
    assert client.get("/api/operator/cases/missing").status_code == 401


@pytest.mark.parametrize("patch", [{"expected_revision": True}, {"expected_revision": "0"},
                                   {"status": "called"}, {"operator": "x" * 81},
                                   {"note": "x" * 2001}, {"tenant": "other"}])
def test_invalid_update_schema_has_no_effect(client, patch):
    _, case_id = make_case(client)
    operator = operator_headers(client)
    response = client.post(f"/api/operator/cases/{case_id}/status", headers=operator, json=change(**patch))
    assert response.status_code == 422
    assert client.get(f"/api/operator/cases/{case_id}", headers=operator).json()["revision"] == 0


def test_resolution_requires_note_and_lost_response_is_reconciled_not_replayed(client):
    _, case_id = make_case(client)
    operator = operator_headers(client)
    path = f"/api/operator/cases/{case_id}/status"
    assert client.post(path, headers=operator, json=change(status="resolved", note="   ")).status_code == 409
    saved = client.post(path, headers=operator, json=change())
    assert saved.status_code == 200
    # Simulate losing the successful HTTP response, then submitting the same revision.
    retry = client.post(path, headers=operator, json=change())
    assert retry.status_code == 409
    assert retry.json()["error"] == "advisor_revision_conflict"
    detail = client.get(f"/api/operator/cases/{case_id}", headers=operator).json()
    assert detail["revision"] == 1 and len(detail["audit"]) == 1


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "status=called", "before=" + "x" * 513])
def test_list_query_bounds(client, query):
    assert client.get("/api/operator/cases?" + query, headers=operator_headers(client)).status_code == 422


def test_storage_unavailable_does_not_claim_action_failed_or_retry(client, monkeypatch):
    operator = operator_headers(client)
    def unavailable(**kwargs):
        raise sqlite3.OperationalError("synthetic storage failure; must not leak")
    monkeypatch.setattr(client.app.state.advisor_queue, "list_cases", unavailable)
    response = client.get("/api/operator/cases", headers=operator)
    assert response.status_code == 503
    assert "reconcile" in response.json()["detail"]
    assert "synthetic" not in response.text


def test_real_network_nonloopback_peer_cannot_get_capability_even_with_local_host(client):
    import asyncio
    async def request():
        transport = httpx.ASGITransport(app=client.app, client=("203.0.113.8", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as remote:
            return await remote.get("/api/operator/capability", headers={"x-forwarded-for": "127.0.0.1"})
    assert asyncio.run(request()).status_code == 403
