"""Deploy, check and tear down the Phase 11 MCP test servers: the deployment kit for Batch 5b.

    python deploy.py plan                  # the dry run: what-if, and every resource it creates
    python deploy.py deploy                # builds the images, deploys, smoke-checks
    python deploy.py outputs               # the URLs and the principal ID the next steps need
    python deploy.py smoke                 # the smoke checks again
    python deploy.py teardown --dry-run    # what teardown would delete
    python deploy.py teardown              # deletes exactly what deploy created

Each command reads parameters.local.json beside this script, or the file --parameters names.
Copy parameters.example.json to start one. The script needs only Python 3.12 or later and the
Azure CLI, signed in to the servers' tenant. It prints every command that changes anything before
it runs it. plan, and teardown --dry-run, change nothing.
"""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import smoke

HERE = Path(__file__).resolve().parent
SERVERS = HERE.parent
TEMPLATE = HERE / "main.bicep"
DEFAULT_PARAMETERS = HERE / "parameters.local.json"
KIT_TAG_NAME, KIT_TAG_VALUE = "mosaic-e2e-kit", "mcp-test-servers"
# The images deploy builds, from these folders, with az acr build.
IMAGES = {"m-tools": SERVERS / "m-tools", "m-agent": SERVERS / "m-agent"}
PROTECTED = SERVERS / "m-protected"
# Everything M-protected runs from. The Flex Consumption remote build installs requirements.txt.
PROTECTED_FILES = ("function_app.py", "tools.py", "host.json", "requirements.txt")
FUNCTIONS = ("echo", "utc_now", "add")
RETRY_SECONDS = 90
FUNCTIONS_TIMEOUT_SECONDS = 600

_GUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
_PREFIX = re.compile(r"[a-z][a-z0-9-]{1,14}[a-z0-9]")
_RESOURCE_GROUP = re.compile(r"[-\w.()]{0,89}[-\w()]")
_LOCATION = re.compile(r"[a-z0-9]{2,40}")
_IMAGE_TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_DEPLOYMENT = re.compile(r"[A-Za-z0-9._-]{1,64}")
_API_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,31}")
_COST_CENTER = re.compile(r"[A-Za-z0-9._-]{1,64}")
_RESOURCE_ID = re.compile(
    r"/subscriptions/(?P<subscription>[^/]+)/resourceGroups/(?P<group>[^/]+)"
    r"/providers/(?P<type>[^/]+/[^/]+)/(?P<name>[^/]+)",
    re.IGNORECASE,
)
_PLACEHOLDER = re.compile(r"<[^<>]+>")


class KitError(Exception):
    """A problem the coordinator has to fix before the command can go on."""


@dataclass(frozen=True)
class ResourceId:
    subscription: str
    group: str
    name: str
    value: str

    @classmethod
    def parse(cls, value: str, field: str, resource_type: str) -> "ResourceId":
        match = _RESOURCE_ID.fullmatch(value.strip())
        if (
            not match
            or match["type"].lower() != resource_type.lower()
            or not _GUID.fullmatch(match["subscription"])
        ):
            raise KitError(f"{field} must be the resource ID of a {resource_type} resource.")
        return cls(match["subscription"], match["group"], match["name"], value.strip())


