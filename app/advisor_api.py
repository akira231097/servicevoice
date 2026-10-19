"""Loopback-only operator view; this is a local capability, not staff identity."""
from __future__ import annotations

import ipaddress
import secrets
import sqlite3
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .advisor_queue import AdvisorQueue
from .domain import DomainError


class CaseUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=0)
    status: Literal["open", "in_progress", "resolved"]
    operator: str = Field(min_length=1, max_length=80)
    note: str = Field(default="", max_length=2000)


def install_advisor_api(app, store, static_root: Path):
    queue = AdvisorQueue(store)
    # This process-local capability is intentionally distinct from caller and
    # provider credentials. It rotates on restart and never goes in a URL/log.
    capability = secrets.token_urlsafe(32)
    app.state.advisor_queue = queue

    def local_access(request: Request):
        client = request.client.host if request.client else ""
        try:
            loopback = ipaddress.ip_address(client).is_loopback
        except ValueError:
            loopback = client == "testclient"  # Starlette's in-process test transport.
        allowed_hosts = {"127.0.0.1", "localhost", "::1"}
        if client == "testclient":
            allowed_hosts.add("testserver")
        if not loopback or request.url.hostname not in allowed_hosts:
            raise HTTPException(403, "The advisor inbox is available on this computer only.")
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Cross-origin operator access is not allowed.")
        if request.headers.get("sec-fetch-site", "") not in ("", "same-origin", "none"):
            raise HTTPException(403, "Open the advisor inbox directly on this computer.")

    def operator_access(request: Request):
        local_access(request)
        supplied = request.headers.getlist("x-operator-token")
        if len(supplied) != 1 or not secrets.compare_digest(supplied[0].encode("utf-8"), capability.encode("ascii")):
            raise HTTPException(401, "Advisor access expired. Refresh and review the latest case before updating it.")

    router = APIRouter()

    @app.middleware("http")
    async def advisor_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/advisor" or request.url.path.startswith(("/api/operator/", "/static/advisor")):
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        return response

    @router.get("/advisor", dependencies=[Depends(local_access)])
    def advisor_page():
        return FileResponse(static_root / "advisor.html")

    @router.get("/api/operator/capability", dependencies=[Depends(local_access)])
    def operator_capability():
        return {"token": capability}

    def guarded(action):
        try:
            return action()
        except DomainError as exc:
            if exc.code == "advisor_case_not_found":
                return JSONResponse({"detail": exc.message, "error": exc.code}, status_code=404)
            if exc.code == "advisor_case_withdrawn":
                return JSONResponse({"detail": exc.message, "error": exc.code}, status_code=409)
            raise
        except sqlite3.Error:
            # A write may have committed before a transport/storage error. Never
            # claim it failed or replay the operator's action automatically.
            raise HTTPException(503, "Case storage is unavailable. Refresh to reconcile before trying an update again.") from None

    @router.get("/api/operator/cases", dependencies=[Depends(operator_access)])
    def cases(status: Literal["open", "in_progress", "resolved", "all"] = "open",
              limit: int = Query(default=50, ge=1, le=100),
              before: str | None = Query(default=None, max_length=512)):
        return guarded(lambda: queue.list_cases(status=status, limit=limit, before=before))

    @router.get("/api/operator/cases/{case_id}", dependencies=[Depends(operator_access)])
    def case_detail(case_id: str):
        return guarded(lambda: queue.get_case(case_id))

    @router.post("/api/operator/cases/{case_id}/status", dependencies=[Depends(operator_access)])
    def case_status(case_id: str, body: CaseUpdate):
        return guarded(lambda: queue.update_case(case_id, **body.model_dump()))

    app.include_router(router)
