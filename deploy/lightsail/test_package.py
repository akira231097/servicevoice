"""Offline configuration/admin tests. No Docker, network or provider calls."""
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import preflight
import manage


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="servicevoice-lightsail-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "checkout"
        self.project.mkdir()
        self.settings = self.root / "settings.env"
        self.private = self.root / "private.env"
        self.public_values = {
            "SERVICEVOICE_PUBLIC_HOST": "8.8.8.8",
            "SERVICEVOICE_ACME_EMAIL": "operator@cedar.invalid",
            "SERVICEVOICE_PRIVATE_ENV_FILE": str(self.private),
        }
        self.private_values = {
            "GEMINI_API_KEY": "synthetic-google-" + "g" * 24,
            "GEMINI_GATEWAY_SECRET": "synthetic-gateway-" + "x" * 32,
            "VAPI_PHONE_WEBHOOK_SECRET": "synthetic-phone-" + "y" * 32,
            "VAPI_PHONE_ORG_ID": "00000000-0000-4000-8000-000000000001",
            "VAPI_PHONE_NUMBER_ID": "00000000-0000-4000-8000-000000000002",
            "VAPI_PHONE_ASSISTANT_ID": "00000000-0000-4000-8000-000000000003",
            "TYPESAFE_API_KEY": "synthetic-typesafe-" + "t" * 24,
        }
        self.write()

    def write(self):
        for path, values in ((self.settings, self.public_values), (self.private, self.private_values)):
            path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="ascii")
            path.chmod(0o600)

    def validate(self):
        return preflight.validate(self.settings, project=self.project)

    def test_public_ipv4_and_dns_but_not_private_or_injected_hosts(self):
        self.assertEqual(preflight.public_host("8.8.8.8"), "ipv4")
        self.assertEqual(preflight.public_host("voice.my-dealership.com"), "dns")
        for host in ("127.0.0.1", "10.0.0.1", "192.0.2.1", "224.0.0.1", "::1",
                     "8.8.8.8:443", "https://8.8.8.8", "example.com", "x.local", "x.test",
                     "foo.com\nrespond", "Foo.com", "foo.com/path", "foo.com:443"):
            with self.subTest(host=host), self.assertRaises(preflight.PreflightError):
                preflight.public_host(host)

    def test_default_faq_on_and_summary_has_no_credential_values(self):
        settings, summary = self.validate()
        self.assertEqual(settings["SERVICEVOICE_JEV_FAQ"], "on")
        self.assertEqual(settings["SERVICEVOICE_DATABASE_PATH"], "/data/servicevoice.sqlite3")
        combined = json.dumps([settings, summary])
        for value in self.private_values.values():
            self.assertNotIn(value, combined)
        self.assertFalse(summary["provider_credentials_verified"])
        self.assertFalse(summary["public_tls_verified"])

    def test_faq_on_requires_key_but_explicit_off_does_not(self):
        del self.private_values["TYPESAFE_API_KEY"]
        self.write()
        with self.assertRaisesRegex(preflight.PreflightError, "faq_on_requires"):
            self.validate()
        self.public_values["SERVICEVOICE_JEV_FAQ"] = "off"
        self.write()
        self.assertEqual(self.validate()[0]["SERVICEVOICE_JEV_FAQ"], "off")

    def test_content_free_tracing_is_opt_in_and_requires_its_own_key(self):
        self.assertEqual(self.validate()[0]["SERVICEVOICE_LANGSMITH_ENABLED"], "off")
        self.public_values["SERVICEVOICE_LANGSMITH_ENABLED"] = "on"
        self.write()
        with self.assertRaisesRegex(preflight.PreflightError, "tracing_on_requires"):
            self.validate()
        self.private_values["LANGSMITH_API_KEY"] = "synthetic-tracing-" + "s" * 32
        self.private_values["LANGSMITH_ENDPOINT"] = "https://api.smith.langchain.com"
        self.write()
        settings, summary = self.validate()
        self.assertEqual(settings["SERVICEVOICE_LANGSMITH_ENABLED"], "on")
        self.assertNotIn(self.private_values["LANGSMITH_API_KEY"], json.dumps([settings, summary]))
        self.private_values["LANGSMITH_ENDPOINT"] = "https://untrusted.invalid"
        self.write()
        with self.assertRaisesRegex(preflight.PreflightError, "official_tracing_endpoint"):
            self.validate()

    def test_unknown_or_duplicate_key_fails_without_echo(self):
        for extra in ("AWS_SECRET_ACCESS_KEY=do-not-read\n", "GEMINI_API_KEY=evil\n"):
            self.write()
            with self.private.open("a", encoding="ascii") as handle:
                handle.write(extra)
            with self.assertRaisesRegex(preflight.PreflightError, "unknown_or_duplicate"):
                self.validate()

    def test_missing_phone_identity_or_template_fails(self):
        del self.private_values["VAPI_PHONE_NUMBER_ID"]
        self.write()
        with self.assertRaisesRegex(preflight.PreflightError, "required_private"):
            self.validate()
        self.private_values["VAPI_PHONE_NUMBER_ID"] = "REPLACE_WITH_UUID"
        self.write()
        with self.assertRaisesRegex(preflight.PreflightError, "placeholder"):
            self.validate()

    def test_distinct_long_secrets_and_canonical_ids_required(self):
        self.private_values["VAPI_PHONE_WEBHOOK_SECRET"] = self.private_values["GEMINI_GATEWAY_SECRET"]
        self.write()
        with self.assertRaisesRegex(preflight.PreflightError, "independent"):
            self.validate()
        self.private_values["VAPI_PHONE_WEBHOOK_SECRET"] = "too-short"
        self.write()
        with self.assertRaisesRegex(preflight.PreflightError, "32_to_512"):
            self.validate()

    def test_raw_dollar_is_preserved_and_shell_quotes_rejected(self):
        self.private_values["GEMINI_GATEWAY_SECRET"] = "$not-expanded-" + "x" * 32
        self.write()
        self.validate()
        loaded = preflight.read_env(self.private, preflight.PRIVATE_KEYS, private=True)
        self.assertEqual(loaded["GEMINI_GATEWAY_SECRET"], self.private_values["GEMINI_GATEWAY_SECRET"])
        self.private_values["GEMINI_GATEWAY_SECRET"] = '"' + "x" * 32 + '"'
        self.write()
        with self.assertRaisesRegex(preflight.PreflightError, "raw_single_line"):
            self.validate()

    def test_public_compose_interpolation_is_rejected(self):
        self.public_values["SERVICEVOICE_ACME_EMAIL"] = "operator$UNREVIEWED@cedar.invalid"
        self.write()
        with self.assertRaisesRegex(preflight.PreflightError, "settings_interpolation"):
            self.validate()

    def test_database_restore_path_is_bounded_and_no_traversal(self):
        for path in ("/data/restores/recovered-20261018.sqlite3", "/data/servicevoice.sqlite3"):
            self.public_values["SERVICEVOICE_DATABASE_PATH"] = path
            self.write()
            self.validate()
        for path in ("/tmp/new.sqlite3", "/data/../old.sqlite3", "/data/a/b.sqlite3", "/data/file.db"):
            self.public_values["SERVICEVOICE_DATABASE_PATH"] = path
            self.write()
            with self.assertRaisesRegex(preflight.PreflightError, "explicit_data_volume"):
                self.validate()

    def test_settings_and_secrets_must_be_outside_checkout(self):
        with self.assertRaisesRegex(preflight.PreflightError, "settings_must"):
            preflight.validate(self.settings, project=self.root)
        self.public_values["SERVICEVOICE_PRIVATE_ENV_FILE"] = str(self.project / "private.env")
        self.write()
        with self.assertRaisesRegex(preflight.PreflightError, "private_environment"):
            self.validate()

    @unittest.skipUnless(os.name == "posix", "POSIX owner permissions checked in Linux image")
    def test_group_readable_secret_fails(self):
        self.private.chmod(0o640)
        with self.assertRaisesRegex(preflight.PreflightError, "owner_only"):
            self.validate()

    def test_oversized_file_rejected_before_parsing(self):
        self.private.write_bytes(b"#" * 16_385)
        with self.assertRaisesRegex(preflight.PreflightError, "too_large"):
            self.validate()

    def test_admin_never_uses_shell_and_removes_env_setting_overrides(self):
        with patch.dict(os.environ, {"SERVICEVOICE_PRIVATE_ENV_FILE": "/evil/file",
                                     "SERVICEVOICE_PUBLIC_HOST": "evil.com"}), \
             patch.object(manage.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="ok")) as invoke:
            self.assertEqual(manage.run(["docker", "version"], capture=True), "ok")
        kwargs = invoke.call_args.kwargs
        self.assertNotIn("shell", kwargs)
        self.assertNotIn("SERVICEVOICE_PRIVATE_ENV_FILE", kwargs["env"])
        self.assertNotIn("SERVICEVOICE_PUBLIC_HOST", kwargs["env"])
        self.assertEqual(kwargs["env"]["COMPOSE_DISABLE_ENV_FILE"], "1")
        self.assertEqual(kwargs["timeout"], 120)

    def test_admin_fixes_project_and_explicit_settings(self):
        command = manage.compose(self.settings)
        self.assertEqual(command[:4], ["docker", "compose", "--project-name", "servicevoice-lightsail"])
        self.assertIn(str(self.settings), command)
        self.assertEqual(command[-2], "-f")

    def test_backup_labels_cannot_escape_or_inject_options(self):
        self.assertEqual(manage.label("before-update-20261018"), "before-update-20261018")
        for value in ("", "../other", "a/b", "--help", "UPPER", "x" * 65, "a b", "a\nnext"):
            with self.assertRaises(preflight.PreflightError):
                manage.label(value)

    def test_source_contract_has_no_public_console_or_cancellation_disabling_flush(self):
        caddy = (HERE / "Caddyfile").read_text()
        active = "\n".join(line for line in caddy.splitlines() if not line.strip().startswith("#"))
        self.assertIn("path /webhooks/vapi/inbound /llm/chat/completions", active)
        self.assertNotIn("flush_interval", active)
        self.assertNotIn("lb_retries", active)
        self.assertIn("profile shortlived", (HERE / "tls.acme.caddy").read_text())
        self.assertIn("default_sni {$SERVICEVOICE_PUBLIC_HOST}", active)
        self.assertIn("setcap -r /usr/bin/caddy", (HERE / "Dockerfile.caddy").read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
