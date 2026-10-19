"""Bounded webhook parsing, independent of the listener's middleware stack."""
from __future__ import annotations

import asyncio
import json

from fastapi import HTTPException, Request
from starlette.requests import ClientDisconnect

MAX_WEBHOOK_BYTES = 262144
WEBHOOK_BODY_TIMEOUT_SECONDS = 5.0


async def read_json_body(request: Request):
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            length = int(declared)
        except ValueError:
            raise HTTPException(400, "Invalid content length.")
        if length < 0:
            raise HTTPException(400, "Invalid content length.")
        if length > MAX_WEBHOOK_BYTES:
            raise HTTPException(413, "Request too large.")

    async def collect():
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_WEBHOOK_BYTES:
                raise HTTPException(413, "Request too large.")
            data.extend(chunk)
        return data

    try:
        raw = await asyncio.wait_for(collect(), timeout=WEBHOOK_BODY_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        raise HTTPException(408, "Webhook body read timed out.")
    except ClientDisconnect:
        raise HTTPException(400, "Incomplete webhook body.")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise HTTPException(400, "Invalid JSON.")


# Compatibility name for the webhook; the authenticated LLM gateway can share
# exactly the same streaming cap and deadline through read_json_body().
read_webhook_json = read_json_body
