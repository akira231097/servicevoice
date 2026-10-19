"""Guard against credential leakage and broadened AWS permissions in examples."""
from __future__ import annotations

from copy import deepcopy
import importlib.util
from pathlib import Path

MODULE = Path(__file__).with_name("validate_templates.py")
SPEC = importlib.util.spec_from_file_location("langsmith_aws_templates", MODULE)
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


def test_checked_in_fragments_pass():
    assert validator.validate_templates(*validator.load_templates()) == []


def test_plaintext_key_or_static_aws_credentials_fail():
    for name in ("LANGSMITH_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        task, policy, kms = deepcopy(validator.load_templates())
        task["containerDefinitions"][0]["environment"].append({"name": name, "value": "do-not-store-secrets-here"})
        assert "environment_allowlist" in validator.validate_templates(task, policy, kms)


def test_wildcard_secret_read_fails():
    task, policy, kms = deepcopy(validator.load_templates())
    policy["Statement"][0]["Resource"] = "*"
    assert "least_privilege_secret_read" in validator.validate_templates(task, policy, kms)


def test_secret_write_permission_fails():
    task, policy, kms = deepcopy(validator.load_templates())
    policy["Statement"][0]["Action"].append("secretsmanager:PutSecretValue")
    assert "least_privilege_secret_read" in validator.validate_templates(task, policy, kms)


def test_key_outside_runtime_secret_reference_fails():
    task, policy, kms = deepcopy(validator.load_templates())
    task["containerDefinitions"][0]["secrets"][0]["valueFrom"] = "literal-key"
    assert "runtime_secret_reference" in validator.validate_templates(task, policy, kms)


def test_activation_requires_deliberate_edit_after_smoke_check():
    task, policy, kms = deepcopy(validator.load_templates())
    task["containerDefinitions"][0]["environment"][0]["value"] = "true"
    assert "explicit_activation_required" in validator.validate_templates(task, policy, kms)


def test_budget_cannot_be_ephemeral():
    task, policy, kms = deepcopy(validator.load_templates())
    for row in task["containerDefinitions"][0]["environment"]:
        if row["name"] == "SERVICEVOICE_LANGSMITH_BUDGET_PATH":
            row["value"] = "/tmp/budget.sqlite3"
    assert "durable_budget_path" in validator.validate_templates(task, policy, kms)


def test_duplicate_environment_and_extra_policy_statement_fail():
    task, policy, kms = deepcopy(validator.load_templates())
    task["containerDefinitions"][0]["environment"].append(task["containerDefinitions"][0]["environment"][0])
    policy["Statement"].append({"Effect": "Allow", "Action": "*", "Resource": "*"})
    errors = validator.validate_templates(task, policy, kms)
    assert "duplicate_environment" in errors
    assert "secrets_policy_shape" in errors


def test_custom_kms_policy_cannot_decrypt_other_keys_or_services():
    for field in ("Resource", "Condition"):
        task, policy, kms = deepcopy(validator.load_templates())
        kms["Statement"][0][field] = "*" if field == "Resource" else {}
        assert "customer_kms_scope" in validator.validate_templates(task, policy, kms)


def test_endpoint_with_key_in_url_fails():
    task, policy, kms = deepcopy(validator.load_templates())
    for row in task["containerDefinitions"][0]["environment"]:
        if row["name"] == "LANGSMITH_ENDPOINT":
            row["value"] = "https://token@aws.api.smith.langchain.com"
    assert "official_endpoint_required" in validator.validate_templates(task, policy, kms)
