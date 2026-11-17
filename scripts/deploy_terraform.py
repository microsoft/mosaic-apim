"""Standalone public deployment; no azd process or environment is required."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import mosaic_entra

ROOT = Path(__file__).resolve().parent.parent
DIRECTORY = ROOT / "infra" / "terraform"
VARIABLES = DIRECTORY / "mosaic.auto.tfvars.json"
STATE = DIRECTORY / ".mosaic-terraform" / "environment.json"
PLAN = DIRECTORY / "mosaic.tfplan"
RECEIPT = DIRECTORY / ".mosaic-terraform" / "plan.json"
APPS = ("api", "web", "portal")
IDENTITY_INPUTS = {
    "tenant_id": "MOSAIC_TENANT_ID",
    "api_client_id": "MOSAIC_API_CLIENT_ID",
    "spa_client_id": "MOSAIC_SPA_CLIENT_ID",
    "portal_client_id": "MOSAIC_PORTAL_CLIENT_ID",
    "api_application_id_uri": "MOSAIC_API_APPLICATION_ID_URI",
    "api_scope": "MOSAIC_API_SCOPE",
    "model_runtime_client_id": "MOSAIC_MODEL_RUNTIME_CLIENT_ID",
    "model_client_id": "MOSAIC_MODEL_CLIENT_ID",
}


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise mosaic_entra.OperationFailed(f"Cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise mosaic_entra.OperationFailed(f"Expected a JSON object in {path}")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class EnvironmentRunner(mosaic_entra.CliRunner):
    """Adapt only the legacy hook's environment sink, never invoke azd."""

    def azd(self, args: list[str], operation_name: str) -> None:
        if len(args) != 4 or args[:2] != ["env", "set"]:
            raise mosaic_entra.OperationFailed(
                f"Unsupported environment operation: {operation_name}"
            )
        if self.dry_run:
            print(f"[dry-run] persist {args[2]}", file=sys.stderr)
            return
        environment = read_json(STATE)
        environment[args[2]] = args[3]
        write_json(STATE, environment)


def invoke(command: list[str], *, json_output: bool = False) -> Any:
    result = mosaic_entra.run_json_command(
        command, operation_name=" ".join(command[:3]), allow_non_json=not json_output
    )
    if not json_output and isinstance(result, str):
        print(result)
    return result


def terraform(*args: str, json_output: bool = False) -> Any:
    return invoke(["terraform", f"-chdir={DIRECTORY}", *args], json_output=json_output)


def check_account(subscription: str, tenant: str | None = None) -> None:
    account = invoke(["az", "account", "show", "--output", "json"], json_output=True)
    if str(account.get("id", "")).lower() != subscription.lower():
        raise mosaic_entra.OperationFailed(
            "Azure CLI subscription differs from this deployment. Run az account set explicitly."
        )
    if tenant and str(account.get("tenantId", "")).lower() != tenant.lower():
        raise mosaic_entra.OperationFailed("Azure CLI tenant differs from this deployment.")
    if account.get("environmentName") != "AzureCloud":
        raise mosaic_entra.OperationFailed(
            "The standalone Terraform reference supports Azure Commercial (AzureCloud) only."
        )


def load_environment() -> dict[str, Any]:
    environment = read_json(STATE)
    inputs = read_json(VARIABLES)
    if (
        inputs["subscription_id"] != environment["AZURE_SUBSCRIPTION_ID"]
        or inputs["environment_name"] != environment["AZURE_ENV_NAME"]
        or inputs["tenant_id"] != environment["MOSAIC_TENANT_ID"]
    ):
        raise mosaic_entra.OperationFailed("Bootstrap metadata and Terraform inputs disagree.")
    check_account(inputs["subscription_id"], inputs["tenant_id"])
    os.environ.update({key: str(value) for key, value in environment.items()})
    return environment


