"""Run the existing Python and Node regression suites with immutable evidence.

This entry point never runs the paid voice/model scripts or starts the demo.
The tests use temporary fixtures, mocked providers, and local subprocesses.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

from .run import ROOT, source_manifest, write_json


INPUT_POLICY = {
    "directories": ["app", "tests", "evals", "scripts", "integrations", "deploy"],
    "excluded_directory_names": ["__pycache__", "node_modules", ".pytest_cache", ".venv", "venv", ".git"],
    "excluded_files": "Credentials (.env and variants except .env.example), bytecode, logs and database sidecars; generated docs/runs and installed runtimes are outside the source roots.",
    "root_inputs": ["run.py", "runtime_sqlite.py", "pytest.ini", "Dockerfile", ".dockerignore", ".env.example", "package.json", "package-lock.json", "requirements*.txt"],
    "scope": "Code and test inputs within the declared project roots, including app/static/vendor bundles and fixture/dataset JSON. Installed dependency binaries are represented by lockfiles, not recursively hashed.",
}


def code_test_input_manifest(root=None):
    root = Path(root or ROOT).resolve()
    excluded = set(INPUT_POLICY["excluded_directory_names"])
    paths = set()
    for name in INPUT_POLICY["directories"]:
        directory = root / name
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            relative = path.relative_to(root)
            if excluded.intersection(relative.parts) or not path.is_file():
                continue
            if path.name.startswith(".env") and path.name != ".env.example":
                continue
            if path.suffix in {".pyc", ".pyo", ".log", ".sqlite3", ".db"} or path.name.endswith(("-wal", "-shm", "-journal")):
                continue
            paths.add(path)
    for pattern in INPUT_POLICY["root_inputs"]:
        paths.update(path for path in root.glob(pattern) if path.is_file())
    result = {}
    for path in sorted(paths):
        # Inputs escaping the project cannot silently enter a source snapshot.
        if not path.resolve().is_relative_to(root):
            raise ValueError("input_path_outside_project")
        result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def junit_counts(path):
    if not path.exists():
        raise ValueError("missing_junit")
    try:
        root = ET.parse(path).getroot()
        suites = [root] if root.tag == "testsuite" else root.findall("testsuite") if root.tag == "testsuites" else []
        if not suites:
            raise ValueError("invalid_junit")
        counts = {}
        for name in ("tests", "failures", "errors", "skipped"):
            values = [int(s.attrib.get(name, "0")) for s in suites]
            if any(v < 0 for v in values):
                raise ValueError("invalid_junit")
            counts[name] = sum(values)
        if counts["failures"] + counts["errors"] + counts["skipped"] > counts["tests"]:
            raise ValueError("invalid_junit")
        counts["passed"] = counts["tests"] - counts["failures"] - counts["errors"] - counts["skipped"]
        return counts
    except (ET.ParseError, OSError, TypeError, ValueError):
        raise ValueError("invalid_junit") from None


def node_counts(path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"# (tests|pass|fail|cancelled|skipped|todo) ([0-9]+)", line.strip())
        if match:
            if match[1] in values:
                raise ValueError("invalid_node_summary")
            values[match[1]] = int(match[2])
    required = {"tests", "pass", "fail", "cancelled", "skipped", "todo"}
    if set(values) != required or sum(values[name] for name in required - {"tests"}) != values["tests"]:
        raise ValueError("missing_or_invalid_node_summary")
    return values


def evidence_status(label, counts):
    if counts["tests"] == 0:
        return "empty_suite"
    if label == "python":
        if counts["failures"] or counts["errors"]:
            return "failed"
        if counts["skipped"]:
            return "incomplete_skipped_tests"
    else:
        if counts["fail"] or counts["cancelled"]:
            return "failed"
        if counts["skipped"] or counts["todo"]:
            return "incomplete_skipped_or_todo_tests"
    return "passed"


def main():
    rid = str(uuid.uuid4())
    directory = ROOT / "docs/evals/runs" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-offline-" + rid)
    directory.mkdir(parents=True, exist_ok=False)
    before = source_manifest()
    inputs_before = code_test_input_manifest()
    node = shutil.which("node")
    node_tests = sorted(str(path.relative_to(ROOT)) for path in (ROOT / "tests").glob("*.mjs"))
    junit = directory / "python.xml"
    python_code = "from runtime_sqlite import ensure_sqlite; ensure_sqlite(); import pytest,sys; raise SystemExit(pytest.main(['-q','--junitxml='+sys.argv[1]]))"
    commands = [("python", [sys.executable, "-c", python_code, str(junit)])]
    if node and node_tests:
        commands.append(("node", [node, "--test", "--test-reporter=tap", *node_tests]))
    results = []
    for label, command in commands:
        start = time.perf_counter()
        log = directory / (label + ".log")
        with log.open("x", encoding="utf-8") as stream:
            try:
                completed = subprocess.run(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT,
                                           env={**os.environ, "SERVICEVOICE_LANGSMITH_ENABLED": "off"},
                                           timeout=600, check=False)
                row = {"suite": label, "exit_code": completed.returncode, "status": "passed" if completed.returncode == 0 else "failed"}
            except (OSError, subprocess.TimeoutExpired):
                row = {"suite": label, "exit_code": None, "status": "error_or_timeout"}
        row.update(duration_ms=round((time.perf_counter() - start)*1000, 2), log=log.name)
        try:
            row["counts"] = junit_counts(junit) if label == "python" else node_counts(log)
            row["evidence_status"] = evidence_status(label, row["counts"])
        except (ValueError, OSError) as exc:
            allowed = {"missing_junit", "invalid_junit", "invalid_node_summary", "missing_or_invalid_node_summary"}
            row["evidence_status"] = str(exc) if str(exc) in allowed else "unreadable_test_evidence"
        if row["status"] == "passed" and row["evidence_status"] != "passed":
            row["status"] = row["evidence_status"]
        results.append(row)
        print(json.dumps(row), flush=True)
    if not node or not node_tests:
        results.append({"suite": "node", "status": "not_run_missing_runtime_or_tests"})
    after = source_manifest()
    inputs_after = code_test_input_manifest()
    input_stable = bool(inputs_before) and inputs_before == inputs_after
    passed = bool(before) and before == after and input_stable and len(results) == 2 and all(r["status"] == "passed" for r in results)
    report = {"schema_version": 2, "run_id": rid, "scope": "offline_regressions", "source": {"before": before, "after": after},
              "code_test_inputs": {"policy": INPUT_POLICY, "before": inputs_before, "after": inputs_after, "stable": input_stable},
              "suites": results, "passed": passed, "release_ready": False,
              "limits": ["Offline checks do not establish model semantics, external provider behavior, microphone quality or production readiness.",
                         "Logs/JUnit preserve failures and skips; a skip is not a passing test and blocks this strict offline gate.",
                         "The code_test_inputs manifest is additional coverage in schema2 reports; earlier reports retain their original narrower source scope."]}
    write_json(directory / "offline-result.json", report, exclusive=True)
    print(json.dumps({"passed": passed, "report": str(directory / "offline-result.json")}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
