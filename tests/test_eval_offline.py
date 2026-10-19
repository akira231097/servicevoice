"""Offline orchestration with mocked subprocesses; no full-suite recursion."""
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from evals import offline


JUNIT = '<testsuites><testsuite tests="3" failures="0" errors="0" skipped="0"/></testsuites>'
TAP = "# tests 2\n# pass 2\n# fail 0\n# cancelled 0\n# skipped 0\n# todo 0\n"


def fixture_root(tmp_path):
    for name, data in {"tests/test_fixture.py": "# Python fixture", "tests/check.mjs": "// Node fixture",
                       "app/main.py": "# app source", "app/static/vendor/vapi.js": "// bundled SDK",
                       "evals/datasets/gold.json": "{}", "evals/coverage-registry.json": "{}",
                       "tests/fixtures/corpus.json": "[]", "requirements-lock.txt": "dependency==1.0",
                       "package-lock.json": "{}", "pytest.ini": "[pytest]\n",
                       ".env": "PRIVATE-SECRET", "app/.env.local": "PRIVATE-SECRET",
                       "app/__pycache__/x.pyc": "cache", "tests/node_modules/dep.js": "installed dependency"}.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data, encoding="utf-8")
    return tmp_path


def invoke(tmp_path, monkeypatch, *, junit=JUNIT, tap=TAP, python_exit=0, node_exit=0,
           missing_node=False, missing_node_tests=False, fail=None, drift=None):
    root = fixture_root(tmp_path)
    if missing_node_tests:
        (root / "tests/check.mjs").unlink()
    monkeypatch.setattr(offline, "ROOT", root)
    monkeypatch.setattr(offline, "source_manifest", lambda: {"app/main.py": "a" * 64})
    monkeypatch.setattr(offline.shutil, "which", lambda name: None if missing_node else "synthetic-node")
    calls = []
    def command(args, *, cwd, stdout, stderr, timeout, check, env):
        assert cwd == root and timeout == 600 and check is False
        assert env["SERVICEVOICE_LANGSMITH_ENABLED"] == "off"
        calls.append(args)
        is_python = len(calls) == 1
        if is_python:
            if junit is not None:
                Path(args[-1]).write_text(junit, encoding="utf-8")
            if drift:
                (root / drift).write_text("changed while tests ran", encoding="utf-8")
            if fail == "timeout":
                raise subprocess.TimeoutExpired(args, 600, output="PRIVATE-ERROR")
            if fail == "runtime":
                raise OSError("PRIVATE-ERROR")
            return SimpleNamespace(returncode=python_exit)
        assert "--test-reporter=tap" in args
        stdout.write(tap)
        return SimpleNamespace(returncode=node_exit)
    monkeypatch.setattr(offline.subprocess, "run", command)
    code = offline.main()
    reports = list((root / "docs/evals/runs").glob("*/offline-result.json"))
    assert len(reports) == 1
    return code, json.loads(reports[0].read_text(encoding="utf-8")), calls


def test_complete_manifest_includes_code_bundles_fixtures_and_locks_but_no_secrets(tmp_path):
    root = fixture_root(tmp_path)
    manifest = offline.code_test_input_manifest(root)
    required = {"app/main.py", "app/static/vendor/vapi.js", "tests/test_fixture.py", "tests/check.mjs",
                "tests/fixtures/corpus.json", "evals/datasets/gold.json", "evals/coverage-registry.json",
                "package-lock.json", "requirements-lock.txt", "pytest.ini"}
    assert set(manifest) == required
    assert all(len(value) == 64 for value in manifest.values())
    assert "PRIVATE" not in json.dumps(manifest)


def test_successful_runs_preserve_both_manifests_and_count_every_outcome(tmp_path, monkeypatch):
    code, report, calls = invoke(tmp_path, monkeypatch)
    assert code == 0 and report["passed"] is True and len(calls) == 2
    assert report["schema_version"] == 2 and report["code_test_inputs"]["stable"] is True
    assert report["code_test_inputs"]["before"] == report["code_test_inputs"]["after"]
    assert report["suites"][0]["counts"] == {"tests": 3, "failures": 0, "errors": 0, "skipped": 0, "passed": 3}
    assert report["suites"][1]["counts"]["pass"] == 2
    assert report["release_ready"] is False