def fingerprint() -> str:
    digest = hashlib.sha256()
    files = sorted(DIRECTORY.glob("*.tf"))
    files += sorted(DIRECTORY.glob("*.tfvars"))
    files += sorted(DIRECTORY.glob("*.tfvars.json"))
    files += [DIRECTORY / ".terraform.lock.hcl"]
    for path in files:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def invalidate_plan() -> None:
    PLAN.unlink(missing_ok=True)
    RECEIPT.unlink(missing_ok=True)


def bootstrap(options: argparse.Namespace) -> None:
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,23}", options.environment_name):
        raise mosaic_entra.OperationFailed(
            "Environment must be 3-24 lowercase letters/digits/hyphens."
        )
    check_account(options.subscription_id)
    terraform("version", "-json", json_output=True)
    environment = {
        "AZURE_ENV_NAME": options.environment_name,
        "AZURE_SUBSCRIPTION_ID": options.subscription_id,
        "AZURE_LOCATION": options.location,
    }
    if STATE.exists():
        existing = read_json(STATE)
        if any(existing.get(key) != value for key, value in environment.items()):
            raise mosaic_entra.OperationFailed(
                "This checkout already belongs to a different deployment. Use a fresh checkout."
            )
        environment = {**existing, **environment}
    elif VARIABLES.exists() or (DIRECTORY / "terraform.tfstate").exists():
        raise mosaic_entra.OperationFailed(
            "Existing Terraform inputs/state without bootstrap metadata; "
            "refusing automatic adoption."
        )
    write_json(STATE, environment)
    os.environ.update({key: str(value) for key, value in environment.items()})
    invalidate_plan()
    mosaic_entra.preprovision(EnvironmentRunner(), options.environment_name)
    generated = read_json(STATE)
    inputs = read_json(VARIABLES) if VARIABLES.exists() else {}
    inputs.update(
        {
            key: generated.get(value, "") if key == "model_client_id" else generated[value]
            for key, value in IDENTITY_INPUTS.items()
        }
    )
    inputs.update(
        environment_name=options.environment_name,
        subscription_id=options.subscription_id,
        location=options.location,
        publisher_name=options.publisher_name or generated["MOSAIC_APIM_PUBLISHER_NAME"],
        publisher_email=options.publisher_email or generated["MOSAIC_APIM_PUBLISHER_EMAIL"],
        entra_group_claims=mosaic_entra.group_claims_enabled(),
    )
    write_json(VARIABLES, inputs)
    print("Bootstrap complete. Review inputs, then run plan. No Azure hosting was provisioned.")


def make_plan() -> None:
    load_environment()
    invalidate_plan()
    terraform("init", "-input=false", "-no-color")
    terraform("plan", "-input=false", "-no-color", f"-out={PLAN}")
    write_json(RECEIPT, {"fingerprint": fingerprint()})
    print("Review the plan above. The separate apply command executes this saved plan.")


def apply_plan() -> None:
    load_environment()
    if not PLAN.exists() or read_json(RECEIPT)["fingerprint"] != fingerprint():
        raise mosaic_entra.OperationFailed("No current reviewed plan. Run plan before apply.")
    terraform("apply", "-input=false", "-no-color", str(PLAN))
    invalidate_plan()


def outputs() -> dict[str, str]:
    result = terraform("output", "-json", "deployment", json_output=True)
    if not isinstance(result, dict) or not all(isinstance(value, str) for value in result.values()):
        raise mosaic_entra.OperationFailed("Missing or invalid Terraform deployment output.")
    environment = read_json(STATE)
    for key in ("AZURE_ENV_NAME", "AZURE_SUBSCRIPTION_ID", "AZURE_LOCATION"):
        if result.get(key) != environment.get(key):
            raise mosaic_entra.OperationFailed(f"Terraform output does not match bootstrap: {key}")
    return result


def configure() -> None:
    environment = load_environment()
    environment.update(outputs())
    write_json(STATE, environment)
    os.environ.update(environment)
    mosaic_entra.postprovision(EnvironmentRunner(), environment["AZURE_ENV_NAME"], None)
    print(
        "Redirects updated; inspect any consent warnings above "
        "before using directory/runtime flows."
    )


