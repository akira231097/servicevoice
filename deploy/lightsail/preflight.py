"""Read explicit local settings only. Never print credential values or call providers."""
from __future__ import annotations
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
from uuid import UUID

PROJECT = Path(__file__).resolve().parents[2]
PUBLIC_KEYS = frozenset({"SERVICEVOICE_PUBLIC_HOST", "SERVICEVOICE_ACME_EMAIL",
                         "SERVICEVOICE_PRIVATE_ENV_FILE", "SERVICEVOICE_DATABASE_PATH", "SERVICEVOICE_JEV_FAQ",
                         "SERVICEVOICE_LANGSMITH_ENABLED"})
PRIVATE_REQUIRED = frozenset({"GEMINI_API_KEY", "GEMINI_GATEWAY_SECRET", "VAPI_PHONE_WEBHOOK_SECRET",
                          "VAPI_PHONE_ORG_ID", "VAPI_PHONE_NUMBER_ID", "VAPI_PHONE_ASSISTANT_ID"})
PRIVATE_KEYS = PRIVATE_REQUIRED | {"TYPESAFE_API_KEY", "LANGSMITH_API_KEY", "LANGSMITH_WORKSPACE_ID", "LANGSMITH_ENDPOINT"}


class PreflightError(ValueError):
    """Only fixed codes: never interpolate a file's content in diagnostics."""


def read_env(path: Path, allowed, *, private=False, check_permissions=True):
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise PreflightError("explicit_regular_file_required")
    info = path.stat()
    if check_permissions and os.name == "posix":
        if stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid not in (0, os.geteuid()):
            raise PreflightError("owner_only_file_permissions_required")
    with path.open("rb") as handle:
        raw = handle.read(16_385)
    if len(raw) > 16_384:
        raise PreflightError("environment_file_too_large")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise PreflightError("ascii_environment_file_required") from None
    values = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in allowed or key in values:
            raise PreflightError("unknown_or_duplicate_environment_key")
        if (not value or len(value) > 4096 or value != value.strip()
                or any(ord(char) < 33 or ord(char) > 126 for char in value)
                or value[:1] in "\"'" or value[-1:] in "\"'"):
            raise PreflightError("raw_single_line_environment_value_required")
        if "REPLACE_" in value:
            raise PreflightError("placeholder_configuration")
        if not private and "$" in value:
            raise PreflightError("settings_interpolation_not_allowed")
        values[key] = value
    if private and not PRIVATE_REQUIRED.issubset(values):
        raise PreflightError("required_private_configuration_missing")
    return values


def public_host(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        if (not isinstance(value, str) or len(value) > 253 or value != value.lower()
                or not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", value)
                or value.endswith((".localhost", ".local", ".internal", ".test", ".invalid", ".example"))
                or value in {"example.com", "example.org", "example.net"}
                or value.endswith((".example.com", ".example.org", ".example.net"))):
            raise PreflightError("public_ipv4_or_dns_name_required")
        return "dns"
    if not isinstance(address, ipaddress.IPv4Address) or not address.is_global or address.is_multicast:
        raise PreflightError("public_ipv4_or_dns_name_required")
    return "ipv4"


def validate(settings_path, *, project=PROJECT, check_permissions=True):
    settings_path = Path(settings_path)
    if not settings_path.is_absolute() or settings_path.resolve().is_relative_to(project.resolve()):
        raise PreflightError("settings_must_be_outside_checkout")
    settings = read_env(settings_path, PUBLIC_KEYS, check_permissions=check_permissions)
    if not PUBLIC_KEYS.difference({"SERVICEVOICE_DATABASE_PATH", "SERVICEVOICE_JEV_FAQ", "SERVICEVOICE_LANGSMITH_ENABLED"}).issubset(settings):
        raise PreflightError("required_public_configuration_missing")
    kind = public_host(settings["SERVICEVOICE_PUBLIC_HOST"])
    if not re.fullmatch(r"[A-Za-z0-9.!#$%&*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}", settings["SERVICEVOICE_ACME_EMAIL"]):
        raise PreflightError("certificate_contact_email_required")
    database = settings.setdefault("SERVICEVOICE_DATABASE_PATH", "/data/servicevoice.sqlite3")
    if not re.fullmatch(r"/data/(?:restores/)?[A-Za-z0-9][A-Za-z0-9_-]{0,80}\.sqlite3", database):
        raise PreflightError("database_must_use_explicit_data_volume")
    private_path = Path(settings["SERVICEVOICE_PRIVATE_ENV_FILE"])
    if (not private_path.is_absolute() or private_path.resolve().is_relative_to(project.resolve())
            or private_path.resolve() == settings_path.resolve()):
        raise PreflightError("private_environment_must_be_outside_checkout")
    private = read_env(private_path, PRIVATE_KEYS, private=True, check_permissions=check_permissions)
    faq = settings.setdefault("SERVICEVOICE_JEV_FAQ", "on")
    if faq not in {"on", "off"} or faq == "on" and len(private.get("TYPESAFE_API_KEY", "")) < 20:
        raise PreflightError("faq_on_requires_typesafe_key")
    tracing = settings.setdefault("SERVICEVOICE_LANGSMITH_ENABLED", "off")
    if tracing not in {"on", "off"} or tracing == "on" and not 16 <= len(private.get("LANGSMITH_API_KEY", "")) <= 512:
        raise PreflightError("tracing_on_requires_langsmith_key")
    if "LANGSMITH_ENDPOINT" in private and private["LANGSMITH_ENDPOINT"] not in {
            "https://aws.api.smith.langchain.com", "https://api.smith.langchain.com",
            "https://eu.api.smith.langchain.com", "https://apac.api.smith.langchain.com"}:
        raise PreflightError("official_tracing_endpoint_required")
    for key in ("GEMINI_GATEWAY_SECRET", "VAPI_PHONE_WEBHOOK_SECRET"):
        if not 32 <= len(private[key]) <= 512:
            raise PreflightError("webhook_secrets_require_32_to_512_characters")
    if private["GEMINI_GATEWAY_SECRET"] == private["VAPI_PHONE_WEBHOOK_SECRET"]:
        raise PreflightError("independent_credentials_required")
    if len(private["GEMINI_API_KEY"]) < 20:
        raise PreflightError("google_api_key_missing_or_placeholder")
    for key in ("VAPI_PHONE_ORG_ID", "VAPI_PHONE_NUMBER_ID", "VAPI_PHONE_ASSISTANT_ID", *(["LANGSMITH_WORKSPACE_ID"] if "LANGSMITH_WORKSPACE_ID" in private else [])):
        try:
            if str(UUID(private[key])) != private[key]:
                raise ValueError()
        except ValueError:
            raise PreflightError("canonical_phone_identity_uuids_required") from None
    # Values are deliberately not returned: callers need public settings only.
    return settings, {"configuration_shape_valid": True, "public_address_kind": kind,
                      "capacity_profile": "default", "provider_credentials_verified": False,
                      "public_tls_verified": False, "private_console_exposed": False,
                      "private_file_permissions_checked": check_permissions and os.name == "posix"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", required=True, type=Path)
    args = parser.parse_args()
    try:
        _, summary = validate(args.settings)
        print(json.dumps(summary, sort_keys=True))
        return 0
    except (PreflightError, OSError):
        # A raw OS exception may contain paths; credential contents never leave.
        print(json.dumps({"configuration_shape_valid": False,
                          "error": "configuration_or_file_permissions_invalid"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
