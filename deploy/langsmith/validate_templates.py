"""Offline guardrails for reviewed LangSmith ECS fragments, not AWS validation.

No credentials, environment files, network access, or AWS APIs are used.
This checks source templates only: placeholders intentionally remain unresolved.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SECRET_ARN = "arn:aws:secretsmanager:${AWS_REGION}:${AWS_ACCOUNT_ID}:secret:servicevoice/${DEPLOY_ENVIRONMENT}/langsmith-${SECRET_SUFFIX}"
OFFICIAL_ENDPOINTS = {
    "https://api.smith.langchain.com",
    "https://aws.api.smith.langchain.com",
    "https://eu.api.smith.langchain.com",
    "https://apac.api.smith.langchain.com",
}


def validate_templates(task: dict, policy: dict, kms: dict) -> list[str]:
    errors = []
    if task.get("executionRoleArn") != "arn:aws:iam::${AWS_ACCOUNT_ID}:role/servicevoice-${DEPLOY_ENVIRONMENT}-execution":
        errors.append("execution_role_template")
    containers = task.get("containerDefinitions")
    if not isinstance(containers, list) or len(containers) != 1 or not isinstance(containers[0], dict):
        return errors + ["container_template"]
    container = containers[0]
    if container.get("name") != "servicevoice":
        errors.append("container_name")
    rows = container.get("environment", [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return errors + ["environment_template"]
    values = {row.get("name"): row.get("value") for row in rows}
    if len(values) != len(rows):
        errors.append("duplicate_environment")
    permitted = {
        "SERVICEVOICE_LANGSMITH_ENABLED", "LANGSMITH_ENDPOINT", "LANGSMITH_PROJECT",
        "SERVICEVOICE_ENVIRONMENT", "SERVICEVOICE_LANGSMITH_SAMPLE_RATE",
        "SERVICEVOICE_LANGSMITH_MONTHLY_SPAN_LIMIT", "SERVICEVOICE_LANGSMITH_BUDGET_PATH",
    }
    if set(values) != permitted:
        errors.append("environment_allowlist")
    if values.get("SERVICEVOICE_LANGSMITH_ENABLED") != "false":
        errors.append("explicit_activation_required")
    if values.get("LANGSMITH_ENDPOINT") not in OFFICIAL_ENDPOINTS:
        errors.append("official_endpoint_required")
    if values.get("LANGSMITH_PROJECT") != "servicevoice-production" or values.get("SERVICEVOICE_ENVIRONMENT") != "production":
        errors.append("production_project_separation")
    try:
        if not 0 < float(values.get("SERVICEVOICE_LANGSMITH_SAMPLE_RATE", "invalid")) <= 0.25:
            errors.append("conservative_sampling")
        limit = int(values.get("SERVICEVOICE_LANGSMITH_MONTHLY_SPAN_LIMIT", "invalid"))
        if not 1 <= limit <= 1000:
            errors.append("bounded_export_budget")
    except (ValueError, TypeError):
        errors.append("numeric_export_controls")
    if values.get("SERVICEVOICE_LANGSMITH_BUDGET_PATH") != "/data/langsmith-budget.sqlite3":
        errors.append("durable_budget_path")
    if container.get("secrets") != [{"name": "LANGSMITH_API_KEY", "valueFrom": SECRET_ARN}]:
        errors.append("runtime_secret_reference")
    statements = policy.get("Statement")
    if policy.get("Version") != "2012-10-17" or not isinstance(statements, list) or len(statements) != 1:
        errors.append("secrets_policy_shape")
    elif any(statements[0].get(key) != expected for key, expected in {
        "Effect": "Allow", "Action": ["secretsmanager:GetSecretValue"], "Resource": SECRET_ARN,
    }.items()):
        errors.append("least_privilege_secret_read")
    expected_kms = {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "DecryptOnlyThisSecretsManagerKey", "Effect": "Allow", "Action": ["kms:Decrypt"],
            "Resource": "arn:aws:kms:${AWS_REGION}:${AWS_ACCOUNT_ID}:key/${KMS_KEY_ID}",
            "Condition": {"StringEquals": {
                "kms:ViaService": "secretsmanager.${AWS_REGION}.amazonaws.com",
                "kms:EncryptionContext:SecretARN": SECRET_ARN,
            }},
        }],
    }
    if kms != expected_kms:
        errors.append("customer_kms_scope")
    return errors


def load_templates(root: Path = ROOT) -> tuple[dict, dict, dict]:
    return tuple(json.loads((root / name).read_text(encoding="utf-8")) for name in (
        "ecs-task-definition.fragment.json", "execution-role-policy.fragment.json", "customer-kms-policy.fragment.json"
    ))


def main() -> int:
    errors = validate_templates(*load_templates())
    print(json.dumps({"template_checks_passed": not errors, "errors": errors,
                      "scope": "Static fragment checks only; not deployed, not a full AWS task or security review."}))
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