def build_images(index_url: str) -> None:
    load_environment()
    parsed = urlsplit(index_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise mosaic_entra.OperationFailed("Use an HTTPS package index URL without credentials.")
    deployment = outputs()
    images = {}
    tag = uuid.uuid4().hex
    for app in APPS:
        repository = f"mosaic-{app}"
        command = [
            "az",
            "acr",
            "build",
            "--registry",
            deployment["AZURE_CONTAINER_REGISTRY_NAME"],
            "--image",
            f"{repository}:{tag}",
            "--file",
            str(ROOT / "apps" / app / "Dockerfile"),
        ]
        if app == "api":
            command += ["--build-arg", f"PIP_INDEX_URL={index_url}"]
        invoke([*command, str(ROOT)])
        digest = invoke(
            [
                "az",
                "acr",
                "repository",
                "show",
                "--name",
                deployment["AZURE_CONTAINER_REGISTRY_NAME"],
                "--image",
                f"{repository}:{tag}",
                "--output",
                "json",
            ],
            json_output=True,
        )["digest"]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise mosaic_entra.OperationFailed(f"Registry returned an invalid digest for {app}")
        images[app] = f"{deployment['AZURE_CONTAINER_REGISTRY_ENDPOINT']}/{repository}@{digest}"
    inputs = read_json(VARIABLES)
    inputs["images"] = images
    invalidate_plan()
    write_json(VARIABLES, inputs)
    print("All images built. Run plan then apply to deploy them; images remain Terraform-owned.")


def check_readiness(timeout: int) -> None:
    load_environment()
    deployment = outputs()
    pending = set(APPS)
    deadline = time.monotonic() + timeout
    while pending and time.monotonic() < deadline:
        for app in sorted(pending):
            suffix = "/readyz" if app == "api" else "/healthz"
            url = deployment[f"{app.upper()}_APP_URL"] + suffix
            try:
                with urllib.request.urlopen(url, timeout=10) as response:
                    body = json.load(response)
                    if response.status == 200 and body.get("status") in ("ok", "ready"):
                        pending.remove(app)
                    else:
                        print(f"{app}: not ready: {body}", file=sys.stderr)
            except (urllib.error.URLError, TimeoutError, ValueError) as error:
                print(f"{app}: waiting for readiness: {error}", file=sys.stderr)
        if pending:
            time.sleep(5)
    if pending:
        raise mosaic_entra.OperationFailed(f"Readiness timed out for: {', '.join(sorted(pending))}")
    for app in APPS:
        print(f"{app}: {deployment[f'{app.upper()}_APP_URL']}")
    print("Health checks passed; these do not prove Entra consent or an inference journey.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap_parser = commands.add_parser(
        "bootstrap", help="Create/update Entra objects explicitly."
    )
    bootstrap_parser.add_argument("--environment-name", required=True)
    bootstrap_parser.add_argument("--subscription-id", required=True)
    bootstrap_parser.add_argument("--location", default="eastus2")
    bootstrap_parser.add_argument("--publisher-name")
    bootstrap_parser.add_argument("--publisher-email")
    for name in ("plan", "apply", "configure"):
        commands.add_parser(name)
    build = commands.add_parser("build")
    build.add_argument("--python-index-url", default="https://pypi.org/simple")
    check = commands.add_parser("check")
    check.add_argument("--timeout", type=int, default=600)
    options = parser.parse_args(argv)
    try:
        if options.command == "bootstrap":
            bootstrap(options)
        elif options.command == "plan":
            make_plan()
        elif options.command == "apply":
            apply_plan()
        elif options.command == "configure":
            configure()
        elif options.command == "build":
            build_images(options.python_index_url)
        else:
            if options.timeout < 1:
                parser.error("--timeout must be positive")
            check_readiness(options.timeout)
    except (mosaic_entra.OperationFailed, OSError, KeyError) as error:
        print(f"Terraform deployment failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
