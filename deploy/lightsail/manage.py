"""Explicit local Linux administration; no AWS API or provider provisioning."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from preflight import PROJECT, PUBLIC_KEYS, PreflightError, validate

HERE = Path(__file__).resolve().parent


def compose(settings_path):
    return ["docker", "compose", "--project-name", "servicevoice-lightsail", "--env-file", str(settings_path), "-f", str(HERE / "compose.yaml")]


def run(arguments, *, capture=False, timeout=120):
    # Never use a shell or echo resolved Compose configuration (it contains env values).
    # Shell variables must not override the settings file that preflight validated.
    environment = {key: value for key, value in os.environ.items() if key not in PUBLIC_KEYS}
    environment["COMPOSE_DISABLE_ENV_FILE"] = "1"
    result = subprocess.run(arguments, cwd=PROJECT, text=True, capture_output=capture,
                            timeout=timeout, env=environment)
    if result.returncode:
        raise RuntimeError("local_command_failed")
    return result.stdout if capture else None


def label(value):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", value or ""):
        raise PreflightError("new_backup_or_restore_label_required")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", required=True, type=Path)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("check", "build", "validate-edge", "up", "status", "ready", "stop", "logs"):
        commands.add_parser(action)
    for action in ("backup", "verify-backup", "restore"):
        item = commands.add_parser(action)
        item.add_argument("--label", required=True)
        if action == "restore": item.add_argument("--backup", required=True)
    args = parser.parse_args()
    try:
        settings, summary = validate(args.settings)
        if not sys.platform.startswith("linux"):
            raise RuntimeError("administration_requires_the_linux_vm")
        base = compose(args.settings)
        run(base + ["config", "--quiet"], capture=True)
        if args.action == "check":
            memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
            if memory < 1.5 * 1024**3 or (os.cpu_count() or 0) < 2:
                raise RuntimeError("requires_2gb_plan_with_two_vcpus")
            summary.update(host_memory_at_least_1_5g=True, host_cpu_at_least_two=True)
            print(json.dumps(summary, sort_keys=True))
        elif args.action == "build":
            run(base + ["build"], timeout=900)
        elif args.action == "validate-edge":
            run(base + ["run", "--rm", "--no-deps", "--entrypoint", "caddy", "caddy",
                        "validate", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"])
        elif args.action == "up":
            run(base + ["up", "-d", "--no-build"])
        elif args.action == "status":
            run(base + ["ps"])
        elif args.action == "stop":
            run(base + ["stop"], timeout=100)
        elif args.action == "logs":
            run(base + ["logs", "--tail", "100"])
        elif args.action == "ready":
            script = "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/readyz',timeout=2).read(128).decode())"
            run(base + ["exec", "-T", "servicevoice", "python", "-c", script], timeout=15)
        else:
            name = label(args.label)
            if args.action == "verify-backup":
                operation = ["verify", "--source", f"/data/backups/{name}.sqlite3"]
            else:
                directory = "/data/backups" if args.action == "backup" else "/data/restores"
                run(base + ["exec", "-T", "servicevoice", "python", "-c",
                            f"from pathlib import Path;Path({directory!r}).mkdir(exist_ok=True,mode=0o700)"])
                source = settings["SERVICEVOICE_DATABASE_PATH"] if args.action == "backup" else f"/data/backups/{label(args.backup)}.sqlite3"
                operation = [args.action, "--source", source, "--destination", f"{directory}/{name}.sqlite3"]
            run(base + ["exec", "-T", "servicevoice", "python", "scripts/database_backup.py", *operation, "--timeout", "30"], timeout=40)
        return 0
    except (PreflightError, OSError, RuntimeError, subprocess.TimeoutExpired):
        print(json.dumps({"ok": False, "error": "local_administration_failed_check_configuration_and_status"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
