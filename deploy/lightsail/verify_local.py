"""Bounded isolated Docker edge acceptance. Synthetic credentials, no provider requests.

Requires already-built app/edge images. New containers/volumes/network only; never
recreates the existing keyless sandbox. Public ACME is validated without network;
actual HTTPS uses a disposable local CA, never installed in the host trust store.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import re
from pathlib import Path
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from uuid import uuid4

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]


def docker(*arguments, timeout=90, check=True):
    result = subprocess.run(["docker", *map(str, arguments)], capture_output=True, text=True,
                            timeout=timeout, env={**os.environ, "COMPOSE_DISABLE_ENV_FILE": "1"})
    if check and result.returncode:
        raise RuntimeError("docker_command_failed:" + str(arguments[0]))
    return result


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def request(url, *, context=None, body=None, headers=None, method=None):
    opener = urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({}),
                                        urllib.request.HTTPSHandler(context=context))
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        response = opener.open(req, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    return response


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--edge-image", default="servicevoice-edge:lightsail")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--scratch", required=True, type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        handle.write('{}\n')
    args.scratch.mkdir(parents=True, exist_ok=False)
    args.scratch = args.scratch.resolve()
    prefix = "sv-lightsail-check-" + uuid4().hex[:10]
    network = prefix + "-net"
    app, edge, fixture = [prefix + suffix for suffix in ("-app", "-edge", "-fixture")]
    volumes = [prefix + suffix for suffix in ("-data", "-tls", "-config")]
    created_containers, created_volumes = [], []
    network_created = False
    report = {"schema_version": 1, "passed": False, "checks": [], "limitations": [
        "Local Docker Desktop, not Lightsail; no AWS resources or public ACME issuance.",
        "Synthetic credentials and local CA; no provider API, phone call or audio capacity claim.",
        "FAQ classifier disabled in this credential-free fixture only; package defaults to on.",
        "Reboot, certificate renewal, provider quotas and real Vapi IP TLS remain unverified."]}

    def check(name, condition, **details):
        report["checks"].append({"name": name, "passed": bool(condition), **details})
        if not condition:
            raise AssertionError(name)

    def exec_python(container, source):
        return docker("exec", container, "python", "-c", source).stdout.strip()

    before = [json.loads(line) for line in docker("ps", "--format", "json").stdout.splitlines()]
    # Concurrent independent stress jobs can exit naturally. The preexisting
    # persistent local sandbox is the deployment state we promise to preserve.
    before_ids = sorted(row["ID"] for row in before if "servicevoice-local" in row["Names"])
    try:
        app_id = json.loads(docker("image", "inspect", args.image).stdout)[0]["Id"]
        edge_id = json.loads(docker("image", "inspect", args.edge_image).stdout)[0]["Id"]
        report.update(application_image=app_id, edge_image=edge_id,
                      source_sha256={path.name: digest(path) for path in sorted(HERE.iterdir()) if path.is_file()})
        linux_tests = docker("run", "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp:rw,size=32m",
                             "--entrypoint", "python", "--mount", f"type=bind,src={HERE},dst=/checks/deploy/lightsail,readonly",
                             app_id, "/checks/deploy/lightsail/test_package.py")
        (args.scratch / "package-unit-tests.txt").write_text(linux_tests.stdout + linux_tests.stderr, encoding="utf-8")
        unit_output=linux_tests.stdout+linux_tests.stderr
        unit_count=re.search(r"Ran (\d+) tests?",unit_output)
        unit_skips=re.search(r"skipped=(\d+)",unit_output)
        test_count=int(unit_count.group(1)) if unit_count else 0
        skips=int(unit_skips.group(1)) if unit_skips else 0
        check("linux_offline_tests", linux_tests.returncode == 0 and test_count > 0 and skips == 0,
              test_count=test_count, skips=skips)
        unit_hashes = json.loads(docker("run", "--rm", "--network", "none", "--read-only", "--entrypoint", "python", app_id,
            "-c", "import hashlib,json;from pathlib import Path;print(json.dumps({str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path('app/capacity.py'),Path('app/main.py'),Path('app/gemini_gateway.py'),Path('app/telephony_ingress.py')]}))").stdout)
        check("image_matches_current_capacity_sources", all(digest(PROJECT / path) == value for path, value in unit_hashes.items()))
        report["application_sources"] = unit_hashes
        common_caddy = ["--read-only", "--tmpfs", "/tmp:rw,size=16m", "--mount", f"type=bind,src={HERE / 'Caddyfile'},dst=/etc/caddy/Caddyfile,readonly",
                        "--env", "SERVICEVOICE_ACME_EMAIL=synthetic@cedar.invalid"]
        validation = docker("run", "--rm", "--network", "none", *common_caddy,
                            "--mount", f"type=bind,src={HERE / 'tls.acme.caddy'},dst=/etc/caddy/tls.caddy,readonly",
                            "--env", "SERVICEVOICE_PUBLIC_HOST=8.8.8.8", edge_id,
                            "caddy", "validate", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile")
        check("public_ip_acme_config_valid_without_network", "Valid configuration" in validation.stdout + validation.stderr)
        synthetic = {"GEMINI_API_KEY": "synthetic-google-key-never-used-0123456789",
                     "GEMINI_GATEWAY_SECRET": "synthetic-gateway-" + "g" * 32,
                     "VAPI_PHONE_WEBHOOK_SECRET": "synthetic-phone-" + "p" * 32,
                     "VAPI_PHONE_ORG_ID": "11111111-1111-4111-8111-111111111111",
                     "VAPI_PHONE_NUMBER_ID": "22222222-2222-4222-8222-222222222222",
                     "VAPI_PHONE_ASSISTANT_ID": "33333333-3333-4333-8333-333333333333"}
        private = args.scratch / "synthetic.env"
        private.write_text("".join(f"{key}={value}\n" for key, value in synthetic.items()), encoding="ascii")
        settings = args.scratch / "settings.env"
        settings.write_text(f"SERVICEVOICE_PUBLIC_HOST=8.8.8.8\nSERVICEVOICE_ACME_EMAIL=synthetic@cedar.invalid\nSERVICEVOICE_PRIVATE_ENV_FILE={private.as_posix()}\nSERVICEVOICE_JEV_FAQ=off\n", encoding="ascii")
        compose = json.loads(docker("compose", "--project-name", prefix, "--env-file", settings,
                                    "-f", HERE / "compose.yaml", "config", "--format", "json").stdout)
        services = compose["services"]
        check("compose_app_private_single_provider_process", not services["servicevoice"].get("ports")
              and services["servicevoice"]["command"] == ["--mode", "provider"])
        check("compose_resources_and_restart", int(services["servicevoice"]["mem_limit"]) == 1024**3
              and float(services["servicevoice"]["cpus"]) == 1.0 and services["servicevoice"]["restart"] == "unless-stopped"
              and services["servicevoice"]["environment"]["SERVICEVOICE_CAPACITY_PROFILE"] == "default")
        check("compose_only_public_http_https", {int(p["published"]) for p in services["caddy"]["ports"]} == {80, 443})
        check("compose_edge_has_no_provider_secrets", not (set(synthetic) & set(services["caddy"]["environment"])))
        docker("network", "create", network); network_created = True
        for volume in volumes:
            docker("volume", "create", volume); created_volumes.append(volume)
        runtime_env = {**synthetic, "SERVICEVOICE_CAPACITY_PROFILE": "default", "SERVICEVOICE_TELEPHONY_ENABLED": "on",
                       "SERVICEVOICE_RECEIPT_NARRATION": "on", "SERVICEVOICE_JEV_FAQ": "off",
                       "VAPI_MODEL_PROFILE": "gemini-3.8-flash-custom", "PUBLIC_BASE_URL": "https://127.0.0.1",
                       "VAPI_CUSTOM_LLM_URL": "https://127.0.0.1/llm"}
        env_args = [item for key, value in runtime_env.items() for item in ("--env", f"{key}={value}")]
        docker("run", "-d", "--name", app, "--network", network, "--network-alias", "servicevoice",
               "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=32m", "--user", "10001:10001",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--memory", "1g", "--cpus", "1.0",
               "--pids-limit", "192", "--init", "--volume", f"{volumes[0]}:/data", *env_args, app_id, "--mode", "provider")
        created_containers.append(app)
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            status = docker("exec", app, "python", "-c", "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/readyz',timeout=1).status)", check=False)
            if status.returncode == 0: break
            time.sleep(.3)
        check("application_private_readiness", status.returncode == 0 and status.stdout.strip() == "200")
        limits = json.loads(exec_python(app, "import json,os;from app.capacity import CapacityConfig;print(json.dumps(CapacityConfig.from_mapping(os.environ).public_limits()))"))
        check("runtime_four_call_config_loaded", limits["max_live_calls"] == 4 and limits["gateway_concurrency"] == 4 and limits["phone_http_capacity"] == 4)
        report["runtime_capacity"] = limits
        internal_tls = args.scratch / "tls.internal.caddy"
        internal_tls.write_text("tls internal\n", encoding="ascii")
        docker("run", "-d", "--name", edge, "--network", network, *common_caddy,
               "--mount", f"type=bind,src={internal_tls},dst=/etc/caddy/tls.caddy,readonly",
               "--env", "SERVICEVOICE_PUBLIC_HOST=127.0.0.1", "--volume", f"{volumes[1]}:/data", "--volume", f"{volumes[2]}:/config",
               "--publish", "127.0.0.1::8080", "--publish", "127.0.0.1::8443", "--user", "10001:10001",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--memory", "192m", "--cpus", ".25", edge_id)
        created_containers.append(edge)
        inspect = json.loads(docker("inspect", edge).stdout)[0]
        ports = inspect["NetworkSettings"]["Ports"]
        http_port = int(ports["8080/tcp"][0]["HostPort"])
        https_port = int(ports["8443/tcp"][0]["HostPort"])
        check("fixture_ports_loopback_only", all(value[0]["HostIp"] == "127.0.0.1" for value in ports.values() if value)
              and not ({http_port, https_port} & {8765, 8766, 8875, 8876}))
        certificate = args.scratch / "local-ca.pem"
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            copied = docker("cp", f"{edge}:/data/caddy/pki/authorities/local/root.crt", certificate, check=False)
            if copied.returncode == 0: break
            time.sleep(.2)
        check("local_ca_created_no_host_trust_install", copied.returncode == 0)
        context = ssl.create_default_context(cafile=certificate)
        base = f"https://127.0.0.1:{https_port}"
        with request(base + "/healthz", context=context) as response:
            check("https_ip_certificate_and_liveness", response.status == 200 and response.read(128) == b'{"status":"ok"}')
        with request(f"http://127.0.0.1:{http_port}/healthz") as response:
            check("redirect_uses_public_https_443", response.status in (301, 308) and response.headers.get("Location") == "https://127.0.0.1/healthz",
                  redirect_location=response.headers.get("Location"))
        for path in ("/", "/advisor", "/api/config", "/api/operator/capability", "/api/sessions", "/docs", "/openapi.json", "/static/app.js", "/readyz", "/webhooks/vapi"):
            with request(base + path, context=context) as response:
                check("private_route_rejected:" + path, response.status == 404)
        for path in ("/webhooks/vapi/inbound", "/llm/chat/completions"):
            with request(base + path, context=context, body=b"{}", headers={"Content-Type": "application/json"}) as response:
                check("provider_auth_required:" + path, response.status == 401)
            with request(base + path, context=context) as response:
                check("provider_get_rejected:" + path, response.status == 404)
        call_id = str(uuid4())
        payload = {"message": {"type": "assistant-request", "call": {"id": call_id, "type": "inboundPhoneCall",
                   "orgId": synthetic["VAPI_PHONE_ORG_ID"], "phoneNumberId": synthetic["VAPI_PHONE_NUMBER_ID"]}}}
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + synthetic["VAPI_PHONE_WEBHOOK_SECRET"]}
        with request(base + "/webhooks/vapi/inbound", context=context, body=json.dumps(payload).encode(), headers=headers) as response:
            admitted = json.loads(response.read(4096))
            check("authenticated_synthetic_phone_admission", response.status == 200 and admitted.get("assistantId") == synthetic["VAPI_PHONE_ASSISTANT_ID"])
        with request(base + "/webhooks/vapi/inbound", context=context, body=json.dumps(payload).encode(), headers=headers) as response:
            check("exact_admission_retry_same_binding", json.loads(response.read(4096)) == admitted)
        count_script = "import sqlite3,json;db=sqlite3.connect('file:/data/servicevoice.sqlite3?mode=ro',uri=True);print(json.dumps({t:db.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ['sessions','telephony_bindings','appointments','handoffs']}))"
        counts = json.loads(exec_python(app, count_script))
        check("admission_no_business_effect", counts == {"sessions": 1, "telephony_bindings": 1, "appointments": 0, "handoffs": 0})
        docker("stop", "--time", "40", app, timeout=55)
        stopped = json.loads(docker("inspect", app).stdout)[0]
        check("graceful_shutdown_no_oom", stopped["State"]["ExitCode"] in (0, 143) and not stopped["State"]["OOMKilled"]
              and "Application shutdown complete" in docker("logs", app).stderr)
        docker("start", app)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            ready = docker("exec", app, "python", "deploy/runtime_health.py", check=False)
            if ready.returncode == 0: break
            time.sleep(.2)
        check("restart_preserves_binding_and_no_booking", json.loads(exec_python(app, count_script)) == counts)
        exec_python(app, "from pathlib import Path;Path('/data/backups').mkdir();Path('/data/restores').mkdir()")
        for operation in (["backup", "--source", "/data/servicevoice.sqlite3", "--destination", "/data/backups/check.sqlite3"],
                          ["verify", "--source", "/data/backups/check.sqlite3"],
                          ["restore", "--source", "/data/backups/check.sqlite3", "--destination", "/data/restores/check.sqlite3"]):
            result = docker("exec", app, "python", "scripts/database_backup.py", *operation, "--timeout", "10")
            check("linux_database_" + operation[0], result.returncode == 0)
        docker("stop", "--time", "40", app, timeout=55)
        docker("network", "disconnect", network, app)
        docker("run", "-d", "--name", fixture, "--network", network, "--network-alias", "servicevoice",
               "--read-only", "--tmpfs", "/tmp:rw,size=16m", "--user", "10001:10001", "--cap-drop", "ALL",
               "--mount", f"type=bind,src={HERE / 'edge_fixture.py'},dst=/fixture.py,readonly", "--entrypoint", "python", app_id, "/fixture.py")
        created_containers.append(fixture)
        time.sleep(.5)
        start = time.monotonic()
        with request(base + "/llm/chat/completions", context=context, body=b"{}") as response:
            first = response.readline()
            first_ms = (time.monotonic() - start) * 1000
            remainder = response.read(16_384)
        total_ms = (time.monotonic() - start) * 1000
        check("sse_first_frame_before_upstream_completion", first.startswith(b"data:") and first_ms < 1500 and total_ms > 2000,
              first_frame_ms=round(first_ms, 1), completion_ms=round(total_ms, 1))
        response = request(base + "/llm/chat/completions", context=context, body=b"{}")
        response.readline()
        response.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            observed = json.loads(exec_python(fixture, "from pathlib import Path;print(Path('/tmp/edge-fixture.json').read_text())"))
            if observed["disconnected"]: break
            time.sleep(.2)
        check("downstream_close_cancels_upstream_sse", observed["disconnected"] == 1 and observed["completed"] == 1)
        def complete_stream(_):
            with request(base + "/llm/chat/completions", context=context, body=b"{}") as stream:
                body = stream.read(16_384)
                return stream.status == 200 and body.count(b"data:") == 24
        with ThreadPoolExecutor(max_workers=4) as pool:
            concurrent_results = list(pool.map(complete_stream, range(4)))
        check("four_concurrent_tls_sse_streams_no_reset", all(concurrent_results), streams=4)
        report["passed"] = True
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "code": str(error)[:160] if isinstance(error, AssertionError) else "local_verification_failed"}
        if edge in created_containers:
            diagnostic = docker("logs", "--tail", "80", edge, check=False)
            (args.scratch / "edge-diagnostic.txt").write_text(diagnostic.stdout + diagnostic.stderr, encoding="utf-8")
            state = json.loads(docker("inspect", edge).stdout)[0]["State"]
            report["edge_failure_state"] = {key: state[key] for key in ("Status", "ExitCode", "OOMKilled")}
    finally:
        cleanup = []
        for name in reversed(created_containers):
            stopped = docker("stop", "--time", "45", name, timeout=60, check=False)
            removed = docker("rm", name, check=False)
            cleanup.append(removed.returncode == 0)
        if network_created:
            cleanup.append(docker("network", "rm", network, check=False).returncode == 0)
        for volume in created_volumes:
            cleanup.append(docker("volume", "rm", volume, check=False).returncode == 0)
        current_ids = docker("ps", "--format", "{{.ID}}").stdout.splitlines()
        report["cleanup"] = {"owned_resources_removed": all(cleanup), "preexisting_containers_still_running": all(value in current_ids for value in before_ids)}
        report["passed"] = report["passed"] and all(report["cleanup"].values())
        report["checks_passed"] = sum(row["passed"] for row in report["checks"])
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "checks_passed": report["checks_passed"], "error": report.get("error")}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
