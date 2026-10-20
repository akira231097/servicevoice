"""Reviewer health probes read the existing heartbeat without importing the app."""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from app import live_evaluations

DOCKERFILE = Path(__file__).resolve().parents[1] / "deploy/lightsail/Dockerfile.speech-evaluator"
LINE = next(line for line in DOCKERFILE.read_text(encoding="utf-8").splitlines() if line.startswith("HEALTHCHECK "))
COMMAND = json.loads(LINE.split(" CMD ", 1)[1])
PROGRAM = COMMAND[2]


def test_health_command_imports_only_stdlib_and_preserves_probe_intervals():
    assert COMMAND[:2] == ["python", "-c"] and len(COMMAND) == 3
    assert "--interval=30s --timeout=3s --start-period=60s --retries=3" in LINE
    imports = set()
    for node in ast.walk(ast.parse(PROGRAM)):
        if isinstance(node, ast.Import): imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom): imports.add(node.module)
    assert imports == {"json", "os", "time", "pathlib"}
    assert "app." not in PROGRAM and "http" not in PROGRAM


@pytest.mark.parametrize("case,expected", [
    ("fresh", 0), ("stale", 1), ("alert", 1), ("worker_error", 1),
    ("source_discovery_error", 1), ("provider_discovery_error", 1),
    ("missing_at", 1), ("malformed_json", 1), ("missing_file", 1),
    # Keep the app health CLI's exact current semantics; the deployment
    # readiness guard separately rejects future timestamps.
    ("future", 0),
])
def test_stdlib_probe_matches_existing_health_cli_without_constructing_worker(tmp_path, monkeypatch, case, expected):
    outbox = tmp_path / "custom-evaluation-location" / "outbox.sqlite3"
    outbox.parent.mkdir()
    health = {"at": time.time(), "alerts": [], "worker_error": None,
              "source_discovery_error": "", "provider_discovery_error": ""}
    if case == "stale": health["at"] -= 190
    elif case == "future": health["at"] += 30
    elif case == "alert": health["alerts"] = ["authored_problem"]
    elif case in {"worker_error", "source_discovery_error", "provider_discovery_error"}: health[case] = "authored_problem"
    elif case == "missing_at": del health["at"]
    if case != "missing_file":
        (outbox.parent / "health.json").write_text("malformed" if case == "malformed_json" else json.dumps(health))
    monkeypatch.setenv("SERVICEVOICE_EVAL_OUTBOX", str(outbox))
    monkeypatch.setattr(sys, "argv", ["reviewer", "--health"])
    monkeypatch.setattr(live_evaluations.os, "umask", lambda _: 0o077)
    monkeypatch.setattr(live_evaluations, "Worker", lambda *a, **k: pytest.fail("Health constructed a worker"))
    assert live_evaluations.main() == expected
    environment = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
    environment["SERVICEVOICE_EVAL_OUTBOX"] = str(outbox)
    result = subprocess.run([sys.executable, "-I", "-c", PROGRAM], env=environment,
                            cwd=tmp_path, capture_output=True, timeout=3)
    assert result.returncode == expected
    if expected == 0:
        assert result.stdout == result.stderr == b""
    assert not outbox.exists(), "Health must never construct/migrate an outbox database"