@dataclass(frozen=True)
class Kit:
    """The deployment's inputs, checked."""

    subscription_id: str
    location: str
    resource_group: str
    name_prefix: str
    workspace: ResourceId
    registry: ResourceId
    image_tag: str | None
    tenant_id: str
    audience_client_id: str
    audience_app_id_uri: str
    gateway_client_id: str
    mosaic_api_client_id: str
    model_endpoint: str
    model_deployment: str
    model_api_version: str
    model_runtime_client_id: str
    model_cost_center: str
    model_token_parameter: str
    model_max_tokens: int
    min_replicas: int

    @classmethod
    def load(cls, path: Path) -> "Kit":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise KitError(
                f"There's no {path.name}. Copy parameters.example.json to it and fill it in."
            ) from None
        except ValueError as error:
            raise KitError(f"{path.name} isn't valid JSON: {error}") from None
        return cls.from_values(raw)

    @classmethod
    def from_values(cls, raw: Any) -> "Kit":
        if not isinstance(raw, dict):
            raise KitError("The parameters must be a JSON object.")
        _refuse_placeholders(raw, "")
        protected = _section(raw, "protected")
        agent = _section(raw, "agent")

        workspace = ResourceId.parse(
            _text(raw, "logAnalyticsWorkspaceId"),
            "logAnalyticsWorkspaceId",
            "Microsoft.OperationalInsights/workspaces",
        )
        registry = ResourceId.parse(
            _text(raw, "containerRegistryId"),
            "containerRegistryId",
            "Microsoft.ContainerRegistry/registries",
        )
        group = _matching(raw, "resourceGroup", _RESOURCE_GROUP)
        if group.lower() in {workspace.group.lower(), registry.group.lower()}:
            raise KitError(
                "resourceGroup must be a dedicated resource group for the servers, not the one "
                "that holds MOSAIC's registry or workspace: teardown deletes it."
            )
        audience = _guid(protected, "protected.audienceClientId", "audienceClientId")
        audience_uri = str(protected.get("audienceAppIdUri") or f"api://{audience}").strip()
        if not audience_uri.startswith(("api://", "https://")) or any(
            c.isspace() for c in audience_uri
        ):
            raise KitError("protected.audienceAppIdUri must be the audience's application ID URI.")
        endpoint = _text(agent, "modelEndpoint", "agent.modelEndpoint").rstrip("/")
        parts = urlsplit(endpoint)
        if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment:
            raise KitError(
                "agent.modelEndpoint must be the https endpoint from MOSAIC's connection details."
            )
        cost_center = str(agent.get("costCenter") or "").strip()
        if cost_center and not _COST_CENTER.fullmatch(cost_center):
            raise KitError("agent.costCenter must be a cost center code, or empty.")
        token_parameter = str(agent.get("tokenParameter") or "max_tokens")
        if token_parameter not in {"max_tokens", "max_completion_tokens"}:
            raise KitError("agent.tokenParameter must be max_tokens or max_completion_tokens.")
        max_tokens = agent.get("maxTokens", 16)
        if (
            not isinstance(max_tokens, int)
            or isinstance(max_tokens, bool)
            or not 1 <= max_tokens <= 256
        ):
            raise KitError("agent.maxTokens must be a whole number from 1 to 256.")
        min_replicas = raw.get("minReplicas", 1)
        if min_replicas not in (0, 1) or isinstance(min_replicas, bool):
            raise KitError("minReplicas must be 0 or 1.")
        image_tag = str(raw.get("imageTag") or "").strip() or None
        if image_tag is not None and not _IMAGE_TAG.fullmatch(image_tag):
            raise KitError("imageTag must be a container image tag.")

        return cls(
            subscription_id=_guid(raw, "subscriptionId", "subscriptionId"),
            location=_matching(raw, "location", _LOCATION),
            resource_group=group,
            name_prefix=_matching(raw, "namePrefix", _PREFIX, default="mosaic-mcp"),
            workspace=workspace,
            registry=registry,
            image_tag=image_tag,
            tenant_id=_guid(protected, "protected.tenantId", "tenantId"),
            audience_client_id=audience,
            audience_app_id_uri=audience_uri,
            gateway_client_id=_guid(protected, "protected.gatewayClientId", "gatewayClientId"),
            mosaic_api_client_id=_guid(
                protected, "protected.mosaicApiClientId", "mosaicApiClientId"
            ),
            model_endpoint=endpoint,
            model_deployment=_matching(
                agent, "modelDeployment", _DEPLOYMENT, field="agent.modelDeployment"
            ),
            model_api_version=_matching(
                agent,
                "modelApiVersion",
                _API_VERSION,
                default="2024-10-21",
                field="agent.modelApiVersion",
            ),
            model_runtime_client_id=_guid(
                agent, "agent.modelRuntimeClientId", "modelRuntimeClientId"
            ),
            model_cost_center=cost_center,
            model_token_parameter=token_parameter,
            model_max_tokens=max_tokens,
            min_replicas=min_replicas,
        )

    @property
    def deployment_name(self) -> str:
        return f"{self.name_prefix}-servers"

    @property
    def registry_pull_deployment(self) -> str:
        return f"{self.name_prefix}-registry-pull"

    def repository(self, server: str) -> str:
        return f"{self.name_prefix}/{server}"

    def image(self, server: str, tag: str) -> str:
        return f"{self.repository(server)}:{tag}"

    def arm_parameters(self, tag: str, *, deploy_servers: bool) -> dict[str, Any]:
        values: dict[str, Any] = {
            "resourceGroupName": self.resource_group,
            "location": self.location,
            "namePrefix": self.name_prefix,
            "logAnalyticsWorkspaceId": self.workspace.value,
            "containerRegistryId": self.registry.value,
            "imageTag": tag,
            "deployServers": deploy_servers,
            "tenantId": self.tenant_id,
            "protectedAudienceClientId": self.audience_client_id,
            "protectedAudienceAppIdUri": self.audience_app_id_uri,
            "gatewayClientId": self.gateway_client_id,
            "mosaicApiClientId": self.mosaic_api_client_id,
            "modelEndpoint": self.model_endpoint,
            "modelDeployment": self.model_deployment,
            "modelApiVersion": self.model_api_version,
            "modelRuntimeClientId": self.model_runtime_client_id,
            "modelCostCenter": self.model_cost_center,
            "modelTokenParameter": self.model_token_parameter,
            "modelMaxTokens": self.model_max_tokens,
            "minReplicas": self.min_replicas,
        }
        return {
            "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
            "contentVersion": "1.0.0.0",
            "parameters": {name: {"value": value} for name, value in values.items()},
        }


