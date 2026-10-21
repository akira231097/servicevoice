"""Run the isolated browser duplex console; private Vapi keys stay local.

The accepted booking/model/speech paths run in the existing candidate backend.
This localhost process authenticates its sessions through the SSH-forwarded
candidate console, allocates only that candidate's WebSocket calls and runs Jev.
"""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
import uvicorn
from dotenv import dotenv_values
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.duplex_api import install_duplex_api


def create_console(values, console_url, *, upstream=None):
    parsed = urlsplit(console_url)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path not in ("", "/")
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("A private loopback candidate console is required.")
    remote = upstream or httpx.Client(base_url=console_url, timeout=3, trust_env=False, follow_redirects=False)
    config = remote.get("/api/config")
    config.raise_for_status()
    config = config.json()
    candidate = values.get("VAPI_BACKCHANNEL_ASSISTANT_ID")
    if (not candidate or config.get("vapi_assistant_id") != candidate or not config.get("live_voice_available")):
        raise ValueError("The console must match the explicitly isolated candidate.")
    if config.get("duplex_browser_cue_control") != "browser-owned-v1":
        raise ValueError("The candidate gateway must support browser-owned cue control before starting calls.")
    options = {**values, "SERVICEVOICE_DUPLEX": "on", "VAPI_ASSISTANT_ID": candidate, "TODAY": config["today"]}

    def snapshot(sid, token, allow_ended=False):
        response = remote.get("/api/sessions/" + sid, headers={"x-session-token": token})
        if response.status_code != 200:
            raise HTTPException(response.status_code, "The isolated session could not be authorized.")
        result = response.json()
        if not allow_ended and result.get("ended"):
            raise HTTPException(409, "This session has ended.")
        return result

    @asynccontextmanager
    async def lifespan(app):
        await app.state.duplex.start()
        try:
            yield
        finally:
            await app.state.duplex.close()
            remote.close()

    app = FastAPI(title="ServiceVoice duplex experiment", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def private_browser(request: Request, call_next):
        # Loopback binding plus same-origin requests; no permissive CORS.
        if request.url.hostname not in ("127.0.0.1", "localhost"):
            return JSONResponse({"detail": "A loopback host is required."}, status_code=403)
        origin = request.headers.get("origin")
        expected = str(request.base_url).rstrip("/")
        if origin and origin != expected:
            return JSONResponse({"detail": "Same-origin operator requests are required."}, status_code=403)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                 "X-Content-Type-Options": "nosniff"})
        return response

    @app.get("/")
    @app.get("/duplex")
    def index():
        return FileResponse(ROOT / "app/static/duplex.html")

    @app.get("/api/config")
    def public_config():
        return config

    @app.post("/api/sessions")
    def new_session():
        response = remote.post("/api/sessions", json={"mode": "live"})
        if response.status_code != 200:
            raise HTTPException(response.status_code, "A candidate session could not be created.")
        return response.json()

    @app.get("/api/sessions/{sid}")
    def get_session(sid: str, x_session_token: str = Header(default="")):
        return snapshot(sid, x_session_token, True)

    @app.post("/api/sessions/{sid}/end")
    def end_session(sid: str, x_session_token: str = Header(default="")):
        snapshot(sid, x_session_token, True)
        if sid in app.state.duplex.sessions:
            app.state.duplex.sessions[sid].close()
        response = remote.post("/api/sessions/" + sid + "/end", headers={"x-session-token": x_session_token})
        return JSONResponse(response.json(), status_code=response.status_code)

    install_duplex_api(app, lambda: options, snapshot, snapshot)
    app.mount("/static", StaticFiles(directory=ROOT / "app/static"), name="static")
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--audio-manifest", type=Path, required=True)
    parser.add_argument("--console-url", default="http://127.0.0.1:8775")
    parser.add_argument("--port", type=int, default=8785)
    args = parser.parse_args()
    values = {**dotenv_values(args.env_file, interpolate=False),
              "VAPI_BACKCHANNEL_ASSISTANT_ID": args.candidate_id,
              "SERVICEVOICE_DUPLEX_CALL_LEDGER": str(ROOT / ".local/duplex-call-ledger.sqlite3"),
              "SERVICEVOICE_BACKCHANNEL_AUDIO_MANIFEST": str(args.audio_manifest.resolve())}
    try:
        app = create_console(values, args.console_url)
    except Exception:
        raise SystemExit("The private candidate console is unavailable or its identity does not match.") from None
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