@pytest.mark.parametrize("path", ["tests/test_fixture.py", "app/static/vendor/vapi.js", "evals/datasets/gold.json", "requirements-lock.txt"])
def test_input_drift_blocks_even_if_all_subprocesses_exit_zero(tmp_path, monkeypatch, path):
    code, report, _ = invoke(tmp_path, monkeypatch, drift=path)
    assert code == 1 and report["passed"] is False
    assert report["source"]["before"] == report["source"]["after"]
    assert report["code_test_inputs"]["stable"] is False


@pytest.mark.parametrize("option", ["missing_node", "missing_node_tests"])
def test_missing_node_capability_cannot_disappear_from_denominator(tmp_path, monkeypatch, option):
    code, report, calls = invoke(tmp_path, monkeypatch, **{option: True})
    assert code == 1 and len(calls) == 1
    assert report["suites"][-1]["status"] == "not_run_missing_runtime_or_tests"


@pytest.mark.parametrize("junit,status", [
    (None, "missing_junit"),
    ("broken XML", "invalid_junit"),
    ('<testsuites><testsuite tests="0" failures="0" errors="0" skipped="0"/></testsuites>', "empty_suite"),
    ('<testsuite tests="3" failures="1" errors="0" skipped="0"/>', "failed"),
    ('<testsuite tests="3" failures="0" errors="1" skipped="0"/>', "failed"),
    ('<testsuite tests="3" failures="0" errors="0" skipped="1"/>', "incomplete_skipped_tests"),
])
def test_missing_invalid_empty_failed_and_skipped_junit_do_not_green(tmp_path, monkeypatch, junit, status):
    code, report, _ = invoke(tmp_path, monkeypatch, junit=junit)
    assert code == 1 and report["suites"][0]["status"] == status
    if status == "incomplete_skipped_tests":
        assert report["suites"][0]["counts"]["skipped"] == 1
        assert report["suites"][0]["counts"]["passed"] == 2


@pytest.mark.parametrize("fail", ["runtime", "timeout"])
def test_runtime_failure_and_timeout_preserve_report_and_still_run_node(tmp_path, monkeypatch, fail):
    code, report, calls = invoke(tmp_path, monkeypatch, fail=fail)
    assert code == 1 and len(calls) == 2
    assert report["suites"][0]["status"] == "error_or_timeout"
    assert report["suites"][1]["status"] == "passed"
    assert "PRIVATE" not in json.dumps(report)


@pytest.mark.parametrize("tap,status", [
    ("", "missing_or_invalid_node_summary"),
    (TAP + "# tests 2\n", "invalid_node_summary"),
    ("# tests 0\n# pass 0\n# fail 0\n# cancelled 0\n# skipped 0\n# todo 0\n", "empty_suite"),
    ("# tests 2\n# pass 1\n# fail 1\n# cancelled 0\n# skipped 0\n# todo 0\n", "failed"),
    ("# tests 2\n# pass 1\n# fail 0\n# cancelled 1\n# skipped 0\n# todo 0\n", "failed"),
    ("# tests 2\n# pass 1\n# fail 0\n# cancelled 0\n# skipped 1\n# todo 0\n", "incomplete_skipped_or_todo_tests"),
])
def test_node_summary_outcomes_not_only_exit_code_control_gate(tmp_path, monkeypatch, tap, status):
    code, report, _ = invoke(tmp_path, monkeypatch, tap=tap)
    assert code == 1 and report["suites"][1]["status"] == status


@pytest.mark.parametrize("suite", ["python", "node"])
def test_nonzero_exit_cannot_be_overridden_by_successful_summary(tmp_path, monkeypatch, suite):
    code, report, _ = invoke(tmp_path, monkeypatch, **{suite + "_exit": 3})
    assert code == 1 and next(row for row in report["suites"] if row["suite"] == suite)["status"] == "failed"
