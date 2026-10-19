"""Liveness probe only. /readyz separately checks mode configuration and DB."""
import sys
import urllib.request

try:
    with urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2) as response:
        healthy = response.status == 200 and response.read(128) == b'{"status":"ok"}'
except Exception:
    healthy = False
raise SystemExit(0 if healthy else 1)