def _refuse_placeholders(value: Any, path: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _refuse_placeholders(item, f"{path}.{key}" if path else key)
    elif isinstance(value, str) and _PLACEHOLDER.search(value):
        raise KitError(f"Replace the placeholder in {path}.")


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    section = raw.get(name)
    if not isinstance(section, dict):
        raise KitError(f"The parameters need a {name} object.")
    return section


def _text(values: dict[str, Any], key: str, field: str | None = None) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise KitError(f"Set {field or key}.")
    return value.strip()


def _matching(
    values: dict[str, Any],
    key: str,
    pattern: re.Pattern[str],
    *,
    default: str | None = None,
    field: str | None = None,
) -> str:
    value = values.get(key, default)
    if not isinstance(value, str) or not pattern.fullmatch(value.strip()):
        raise KitError(f"{field or key} isn't valid.")
    return value.strip()


def _guid(values: dict[str, Any], field: str, key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not _GUID.fullmatch(value.strip()):
        raise KitError(f"{field} must be a GUID.")
    return value.strip().lower()


def az_command() -> list[str]:
    """The Azure CLI, run through its own Python on Windows so no shell parses the arguments."""

    executable = shutil.which("az")
    if executable is None:
        raise KitError("The Azure CLI (az) isn't on PATH.")
    if os.name == "nt" and Path(executable).name.lower() == "az.cmd":
        python = Path(executable).parent.parent / "python.exe"
        if python.exists():
            return [str(python), "-IBm", "azure.cli"]
    return [executable]


def run_az(args: Sequence[str], capture: bool) -> Any:
    command = [*az_command(), *args]
    if not capture:
        if subprocess.run(command, check=False).returncode != 0:
            raise KitError(f"az {args[0]} {args[1]} failed; its output is above.")
        return None
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise KitError(f"az {' '.join(args[:3])} failed: {detail[-1] if detail else 'no output'}")
    text = result.stdout.strip()
    return json.loads(text) if text else None


Runner = Callable[[Sequence[str], bool], Any]
Say = Callable[[str], None]


class Az:
    """The Azure CLI. Every command that changes anything goes through change()."""

    def __init__(
        self, *, dry_run: bool = False, runner: Runner | None = None, say: Say = print
    ) -> None:
        self.dry_run = dry_run
        self._runner = runner or run_az
        self._say = say

    def read(self, args: Sequence[str]) -> Any:
        return self._runner(args, True)

    def show(self, args: Sequence[str]) -> None:
        """A read-only command whose output the coordinator reads, such as what-if."""

        self._runner(args, False)

    def change(self, args: Sequence[str]) -> None:
        line = "az " + " ".join(shlex.quote(str(arg)) for arg in args)
        if self.dry_run:
            self._say(f"Would run: {line}")
            return
        self._say(f"Running: {line}")
        self._runner(args, False)


@contextmanager
def parameters_file(kit: Kit, tag: str, *, deploy_servers: bool) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="mcp-servers-") as folder:
        path = Path(folder) / "parameters.json"
        path.write_text(
            json.dumps(kit.arm_parameters(tag, deploy_servers=deploy_servers)), encoding="utf-8"
        )
        yield path


def image_tag(kit: Kit) -> str:
    if kit.image_tag:
        return kit.image_tag
    git = shutil.which("git")
    result = (
        subprocess.run(
            [git, "rev-parse", "--short=12", "HEAD"],
            cwd=SERVERS,
            capture_output=True,
            text=True,
            check=False,
        )
        if git
        else None
    )
    if result is None or result.returncode != 0 or not _IMAGE_TAG.fullmatch(result.stdout.strip()):
        raise KitError("Set imageTag: the commit the images are built from couldn't be read.")
    return result.stdout.strip()


def resource_group_state(kit: Kit, az: Az) -> str:
    """'missing', 'kit' when this kit created it, or 'foreign'."""

    common = ["--subscription", kit.subscription_id, "--name", kit.resource_group]
    if not az.read(["group", "exists", *common]):
        return "missing"
    tags = az.read(["group", "show", *common, "--query", "tags", "--output", "json"]) or {}
    return "kit" if tags.get(KIT_TAG_NAME) == KIT_TAG_VALUE else "foreign"


def check_account(kit: Kit, az: Az) -> None:
    account = az.read(
        ["account", "show", "--subscription", kit.subscription_id, "--output", "json"]
    )
    if not isinstance(account, dict) or str(account.get("id", "")).lower() != kit.subscription_id:
        raise KitError("The Azure CLI isn't signed in to the subscription the parameters name.")


def planned_resources(kit: Kit) -> list[tuple[str, str]]:
    """Every Azure resource deploy creates, as (type, name). A * stands for a unique suffix."""

    prefix = kit.name_prefix
    return [
        ("Microsoft.Resources/resourceGroups", kit.resource_group),
        ("Microsoft.ManagedIdentity/userAssignedIdentities", f"{prefix}-pull"),
        (
            "Microsoft.Authorization/roleAssignments",
            f"AcrPull on {kit.registry.name} for {prefix}-pull",
        ),
        ("Microsoft.App/managedEnvironments", f"{prefix}-env"),
        ("Microsoft.Insights/diagnosticSettings", f"logs-to-workspace on {prefix}-env"),
        ("Microsoft.App/containerApps", f"{prefix}-tools"),
        ("Microsoft.App/containerApps", f"{prefix}-tools-sse"),
        ("Microsoft.App/containerApps", f"{prefix}-agent"),
        ("Microsoft.Storage/storageAccounts", f"st{prefix.replace('-', '')}*"),
        ("Microsoft.Storage/storageAccounts/blobServices", "default"),
        ("Microsoft.Storage/storageAccounts/blobServices/containers", "app-package"),
        ("Microsoft.Web/serverfarms", f"{prefix}-protected-plan"),
        ("Microsoft.Web/sites", f"{prefix}-protected-*"),
        (
            "Microsoft.Authorization/roleAssignments",
            f"Storage Blob Data Owner for {prefix}-protected-*",
        ),
        (
            "Microsoft.Authorization/roleAssignments",
            f"Storage Queue Data Contributor for {prefix}-protected-*",
        ),
        ("Microsoft.Web/sites/config", "appsettings"),
        ("Microsoft.Web/sites/config", "authsettingsV2 (Easy Auth)"),
        ("Microsoft.Insights/diagnosticSettings", f"logs-to-workspace on {prefix}-protected-*"),
    ]


def describe_plan(kit: Kit, tag: str, say: Say) -> None:
    say(f"Subscription {kit.subscription_id}, region {kit.location}.")
    say(
        f"In the new resource group {kit.resource_group}, and on MOSAIC's registry, deploy creates:"
    )
    for resource_type, name in planned_resources(kit):
        say(f"  {resource_type:<58} {name}")
    say(f"It also creates the deployment records {kit.deployment_name} (subscription) and")
    say(f"{kit.registry_pull_deployment} (in the registry's resource group, {kit.registry.group}).")
    say(f"It builds these images in {kit.registry.name} with az acr build:")
    for server in IMAGES:
        say(f"  {kit.image(server, tag)}")
    say("It deploys M-protected's code with a remote build: " + ", ".join(PROTECTED_FILES) + ".")


def plan(kit: Kit, az: Az, say: Say) -> None:
    check_account(kit, az)
    state = resource_group_state(kit, az)
    if state == "foreign":
        raise KitError(
            f"{kit.resource_group} exists and wasn't created by this kit. Choose a new name."
        )
    tag = image_tag(kit)
    describe_plan(kit, tag, say)
    say("What-if, from Azure Resource Manager:")
    with parameters_file(kit, tag, deploy_servers=True) as parameters:
        try:
            az.show(
                [
                    "deployment",
                    "sub",
                    "what-if",
                    "--subscription",
                    kit.subscription_id,
                    "--location",
                    kit.location,
                    "--name",
                    kit.deployment_name,
                    "--template-file",
                    str(TEMPLATE),
                    "--parameters",
                    f"@{parameters}",
                    "--result-format",
                    "ResourceIdOnly",
                ]
            )
        except KitError as error:
            say(f"What-if didn't finish ({error}). The list above is still what deploy creates.")
    say("Plan changed nothing.")


def _deploy_template(kit: Kit, az: Az, tag: str, *, deploy_servers: bool) -> None:
    with parameters_file(kit, tag, deploy_servers=deploy_servers) as parameters:
        az.change(
            [
                "deployment",
                "sub",
                "create",
                "--subscription",
                kit.subscription_id,
                "--location",
                kit.location,
                "--name",
                kit.deployment_name,
                "--template-file",
                str(TEMPLATE),
                "--parameters",
                f"@{parameters}",
                "--output",
                "none",
            ]
        )


def read_outputs(kit: Kit, az: Az) -> dict[str, str] | None:
    try:
        outputs = az.read(
            [
                "deployment",
                "sub",
                "show",
                "--subscription",
                kit.subscription_id,
                "--name",
                kit.deployment_name,
                "--query",
                "properties.outputs",
                "--output",
                "json",
            ]
        )
    except KitError:
        return None
    if not isinstance(outputs, dict):
        return None
    return {
        name: str(item.get("value", "")) for name, item in outputs.items() if isinstance(item, dict)
    }


def build_protected_package(folder: Path) -> Path:
    package = folder / "m-protected.zip"
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in PROTECTED_FILES:
            archive.write(PROTECTED / name, name)
    return package


def wait_for_functions(
    kit: Kit, az: Az, app: str, say: Say, sleep: Callable[[float], None]
) -> None:
    say("Waiting for the Functions host to list M-protected's tools...")
    waited = 0
    while True:
        names = (
            az.read(
                [
                    "functionapp",
                    "function",
                    "list",
                    "--subscription",
                    kit.subscription_id,
                    "--resource-group",
                    kit.resource_group,
                    "--name",
                    app,
                    "--query",
                    "[].name",
                    "--output",
                    "json",
                ]
            )
            or []
        )
        found = {str(name).rsplit("/", 1)[-1] for name in names}
        if found >= set(FUNCTIONS):
            say("M-protected lists " + ", ".join(FUNCTIONS) + ".")
            return
        if waited >= FUNCTIONS_TIMEOUT_SECONDS:
            raise KitError(
                "M-protected's functions didn't appear. Check its logs in the workspace."
            )
        sleep(15)
        waited += 15


OUTPUT_LINES = (
    ("toolsUrl", "M-tools", "register with upstream authentication None"),
    ("toolsSseUrl", "M-tools SSE-only", "M1's negative case: MOSAIC must refuse to register it"),
    ("protectedUrl", "M-protected", "register with upstream authentication Managed identity"),
    ("protectedAudience", "M-protected audience", "the audience to register M-protected with"),
    ("agentUrl", "M-agent", "register with upstream authentication None"),
    ("agentPrincipalId", "M-agent principal ID", "assign Models.Invoke.Application to it"),
)


def print_outputs(outputs: dict[str, str], say: Say) -> None:
    say("Outputs:")
    for key, label, note in OUTPUT_LINES:
        say(f"  {label:<22} {outputs.get(key) or '(not deployed)'}")
        say(f"  {'':<22} {note}")


def smoke_all(outputs: dict[str, str], say: Say) -> bool:
    checks = (
        ("tools", "toolsUrl"),
        ("sse", "toolsSseUrl"),
        ("protected", "protectedUrl"),
        ("agent", "agentUrl"),
    )
    results = [smoke.run(name, outputs.get(key, ""), say) for name, key in checks]
    return all(results)


def deploy(
    kit: Kit,
    az: Az,
    say: Say,
    *,
    skip_smoke: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    check_account(kit, az)
    if resource_group_state(kit, az) == "foreign":
        raise KitError(
            f"{kit.resource_group} exists and wasn't created by this kit. Choose a new name."
        )
    tag = image_tag(kit)

    say("1. The resource group, the pull identity and its AcrPull grant on the registry.")
    _deploy_template(kit, az, tag, deploy_servers=False)

    # The builds take a few minutes, which gives the new AcrPull grant time to take effect.
    say("2. The images, built in the registry.")
    for server, folder in IMAGES.items():
        az.change(
            [
                "acr",
                "build",
                "--subscription",
                kit.registry.subscription,
                "--registry",
                kit.registry.name,
                "--image",
                kit.image(server, tag),
                "--file",
                str(folder / "Dockerfile"),
                str(folder),
            ]
        )

    say("3. The servers.")
    try:
        _deploy_template(kit, az, tag, deploy_servers=True)
    except KitError as error:
        # A role assignment can take minutes to reach the registry. Deploying again is safe.
        say(f"The deployment failed ({error}). Deploying again in {RETRY_SECONDS} seconds.")
        sleep(RETRY_SECONDS)
        _deploy_template(kit, az, tag, deploy_servers=True)
    outputs = read_outputs(kit, az)
    if not outputs or not outputs.get("functionAppName"):
        raise KitError("The deployment's outputs couldn't be read.")

    say("4. M-protected's code.")
    with tempfile.TemporaryDirectory(prefix="mcp-servers-") as scratch:
        package = build_protected_package(Path(scratch))
        az.change(
            [
                "functionapp",
                "deployment",
                "source",
                "config-zip",
                "--subscription",
                kit.subscription_id,
                "--resource-group",
                kit.resource_group,
                "--name",
                outputs["functionAppName"],
                "--src",
                str(package),
                "--build-remote",
                "true",
            ]
        )
    wait_for_functions(kit, az, outputs["functionAppName"], say, sleep)
    print_outputs(outputs, say)
    if skip_smoke:
        return True
    say("5. Smoke checks.")
    return smoke_all(outputs, say)


def _find_pull_assignment(kit: Kit, az: Az) -> str | None:
    try:
        principal = az.read(
            [
                "identity",
                "show",
                "--subscription",
                kit.subscription_id,
                "--resource-group",
                kit.resource_group,
                "--name",
                f"{kit.name_prefix}-pull",
                "--query",
                "principalId",
                "--output",
                "json",
            ]
        )
        found = az.read(
            [
                "role",
                "assignment",
                "list",
                "--scope",
                kit.registry.value,
                "--assignee",
                str(principal),
                "--query",
                "[].id",
                "--output",
                "json",
            ]
        )
    except KitError:
        return None
    return found[0] if found else None


def _deployment_exists(az: Az, args: Sequence[str]) -> bool:
    try:
        az.read([*args, "--query", "name", "--output", "json"])
    except KitError:
        return False
    return True


def image_commands(kit: Kit) -> list[list[str]]:
    return [
        [
            "acr",
            "repository",
            "delete",
            "--subscription",
            kit.registry.subscription,
            "--name",
            kit.registry.name,
            "--repository",
            kit.repository(server),
            "--yes",
        ]
        for server in IMAGES
    ]


def teardown(kit: Kit, az: Az, say: Say, *, delete_images: bool = False) -> None:
    check_account(kit, az)
    state = resource_group_state(kit, az)
    if state == "foreign":
        raise KitError(
            f"{kit.resource_group} wasn't created by this kit, so teardown won't touch it."
        )

    outputs = read_outputs(kit, az) or {}
    assignment = outputs.get("acrPullRoleAssignmentId") or _find_pull_assignment(kit, az)
    if assignment:
        az.change(["role", "assignment", "delete", "--ids", assignment])
    else:
        say("No AcrPull assignment of this kit's was found on the registry.")

    if state == "kit":
        az.change(
            [
                "group",
                "delete",
                "--subscription",
                kit.subscription_id,
                "--name",
                kit.resource_group,
                "--yes",
            ]
        )
    else:
        say(f"The resource group {kit.resource_group} doesn't exist.")

    registry_record = [
        "deployment",
        "group",
        "show",
        "--subscription",
        kit.registry.subscription,
        "--resource-group",
        kit.registry.group,
        "--name",
        kit.registry_pull_deployment,
    ]
    if _deployment_exists(az, registry_record):
        az.change(["deployment", "group", "delete", *registry_record[3:]])
    subscription_record = [
        "deployment",
        "sub",
        "show",
        "--subscription",
        kit.subscription_id,
        "--name",
        kit.deployment_name,
    ]
    if _deployment_exists(az, subscription_record):
        az.change(["deployment", "sub", "delete", *subscription_record[3:]])

    if delete_images:
        for command in image_commands(kit):
            az.change(command)
    else:
        say(f"The images stay in {kit.registry.name}. To delete the repositories deploy pushed:")
        for command in image_commands(kit):
            say("  az " + " ".join(shlex.quote(arg) for arg in command))
    say("Teardown changed nothing." if az.dry_run else "Teardown is done.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The Phase 11 MCP test servers' deployment kit.")
    parser.add_argument("command", choices=("plan", "deploy", "outputs", "smoke", "teardown"))
    parser.add_argument("--parameters", type=Path, default=DEFAULT_PARAMETERS)
    parser.add_argument(
        "--dry-run", action="store_true", help="change nothing: deploy runs plan, teardown lists"
    )
    parser.add_argument(
        "--delete-images", action="store_true", help="teardown: delete the image repositories too"
    )
    parser.add_argument("--skip-smoke", action="store_true", help="deploy: skip the smoke checks")
    arguments = parser.parse_args(argv)
    command = "plan" if arguments.command == "deploy" and arguments.dry_run else arguments.command
    try:
        kit = Kit.load(arguments.parameters)
        az = Az(dry_run=arguments.dry_run)
        if command == "plan":
            plan(kit, az, print)
        elif command == "deploy":
            return 0 if deploy(kit, az, print, skip_smoke=arguments.skip_smoke) else 1
        elif command == "teardown":
            teardown(kit, az, print, delete_images=arguments.delete_images)
        else:
            outputs = read_outputs(kit, az)
            if outputs is None:
                raise KitError("There's no deployment to read. Run deploy first.")
            if command == "outputs":
                print_outputs(outputs, print)
            else:
                return 0 if smoke_all(outputs, print) else 1
    except KitError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
