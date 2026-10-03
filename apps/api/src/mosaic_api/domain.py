import math
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Self
from urllib.parse import urlsplit, urlunsplit
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

APIM_API_VERSION = "2024-05-01"
# MCP servers are only visible on a preview contract. It is deliberately not the version the rest
# of the inventory uses: a preview API that changes or disappears must degrade MCP discovery alone,
# never the gateway sync that administrators depend on.
APIM_MCP_API_VERSION = "2025-09-01-preview"
# An API diagnostic's ``largeLanguageModel`` settings, which turn on the LLM log that carries token
# counts, exist only on preview contracts. Like the MCP version, it is used for that one resource
# alone, so a change to the preview can only affect telemetry setup.
APIM_LLM_DIAGNOSTIC_API_VERSION = "2025-09-01-preview"
DIAGNOSTIC_SETTINGS_API_VERSION = "2021-05-01-preview"
AUTHORIZATION_API_VERSION = "2022-04-01"
# Role definitions are read on a preview contract because the stable 2022-04-01 one drops the
# per-permission ``condition`` Azure evaluates: Foundry Owner's delegation condition, for instance,
# is simply absent there. Judging a role by permissions with its conditions stripped could claim a
# grant Azure would refuse. If this version is retired the read fails and the runtime check falls
# back to its list of known-sufficient built-ins, so it degrades rather than guesses.
ROLE_DEFINITIONS_API_VERSION = "2022-05-01-preview"
SUBSCRIPTIONS_API_VERSION = "2022-12-01"
COGNITIVE_SERVICES_API_VERSION = "2024-10-01"
APIM_PROVIDER_NAMESPACE = "Microsoft.ApiManagement"
APIM_RESOURCE_TYPE = "service"
COGNITIVE_SERVICES_PROVIDER_NAMESPACE = "Microsoft.CognitiveServices"
COGNITIVE_SERVICES_RESOURCE_TYPE = "accounts"
APIM_READER_ROLE_NAME = "API Management Service Reader Role"
APIM_READER_ROLE_ID = "71522526-b88f-4d52-b57f-d31fc3546d0d"
APIM_CONTRIBUTOR_ROLE_NAME = "API Management Service Contributor"
APIM_CONTRIBUTOR_ROLE_ID = "312a565d-c81f-4fd8-895a-4e21e48d571c"

# MOSAIC enumerates model deployments with the built-in Reader role. Every "Cognitive Services *"
# and "Foundry *" role that grants control-plane deployment read also carries data-plane
# ``dataActions`` (usually ``Microsoft.CognitiveServices/*``, i.e. full inference) and frequently
# ``accounts/listkeys/action``. Reader is the only built-in that grants
# ``Microsoft.CognitiveServices/accounts/deployments/read`` with no data actions, no key access, and
# no write. It also grants ``Microsoft.Authorization/roleAssignments/read``, which is what verifying
# a gateway's runtime access requires, so one assignment covers both jobs.
READER_ROLE_NAME = "Reader"
READER_ROLE_ID = "acdd72a7-3385-48ef-bd42-f606fba81ae7"

# Runtime roles are reported for the gateway's managed identity and never granted by MOSAIC. The
# runtime check accepts any role whose data actions cover the published API (ADR 0013); these are
# the built-ins it recommends and the ones it falls back to when it cannot read a role definition.
# They are keyed by role definition ID rather than name: the Foundry roles were renamed in 2026
# ("Azure AI User" became "Foundry User") and Microsoft advises binding to the GUID while the
# rename rolls out. The GUIDs are unchanged by the rename.
AZURE_OPENAI_USER_ROLE_NAME = "Cognitive Services OpenAI User"
AZURE_OPENAI_USER_ROLE_ID = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"
AZURE_OPENAI_CONTRIBUTOR_ROLE_NAME = "Cognitive Services OpenAI Contributor"
AZURE_OPENAI_CONTRIBUTOR_ROLE_ID = "a001fd3d-188f-4b5d-821b-7da978bf7442"
COGNITIVE_SERVICES_USER_ROLE_NAME = "Cognitive Services User"
COGNITIVE_SERVICES_USER_ROLE_ID = "a97b65f3-24c7-4388-baec-2e87135dc908"
COGNITIVE_SERVICES_DATA_CONTRIBUTOR_ROLE_NAME = "Cognitive Services Data Contributor (Preview)"
COGNITIVE_SERVICES_DATA_CONTRIBUTOR_ROLE_ID = "19c28022-e58e-450d-a464-0b2a53034789"
FOUNDRY_USER_ROLE_NAME = "Foundry User"
FOUNDRY_USER_ROLE_ID = "53ca6127-db72-4b80-b1b0-d745d6d5456d"
FOUNDRY_OWNER_ROLE_NAME = "Foundry Owner"
FOUNDRY_OWNER_ROLE_ID = "c883944f-8b7b-4483-af10-35834be79c4a"
FOUNDRY_PROJECT_MANAGER_ROLE_NAME = "Foundry Project Manager"
FOUNDRY_PROJECT_MANAGER_ROLE_ID = "eadc314b-1a2d-4efa-be10-5d325db5065e"
AZURE_AI_DEVELOPER_ROLE_NAME = "Azure AI Developer"
AZURE_AI_DEVELOPER_ROLE_ID = "64702f94-c441-49e6-a78b-ef80e0188fee"

# A key-authenticated endpoint's key lives in Key Vault, and both MOSAIC and each gateway read it
# there with their own managed identities (ADR 0018). Key Vault Secrets User is the narrowest
# built-in that grants the read; the check accepts any role whose data actions cover it.
KEY_VAULT_API_VERSION = "2023-07-01"
KEY_VAULT_SECRETS_USER_ROLE_NAME = "Key Vault Secrets User"
KEY_VAULT_SECRETS_USER_ROLE_ID = "4633458b-17de-408a-b874-0445c86b69e6"
KEY_VAULT_SECRETS_OFFICER_ROLE_NAME = "Key Vault Secrets Officer"
KEY_VAULT_SECRETS_OFFICER_ROLE_ID = "b86a8fe4-44ce-4948-aee5-eccb2c155cd7"
KEY_VAULT_ADMINISTRATOR_ROLE_NAME = "Key Vault Administrator"
KEY_VAULT_ADMINISTRATOR_ROLE_ID = "00482a5a-887f-4fb3-b363-3b7fe8e74483"
KEY_VAULT_GET_SECRET_DATA_ACTION = "Microsoft.KeyVault/vaults/secrets/getSecret/action"

# MCP protocol revision MOSAIC offers when it connects to a registered MCP server.
#
# The current published revision, 2026-07-28, is a *stateless* protocol: it removed the
# ``initialize`` handshake, the session header, and the GET stream outright. Everything from
# 2025-11-25 back is the handshake era, and the handshake era is what API Management speaks.
# MOSAIC implements that era alone, offers the newest revision of it, and accepts a server's
# counter-offer from the set below. A server answering with anything else -- including a modern
# stateless server -- is recorded as an unsupported protocol, which is a capability and not a
# failure, exactly as an API Management service too old for MCP is.
MCP_PROTOCOL_VERSION = "2025-11-25"
MCP_SUPPORTED_PROTOCOL_VERSIONS: frozenset[str] = frozenset(
    {"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"}
)
# The ``MCP-Protocol-Version`` header was introduced in 2025-06-18. A server that negotiated an
# earlier revision never defined it, so MOSAIC omits it rather than sending a header the server
# is entitled to reject with 400.
MCP_PROTOCOL_VERSION_HEADER_MINIMUM = "2025-06-18"
MCP_CLIENT_NAME = "mosaic"

_APIM_RESOURCE_ID_PATTERN = re.compile(
    r"^/subscriptions/(?P<subscription>[0-9a-fA-F-]{36})"
    r"/resourceGroups/(?P<resourceGroup>[^/]{1,90})"
    r"/providers/Microsoft\.ApiManagement/service"
    r"/(?P<serviceName>[^/]{1,50})$",
    re.IGNORECASE,
)

_COGNITIVE_SERVICES_RESOURCE_ID_PATTERN = re.compile(
    r"^/subscriptions/(?P<subscription>[0-9a-fA-F-]{36})"
    r"/resourceGroups/(?P<resourceGroup>[^/]{1,90})"
    r"/providers/Microsoft\.CognitiveServices/accounts"
    r"/(?P<accountName>[^/]{1,64})"
    r"(?:/projects/(?P<projectName>[^/]{1,64}))?$",
    re.IGNORECASE,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def deterministic_id(prefix: str, *parts: str) -> str:
    value = "|".join(part.casefold() for part in parts)
    return f"{prefix}_{uuid5(NAMESPACE_URL, value).hex}"


# A caller names the cost center a call is charged to with this header. See ADR 0022.
COST_CENTER_HEADER = "x-mosaic-cost-center"
COST_CENTER_CODE_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")
# The reference to the MCP call a published MCP server's model call is made for. The MCP policy
# sets it to the call's request ID; the server copies it onto its model calls. See ADR 0025.
ON_BEHALF_HEADER = "x-mosaic-on-behalf-of"
GENERAL_COST_CENTER_CODE = "general"
GENERAL_COST_CENTER_NAME = "General"


def general_cost_center_id(tenant_id: str) -> str:
    """The built-in General cost center's ID, which every tenant has and can't delete."""

    return deterministic_id("costCenter", tenant_id, GENERAL_COST_CENTER_CODE)


class ApimResourceId(BaseModel):
    model_config = ConfigDict(frozen=True)

    subscription_id: str
    resource_group: str
    service_name: str

    @classmethod
    def parse(cls, value: str) -> "ApimResourceId":
        candidate = value.strip().rstrip("/")
        if not candidate.startswith("/"):
            candidate = f"/{candidate}"
        match = _APIM_RESOURCE_ID_PATTERN.match(candidate)
        if not match:
            raise ValueError(
                "Expected an Azure API Management resource ID of the form /subscriptions/"
                "{subscriptionId}/resourceGroups/{resourceGroup}/providers/"
                "Microsoft.ApiManagement/service/{serviceName}"
            )
        return cls(
            subscription_id=match.group("subscription").lower(),
            resource_group=match.group("resourceGroup"),
            service_name=match.group("serviceName"),
        )

    @property
    def canonical(self) -> str:
        return (
            f"/subscriptions/{self.subscription_id}"
            f"/resourceGroups/{self.resource_group}"
            f"/providers/{APIM_PROVIDER_NAMESPACE}/{APIM_RESOURCE_TYPE}/{self.service_name}"
        )

    @property
    def dedupe_key(self) -> str:
        return self.canonical.casefold()


class CognitiveServicesResourceId(BaseModel):
    """An Azure AI resource ID, optionally naming a Foundry project.

    Deployments are never children of a project: ``accounts/{account}/projects/{project}`` exposes
    only descriptive properties, and models are enumerated at the parent account. ``account_scope``
    therefore resolves upward, while ``canonical`` preserves whatever the administrator registered.
    """

    model_config = ConfigDict(frozen=True)

    subscription_id: str
    resource_group: str
    account_name: str
    project_name: str | None = None

    @classmethod
    def parse(cls, value: str) -> "CognitiveServicesResourceId":
        candidate = value.strip().rstrip("/")
        if not candidate.startswith("/"):
            candidate = f"/{candidate}"
        match = _COGNITIVE_SERVICES_RESOURCE_ID_PATTERN.match(candidate)
        if not match:
            raise ValueError(
                "Expected an Azure AI resource ID of the form /subscriptions/{subscriptionId}"
                "/resourceGroups/{resourceGroup}/providers/Microsoft.CognitiveServices/accounts"
                "/{accountName}, optionally followed by /projects/{projectName}"
            )
        return cls(
            subscription_id=match.group("subscription").lower(),
            resource_group=match.group("resourceGroup"),
            account_name=match.group("accountName"),
            project_name=match.group("projectName"),
        )

    @property
    def account_scope(self) -> str:
        """The account that owns the deployments, regardless of whether a project was registered."""

        return (
            f"/subscriptions/{self.subscription_id}"
            f"/resourceGroups/{self.resource_group}"
            f"/providers/{COGNITIVE_SERVICES_PROVIDER_NAMESPACE}"
            f"/{COGNITIVE_SERVICES_RESOURCE_TYPE}/{self.account_name}"
        )

    @property
    def canonical(self) -> str:
        if self.project_name:
            return f"{self.account_scope}/projects/{self.project_name}"
        return self.account_scope

    @property
    def dedupe_key(self) -> str:
        return self.canonical.casefold()


# The Key Vault DNS suffixes MOSAIC reads secrets from, one per Azure cloud. The reader in
# ``integrations/mcp/credentials.py`` accepts exactly these.
KEY_VAULT_HOST_SUFFIXES: tuple[str, ...] = (
    ".vault.azure.net",
    ".vault.azure.cn",
    ".vault.usgovcloudapi.net",
    ".vault.microsoftazure.de",
)
# Key Vault's own naming rules: 3 to 24 letters, digits and hyphens, starting with a letter, ending
# with a letter or digit, with no consecutive hyphens. A secret name is 1 to 127 letters, digits and
# hyphens, and a version is 32 hexadecimal characters.
_VAULT_NAME_PATTERN = re.compile(r"^(?!.*--)[a-z][a-z0-9-]{1,22}[a-z0-9]$")
_SECRET_NAME_PATTERN = re.compile(r"^[0-9A-Za-z-]{1,127}$")
_SECRET_VERSION_PATTERN = re.compile(r"^[0-9A-Fa-f]{32}$")
_SECRET_IDENTIFIER_FORM = "https://<vault>.vault.azure.net/secrets/<name>"


class KeyVaultSecretId(BaseModel):
    """A Key Vault secret identifier, which names a secret and never carries its value.

    MOSAIC stores and hands API Management the *versionless* identifier. API Management refreshes a
    versionless reference from Key Vault within four hours, so a rotated key reaches the gateway
    without anyone touching MOSAIC; a versioned one would pin the old key forever.
    """

    model_config = ConfigDict(frozen=True)

    vault_host: str
    secret_name: str
    version: str | None = None

    @classmethod
    def parse(cls, value: str) -> "KeyVaultSecretId":
        candidate = value.strip()
        try:
            parts = urlsplit(candidate)
            port = parts.port
        except ValueError:
            raise ValueError(
                f"Expected a Key Vault secret identifier such as {_SECRET_IDENTIFIER_FORM}"
            ) from None
        host = (parts.hostname or "").casefold()
        if (
            parts.scheme.casefold() != "https"
            or parts.username is not None
            or parts.password is not None
            or port is not None
            or parts.query
            or parts.fragment
        ):
            raise ValueError(
                f"Expected a Key Vault secret identifier such as {_SECRET_IDENTIFIER_FORM}, "
                "with no port, query or fragment"
            )
        suffix = next((item for item in KEY_VAULT_HOST_SUFFIXES if host.endswith(item)), None)
        vault_name = host.removesuffix(suffix) if suffix else ""
        if suffix is None or not _VAULT_NAME_PATTERN.fullmatch(vault_name):
            raise ValueError(
                "The secret must be in an Azure Key Vault, such as "
                f"{_SECRET_IDENTIFIER_FORM}"
            )
        segments = parts.path.split("/")
        if segments and segments[-1] == "":
            segments = segments[:-1]
        if (
            len(segments) not in {3, 4}
            or segments[0] != ""
            or segments[1].casefold() != "secrets"
            or not _SECRET_NAME_PATTERN.fullmatch(segments[2])
            or (len(segments) == 4 and not _SECRET_VERSION_PATTERN.fullmatch(segments[3]))
        ):
            raise ValueError(
                f"Expected a Key Vault secret identifier such as {_SECRET_IDENTIFIER_FORM}, "
                "optionally followed by a version"
            )
        return cls(
            vault_host=host,
            secret_name=segments[2],
            version=segments[3].casefold() if len(segments) == 4 else None,
        )

    @property
    def vault_name(self) -> str:
        return self.vault_host.split(".", 1)[0]

    @property
    def vault_uri(self) -> str:
        return f"https://{self.vault_host}"

    @property
    def versionless(self) -> str:
        return f"{self.vault_uri}/secrets/{self.secret_name}"


# An Azure AI resource's keys are 32 or 84 printable characters, and an Amazon Bedrock API key is a
# longer printable string. MOSAIC trims what was pasted around a key, then refuses anything else
# that isn't printable ASCII, so a key with a stray line break is never stored or sent. No message
# here ever repeats the value.
_API_KEY_PATTERN = re.compile(r"^[\x21-\x7e]{16,512}$")


def normalized_api_key(value: SecretStr) -> SecretStr:
    """An API key an administrator pasted, trimmed, or a ValueError that never repeats it."""

    key = value.get_secret_value().strip()
    if not key:
        raise ValueError("Paste the API key")
    if not _API_KEY_PATTERN.fullmatch(key):
        raise ValueError(
            "An API key is 16 to 512 letters, digits and symbols, with no spaces or line breaks. "
            "Copy it again"
        )
    return SecretStr(key)


# Every public host an Azure AI (Cognitive Services) account answers on is its custom subdomain
# under one of these. The hostname is the only identity a resource MOSAIC reaches with a key has.
AZURE_AI_HOST_SUFFIXES: tuple[str, ...] = (
    ".services.ai.azure.com",
    ".cognitiveservices.azure.com",
    ".openai.azure.com",
)
_CUSTOM_SUBDOMAIN_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_FOUNDRY_PROJECT_PATH_PATTERN = re.compile(
    r"^/api/projects/(?P<project>[A-Za-z0-9][A-Za-z0-9._-]{0,63})/?$"
)
# The base paths the Azure and Foundry portals show beside a resource's endpoint. Each is served at
# the resource, so pasting one is the same as pasting the resource endpoint.
_API_BASE_PATHS = frozenset({"/openai", "/openai/v1", "/models", "/anthropic", "/anthropic/v1"})
_AZURE_AI_ENDPOINT_FORM = (
    "https://<resource>.services.ai.azure.com, https://<resource>.cognitiveservices.azure.com, "
    "https://<resource>.openai.azure.com, or a Foundry project endpoint "
    "https://<resource>.services.ai.azure.com/api/projects/<project>"
)


def azure_ai_host_suffix(host: str | None) -> str | None:
    """The Azure AI suffix a hostname sits under, or None for any other host."""

    normalized = (host or "").casefold().rstrip(".")
    return next((suffix for suffix in AZURE_AI_HOST_SUFFIXES if normalized.endswith(suffix)), None)


def azure_ai_account_subdomain(url: str | None) -> str | None:
    """The account a URL on an Azure AI host belongs to: its custom subdomain.

    ``contoso.openai.azure.com``, ``contoso.cognitiveservices.azure.com`` and
    ``contoso.services.ai.azure.com`` are one account, so this is what duplicates are judged by.
    """

    try:
        host = (urlsplit(url or "").hostname or "").casefold().rstrip(".")
    except ValueError:
        return None
    suffix = azure_ai_host_suffix(host)
    if suffix is None:
        return None
    subdomain = host.removesuffix(suffix)
    return subdomain if _CUSTOM_SUBDOMAIN_PATTERN.fullmatch(subdomain) else None


class AzureAiEndpointUrl(BaseModel):
    """An Azure OpenAI or Foundry endpoint MOSAIC reaches by URL and API key rather than by ID.

    A Foundry project endpoint names a project, but a project isn't where models are served: the
    inference routes and the key belong to the resource, so the project is recorded and the
    resource's origin is what MOSAIC publishes from.
    """

    model_config = ConfigDict(frozen=True)

    host: str
    subdomain: str
    project_name: str | None = None

    @classmethod
    def parse(cls, value: str) -> "AzureAiEndpointUrl":
        candidate = value.strip()
        try:
            parts = urlsplit(candidate)
            port = parts.port
        except ValueError:
            raise ValueError(f"Expected {_AZURE_AI_ENDPOINT_FORM}") from None
        host = (parts.hostname or "").casefold().rstrip(".")
        if (
            parts.scheme.casefold() != "https"
            or parts.username is not None
            or parts.password is not None
            or port is not None
            or parts.query
            or parts.fragment
        ):
            raise ValueError(
                f"Expected {_AZURE_AI_ENDPOINT_FORM}, over https with no port, query or fragment"
            )
        subdomain = azure_ai_account_subdomain(f"https://{host}")
        if subdomain is None:
            raise ValueError(
                "Use the resource's own endpoint rather than a regional or custom one. Expected "
                f"{_AZURE_AI_ENDPOINT_FORM}"
            )
        path = parts.path
        project: str | None = None
        if path not in {"", "/"} and path.rstrip("/").casefold() not in _API_BASE_PATHS:
            match = _FOUNDRY_PROJECT_PATH_PATTERN.fullmatch(path)
            if match is None or azure_ai_host_suffix(host) != ".services.ai.azure.com":
                raise ValueError(
                    "Paste the resource endpoint or the Foundry project endpoint, without an "
                    f"operation path. Expected {_AZURE_AI_ENDPOINT_FORM}"
                )
            project = match.group("project")
        return cls(host=host, subdomain=subdomain, project_name=project)

    @property
    def origin(self) -> str:
        return f"https://{self.host}"

    @property
    def provider(self) -> "ModelProvider":
        """The resource kind the hostname implies, which ARM would otherwise report."""

        if self.host.endswith(".openai.azure.com"):
            return ModelProvider.AZURE_OPENAI
        return ModelProvider.AZURE_AI_FOUNDRY


# Amazon Bedrock serves the Anthropic Messages API, at /anthropic/v1/messages, on two regional
# hosts: bedrock-runtime, which AWS recommends, and bedrock-mantle. Every AWS account in a region
# shares its hosts, so the host and the key are all the identity a Bedrock endpoint has.
_BEDROCK_HOST_PATTERN = re.compile(
    r"^bedrock-(?:runtime\.(?P<runtime>[a-z]{2}(?:-gov)?-[a-z]+-\d{1,2})\.amazonaws\.com"
    r"|mantle\.(?P<mantle>[a-z]{2}(?:-gov)?-[a-z]+-\d{1,2})\.api\.aws)$"
)
# The Anthropic base paths AWS documents beside the host. Each is served at the host, so pasting
# one is the same as pasting the host.
_BEDROCK_BASE_PATHS = frozenset({"", "/anthropic", "/anthropic/v1"})
_BEDROCK_ENDPOINT_FORM = (
    "https://bedrock-runtime.<region>.amazonaws.com or https://bedrock-mantle.<region>.api.aws"
)


def bedrock_region(host: str | None) -> str | None:
    """The AWS region of an Amazon Bedrock host that serves the Anthropic API, or None."""

    match = _BEDROCK_HOST_PATTERN.fullmatch((host or "").casefold().rstrip("."))
    if match is None:
        return None
    return match.group("runtime") or match.group("mantle")


def is_bedrock_host(host: str | None) -> bool:
    """Whether a host is one of Amazon Bedrock's, including the ones MOSAIC can't reach.

    A FIPS or control-plane host is one, so it's told which Bedrock host MOSAIC needs rather than
    taken for an OpenAI-compatible endpoint.
    """

    folded = (host or "").casefold().rstrip(".")
    return folded.startswith("bedrock") and folded.endswith(
        (".amazonaws.com", ".amazonaws.com.cn", ".api.aws")
    )


class BedrockEndpointUrl(BaseModel):
    """An Amazon Bedrock endpoint MOSAIC reaches with a Bedrock API key (ADR 0024)."""

    model_config = ConfigDict(frozen=True)

    host: str
    region: str

    @classmethod
    def parse(cls, value: str) -> "BedrockEndpointUrl":
        candidate = value.strip()
        try:
            parts = urlsplit(candidate)
            port = parts.port
        except ValueError:
            raise ValueError(f"Expected {_BEDROCK_ENDPOINT_FORM}") from None
        host = (parts.hostname or "").casefold().rstrip(".")
        if (
            parts.scheme.casefold() != "https"
            or parts.username is not None
            or parts.password is not None
            or port is not None
            or parts.query
            or parts.fragment
        ):
            raise ValueError(
                f"Expected {_BEDROCK_ENDPOINT_FORM}, over https with no port, query or fragment"
            )
        region = bedrock_region(host)
        if region is None:
            raise ValueError(
                "MOSAIC reaches AWS Bedrock through its regional runtime endpoint, which serves "
                f"the Anthropic Messages API. Expected {_BEDROCK_ENDPOINT_FORM}"
            )
        if parts.path.rstrip("/").casefold() not in _BEDROCK_BASE_PATHS:
            raise ValueError(
                f"Paste the endpoint without an operation path. Expected {_BEDROCK_ENDPOINT_FORM}"
            )
        return cls(host=host, region=region)

    @property
    def origin(self) -> str:
        return f"https://{self.host}"

    @property
    def slug(self) -> str:
        """The host as one lowercase name, such as ``bedrock-runtime-us-east-1``."""

        service = self.host.split(".", 1)[0]
        return f"{service}-{self.region}"


class MosaicModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
        use_enum_values=True,
    )


class Entity(MosaicModel):
    id: str
    tenant_id: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    etag: str | None = Field(default=None, exclude=True)


class PrincipalKind(StrEnum):
    """What an Entra object is, which decides how it is granted and how it signs in.

    ``agentIdentity`` is a Microsoft Entra Agent ID agent: a service principal that signs in as an
    application. ``agentUser`` is an agent's user account, which signs in with delegated user
    tokens. ``securityGroup`` is an Entra security group; granting it grants every member, whether
    a person or an agent, and the gateway enforces that through the token's ``groups`` claim.
    """

    USER = "user"
    SERVICE_PRINCIPAL = "servicePrincipal"
    MANAGED_IDENTITY = "managedIdentity"
    AGENT_IDENTITY = "agentIdentity"
    AGENT_USER = "agentUser"
    SECURITY_GROUP = "securityGroup"


class Principal(Entity):
    entity_type: Literal["principal"] = "principal"
    object_id: str
    kind: PrincipalKind
    label: str | None = None
    # A secondary identifier read from the directory, such as a user principal name, an agent's
    # app ID, or a group's mail nickname. Display only; never used to authorize anything.
    detail: str | None = None
    # For an agent user: the object ID of the agent identity it belongs to, which is the client
    # that requests its delegated tokens.
    identity_parent_id: str | None = None
    # For an agent identity: the app ID of the blueprint it was created from. Agent identities
    # inherit an app role from their blueprint only when the resource app is listed in the
    # blueprint's inheritable permissions and the role is granted to the blueprint's principal.
    blueprint_id: str | None = None
    # When Microsoft Graph last confirmed this object exists and is this kind. None for a
    # principal an administrator entered by hand.
    directory_verified_at: datetime | None = None
    # The cost center a caller's calls are charged to when they name none. Set when the principal
    # is onboarded, to the one an administrator picks or the tenant's default. None for a security
    # group, which is never the caller, and for a principal recorded before cost centers, for whom
    # the tenant's default applies.
    default_cost_center_id: str | None = None


class Group(Entity):
    entity_type: Literal["group"] = "group"
    name: str
    description: str | None = None


class GroupMembership(Entity):
    entity_type: Literal["groupMembership"] = "groupMembership"
    group_id: str
    principal_id: str


class GatewayProvider(StrEnum):
    APIM = "apim"


class ManagementMode(StrEnum):
    OBSERVE = "observe"
    MANAGE = "manage"


class GatewayStatus(StrEnum):
    PENDING = "pending"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    UNAUTHORIZED = "unauthorized"
    UNREACHABLE = "unreachable"


class CapabilitySupport(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class AccessEvaluation(StrEnum):
    EFFECTIVE_PERMISSIONS = "effectivePermissions"
    PROBE = "probe"
    NOT_EVALUATED = "notEvaluated"


class AccessRemediation(MosaicModel):
    role_name: str
    role_definition_id: str
    scope: str
    principal_id: str | None = None
    command: str
    custom_role_definition: dict[str, Any] | None = Field(
        default=None,
        description=(
            "A narrower custom role an operator may create instead of the built-in role. It is "
            "offered, never created: MOSAIC cannot define roles any more than it can assign them."
        ),
    )


class GatewayAccess(MosaicModel):
    can_read: bool = False
    can_write: bool = False
    evaluation: AccessEvaluation = AccessEvaluation.NOT_EVALUATED
    checked_at: datetime | None = None
    missing_actions: list[str] = Field(default_factory=list)
    remediation: AccessRemediation | None = None
    message: str | None = None


class AiBackendKind(StrEnum):
    """Which model provider an API or backend fronts.

    Lives here rather than in ``observed`` because adopted desired state records the classification
    that was true at import, and ``observed`` imports from this module rather than the reverse.
    """

    AZURE_OPENAI = "azureOpenAi"
    AZURE_AI_FOUNDRY = "azureAiFoundry"
    AZURE_AI_INFERENCE = "azureAiInference"
    OPEN_AI = "openAi"
    ANTHROPIC = "anthropic"
    GOOGLE_VERTEX = "googleVertex"
    AWS_BEDROCK = "awsBedrock"
    OTHER_LLM = "otherLlm"
    NONE = "none"


class GatewayCapabilities(MosaicModel):
    sku_name: str | None = None
    sku_capacity: int | None = None
    provisioning_state: str | None = None
    location: str | None = None
    gateway_url: AnyHttpUrl | None = None
    management_api_version: str = APIM_API_VERSION
    ai_gateway_policies: CapabilitySupport = CapabilitySupport.UNKNOWN
    mcp_servers: CapabilitySupport = CapabilitySupport.UNKNOWN
    principal_id: str | None = Field(
        default=None,
        description=(
            "Object ID of the gateway's managed identity. This is the principal that must hold a "
            "data-plane role on a model endpoint for the gateway to call it at runtime."
        ),
    )
    identity_observed: bool = Field(
        default=False,
        description=(
            "Whether MOSAIC has actually read this gateway's identity block. A missing "
            "``principal_id`` on a gateway that was never observed means 'not known yet', not "
            "'has no identity', and the two must not be reported the same way."
        ),
    )
    virtual_network_type: str | None = Field(
        default=None,
        description=(
            "The service's ``virtualNetworkType``: ``None``, ``External``, or ``Internal``. "
            "``None`` means the gateway has no virtual network and so no private path to a model "
            "endpoint whose public network access is disabled. A missing value means the gateway "
            "has not been read since MOSAIC started recording it."
        ),
    )
    egress_ip_addresses: list[str] = Field(
        default_factory=list,
        description=(
            "Where the gateway's outbound calls come from: the NAT gateway prefixes when the "
            "service has them, otherwise its public IP addresses. Empty when the tier publishes "
            "no stable addresses. Compared against a model endpoint's firewall rules."
        ),
    )
    notes: list[str] = Field(default_factory=list)


class GatewayTier(StrEnum):
    """The API Management tier family, which decides which AI gateway policies a gateway runs.

    Some policies differ by family rather than by SKU. For example, ``llm-token-limit`` meters the
    Anthropic Messages API only on the v2 tiers.
    """

    V2 = "v2"
    CLASSIC = "classic"
    CONSUMPTION = "consumption"
    UNKNOWN = "unknown"


_TIER_BY_SKU: dict[str, GatewayTier] = {
    "basicv2": GatewayTier.V2,
    "standardv2": GatewayTier.V2,
    "premiumv2": GatewayTier.V2,
    "developer": GatewayTier.CLASSIC,
    "basic": GatewayTier.CLASSIC,
    "standard": GatewayTier.CLASSIC,
    "premium": GatewayTier.CLASSIC,
    "isolated": GatewayTier.CLASSIC,
    "consumption": GatewayTier.CONSUMPTION,
}


def gateway_tier(sku_name: str | None) -> GatewayTier:
    """Classify an ARM ``sku.name``. An unread or unrecognized SKU is unknown, never assumed."""

    return _TIER_BY_SKU.get((sku_name or "").strip().casefold(), GatewayTier.UNKNOWN)


class GatewayInventorySummary(MosaicModel):
    apis: int = 0
    ai_apis: int = 0
    mcp_servers: int = 0
    operations: int = 0
    products: int = 0
    subscriptions: int = 0
    users: int = 0
    groups: int = 0
    backends: int = 0
    named_values: int = 0
    policy_documents: int = 0
    policy_fragments: int = 0
    recognized_facets: int = 0
    unrecognized_facets: int = 0
    mosaic_managed_facets: int = 0


class Gateway(Entity):
    entity_type: Literal["gateway"] = "gateway"
    name: str
    provider: GatewayProvider = GatewayProvider.APIM
    azure_resource_id: str
    subscription_id: str
    resource_group: str
    service_name: str
    environment_label: str | None = Field(
        default=None,
        description="Deprecated: free-text legacy label. Use environment.",
        json_schema_extra={"deprecated": True},
    )
    environment: str | None = None
    azure_environment_tag: str | None = None
    management_mode: ManagementMode = ManagementMode.OBSERVE
    status: GatewayStatus = GatewayStatus.PENDING
    access: GatewayAccess = Field(default_factory=GatewayAccess)
    capabilities: GatewayCapabilities = Field(default_factory=GatewayCapabilities)
    inventory: GatewayInventorySummary = Field(default_factory=GatewayInventorySummary)
    last_synced_at: datetime | None = None
    last_sync_error: str | None = None


class GatewaySyncStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class GatewaySyncRun(Entity):
    entity_type: Literal["gatewaySyncRun"] = "gatewaySyncRun"
    gateway_id: str
    status: GatewaySyncStatus = GatewaySyncStatus.RUNNING
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    duration_ms: int | None = None
    counts: GatewayInventorySummary = Field(default_factory=GatewayInventorySummary)
    removed: int = 0
    errors: list[str] = Field(default_factory=list)
    actor_object_id: str | None = None


class ModelProvider(StrEnum):
    AZURE_OPENAI = "azureOpenAi"
    AZURE_AI_FOUNDRY = "azureAiFoundry"
    OPENAI_COMPATIBLE = "openAiCompatible"
    # Amazon Bedrock's Anthropic Messages API, reached with a Bedrock API key. MOSAIC serves it only
    # as a member of a model pool (ADR 0024).
    AWS_BEDROCK = "awsBedrock"


class ApiShape(StrEnum):
    """The curated operation set, backend host, and runtime auth a published model API uses.

    The provider alone doesn't decide this: a Foundry resource serves Anthropic models through the
    Anthropic Messages API, not the model inference API its other deployments use.
    """

    AZURE_OPENAI = "azureOpenAi"
    FOUNDRY_MODELS = "foundryModels"
    ANTHROPIC_MESSAGES = "anthropicMessages"


def default_api_shape(provider: str) -> ApiShape | None:
    """The shape a provider's deployments used before shapes were chosen per model format."""

    if provider == ModelProvider.AZURE_OPENAI:
        return ApiShape.AZURE_OPENAI
    if provider == ModelProvider.AZURE_AI_FOUNDRY:
        return ApiShape.FOUNDRY_MODELS
    return None


class EndpointAuthMode(StrEnum):
    MANAGED_IDENTITY = "managedIdentity"
    API_KEY = "apiKey"


class ModelEndpointStatus(StrEnum):
    PENDING = "pending"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    UNAUTHORIZED = "unauthorized"
    UNREACHABLE = "unreachable"


class RuntimeAccessEvaluation(StrEnum):
    ROLE_ASSIGNMENTS = "roleAssignments"
    NO_GATEWAY_IDENTITY = "noGatewayIdentity"
    NOT_APPLICABLE = "notApplicable"
    NOT_EVALUATED = "notEvaluated"


class RuntimeAccessReason(StrEnum):
    """Why a gateway runtime check reached its verdict.

    ``evaluation`` says whether MOSAIC reached a definite answer: it is ``notEvaluated`` whenever
    something MOSAIC cannot read or evaluate stands between it and one, such as an unreadable role
    definition, an ABAC condition, a deny assignment that depends on a group, or a network path it
    cannot see. ``reason`` says what it found. The console keys its wording on this so that
    "MOSAIC could not read X" is never presented as a denial.
    """

    GRANTED = "granted"
    MISSING_ROLE = "missingRole"
    NARROWER_SCOPE = "narrowerScope"
    CONDITIONAL = "conditional"
    ROLE_UNREADABLE = "roleUnreadable"
    DENY_ASSIGNMENT = "denyAssignment"
    NETWORK_UNREACHABLE = "networkUnreachable"
    NETWORK_UNVERIFIED = "networkUnverified"
    ASSIGNMENTS_UNREADABLE = "assignmentsUnreadable"
    NO_GATEWAY_IDENTITY = "noGatewayIdentity"
    IDENTITY_NOT_OBSERVED = "identityNotObserved"


class RuntimeRoleFindingKind(StrEnum):
    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"
    NARROWER_SCOPE = "narrowerScope"
    CONDITIONAL = "conditional"
    UNREADABLE = "unreadable"


class RuntimeRoleFinding(MosaicModel):
    """One of the gateway's role assignments, and what it does for the published API.

    ``insufficient`` is only recorded for assignments that could matter: covering the evaluated
    scope, or narrower than it. An assignment on an unrelated resource is not a finding.
    """

    kind: RuntimeRoleFindingKind
    role_name: str | None = None
    role_definition_id: str | None = None
    scope: str
    inherited: bool = False
    missing_data_actions: list[str] = Field(default_factory=list)


class NetworkReachability(StrEnum):
    """Whether the gateway has a network path to the endpoint, as far as MOSAIC can tell.

    ``unreachable`` is only claimed when it is certain: public network access is disabled and the
    gateway has no virtual network. A private endpoint, private DNS, or a firewall MOSAIC cannot
    match the gateway's published addresses against is ``unverified``, because MOSAIC cannot see
    the gateway's routing. ``unknown`` means MOSAIC could not read the endpoint's network settings.
    """

    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"
    UNVERIFIED = "unverified"
    UNKNOWN = "unknown"


class EndpointAccess(MosaicModel):
    """Whether MOSAIC's own identity can enumerate models on an endpoint."""

    can_read: bool = False
    evaluation: AccessEvaluation = AccessEvaluation.NOT_EVALUATED
    checked_at: datetime | None = None
    missing_actions: list[str] = Field(default_factory=list)
    remediation: AccessRemediation | None = None
    message: str | None = None


class GatewayRuntimeAccess(MosaicModel):
    """Whether one registered gateway's managed identity can call this endpoint at runtime.

    This is a different question from :class:`EndpointAccess`, asked of a different principal
    against a different plane. MOSAIC reads role assignments to answer it and never grants one.
    """

    gateway_id: str
    gateway_name: str
    apim_principal_id: str | None = None
    can_invoke: bool = False
    evaluation: RuntimeAccessEvaluation = RuntimeAccessEvaluation.NOT_EVALUATED
    reason: RuntimeAccessReason | None = Field(
        default=None,
        description="What the check found. Missing on results recorded before it was introduced.",
    )
    checked_at: datetime | None = None
    required_role_name: str | None = Field(
        default=None,
        description=(
            "The role MOSAIC recommends granting. Any role whose data actions cover "
            "``required_data_actions`` is accepted: Cognitive Services OpenAI User for an Azure "
            "OpenAI resource, and Foundry User, as Microsoft's Foundry guidance advises, otherwise."
        ),
    )
    required_role_definition_id: str | None = None
    granted_role_name: str | None = Field(
        default=None,
        description="The role that satisfied the check, which need not be the recommended one.",
    )
    granted_role_definition_id: str | None = None
    assignment_scope: str | None = None
    inherited: bool = False
    evaluated_scope: str | None = Field(
        default=None,
        description=(
            "The scope the published API calls, which is always the account: a Foundry project's "
            "models are deployed on its parent resource."
        ),
    )
    required_data_actions: list[str] = Field(default_factory=list)
    role_findings: list[RuntimeRoleFinding] = Field(default_factory=list)
    network_reachability: NetworkReachability = NetworkReachability.UNKNOWN
    remediation: AccessRemediation | None = None
    message: str | None = None


class ModelEndpointCapabilities(MosaicModel):
    kind: str | None = None
    sku_name: str | None = None
    location: str | None = None
    provisioning_state: str | None = None
    public_network_access: str | None = None
    network_default_action: str | None = Field(
        default=None,
        description=(
            "``networkAcls.defaultAction``. ``Deny`` means only the listed addresses and virtual "
            "networks may reach the endpoint while public network access is enabled."
        ),
    )
    network_ip_rules: list[str] = Field(
        default_factory=list,
        description="Addresses and CIDR ranges the endpoint's firewall admits.",
    )
    network_virtual_network_rule_count: int = 0
    local_auth_disabled: bool | None = None
    management_api_version: str = COGNITIVE_SERVICES_API_VERSION
    notes: list[str] = Field(default_factory=list)


class ModelInventorySummary(MosaicModel):
    deployments: int = 0
    available_models: int = 0
    succeeded_deployments: int = 0
    deprecated_deployments: int = 0


def _unset(value: object) -> bool:
    """Whether a field added after release holds nothing, so it's left out of stored documents.

    Every model forbids unknown fields, so a release reading a document another release wrote
    fails on any field it doesn't know. Leaving a new field out while it's empty keeps records that
    don't use it readable by the release before it.
    """

    return value is None or value == [] or value is False


# Deployment names become literal operation paths and a pinned request model in the gateway policy,
# so they are held to the characters Azure deployment names use. A Bedrock model ID, which is what
# a Bedrock endpoint declares in a deployment's place, can also hold colons, as in ``...-v1:0``. It
# never reaches an operation path: MOSAIC serves Bedrock only through a pool, whose policy writes
# it into the request as a string literal.
DEPLOYMENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
BEDROCK_MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")
_MODEL_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_DECLARED_DEPLOYMENTS = 50


class DeclaredDeployment(MosaicModel):
    """A deployment an administrator declared on an endpoint MOSAIC reaches with an API key.

    Foundry lists a resource's deployments only to a Microsoft Entra token, and an API key can't
    read them (ADR 0018), so these are what the administrator says is deployed. MOSAIC publishes a
    declared deployment with the shape declared for it, and never presents it as discovered. On an
    AWS Bedrock endpoint the deployment name is the Bedrock model ID requests are sent to.
    """

    deployment_name: str
    model_name: str
    model_version: str | None = None
    api_shape: ApiShape
    declared_at: datetime = Field(default_factory=utc_now)
    declared_by: str | None = None


class DeclaredDeploymentCreate(MosaicModel):
    deployment_name: str = Field(min_length=1, max_length=64)
    model_name: str = Field(min_length=1, max_length=120)
    model_version: str | None = Field(default=None, max_length=64)
    api_shape: ApiShape

    @field_validator("deployment_name")
    @classmethod
    def validate_deployment_name(cls, value: str) -> str:
        value = value.strip()
        if not BEDROCK_MODEL_ID_PATTERN.fullmatch(value):
            raise ValueError(
                "A deployment name is up to 64 letters, digits, periods, hyphens and underscores, "
                "starting with a letter or digit. A Bedrock model ID can also have colons"
            )
        return value

    @field_validator("model_name")
    @classmethod
    def validate_model_name(cls, value: str) -> str:
        value = value.strip()
        if not _MODEL_NAME_PATTERN.fullmatch(value):
            raise ValueError(
                "A model name is up to 120 letters, digits, periods, colons, hyphens and "
                "underscores, starting with a letter or digit"
            )
        return value

    @field_validator("model_version")
    @classmethod
    def validate_model_version(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if not _MODEL_VERSION_PATTERN.fullmatch(value):
            raise ValueError(
                "A model version is up to 64 letters, digits, periods, hyphens and underscores"
            )
        return value


def shape_fits_provider(shape: str, provider: str) -> bool:
    """Whether a resource of this kind serves an API shape at all.

    Every Azure AI resource serves the Azure OpenAI routes. Only a Foundry (AI Services) resource
    serves the Foundry Models routes and the Anthropic Messages API. MOSAIC reaches AWS Bedrock
    only through its Anthropic Messages API.
    """

    if provider == ModelProvider.AWS_BEDROCK:
        return shape == ApiShape.ANTHROPIC_MESSAGES
    if shape == ApiShape.AZURE_OPENAI:
        return provider in {ModelProvider.AZURE_OPENAI, ModelProvider.AZURE_AI_FOUNDRY}
    return provider == ModelProvider.AZURE_AI_FOUNDRY


def validate_declarations(
    declarations: list[DeclaredDeploymentCreate], provider: str
) -> list[DeclaredDeploymentCreate]:
    """Refuse duplicate names and shapes the resource can't serve. Returns the declarations."""

    if len(declarations) > MAX_DECLARED_DEPLOYMENTS:
        raise ValueError(f"Declare at most {MAX_DECLARED_DEPLOYMENTS} deployments per endpoint")
    seen: set[str] = set()
    for declaration in declarations:
        name = declaration.deployment_name
        if name.casefold() in seen:
            raise ValueError(f"Deployment {name} is declared twice")
        seen.add(name.casefold())
        if provider != ModelProvider.AWS_BEDROCK and not DEPLOYMENT_NAME_PATTERN.fullmatch(name):
            raise ValueError(
                f"{name} isn't an Azure deployment name, which is up to 64 letters, digits, "
                "periods, hyphens and underscores, starting with a letter or digit"
            )
        if shape_fits_provider(declaration.api_shape, provider):
            continue
        if provider == ModelProvider.AWS_BEDROCK:
            raise ValueError(
                f"MOSAIC reaches AWS Bedrock only through its Anthropic Messages API, so {name} "
                "has to be declared with the Anthropic Messages API"
            )
        raise ValueError(
            f"An Azure OpenAI resource serves only the Azure OpenAI API, so {name} can't use the "
            "Foundry Models or Anthropic Messages API. Register the Foundry resource's "
            "services.ai.azure.com endpoint instead."
        )
    return declarations


class ModelEndpoint(Entity):
    """A registered provider endpoint MOSAIC reads models from.

    Azure endpoints are identified by resource ID and read with MOSAIC's managed identity. An Azure
    endpoint MOSAIC can't reach that way, such as one in another Microsoft Entra tenant, can be
    identified by URL instead and authenticated with an API key held in Key Vault; its deployments
    are declared rather than read (ADR 0018). An AWS Bedrock endpoint is identified by its regional
    host and reached the same way, with a Bedrock API key and declared model IDs (ADR 0024).
    OpenAI-compatible endpoints are identified by URL and read with a key MOSAIC resolves from Key
    Vault at call time; only the secret URI is ever stored.
    """

    entity_type: Literal["modelEndpoint"] = "modelEndpoint"
    name: str
    provider: ModelProvider
    endpoint: AnyHttpUrl
    azure_resource_id: str | None = None
    subscription_id: str | None = None
    resource_group: str | None = None
    account_name: str | None = None
    project_name: str | None = None
    environment_label: str | None = Field(
        default=None,
        description="Deprecated: free-text legacy label. Use environment.",
        json_schema_extra={"deprecated": True},
    )
    environment: str | None = None
    azure_environment_tag: str | None = None
    auth_mode: EndpointAuthMode = EndpointAuthMode.MANAGED_IDENTITY
    credential_reference_id: str | None = None
    # Set when MOSAIC wrote the key into its own Key Vault because an administrator gave it the key
    # (ADR 0021). MOSAIC then replaces the key on request and deletes the secret with the endpoint.
    key_stored_by_mosaic: bool = Field(default=False, exclude_if=_unset)
    # Authored by administrators, and only on an endpoint registered with an API key.
    declared_deployments: list[DeclaredDeployment] = Field(
        default_factory=list, exclude_if=_unset
    )
    status: ModelEndpointStatus = ModelEndpointStatus.PENDING
    access: EndpointAccess = Field(default_factory=EndpointAccess)
    runtime_access: list[GatewayRuntimeAccess] = Field(default_factory=list)
    capabilities: ModelEndpointCapabilities = Field(default_factory=ModelEndpointCapabilities)
    inventory: ModelInventorySummary = Field(default_factory=ModelInventorySummary)
    last_synced_at: datetime | None = None
    last_sync_error: str | None = None

    def uses_backend_key(self) -> bool:
        """Whether MOSAIC and its gateways reach this endpoint with an API key from Key Vault.

        An Azure endpoint registered by URL, or an AWS Bedrock endpoint. An OpenAI-compatible
        endpoint has a key too, but MOSAIC never publishes it.
        """

        return (
            self.auth_mode == EndpointAuthMode.API_KEY
            and self.provider != ModelProvider.OPENAI_COMPATIBLE
        )

    def is_bedrock(self) -> bool:
        return self.provider == ModelProvider.AWS_BEDROCK

    def declared(self, deployment_name: str) -> DeclaredDeployment | None:
        return next(
            (item for item in self.declared_deployments if item.deployment_name == deployment_name),
            None,
        )


class ModelEndpointSyncRun(Entity):
    entity_type: Literal["modelEndpointSyncRun"] = "modelEndpointSyncRun"
    endpoint_id: str
    status: GatewaySyncStatus = GatewaySyncStatus.RUNNING
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    duration_ms: int | None = None
    counts: ModelInventorySummary = Field(default_factory=ModelInventorySummary)
    removed: int = 0
    errors: list[str] = Field(default_factory=list)
    actor_object_id: str | None = None


class CatalogModel(Entity):
    entity_type: Literal["catalogModel"] = "catalogModel"
    provider: str
    model_name: str
    model_version: str | None = None


class ModelDeployment(Entity):
    entity_type: Literal["modelDeployment"] = "modelDeployment"
    model_endpoint_id: str
    catalog_model_id: str | None = None
    deployment_name: str
    endpoint: AnyHttpUrl


class ImportSelection(StrEnum):
    """Whether MOSAIC recommended an import or the administrator chose it themselves."""

    DETECTED = "detected"
    MANUAL = "manual"


class McpTransportType(StrEnum):
    STREAMABLE = "streamable"
    SSE = "sse"
    UNKNOWN = "unknown"


class McpServerKind(StrEnum):
    REST_API_BACKED = "restApiBacked"
    PASSTHROUGH = "passthrough"


class McpServerRoute(MosaicModel):
    """One named URI template an API Management MCP server is reachable on.

    Named for what API Management calls it, not "endpoint": a streamable server declares a single
    ``message`` route and an SSE server declares ``sse`` and ``message``. The registered-endpoint
    entity is :class:`McpEndpoint`.
    """

    name: str
    uri_template: str


class McpTool(MosaicModel):
    name: str
    display_name: str
    description: str | None = None
    backing_api_name: str | None = None
    backing_operation_name: str | None = None


class CatalogVisibility(StrEnum):
    """Whether a governed resource is discoverable in the end-user portal.

    ``catalog`` means any portal user can see that it exists and request access; it does not grant
    use. ``private`` means only entitled users see it at all.
    """

    CATALOG = "catalog"
    PRIVATE = "private"


class ModelApi(Entity):
    """An API Management API an administrator adopted as a governed model endpoint.

    Adoption is a Cosmos write, never an Azure one. The record is desired state: it says MOSAIC
    governs this API, and it survives the sweep that rebuilds ``observed-state`` on every sync.
    """

    entity_type: Literal["modelApi"] = "modelApi"
    gateway_id: str
    api_name: str
    display_name: str
    path: str
    service_url: str | None = None
    protocols: list[str] = Field(default_factory=list)
    ai_kind: AiBackendKind = AiBackendKind.NONE
    ai_signals: list[str] = Field(default_factory=list)
    subscription_required: bool = True
    operation_count: int = 0
    product_names: list[str] = Field(default_factory=list)
    visibility: CatalogVisibility = CatalogVisibility.CATALOG
    summary: str | None = None
    selection: ImportSelection = ImportSelection.DETECTED
    imported_from_snapshot_id: str | None = None
    publication_id: str | None = None
    imported_at: datetime = Field(default_factory=utc_now)
    imported_by: str | None = None

    @model_validator(mode="after")
    def validate_provenance(self) -> Self:
        if self.imported_from_snapshot_id is None and self.publication_id is None:
            raise ValueError("A model API needs an observed snapshot or a publication")
        return self


class McpServer(Entity):
    """An API Management MCP server an administrator adopted, or one MOSAIC publishes.

    An adopted server comes from an observed snapshot and MOSAIC doesn't own its policy, so grants
    on it are recorded but not enforced. A published server names its :class:`McpPublication`,
    whose fragment enforces the grants at the gateway.
    """

    entity_type: Literal["mcpServer"] = "mcpServer"
    gateway_id: str
    api_name: str
    display_name: str
    path: str
    service_url: str | None = None
    protocols: list[str] = Field(default_factory=list)
    kind: McpServerKind = McpServerKind.REST_API_BACKED
    transport_type: McpTransportType = McpTransportType.UNKNOWN
    endpoints: list[McpServerRoute] = Field(default_factory=list)
    tools: list[McpTool] = Field(default_factory=list)
    tool_count: int = 0
    subscription_required: bool = True
    product_names: list[str] = Field(default_factory=list)
    visibility: CatalogVisibility = CatalogVisibility.CATALOG
    summary: str | None = None
    selection: ImportSelection = ImportSelection.DETECTED
    imported_from_snapshot_id: str | None = None
    publication_id: str | None = None
    imported_at: datetime = Field(default_factory=utc_now)
    imported_by: str | None = None

    @model_validator(mode="after")
    def validate_provenance(self) -> Self:
        if self.imported_from_snapshot_id is None and self.publication_id is None:
            raise ValueError("An MCP server needs an observed snapshot or a publication")
        return self


class CatalogEntryUpdate(MosaicModel):
    """Administrator-authored catalog metadata, kept separate from what a sync discovers."""

    visibility: CatalogVisibility | None = None
    summary: str | None = None


def model_api_id(tenant_id: str, gateway_id: str, api_name: str) -> str:
    """Deterministic so re-importing an API updates the record instead of duplicating it."""

    return deterministic_id("modelApi", tenant_id, gateway_id, api_name)


def mcp_server_id(tenant_id: str, gateway_id: str, api_name: str) -> str:
    return deterministic_id("mcpServer", tenant_id, gateway_id, api_name)


def mcp_publication_id(tenant_id: str, gateway_id: str, mcp_endpoint_id: str) -> str:
    """Deterministic so publishing the same MCP server through a gateway twice never forks it."""

    return deterministic_id("mcpPublication", tenant_id, gateway_id, mcp_endpoint_id)


# The runtime app registration's MCP permissions. They're distinct from the model permissions on
# the same registration, so a model grant never opens an MCP server and an MCP grant never opens a
# model.
MCP_DELEGATED_SCOPE = "Mcp.Invoke"
MCP_APPLICATION_ROLE = "Mcp.Invoke.Application"
# API Management serves a streamable MCP server's message endpoint at ``/{api_path}/mcp``.
MCP_MESSAGE_PATH = "mcp"
# RFC 9728 path insertion: the metadata for a resource at ``https://host/{path}`` is served at
# ``https://host/.well-known/oauth-protected-resource/{path}``.
MCP_RESOURCE_METADATA_PREFIX = ".well-known/oauth-protected-resource"
# The one operation on a published server's discovery API: ``GET /mcp``.
MCP_METADATA_OPERATION = "metadata"


def mcp_server_url(gateway_url: str, api_path: str) -> str:
    """The URL MCP clients connect to for a server MOSAIC publishes at ``api_path``."""

    return f"{gateway_url.rstrip('/')}/{api_path.strip('/')}/{MCP_MESSAGE_PATH}"


def mcp_metadata_api_path(api_path: str) -> str:
    """The path of the API that serves a published server's protected resource metadata."""

    return f"{MCP_RESOURCE_METADATA_PREFIX}/{api_path.strip('/')}"


def mcp_resource_metadata_url(gateway_url: str, api_path: str) -> str:
    """Where a published server's protected resource metadata is served."""

    return f"{gateway_url.rstrip('/')}/{mcp_metadata_api_path(api_path)}/{MCP_MESSAGE_PATH}"


class McpAuthMode(StrEnum):
    """How MOSAIC authenticates when it connects to a registered MCP server.

    Deliberately not :class:`EndpointAuthMode`. An MCP server may legitimately require no
    credential at all, and that must never become a valid way to register a model endpoint.
    """

    NONE = "none"
    API_KEY = "apiKey"
    MANAGED_IDENTITY = "managedIdentity"


class McpEndpointStatus(StrEnum):
    PENDING = "pending"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    UNAUTHORIZED = "unauthorized"
    UNREACHABLE = "unreachable"
    # The server answered, and the answer is that it speaks a protocol revision or a transport
    # MOSAIC does not. Neither is a fault an operator can clear by retrying, so both are held
    # apart from the failure states above.
    UNSUPPORTED_PROTOCOL = "unsupportedProtocol"
    UNSUPPORTED_TRANSPORT = "unsupportedTransport"


class McpDiscoveryEvaluation(StrEnum):
    HANDSHAKE = "handshake"
    AUTHORIZATION_REQUIRED = "authorizationRequired"
    NOT_EVALUATED = "notEvaluated"


class McpAuthChallenge(MosaicModel):
    """What a ``401`` asked for, parsed from ``WWW-Authenticate``.

    Recorded so that "this server wants credentials MOSAIC was not given" is never presented as
    "this server is unreachable".
    """

    scheme: str | None = None
    resource_metadata_url: str | None = None
    scope: str | None = None


class McpDiscoveryAccess(MosaicModel):
    """Whether MOSAIC can reach and read a registered MCP server.

    An MCP server has no control plane, so unlike :class:`EndpointAccess` this can only be
    answered by connecting. There is deliberately no second, gateway-runtime relationship here:
    API Management fronts an MCP server with a backend credential rather than a role assignment,
    and Entra app roles are not readable over ARM at all.
    """

    can_discover: bool = False
    evaluation: McpDiscoveryEvaluation = McpDiscoveryEvaluation.NOT_EVALUATED
    checked_at: datetime | None = None
    challenge: McpAuthChallenge | None = None
    message: str | None = None


class McpEndpointCapabilities(MosaicModel):
    protocol_version: str | None = None
    offered_protocol_version: str = MCP_PROTOCOL_VERSION
    transport_type: McpTransportType = McpTransportType.STREAMABLE
    server_name: str | None = None
    server_title: str | None = None
    server_version: str | None = None
    instructions: str | None = None
    supports_tools: CapabilitySupport = CapabilitySupport.UNKNOWN
    session_managed: bool = False
    notes: list[str] = Field(default_factory=list)


class McpInventorySummary(MosaicModel):
    """Counts only what the server actually stated.

    There is no "destructive tools" count on purpose. ``destructiveHint`` and ``openWorldHint``
    default to *true* when absent, so a count derived from the defaults would report tools as
    destructive that simply said nothing. ``unannotated_tools`` reports that silence directly.
    """

    tools: int = 0
    read_only_tools: int = 0
    unannotated_tools: int = 0


class McpEndpoint(Entity):
    """A registered MCP server MOSAIC reads tools from.

    The sibling of :class:`ModelEndpoint`: an administrator registers a server that exists
    somewhere, and MOSAIC connects to it as a read-only MCP client to record what it offers.
    MOSAIC never calls a tool, and creates nothing in Azure or API Management.
    """

    entity_type: Literal["mcpEndpoint"] = "mcpEndpoint"
    name: str
    endpoint: AnyHttpUrl
    environment_label: str | None = Field(
        default=None,
        description="Deprecated: free-text legacy label. Use environment.",
        json_schema_extra={"deprecated": True},
    )
    environment: str | None = None
    auth_mode: McpAuthMode = McpAuthMode.NONE
    credential_reference_id: str | None = None
    resource_audience: str | None = Field(
        default=None,
        description=(
            "The Entra audience a managed-identity token is requested for. Required rather than "
            "inferred: a token is only ever attached when the operator named who it is for."
        ),
    )
    status: McpEndpointStatus = McpEndpointStatus.PENDING
    access: McpDiscoveryAccess = Field(default_factory=McpDiscoveryAccess)
    capabilities: McpEndpointCapabilities = Field(default_factory=McpEndpointCapabilities)
    inventory: McpInventorySummary = Field(default_factory=McpInventorySummary)
    last_synced_at: datetime | None = None
    last_sync_error: str | None = None


class McpEndpointSyncRun(Entity):
    entity_type: Literal["mcpEndpointSyncRun"] = "mcpEndpointSyncRun"
    endpoint_id: str
    status: GatewaySyncStatus = GatewaySyncStatus.RUNNING
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    duration_ms: int | None = None
    counts: McpInventorySummary = Field(default_factory=McpInventorySummary)
    removed: int = 0
    errors: list[str] = Field(default_factory=list)
    actor_object_id: str | None = None


def canonical_mcp_url(value: str) -> str:
    """The canonical form of an MCP server URL, per the authorization spec's definition.

    Lowercase scheme and host, no fragment, and no trailing slash on a non-root path. Used both
    to key the record and as the RFC 8707 resource identifier, so the two can never disagree.
    """

    parts = urlsplit(value.strip())
    host = (parts.hostname or "").casefold()
    if not host:
        raise ValueError("An MCP server URL must include a host")
    netloc = f"[{host}]" if ":" in host else host
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.casefold(), netloc, path, parts.query, ""))


def mcp_endpoint_id(tenant_id: str, url: str) -> str:
    """Deterministic so re-registering the same server refreshes it instead of forking it."""

    return deterministic_id("mcpEndpoint", tenant_id, canonical_mcp_url(url))


QuotaPeriod = Literal["Hourly", "Daily", "Weekly", "Monthly", "Yearly"]


class TokenEnforcement(MosaicModel):
    counter_key_expression: str
    tokens_per_minute: int | None = Field(default=None, ge=1)
    token_quota: int | None = Field(default=None, ge=1)
    token_quota_period: QuotaPeriod | None = None
    estimate_prompt_tokens: bool = True

    @model_validator(mode="after")
    def validate_limits(self) -> Self:
        if self.tokens_per_minute is None and self.token_quota is None:
            raise ValueError("At least one token rate or quota must be configured")
        if self.token_quota is not None and self.token_quota_period is None:
            raise ValueError("A token quota period is required when a quota is configured")
        if self.token_quota is None and self.token_quota_period is not None:
            raise ValueError("A token quota is required when a quota period is configured")
        return self


class RequestEnforcement(MosaicModel):
    """Call limits, which API Management enforces with different policies to token limits.

    ``calls`` over ``renewal_period_seconds`` is a short sliding window (``rate-limit-by-key``);
    ``call_quota`` over ``call_quota_period`` is a long accounting window (``quota-by-key``). They
    are independent, and a caller commonly has both.
    """

    counter_key_expression: str
    calls: int | None = Field(default=None, ge=1)
    renewal_period_seconds: int | None = Field(default=None, ge=1)
    call_quota: int | None = Field(default=None, ge=1)
    call_quota_period: QuotaPeriod | None = None

    @model_validator(mode="after")
    def validate_limits(self) -> Self:
        if self.calls is None and self.call_quota is None:
            raise ValueError("At least one call rate or quota must be configured")
        if (self.calls is None) != (self.renewal_period_seconds is None):
            raise ValueError("A call rate needs both a call count and a renewal period")
        if (self.call_quota is None) != (self.call_quota_period is None):
            raise ValueError("A call quota needs both a quota and a quota period")
        return self


class EntitlementEnforcement(MosaicModel):
    """What restricts a subject's use of a resource.

    Token limits keep the exact shape of :class:`TokenEnforcement` because the policy preview and
    the ``llm-token-limit`` renderer already speak it. An entitlement with no enforcement at all is
    a legitimate unrestricted grant, so this whole object is optional on the entitlement; when it
    is present it must actually restrict something.
    """

    tokens: TokenEnforcement | None = None
    requests: RequestEnforcement | None = None

    @model_validator(mode="after")
    def validate_present(self) -> Self:
        if self.tokens is None and self.requests is None:
            raise ValueError(
                "Enforcement must configure a token or request limit; omit it entirely for an "
                "unrestricted entitlement"
            )
        return self


class EntitlementSubjectKind(StrEnum):
    USER = "user"
    GROUP = "group"
    APPLICATION = "application"
    SECURITY_GROUP = "securityGroup"


def subject_kind_for(kind: PrincipalKind) -> EntitlementSubjectKind:
    """The entitlement subject a principal of this kind is granted as.

    People and agent users sign in with delegated user tokens, so both are ``user`` subjects.
    Service principals, managed identities and agent identities sign in as applications. An Entra
    security group is its own subject: it is never the caller, only something the caller is a
    member of. A MOSAIC group (``group``) is not a principal at all, so no kind maps to it.
    """

    if kind in {PrincipalKind.USER, PrincipalKind.AGENT_USER}:
        return EntitlementSubjectKind.USER
    if kind == PrincipalKind.SECURITY_GROUP:
        return EntitlementSubjectKind.SECURITY_GROUP
    return EntitlementSubjectKind.APPLICATION


class EntitlementResourceKind(StrEnum):
    MODEL_API = "modelApi"
    MCP_SERVER = "mcpServer"
    MODEL_DEPLOYMENT = "modelDeployment"
    PRODUCT = "product"
    # One model a model pool offers. Its scope is the pool (ADR 0024).
    POOL_MODEL = "poolModel"


class EntitlementSubject(MosaicModel):
    """Who a grant is for.

    ``user``, ``application`` and ``securityGroup`` name a :class:`Principal`; ``group`` names a
    MOSAIC :class:`Group`. The subject kind is the principal's kind as :func:`subject_kind_for`
    maps it, kept here so a reader of the entitlement does not have to dereference it. Only
    MOSAIC groups are desired state alone: the gateway enforces the other three.
    """

    kind: EntitlementSubjectKind
    id: str


class EntitlementResource(MosaicModel):
    """What a grant is over.

    ``modelApi`` and ``mcpServer`` name desired-state records that carry their own gateway.
    ``product`` and ``modelDeployment`` name observed records, which are scoped to the gateway or
    model endpoint MOSAIC read them from, so those require ``scope_id``. ``poolModel`` names one
    model in a model pool, and its ``scope_id`` names the pool.
    """

    kind: EntitlementResourceKind
    id: str
    scope_id: str | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if self.kind in {"product", "modelDeployment"} and not self.scope_id:
            raise ValueError(
                f"A {self.kind} entitlement needs scopeId naming the gateway or model endpoint "
                "it was observed on"
            )
        if self.kind == "poolModel" and not self.scope_id:
            raise ValueError("A poolModel entitlement needs scopeId naming its model pool")
        return self


class BindingSource(StrEnum):
    INFERRED = "inferred"
    MANUAL = "manual"
    ORCHESTRATED = "orchestrated"


class ModelAccessSettings(MosaicModel):
    keys_enabled: bool = True
    entra_enabled: bool = True


class EntitlementRuntime(MosaicModel):
    """How far a grant has been applied to API Management, derived from its publication.

    ``error`` is the publication's last apply error, verbatim from API Management. It names
    MOSAIC's internal resources and is shared by every grant on the publication, so only the
    administrator routes return it. The end-user routes under ``/api/v1/me`` and ``/api/v1/portal``
    always return it as null and report a failed apply through ``status`` alone.
    """

    publication_id: str
    status: Literal[
        "pending", "applying", "applied", "revocationPending", "revoked", "failed", "unknown"
    ]
    applied_methods: ModelAccessSettings | None = None
    subscription_name: str | None = None
    # Whether the grant's key exists in API Management. Keys are created on request, by the
    # grant's holder or an administrator, never by an apply. See ADR 0022.
    key_exists: bool = False
    applied_at: datetime | None = None
    error: str | None = Field(
        default=None,
        description=(
            "The publication's last API Management apply error, verbatim. Administrator routes "
            "return it; the end-user routes under /api/v1/me and /api/v1/portal always return null."
        ),
    )


class EntitlementBinding(MosaicModel):
    """The API Management object that realizes an entitlement at runtime.

    An orchestrated binding is a server-produced projection of an applied publication snapshot.
    Manual and inferred bindings remain useful metadata, but are never credential-read authority.
    Gateway attribution fields are recorded only by the publication that applied the grant.
    """

    gateway_id: str
    apim_product_name: str | None = None
    apim_subscription_name: str | None = None
    counter_key_expression: str | None = None
    attribution_key: str | None = None
    attribution_per_member: bool = False
    source: BindingSource = BindingSource.MANUAL
    bound_at: datetime | None = None


class GrantRevocation(MosaicModel):
    """Why MOSAIC turned a grant off itself, rather than an administrator turning it off.

    Set when its subject is removed from the grant's cost center. A revoked grant stays disabled
    until its subject can charge the cost center again, and the next apply deletes its key rather
    than suspending it. See ADR 0022.
    """

    reason: Literal["costCenterMembership"] = "costCenterMembership"
    cost_center_id: str
    revoked_at: datetime = Field(default_factory=utc_now)
    revoked_by: str


class CostCenterRef(MosaicModel):
    """A cost center as a grant, a key or a report names it."""

    id: str
    name: str
    code: str


class Entitlement(Entity):
    """A grant of a governed resource to a subject, and the limits that apply to it.

    Cosmos is the source of truth. An entitlement with no ``enforcement`` takes its cost center's
    per-person defaults for the resource, and is otherwise unrestricted, which the portal reports
    as such rather than rendering a limit of zero.

    Every grant is charged to one cost center, and a subject can hold a grant on the same resource
    under each cost center it may charge. A grant recorded without one belongs to General.
    """

    entity_type: Literal["entitlement"] = "entitlement"
    subject: EntitlementSubject
    resource: EntitlementResource
    cost_center_id: str = ""
    enabled: bool = True
    enforcement: EntitlementEnforcement | None = None
    binding: EntitlementBinding | None = None
    notes: str | None = None
    revocation: GrantRevocation | None = None
    runtime: EntitlementRuntime | None = None

    @model_validator(mode="after")
    def fill_cost_center(self) -> Self:
        if not self.cost_center_id:
            self.cost_center_id = general_cost_center_id(self.tenant_id)
        return self


def _writable_binding(binding: EntitlementBinding | None) -> EntitlementBinding | None:
    if binding is not None and binding.source == BindingSource.ORCHESTRATED:
        raise ValueError("Orchestrated bindings are produced only by an applied publication")
    if binding is not None and (
        binding.attribution_key is not None or binding.attribution_per_member
    ):
        raise ValueError("Gateway attribution is recorded only by an applied publication")
    return binding


class EntitlementCreate(MosaicModel):
    subject: EntitlementSubject
    resource: EntitlementResource
    # The cost center the grant's calls are charged to. Omitted, it's the subject's default.
    cost_center_id: str | None = Field(default=None, min_length=1, max_length=128)
    enabled: bool = True
    enforcement: EntitlementEnforcement | None = None
    binding: EntitlementBinding | None = None
    notes: str | None = None

    _validate_binding = field_validator("binding")(_writable_binding)


class EntitlementUpdate(MosaicModel):
    enabled: bool | None = None
    enforcement: EntitlementEnforcement | None = None
    binding: EntitlementBinding | None = None
    notes: str | None = None

    _validate_binding = field_validator("binding")(_writable_binding)


def entitlement_id(
    tenant_id: str,
    subject: EntitlementSubject,
    resource: EntitlementResource,
    cost_center_id: str,
) -> str:
    """Deterministic on subject, resource and cost center, so re-granting the same is a conflict.

    The same subject and resource under two cost centers are two grants, each with its own limits,
    counters and key.
    """

    return deterministic_id(
        "entitlement",
        tenant_id,
        str(subject.kind),
        subject.id,
        str(resource.kind),
        resource.id,
        resource.scope_id or "",
        cost_center_id,
    )


class GrantPath(StrEnum):
    DIRECT = "direct"
    GROUP = "group"
    SECURITY_GROUP = "securityGroup"


class ResolvedEntitlement(MosaicModel):
    """An entitlement that applies to a principal, and how it reached them.

    ``effective`` is false for a grant that applies but loses to another grant on the same
    resource under the same cost center; ``shadowed_by`` then names the entitlement that wins. See
    :func:`grant_precedence_key` for the rules. Grants under different cost centers never shadow
    each other: the caller chooses between them with the cost-center header.
    """

    entitlement: Entitlement
    via: GrantPath
    via_group_id: str | None = None
    via_group_name: str | None = None
    effective: bool = True
    shadowed_by: str | None = None
    resource_summary: "ResourceSummary | None" = None
    cost_center: CostCenterRef | None = None


_QUOTA_PERIOD_HOURS: dict[str, float] = {
    "Hourly": 1.0,
    "Daily": 24.0,
    "Weekly": 168.0,
    "Monthly": 730.0,
    "Yearly": 8760.0,
}


def grant_allowance(
    enforcement: EntitlementEnforcement | None,
) -> tuple[float, float, float, float, float]:
    """How much a grant allows, compared field by field; higher is more generous.

    The fields, in the order they are compared: whether the grant is unlimited, tokens per minute,
    token quota per hour, calls per minute, and call quota per hour. A limit that is not set allows
    without bound, so it counts as infinite. Quotas are normalized to an hour so that a daily and a
    monthly quota can be compared; a month counts as 730 hours.
    """

    if enforcement is None:
        return (1.0, math.inf, math.inf, math.inf, math.inf)
    tokens = enforcement.tokens
    requests = enforcement.requests
    tokens_per_minute = (
        float(tokens.tokens_per_minute)
        if tokens is not None and tokens.tokens_per_minute is not None
        else math.inf
    )
    token_quota_per_hour = (
        tokens.token_quota / _QUOTA_PERIOD_HOURS[tokens.token_quota_period]
        if tokens is not None
        and tokens.token_quota is not None
        and tokens.token_quota_period is not None
        else math.inf
    )
    calls_per_minute = (
        requests.calls * 60.0 / requests.renewal_period_seconds
        if requests is not None
        and requests.calls is not None
        and requests.renewal_period_seconds is not None
        else math.inf
    )
    call_quota_per_hour = (
        requests.call_quota / _QUOTA_PERIOD_HOURS[requests.call_quota_period]
        if requests is not None
        and requests.call_quota is not None
        and requests.call_quota_period is not None
        else math.inf
    )
    return (0.0, tokens_per_minute, token_quota_per_hour, calls_per_minute, call_quota_per_hour)


def grant_precedence_key(
    enforcement: EntitlementEnforcement | None, entitlement_ref: str
) -> tuple[float | str, ...]:
    """Sort key that puts the grant that wins among overlapping group grants first.

    A caller who belongs to several granted security groups gets the most generous of those
    grants: an unlimited grant beats any limited one, then the higher token allowance wins, then
    the higher call allowance. Equal allowances fall back to the lower entitlement ID, so the
    choice never depends on the order records were read. A direct grant to the caller always
    beats every group grant; that rule is applied before this ordering and is not part of it.
    """

    return (*(-value for value in grant_allowance(enforcement)), entitlement_ref)


class AccessRequestState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    WITHDRAWN = "withdrawn"


class AccessRequest(Entity):
    entity_type: Literal["accessRequest"] = "accessRequest"
    requester_object_id: str
    requester_principal_id: str | None = None
    resource: EntitlementResource
    # The cost center the requester chose. Approval grants under it unless the administrator
    # chooses another the requester may charge. None only for requests made before cost centers,
    # which are charged to the requester's default.
    cost_center_id: str | None = None
    # The security groups that cost center listed which the requester's own token said they were
    # in when they asked. Without Microsoft Graph, approval trusts only these, and only while the
    # cost center still lists them.
    cost_center_group_ids: list[str] = Field(default_factory=list)
    justification: str | None = None
    requested_environment: str | None = None
    resource_snapshot: "AccessRequestResourceSnapshot | None" = None
    state: AccessRequestState = AccessRequestState.PENDING
    decided_by_object_id: str | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None
    granted_entitlement_id: str | None = None


class AccessRequestCreate(MosaicModel):
    resource: EntitlementResource
    # One of the requester's cost centers. Omitted, it's their default.
    cost_center_id: str | None = Field(default=None, min_length=1, max_length=128)
    justification: str | None = None


class AccessRequestDecision(MosaicModel):
    note: str | None = None


class AccessRequestApproval(AccessRequestDecision):
    """Approving a request also creates the requester's grant, with these limits.

    ``enforcement`` is the same type ``EntitlementCreate`` takes, so the limits an administrator
    confirms here are validated exactly as if they had created the grant directly. Omitting it
    applies the cost center's per-person defaults, if it has any; publication safeguards still
    apply. ``cost_center_id`` charges the grant to another cost center the requester may charge,
    instead of the one they chose.
    """

    enforcement: EntitlementEnforcement | None = None
    confirmed_environment: str | None = None
    cost_center_id: str | None = Field(default=None, min_length=1, max_length=128)


class AccessRequestResourceSnapshot(MosaicModel):
    display_name: str | None = None
    gateway_id: str | None = None
    gateway_name: str | None = None


class ResourceSummary(MosaicModel):
    """A governed resource as a person reading a grant or a request sees it.

    ``available`` is false when the resource no longer exists, when its gateway is no longer
    registered, and when MOSAIC publishes it but its API isn't in API Management: never applied,
    or unpublished. The name and gateway stay, so a person can still tell what it was.
    """

    kind: EntitlementResourceKind
    id: str
    scope_id: str | None = None
    display_name: str | None = None
    gateway_id: str | None = None
    gateway_name: str | None = None
    environment: str | None = None
    available: bool = False


class AdminAccessRequestListItem(AccessRequest):
    resource_summary: ResourceSummary | None = None
    cost_center: CostCenterRef | None = None


class CatalogEntryKind(StrEnum):
    MODEL_API = "modelApi"
    MCP_SERVER = "mcpServer"
    # One model a model pool offers (ADR 0024). The pool's endpoints and members aren't shown.
    POOL_MODEL = "poolModel"


class CatalogEntry(MosaicModel):
    """One governed resource as an end user sees it.

    Deliberately narrower than the administrator's view of the same record: a portal user has no
    business seeing gateway internals, policy state, or how the resource was detected. A model API
    or MCP server MOSAIC publishes is an entry only while its API is in API Management. A pool
    model is an entry only while its pool serves it.
    """

    kind: CatalogEntryKind
    id: str
    # The pool, for a pool model: a request for it names the pool as its resource's scope.
    scope_id: str | None = None
    display_name: str
    summary: str | None = None
    gateway_id: str
    gateway_name: str | None = None
    environment: str | None = None
    entitled: bool = False
    request_state: AccessRequestState | None = None
    # The caller's cost centers they already hold a grant under, or have an open request under.
    # A person can still ask for the resource under another of their cost centers.
    entitled_cost_center_ids: list[str] = Field(default_factory=list)
    requested_cost_center_ids: list[str] = Field(default_factory=list)
    # Whether the gateway enforces grants on this resource. Reported for MCP servers only: True
    # when MOSAIC publishes the server and has applied its access, False for an adopted server or
    # a published one whose latest apply didn't finish. None for other kinds.
    enforced: bool | None = None
    # Pool models only: the API the model is called with, such as ``anthropicMessages``, and its
    # capacity, when the pool shows it and MOSAIC knows what every active deployment behind the
    # model is. ``provisionedWithOverflow`` means some are provisioned and the rest pay-as-you-go.
    api_style: ApiShape | None = None
    capacity: Literal["provisioned", "payAsYouGo", "provisionedWithOverflow"] | None = None


class PortalResolvedEntitlement(ResolvedEntitlement):
    """One of the caller's grants, named the way the catalog names the resource.

    The name is resolved for every grant the caller holds, including one over a resource the
    catalog does not list, because hiding a resource from the catalog does not revoke access to it.
    ``resource_display_name`` is null only when MOSAIC can no longer resolve the resource — for
    example because it was deleted after the grant was made.
    """

    resource_display_name: str | None = None


class PortalAccessRequest(AccessRequest):
    """One of the caller's access requests, named the way the catalog names the resource.

    The name is derived when the request is read and never persisted, which is why it lives on this
    response model rather than on :class:`AccessRequest`. It follows the same rules as
    :class:`PortalResolvedEntitlement`. ``resource_summary`` is derived when read too: live gateway
    and environment details for a resource the caller can still see, and otherwise only what the
    request recorded when it was made.
    """

    resource_display_name: str | None = None
    resource_summary: ResourceSummary | None = None
    cost_center: CostCenterRef | None = None


class PortalProfile(MosaicModel):
    """Who the caller is, as the portal understands them.

    ``principal_id`` is null when MOSAIC has never recorded this person as a ``Principal``. That is
    a real state rather than an error: they authenticated and hold the role, but nothing has been
    granted to them, so the portal says so instead of failing.
    """

    object_id: str
    tenant_id: str
    roles: list[str] = Field(default_factory=list)
    is_admin: bool = False
    principal_id: str | None = None
    display_label: str | None = None
    entitlement_count: int = 0
    pending_request_count: int = 0
    # True when the caller is in more groups than their sign-in token can list. MOSAIC then can't
    # tell which security-group grants apply to them, and neither can the gateway.
    groups_overage: bool = False
    # The cost center the caller's calls are charged to when they name none.
    default_cost_center: CostCenterRef | None = None


class ConsoleAccess(MosaicModel):
    """Which MOSAIC role the administrator console's caller holds.

    Read from the validated access token. MOSAIC's app roles are defined on the API's registration,
    so they arrive in the API token's ``roles`` claim and in neither SPA's ID token: the console
    asks here rather than decoding a token in the browser.

    Only a caller holding a MOSAIC role gets an answer, so ``is_admin`` false means the caller holds
    the portal role and not the administrator one.
    """

    roles: list[str] = Field(default_factory=list)
    is_admin: bool = False


MOSAIC_RESOURCE_PREFIX = "mosaic-"
_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


def apim_slug(value: str, *, max_length: int = 40) -> str:
    """Reduce a display name to something API Management accepts as a resource name."""

    slug = _SLUG_PATTERN.sub("-", value.casefold()).strip("-")
    return slug[:max_length].strip("-")


def publication_slug(endpoint_name: str, deployment_name: str) -> str:
    parts = [part for part in (apim_slug(endpoint_name), apim_slug(deployment_name)) if part]
    return "-".join(parts) or "model"


def publication_id(
    tenant_id: str, gateway_id: str, model_endpoint_id: str, deployment_name: str
) -> str:
    """Deterministic so publishing the same deployment twice refreshes intent, never forks it."""

    return deterministic_id(
        "publication", tenant_id, gateway_id, model_endpoint_id, deployment_name
    )


class PublicationStatus(StrEnum):
    DRAFT = "draft"
    PLANNED = "planned"
    APPLYING = "applying"
    PUBLISHED = "published"
    FAILED = "failed"
    ROLLED_BACK = "rolledBack"


class PublishedResourceKind(StrEnum):
    """The API Management resource types a publication creates, in dependency order."""

    NAMED_VALUE = "namedValue"
    BACKEND = "backend"
    # A load-balanced pool over several member backends. Only model pools write them (ADR 0024).
    BACKEND_POOL = "backendPool"
    POLICY_FRAGMENT = "policyFragment"
    API = "api"
    API_OPERATION = "apiOperation"
    API_POLICY = "apiPolicy"
    PRODUCT = "product"
    PRODUCT_API = "productApi"
    SUBSCRIPTION = "subscription"


class PublishedResource(MosaicModel):
    """One API Management resource a publication apply touched.

    ``created_by_mosaic`` is recorded at the moment of the write, never inferred afterwards from a
    name. A product that merely happens to match a MOSAIC name was not created by MOSAIC, and
    rollback and unpublish must never delete it.
    """

    kind: PublishedResourceKind
    name: str
    resource_id: str
    created_by_mosaic: bool = False
    applied_at: datetime = Field(default_factory=utc_now)


def _holds_api(resources: list[PublishedResource], api_name: str) -> bool:
    return any(
        item.kind == PublishedResourceKind.API
        and item.name == api_name
        and item.created_by_mosaic
        for item in resources
    )


class AppliedCostCenterPool(MosaicModel):
    """A cost center's pooled monthly quota on one publication, exactly as an apply compiled it.

    Every grant under the cost center on the publication draws on it, whoever calls. Tokens are
    limited with a second ``llm-token-limit`` and calls with ``quota-by-key``, each counted per cost
    center and publication. API Management counts per gateway, so a pool is per gateway too.
    """

    cost_center_id: str
    cost_center_code: str
    monthly_tokens: int | None = Field(default=None, ge=1)
    monthly_calls: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_present(self) -> Self:
        if self.monthly_tokens is None and self.monthly_calls is None:
            raise ValueError("A pooled quota needs monthly tokens or monthly calls")
        return self


class ModelAccessGrant(MosaicModel):
    entitlement_id: str
    subject: EntitlementSubject
    # The caller's object ID for a direct grant; the group's object ID for a security-group grant,
    # which the gateway matches against the caller token's ``groups`` claim.
    object_id: str
    display_name: str
    # None exactly when the subject is a security group. A group grant authorizes Entra tokens
    # only: MOSAIC issues no subscription, so there is no key to reveal and none to rotate.
    subscription_name: str | None = None
    enabled: bool
    enforcement: EntitlementEnforcement | None = None
    intent_digest: str
    # The cost center the grant charges, and its code as the cost-center header names it.
    cost_center_id: str = ""
    cost_center_code: str = ""
    # Whether this is a direct grant under its subject's default cost center. A call that names no
    # cost center uses it before the subject's other grants, which go oldest first.
    default_cost_center: bool = False
    granted_at: datetime | None = None
    # False when the grant's cost center turned keys off. The gateway then refuses its key and an
    # apply suspends it, keeping it for when keys are allowed again.
    keys_allowed: bool = True
    # True when the grant was revoked because its subject left the cost center. An apply deletes
    # its key rather than suspending it.
    revoked: bool = False

    @model_validator(mode="after")
    def enforceable_subject_only(self) -> Self:
        if self.subject.kind == EntitlementSubjectKind.GROUP:
            raise ValueError(
                "MOSAIC groups are not enforced at runtime; grant an Entra security group instead"
            )
        if self.subject.kind == EntitlementSubjectKind.SECURITY_GROUP:
            if self.subscription_name is not None:
                raise ValueError("A security-group grant has no subscription")
        elif not self.subscription_name:
            raise ValueError("A direct grant needs its subscription name")
        return self

    @property
    def is_group_grant(self) -> bool:
        return self.subject.kind == EntitlementSubjectKind.SECURITY_GROUP


class ModelAccessSnapshot(MosaicModel):
    version: int = Field(ge=1)
    settings: ModelAccessSettings
    audience: str | None = None
    # None when the publication's API shape can't be token-metered on its gateway's tier. Such a
    # snapshot carries no token policies at all, so none of its grants may carry token limits.
    publication_enforcement: TokenEnforcement | None = None
    grants: list[ModelAccessGrant] = Field(default_factory=list)
    pools: list[AppliedCostCenterPool] = Field(default_factory=list)


def model_access_subscription_name(
    tenant_id: str, publication_ref: str, entitlement_ref: str
) -> str:
    return deterministic_id(
        "mosaic-grant", tenant_id, publication_ref, entitlement_ref
    ).replace("_", "-")


class McpAccessGrant(MosaicModel):
    """One grant compiled into an MCP publication's fragment.

    MCP grants authorize Entra tokens only. There's no subscription and no key, so unlike
    :class:`ModelAccessGrant` nothing here names one. MCP servers are limited by calls, never by
    tokens.
    """

    entitlement_id: str
    subject: EntitlementSubject
    # The caller's object ID for a direct grant; the group's object ID for a security-group grant,
    # which the gateway matches against the caller token's ``groups`` claim.
    object_id: str
    display_name: str
    enabled: bool
    enforcement: EntitlementEnforcement | None = None
    intent_digest: str
    # As on ModelAccessGrant: the cost center the grant charges, which the cost-center header
    # selects, and whether it's the subject's default.
    cost_center_id: str = ""
    cost_center_code: str = ""
    default_cost_center: bool = False
    granted_at: datetime | None = None

    @model_validator(mode="after")
    def enforceable_subject_only(self) -> Self:
        if self.subject.kind == EntitlementSubjectKind.GROUP:
            raise ValueError(
                "MOSAIC groups are not enforced at runtime; grant an Entra security group instead"
            )
        if self.enforcement is not None and self.enforcement.tokens is not None:
            raise ValueError("MCP servers are limited by calls, not tokens")
        return self

    @property
    def is_group_grant(self) -> bool:
        return self.subject.kind == EntitlementSubjectKind.SECURITY_GROUP


class McpModelCaller(MosaicModel):
    """The application an MCP server calls governed models as, exactly as an apply compiled it.

    Its model calls carry the MCP call's reference, and MOSAIC attributes them to the MCP call's
    caller only when the model was called by this application. See ADR 0025.
    """

    principal_id: str
    object_id: str
    display_name: str


class McpAccessSnapshot(MosaicModel):
    """The grants an MCP publication's fragment enforces, exactly as they were applied."""

    version: int = Field(ge=1)
    # The runtime app registration's client ID: the audience MCP tokens are validated against.
    audience: str
    delegated_scope: str = MCP_DELEGATED_SCOPE
    application_role: str = MCP_APPLICATION_ROLE
    grants: list[McpAccessGrant] = Field(default_factory=list)
    # MCP servers carry no tokens, so their pools count calls only.
    pools: list[AppliedCostCenterPool] = Field(default_factory=list)
    model_caller: McpModelCaller | None = Field(default=None, exclude_if=_unset)


class PoolAccessGrant(MosaicModel):
    """One grant compiled into a model pool's policy: a subject's access to one pool model.

    Like :class:`ModelAccessGrant`, except for its key. A direct grant's key is shared: there's one
    per subject, pool, and cost center, and it serves every model the subject holds directly in
    the pool under that cost center (ADR 0024).
    """

    entitlement_id: str
    pool_model_id: str
    subject: EntitlementSubject
    # The caller's object ID for a direct grant; the group's object ID for a security-group grant,
    # which the gateway matches against the caller token's ``groups`` claim.
    object_id: str
    display_name: str
    # The subscription that is the grant's key, from ``model_pools.pool_key_name``. None exactly
    # when the subject is a security group: a group grant authorizes Entra tokens only.
    key_name: str | None = None
    enabled: bool
    enforcement: EntitlementEnforcement | None = None
    intent_digest: str
    cost_center_id: str = ""
    cost_center_code: str = ""
    # Whether this is a direct grant under its subject's default cost center. A call that names no
    # cost center uses it before the subject's other grants for the model, which go oldest first.
    default_cost_center: bool = False
    granted_at: datetime | None = None
    # False when the grant's cost center turned keys off. The gateway then refuses the key for
    # this grant's model.
    keys_allowed: bool = True
    # True when the grant was revoked because its subject left the cost center.
    revoked: bool = False

    @model_validator(mode="after")
    def enforceable_subject_only(self) -> Self:
        if self.subject.kind == EntitlementSubjectKind.GROUP:
            raise ValueError(
                "MOSAIC groups are not enforced at runtime; grant an Entra security group instead"
            )
        if self.subject.kind == EntitlementSubjectKind.SECURITY_GROUP:
            if self.key_name is not None:
                raise ValueError("A security-group grant has no key")
        elif not self.key_name:
            raise ValueError("A direct grant needs its key's name")
        return self

    @property
    def is_group_grant(self) -> bool:
        return self.subject.kind == EntitlementSubjectKind.SECURITY_GROUP


class PoolModelQuota(AppliedCostCenterPool):
    """A cost center's pooled monthly quota on one pool model, exactly as an apply compiled it.

    Every grant under the cost center on the model draws on it, counted per cost center and pool
    model.
    """

    pool_model_id: str


class PoolAccessSnapshot(MosaicModel):
    """The grants a model pool's policy enforces, exactly as an apply compiled them."""

    version: int = Field(ge=1)
    settings: ModelAccessSettings
    audience: str | None = None
    # False when the pool's API shape can't be token-metered on its gateway's tier. Such a
    # snapshot carries no token policies, so none of its grants carry token limits.
    token_metering: bool = True
    grants: list[PoolAccessGrant] = Field(default_factory=list)
    quotas: list[PoolModelQuota] = Field(default_factory=list)

    def grants_for(self, pool_model_id: str) -> list[PoolAccessGrant]:
        return [grant for grant in self.grants if grant.pool_model_id == pool_model_id]

    def key_grants(self) -> dict[str, list[PoolAccessGrant]]:
        """The direct grants each key serves, by key name."""

        keys: dict[str, list[PoolAccessGrant]] = {}
        for grant in self.grants:
            if grant.key_name is not None:
                keys.setdefault(grant.key_name, []).append(grant)
        return keys


class Publication(Entity):
    """An administrator's intent to expose one model deployment through one gateway.

    Desired state. Saving it writes only to Cosmos; API Management is changed by an explicit apply
    against a specific plan, never as a side effect of recording the intent.
    """

    entity_type: Literal["publication"] = "publication"
    gateway_id: str
    model_endpoint_id: str
    deployment_name: str
    provider: ModelProvider
    display_name: str
    api_name: str
    api_path: str
    backend_name: str
    fragment_name: str
    product_name: str
    subscription_name: str
    # The Key Vault-backed named value the gateway reads the backend's API key from, when the
    # endpoint is reached with a key rather than the gateway's managed identity (ADR 0018).
    backend_key_name: str | None = Field(default=None, exclude_if=_unset)
    subscription_required: bool = True
    # None only when the API shape can't be token-metered on the gateway's tier (Anthropic Messages
    # on a classic tier). Publishing validates that rule; renderers simply omit token policies.
    enforcement: TokenEnforcement | None = None
    shape_version: str
    # Recorded when the publication is created. Records that predate shapes are filled from the
    # provider, which is exactly the shape they were published with.
    api_shape: ApiShape | None = None
    status: PublicationStatus = PublicationStatus.DRAFT
    resources: list[PublishedResource] = Field(default_factory=list)
    last_plan_id: str | None = None
    last_plan_digest: str | None = None
    last_run_id: str | None = None
    last_applied_at: datetime | None = None
    # When an unpublish last removed everything MOSAIC created for this publication. The next
    # successful apply clears it. Publications unpublished before MOSAIC recorded this have none.
    unpublished_at: datetime | None = None
    last_error: str | None = None
    model_api_id: str | None = None
    governed_access: ModelAccessSettings | None = None
    applied_access: ModelAccessSnapshot | None = None
    access_state: Literal["pending", "applying", "applied", "failed", "unknown"] = "pending"

    @model_validator(mode="after")
    def fill_legacy_shape(self) -> Self:
        if self.api_shape is None:
            self.api_shape = default_api_shape(self.provider)
        return self

    def created_resources(self) -> list[PublishedResource]:
        """The subset rollback and unpublish are allowed to delete."""

        return [resource for resource in self.resources if resource.created_by_mosaic]

    def has_applied_api(self) -> bool:
        """Whether MOSAIC's record says this publication's API is in API Management.

        False before the first successful apply and once an unpublish has deleted the API. Without
        its API the gateway can't serve the model, so the portal doesn't offer it.
        """

        return _holds_api(self.resources, self.api_name)

    def may_own_gateway_state(self) -> bool:
        """Whether API Management may hold something this publication is responsible for.

        True while MOSAIC-created resources are recorded, while a run is or may be in flight, while
        an interrupted apply left the runtime state unknown, and while any applied grant is still
        enabled. Only a publication for which this is False may be forgotten.
        """

        return bool(
            self.created_resources()
            or self.status == PublicationStatus.APPLYING
            or self.access_state in {"applying", "unknown"}
            or (
                self.applied_access
                and any(grant.enabled for grant in self.applied_access.grants)
            )
        )


class McpPublication(Entity):
    """An administrator's intent to publish one registered MCP server through one gateway.

    Desired state, like :class:`Publication`: saving it writes only to Cosmos. An apply creates an
    MCP API in API Management whose backend is the registered server, an enforcement fragment that
    validates Entra tokens and matches them against the applied grants, and a discovery API that
    serves the server's protected resource metadata (RFC 9728) so MCP clients can find where to
    sign in.
    """

    entity_type: Literal["mcpPublication"] = "mcpPublication"
    gateway_id: str
    mcp_endpoint_id: str
    display_name: str
    api_name: str
    api_path: str
    backend_name: str
    fragment_name: str
    # The anonymous API that serves protected resource metadata at
    # ``/.well-known/oauth-protected-resource/{api_path}/mcp``.
    metadata_api_name: str
    # The governed MCP server record grants name. Created with the publication, so grants can be
    # recorded before the first apply.
    mcp_server_id: str
    status: PublicationStatus = PublicationStatus.DRAFT
    resources: list[PublishedResource] = Field(default_factory=list)
    last_plan_id: str | None = None
    last_plan_digest: str | None = None
    last_run_id: str | None = None
    last_applied_at: datetime | None = None
    # When an unpublish last removed everything MOSAIC created for this publication. The next
    # successful apply clears it.
    unpublished_at: datetime | None = None
    last_error: str | None = None
    applied_access: McpAccessSnapshot | None = None
    access_state: Literal["pending", "applying", "applied", "failed", "unknown"] = "pending"
    # The application principal the server's tools call governed models as, when an administrator
    # names one. Its model calls are then attributed to the person each MCP call served, never
    # authorized by them. The applied snapshot's ``model_caller`` says what's live. See ADR 0025.
    model_caller_id: str | None = Field(default=None, exclude_if=_unset)

    def created_resources(self) -> list[PublishedResource]:
        """The subset rollback and unpublish are allowed to delete."""

        return [resource for resource in self.resources if resource.created_by_mosaic]

    def has_applied_api(self) -> bool:
        """Whether MOSAIC's record says this publication's MCP API is in API Management.

        False for a publication never applied and once an unpublish has deleted the MCP API. The
        metadata API doesn't count: it serves sign-in discovery, not the server.
        """

        return _holds_api(self.resources, self.api_name)

    def may_own_gateway_state(self) -> bool:
        """Whether API Management may hold something this publication is responsible for."""

        return bool(
            self.created_resources()
            or self.status == PublicationStatus.APPLYING
            or self.access_state in {"applying", "unknown"}
            or (
                self.applied_access
                and any(grant.enabled for grant in self.applied_access.grants)
            )
        )


class McpPublishingCapability(MosaicModel):
    """Whether MOSAIC can publish MCP servers through a gateway, and why not when it can't."""

    gateway_id: str
    supported: bool
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CredentialReference(Entity):
    entity_type: Literal["credentialReference"] = "credentialReference"
    name: str
    secret_uri: AnyHttpUrl


class PolicyRevision(Entity):
    entity_type: Literal["policyRevision"] = "policyRevision"
    entitlement_id: str
    revision: int = Field(ge=1)
    content_sha256: str
    policy_xml: str


class SyncStatus(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class SyncOperation(Entity):
    entity_type: Literal["syncOperation"] = "syncOperation"
    status: SyncStatus
    desired_revision: str
    plan: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    completed_at: datetime | None = None


class AuditEvent(Entity):
    entity_type: Literal["auditEvent"] = "auditEvent"
    action: str
    resource_type: str
    resource_id: str
    actor_object_id: str
    details: dict[str, Any] = Field(default_factory=dict)


class PrincipalCreate(MosaicModel):
    object_id: str = Field(min_length=1, max_length=128)
    kind: PrincipalKind
    label: str | None = Field(default=None, max_length=200)
    # Only honoured when directory lookup is off. When it is on, MOSAIC reads this from Microsoft
    # Graph and ignores what the caller sent.
    identity_parent_id: str | None = Field(default=None, max_length=128)
    # The cost center this principal's calls are charged to when they name none. Omitted, it's
    # the tenant's default. Ignored for a security group.
    default_cost_center_id: str | None = Field(default=None, min_length=1, max_length=128)


class PrincipalUpdate(MosaicModel):
    kind: PrincipalKind | None = None
    label: str | None = Field(default=None, max_length=200)
    default_cost_center_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("kind")
    @classmethod
    def kind_cannot_be_null(cls, value: PrincipalKind | None) -> PrincipalKind:
        if value is None:
            raise ValueError("kind cannot be null")
        return value


class GroupCreate(MosaicModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)


class GroupUpdate(MosaicModel):
    description: str | None = Field(default=None, max_length=1000)


class DirectorySearchKind(StrEnum):
    """What a directory search looks for.

    ``user`` finds people and agent users. ``group`` finds security-enabled groups only, because
    only those appear in a token's ``groups`` claim. ``agent`` finds agent identities and agent
    users.
    """

    USER = "user"
    GROUP = "group"
    AGENT = "agent"


class DirectoryObject(MosaicModel):
    """An Entra object read from Microsoft Graph, before or after MOSAIC records it.

    MOSAIC reads the directory with its own identity and never writes to it. ``principal_id`` is
    set when MOSAIC already records this object, so a picker can say so instead of offering to
    add it twice.
    """

    object_id: str
    kind: PrincipalKind
    display_name: str | None = None
    # A user principal name or mail for a user, an app ID for an agent identity, and a mail
    # nickname or description for a group.
    detail: str | None = None
    app_id: str | None = None
    identity_parent_id: str | None = None
    blueprint_id: str | None = None
    principal_id: str | None = None


class DirectoryMemberPage(MosaicModel):
    """The direct and nested members of a security group, as far as MOSAIC read them."""

    group_object_id: str
    members: list[DirectoryObject] = Field(default_factory=list)
    truncated: bool = False


class DirectoryStatus(MosaicModel):
    """Whether directory search is available, and whether group grants can be enforced.

    ``lookup_enabled`` is the deployment's choice (``MOSAIC_ENTRA_DIRECTORY_LOOKUP``). When it is
    false, administrators enter object IDs by hand. ``group_claims_enabled`` records whether the
    deployment configured Entra to put security-group IDs in tokens
    (``MOSAIC_ENTRA_GROUP_CLAIMS``); without that, the gateway can't match a group grant.
    """

    lookup_enabled: bool
    group_claims_enabled: bool
    message: str | None = None


class GrantOverlapKind(StrEnum):
    """Why two or more grants on one resource can apply to the same caller.

    ``groups``: two or more security groups are granted the same resource, so anyone in more than
    one of them gets only the most generous grant. ``directAndGroup``: a principal with a direct
    grant is also a member of a granted group, so the direct grant applies and the group's
    limits don't. ``multipleGroups``: a principal MOSAIC records is in more than one granted group.
    """

    GROUPS = "groups"
    DIRECT_AND_GROUP = "directAndGroup"
    MULTIPLE_GROUPS = "multipleGroups"


class OverlapGrant(MosaicModel):
    entitlement_id: str
    subject: EntitlementSubject
    subject_label: str
    enabled: bool = True
    enforcement: EntitlementEnforcement | None = None


class GrantOverlap(MosaicModel):
    kind: GrantOverlapKind
    resource: EntitlementResource
    resource_label: str
    # Grants overlap only under the same cost center: a caller picks between cost centers with
    # the cost-center header.
    cost_center_id: str | None = None
    # The affected principal, for directAndGroup and multipleGroups. None for groups, which is
    # about every member of both groups rather than one principal.
    principal_id: str | None = None
    principal_label: str | None = None
    winner: OverlapGrant
    shadowed: list[OverlapGrant] = Field(default_factory=list)
    reason: str


class GrantOverlapReport(MosaicModel):
    overlaps: list[GrantOverlap] = Field(default_factory=list)
    # Whether MOSAIC could ask Microsoft Graph who belongs to the granted groups. Without it only
    # ``groups`` overlaps, which need no membership data, are reported.
    membership_checked: bool = False
    skipped: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utc_now)


class GatewayCreate(MosaicModel):
    azure_resource_id: str = Field(min_length=1, max_length=512)
    name: str | None = Field(default=None, max_length=120)
    environment_label: str | None = Field(
        default=None,
        max_length=60,
        description="Deprecated: free-text legacy label. Use environment.",
        json_schema_extra={"deprecated": True},
    )
    environment: str | None = None
    provider: GatewayProvider = GatewayProvider.APIM

    @field_validator("azure_resource_id")
    @classmethod
    def validate_resource_id(cls, value: str) -> str:
        return ApimResourceId.parse(value).canonical


class GatewayUpdate(MosaicModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    environment_label: str | None = Field(
        default=None,
        max_length=60,
        description="Deprecated: free-text legacy label. Use environment.",
        json_schema_extra={"deprecated": True},
    )
    management_mode: ManagementMode | None = None

    @field_validator("name")
    @classmethod
    def name_cannot_be_null(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("name cannot be null")
        return value


class GatewaySuggestion(MosaicModel):
    azure_resource_id: str
    service_name: str
    resource_group: str
    subscription_id: str
    azure_environment_tag: str | None = None
    suggested_environment: str | None = None
    already_registered: bool
    gateway_id: str | None = None
    reason: str


class ModelEndpointCreate(MosaicModel):
    """Register an Azure endpoint by resource ID, or an endpoint by URL.

    ``credential_secret_uri`` is a Key Vault secret identifier, never a key. MOSAIC stores only the
    URI. An Azure OpenAI or Foundry URL with one is registered as a key-authenticated Azure
    endpoint (ADR 0018): the alternative for a resource MOSAIC's managed identity can't reach, such
    as one in another Microsoft Entra tenant. Its ``deployments`` are declared, because an API key
    can't list them. An AWS Bedrock endpoint, chosen with ``provider``, is registered the same way,
    with a Bedrock API key and the model IDs to pool (ADR 0024). Any other URL is an
    OpenAI-compatible endpoint.

    ``api_key`` takes the key itself, instead of a secret URI, for an administrator who can't put it
    in Key Vault: MOSAIC writes it into its own Key Vault and keeps only the secret's identifier
    (ADR 0021). It's write-only, and nothing MOSAIC returns, records or logs repeats it.
    """

    # A validation error's text never shows what was sent, which can be a key.
    model_config = ConfigDict(hide_input_in_errors=True)

    azure_resource_id: str | None = Field(default=None, max_length=512)
    endpoint: AnyHttpUrl | None = None
    name: str | None = Field(default=None, max_length=120)
    environment_label: str | None = Field(
        default=None,
        max_length=60,
        description="Deprecated: free-text legacy label. Use environment.",
        json_schema_extra={"deprecated": True},
    )
    environment: str | None = None
    provider: ModelProvider | None = None
    credential_secret_uri: AnyHttpUrl | None = None
    api_key: SecretStr | None = Field(
        default=None,
        exclude=True,
        description=(
            "An Azure OpenAI or Foundry resource's API key, or an AWS Bedrock API key, which "
            "MOSAIC stores in its own Key Vault. Give this or credentialSecretUri, not both. It "
            "is never returned."
        ),
    )
    deployments: list[DeclaredDeploymentCreate] | None = Field(
        default=None, max_length=MAX_DECLARED_DEPLOYMENTS
    )

    @field_validator("azure_resource_id")
    @classmethod
    def validate_resource_id(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return CognitiveServicesResourceId.parse(value).canonical

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        return None if value is None else normalized_api_key(value)

    @model_validator(mode="after")
    def validate_identification(self) -> Self:
        if not self.azure_resource_id and not self.endpoint:
            raise ValueError(
                "Provide an Azure resource ID for an Azure AI endpoint, or a URL for an AWS "
                "Bedrock or OpenAI-compatible endpoint"
            )
        if self.azure_resource_id:
            if self.provider == ModelProvider.AWS_BEDROCK:
                raise ValueError("Register an AWS Bedrock endpoint by its URL")
            if self.deployments:
                raise ValueError(
                    "MOSAIC reads the deployments of an endpoint registered by resource ID, so "
                    "none can be declared"
                )
            if self.api_key is not None:
                raise ValueError(
                    "MOSAIC reaches an endpoint registered by resource ID with its managed "
                    "identity, so it takes no API key"
                )
            return self
        # A Bedrock host also serves OpenAI-compatible routes, so the host alone doesn't choose
        # Bedrock. A key or declared model IDs do: an OpenAI-compatible endpoint takes neither.
        if self.provider is None and (
            is_bedrock_host(urlsplit(str(self.endpoint)).hostname)
            and (self.api_key is not None or bool(self.deployments))
        ):
            self.provider = ModelProvider.AWS_BEDROCK
        if self.provider == ModelProvider.AWS_BEDROCK:
            if self.credential_secret_uri is None and self.api_key is None:
                raise ValueError(
                    "Give the Bedrock API key, or the Key Vault secret URI that holds it"
                )
            if self.credential_secret_uri is not None and self.api_key is not None:
                raise ValueError(
                    "Give the API key, or the Key Vault secret URI that holds it, not both"
                )
            BedrockEndpointUrl.parse(str(self.endpoint))
            if self.credential_secret_uri is not None:
                KeyVaultSecretId.parse(str(self.credential_secret_uri))
            validate_declarations(self.deployments or [], self.provider)
            return self
        azure_host = azure_ai_host_suffix(urlsplit(str(self.endpoint)).hostname) is not None
        azure_provider = self.provider in {
            ModelProvider.AZURE_OPENAI,
            ModelProvider.AZURE_AI_FOUNDRY,
        }
        if self.provider == ModelProvider.OPENAI_COMPATIBLE and azure_host:
            raise ValueError(
                "That's an Azure OpenAI or Foundry endpoint. Register it by resource ID, or by URL "
                "with its API key, so MOSAIC can publish it"
            )
        if azure_provider or (self.provider is None and azure_host):
            if self.credential_secret_uri is None and self.api_key is None:
                raise ValueError(
                    "Register an Azure OpenAI or Foundry resource by its resource ID, so MOSAIC "
                    "uses its managed identity. If MOSAIC can't reach the resource that way, for "
                    "example because it's in another Microsoft Entra tenant, give its API key, or "
                    "the Key Vault secret URI that holds it"
                )
            if self.credential_secret_uri is not None and self.api_key is not None:
                raise ValueError(
                    "Give the API key, or the Key Vault secret URI that holds it, not both"
                )
            url = AzureAiEndpointUrl.parse(str(self.endpoint))
            if self.provider is None:
                self.provider = url.provider
            if self.credential_secret_uri is not None:
                KeyVaultSecretId.parse(str(self.credential_secret_uri))
            validate_declarations(self.deployments or [], self.provider)
            return self
        if self.deployments:
            raise ValueError(
                "Deployments can be declared only for an endpoint MOSAIC reaches with an API key: "
                "an Azure OpenAI or Foundry resource, or AWS Bedrock"
            )
        if self.api_key is not None:
            raise ValueError(
                "MOSAIC stores an API key itself only for an Azure OpenAI or Foundry resource, or "
                "for AWS Bedrock. Give an OpenAI-compatible endpoint the Key Vault secret URI that "
                "holds its key"
            )
        self.provider = ModelProvider.OPENAI_COMPATIBLE
        if self.credential_secret_uri is None:
            raise ValueError(
                "An OpenAI-compatible endpoint needs a Key Vault secret URI holding its API key"
            )
        return self


class ModelEndpointUpdate(MosaicModel):
    # A validation error's text never shows what was sent, which can be a key.
    model_config = ConfigDict(hide_input_in_errors=True)

    name: str | None = Field(default=None, min_length=1, max_length=120)
    environment_label: str | None = Field(
        default=None,
        max_length=60,
        description="Deprecated: free-text legacy label. Use environment.",
        json_schema_extra={"deprecated": True},
    )
    credential_secret_uri: AnyHttpUrl | None = None
    api_key: SecretStr | None = Field(
        default=None,
        exclude=True,
        description=(
            "A new API key for an endpoint whose key MOSAIC stores. MOSAIC writes it as the next "
            "version of the same Key Vault secret. It is never returned."
        ),
    )

    @field_validator("name")
    @classmethod
    def name_cannot_be_null(cls, value: str | None) -> str:
        # ``exclude_unset`` keeps an explicitly submitted null, which would then fail validation
        # against the required field on the entity and surface as a 500 rather than a 4xx.
        if value is None:
            raise ValueError("name cannot be null")
        return value

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        return None if value is None else normalized_api_key(value)

    @model_validator(mode="after")
    def one_credential(self) -> Self:
        if self.api_key is not None and self.credential_secret_uri is not None:
            raise ValueError("Give a new API key or a new Key Vault secret URI, not both")
        if self.api_key is not None and self.model_fields_set - {"api_key"}:
            raise ValueError("Send a new API key on its own, without other changes")
        return self


class McpEndpointCreate(MosaicModel):
    """Register an MCP server by URL.

    ``credential_secret_uri`` is a Key Vault secret identifier holding a bearer token, never the
    token itself. ``resource_audience`` names who a managed-identity token is for; MOSAIC will not
    infer it, because attaching a token to a host nobody named is how a managed identity leaks.
    """

    endpoint: AnyHttpUrl
    name: str | None = Field(default=None, max_length=120)
    environment_label: str | None = Field(
        default=None,
        max_length=60,
        description="Deprecated: free-text legacy label. Use environment.",
        json_schema_extra={"deprecated": True},
    )
    environment: str | None = None
    auth_mode: McpAuthMode | None = None
    credential_secret_uri: AnyHttpUrl | None = None
    resource_audience: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def validate_auth(self) -> Self:
        if self.auth_mode is None:
            if self.credential_secret_uri is not None:
                self.auth_mode = McpAuthMode.API_KEY
            elif self.resource_audience:
                self.auth_mode = McpAuthMode.MANAGED_IDENTITY
            else:
                self.auth_mode = McpAuthMode.NONE
        if self.auth_mode == McpAuthMode.API_KEY and self.credential_secret_uri is None:
            raise ValueError(
                "A key-authenticated MCP server needs a Key Vault secret URI holding its token"
            )
        if self.auth_mode == McpAuthMode.MANAGED_IDENTITY and not self.resource_audience:
            raise ValueError(
                "A managed-identity MCP server needs the audience its token should be issued for"
            )
        if self.auth_mode == McpAuthMode.NONE and (
            self.credential_secret_uri is not None or self.resource_audience
        ):
            raise ValueError(
                "An unauthenticated MCP server must not carry a secret URI or an audience"
            )
        return self


class McpEndpointUpdate(MosaicModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    environment_label: str | None = Field(
        default=None,
        max_length=60,
        description="Deprecated: free-text legacy label. Use environment.",
        json_schema_extra={"deprecated": True},
    )
    credential_secret_uri: AnyHttpUrl | None = None
    resource_audience: str | None = Field(default=None, max_length=512)

    @field_validator("name")
    @classmethod
    def name_cannot_be_null(cls, value: str | None) -> str:
        # ``exclude_unset`` keeps an explicitly submitted null, which would then fail validation
        # against the required field on the entity and surface as a 500 rather than a 4xx.
        if value is None:
            raise ValueError("name cannot be null")
        return value


class SuggestionSource(StrEnum):
    BOOTSTRAP = "bootstrap"
    GATEWAY_BACKEND = "gatewayBackend"
    SUBSCRIPTION_SCAN = "subscriptionScan"


class ModelEndpointSuggestion(MosaicModel):
    """An endpoint MOSAIC believes is worth registering, and how it found it.

    ``azure_resource_id`` is absent when a gateway routes to a hostname MOSAIC cannot resolve to a
    resource; the hostname is still offered so an administrator can finish the identification.
    """

    source: SuggestionSource
    endpoint: AnyHttpUrl | None = None
    azure_resource_id: str | None = None
    account_name: str | None = None
    resource_group: str | None = None
    subscription_id: str | None = None
    kind: str | None = None
    location: str | None = None
    provider: ModelProvider | None = None
    azure_environment_tag: str | None = None
    suggested_environment: str | None = None
    already_registered: bool = False
    model_endpoint_id: str | None = None
    reason: str


class SubscriptionScanIssue(MosaicModel):
    """One subscription MOSAIC could not fully enumerate, and what would fix it."""

    subscription_id: str
    display_name: str | None = None
    message: str
    remediation: AccessRemediation | None = None


class SubscriptionScanStatus(StrEnum):
    """Whether the subscription scan ran, and if it did not, why it had nothing to read.

    ``noVisibleSubscriptions`` and ``listFailed`` leave no single subscription to blame, so they are
    remediated on the view itself rather than as a :class:`SubscriptionScanIssue`.
    """

    NOT_CONFIGURED = "notConfigured"
    LIST_FAILED = "listFailed"
    NO_VISIBLE_SUBSCRIPTIONS = "noVisibleSubscriptions"
    SCANNED = "scanned"


class ModelEndpointSuggestionView(MosaicModel):
    suggestions: list[ModelEndpointSuggestion] = Field(default_factory=list)
    scan_issues: list[SubscriptionScanIssue] = Field(
        default_factory=list,
        description="Subscriptions whose Azure AI resources MOSAIC could not list at all.",
    )
    partial_scans: list[SubscriptionScanIssue] = Field(
        default_factory=list,
        description=(
            "Subscriptions MOSAIC listed without a role that reads every Azure AI resource in "
            "them. Azure leaves out what the caller cannot read without saying so, so these still "
            "count as scanned and whatever they yielded is still suggested."
        ),
    )
    subscriptions_scanned: int = 0
    scan_status: SubscriptionScanStatus
    scan_message: str | None = Field(
        default=None,
        description=(
            "Why MOSAIC could not list subscriptions. MOSAIC's own wording, never upstream text."
        ),
    )
    scan_remediation: list[AccessRemediation] = Field(
        default_factory=list,
        description=(
            "Reader assignments at subscription scope that would give the scan something to read. "
            "Offered only when it could see no subscription at all; MOSAIC never assigns them."
        ),
    )


class ImportRequest(MosaicModel):
    """The APIM API names an administrator chose to adopt.

    Detection decides which boxes start checked, not which imports are allowed. An administrator
    may adopt an API MOSAIC did not recognise, so no name is rejected for failing classification —
    only for being absent from the gateway's current snapshot.
    """

    api_names: list[str] = Field(min_length=1, max_length=500)

    @field_validator("api_names")
    @classmethod
    def clean_names(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for name in value:
            trimmed = name.strip()
            if not trimmed:
                raise ValueError("apiNames cannot contain blank entries")
            key = trimmed.casefold()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(trimmed)
        return cleaned


class ModelApiCandidate(MosaicModel):
    api_name: str
    display_name: str
    path: str
    service_url: str | None = None
    ai_kind: AiBackendKind = AiBackendKind.NONE
    ai_signals: list[str] = Field(default_factory=list)
    operation_count: int = 0
    product_names: list[str] = Field(default_factory=list)
    recommended: bool = False
    already_imported: bool = False


class McpServerCandidate(MosaicModel):
    api_name: str
    display_name: str
    path: str
    service_url: str | None = None
    kind: McpServerKind = McpServerKind.REST_API_BACKED
    transport_type: McpTransportType = McpTransportType.UNKNOWN
    tool_count: int = 0
    recommended: bool = True
    already_imported: bool = False


class ModelApiCandidateList(MosaicModel):
    gateway_id: str
    snapshot_id: str | None = None
    last_synced_at: datetime | None = None
    candidates: list[ModelApiCandidate] = Field(default_factory=list)


class McpServerCandidateList(MosaicModel):
    gateway_id: str
    snapshot_id: str | None = None
    last_synced_at: datetime | None = None
    support: CapabilitySupport = CapabilitySupport.UNKNOWN
    candidates: list[McpServerCandidate] = Field(default_factory=list)


class PolicyScope(StrEnum):
    GLOBAL = "global"
    PRODUCT = "product"
    API = "api"
    OPERATION = "operation"


class PolicySection(StrEnum):
    INBOUND = "inbound"
    BACKEND = "backend"
    OUTBOUND = "outbound"
    ON_ERROR = "onError"
    UNKNOWN = "unknown"


class PolicyFacetKind(StrEnum):
    RATE_LIMIT = "rateLimit"
    TOKEN_LIMIT = "tokenLimit"
    QUOTA = "quota"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    ROUTING = "routing"
    CACHING = "caching"
    CONTENT_SAFETY = "contentSafety"
    TRANSFORMATION = "transformation"
    OBSERVABILITY = "observability"
    NETWORK = "network"
    FRAGMENT_INCLUDE = "fragmentInclude"
    UNRECOGNIZED = "unrecognized"


class FacetConfidence(StrEnum):
    RECOGNIZED = "recognized"
    PARTIAL = "partial"
    UNRECOGNIZED = "unrecognized"


class PolicyFacet(MosaicModel):
    """One plain-language statement about a gateway policy element.

    ``summary`` is the only field intended for display. ``element`` is retained for diagnostics and
    drift reasoning but is an APIM element name, never markup.
    """

    kind: PolicyFacetKind
    element: str
    section: PolicySection = PolicySection.UNKNOWN
    summary: str
    details: list[str] = Field(default_factory=list)
    attributes: dict[str, str] = Field(default_factory=dict)
    confidence: FacetConfidence = FacetConfidence.RECOGNIZED
    managed_by_mosaic: bool = False


class PolicyPreviewRequest(MosaicModel):
    enforcement: TokenEnforcement
    backend_resource: str = "https://cognitiveservices.azure.com"


class PolicyPreview(MosaicModel):
    """A preview of the policy MOSAIC would author.

    ``policy_xml`` is retained in process because a later apply phase needs it, but it is excluded
    from serialisation: administrators read the plain-language facets, and markup never crosses the
    API boundary into a browser.
    """

    policy_xml: str = Field(exclude=True)
    content_sha256: str
    facets: list[PolicyFacet] = Field(default_factory=list)
    unrecognized_elements: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class PublishAction(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    NO_CHANGE = "noChange"


class PublishStepStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    ROLLED_BACK = "rolledBack"
    ROLLBACK_FAILED = "rollbackFailed"


class PublishRunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLED_BACK = "rolledBack"
    ROLLBACK_FAILED = "rollbackFailed"
    INTERRUPTED = "interrupted"


class PublishPlanStep(MosaicModel):
    """One API Management write the plan intends to perform.

    ``existed`` is what MOSAIC observed *before* applying. It is what makes rollback able to
    distinguish a resource it created from one it merely updated.
    """

    kind: PublishedResourceKind
    name: str
    action: PublishAction
    reason: str
    resource_id: str
    existed: bool = False
    entitlement_id: str | None = None
    subscription_state: Literal["active", "suspended"] | None = None
    stage: Literal["prepare", "policy", "activate"] | None = None


class PublishPlan(Entity):
    """A deterministic, persisted description of the writes an apply will perform.

    Apply runs against a specific plan and rejects one whose digest no longer matches the
    publication, so an administrator can never approve one set of changes and have another applied.
    """

    entity_type: Literal["publishPlan"] = "publishPlan"
    publication_id: str
    # Which kind of publication ``publication_id`` names: a model publication, an MCP
    # publication, or a model pool (ADR 0024). Plans saved before MCP publishing existed are model
    # plans.
    target: Literal["model", "mcp", "pool"] = "model"
    # What running the plan does. A publish plan writes the publication's resources and is run by
    # apply; an unpublish plan deletes the ones MOSAIC created and is run by unpublish, and neither
    # route runs the other's. Plans saved before unpublishing was planned are publish plans.
    operation: Literal["publish", "unpublish"] = "publish"
    gateway_id: str
    digest: str
    steps: list[PublishPlanStep] = Field(default_factory=list)
    facets: list[PolicyFacet] = Field(default_factory=list)
    policy_content_sha256: str | None = None
    warnings: list[str] = Field(default_factory=list)
    actor_object_id: str | None = None
    access_snapshot: ModelAccessSnapshot | None = None
    mcp_access_snapshot: McpAccessSnapshot | None = None
    # A governed model pool's reviewed grants (ADR 0024).
    pool_access_snapshot: PoolAccessSnapshot | None = None
    previous_access_version: int | None = None


class PublishStepResult(MosaicModel):
    kind: PublishedResourceKind
    name: str
    action: PublishAction
    status: PublishStepStatus = PublishStepStatus.PENDING
    resource_id: str
    created_by_mosaic: bool = False
    error: str | None = None
    stage: Literal["prepare", "policy", "activate"] | None = None


class PublishRun(Entity):
    """The audited result of one apply, including what a rollback did or failed to do."""

    entity_type: Literal["publishRun"] = "publishRun"
    publication_id: str
    # Which kind of publication ``publication_id`` names; runs saved before MCP publishing existed
    # are model runs. Each publishing service reaps and recovers only its own runs.
    target: Literal["model", "mcp", "pool"] = "model"
    gateway_id: str
    plan_id: str
    plan_digest: str
    status: PublishRunStatus = PublishRunStatus.RUNNING
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    duration_ms: int | None = None
    steps: list[PublishStepResult] = Field(default_factory=list)
    rolled_back: bool = False
    orphaned_resources: list[PublishedResource] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    actor_object_id: str | None = None
    access_snapshot: ModelAccessSnapshot | None = None
    mcp_access_snapshot: McpAccessSnapshot | None = None
    pool_access_snapshot: PoolAccessSnapshot | None = None


class PublicationCreate(MosaicModel):
    gateway_id: str = Field(min_length=1, max_length=128)
    model_endpoint_id: str = Field(min_length=1, max_length=128)
    deployment_name: str = Field(min_length=1, max_length=64)
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    api_name: str | None = Field(default=None, min_length=1, max_length=80)
    api_path: str | None = Field(default=None, min_length=1, max_length=200)
    product_name: str | None = Field(default=None, min_length=1, max_length=80)
    subscription_required: bool = True
    # Required unless the deployment's API shape can't be token-metered on the gateway's tier, in
    # which case it must be omitted. The publishable-models listing reports which applies.
    enforcement: TokenEnforcement | None = None
    governed_access: ModelAccessSettings | None = None

    @field_validator("api_name", "product_name")
    @classmethod
    def validate_resource_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", candidate):
            raise ValueError(
                "API Management resource names must start with a letter or digit and contain "
                "only letters, digits, and hyphens"
            )
        return candidate

    @field_validator("api_path")
    @classmethod
    def validate_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip().strip("/")
        if not candidate or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9\-/]*", candidate):
            raise ValueError(
                "An API path may contain only letters, digits, hyphens, and forward slashes"
            )
        return candidate


class PublicationUpdate(MosaicModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    subscription_required: bool | None = None
    enforcement: TokenEnforcement | None = None
    governed_access: ModelAccessSettings | None = None


class McpPublicationCreate(MosaicModel):
    gateway_id: str = Field(min_length=1, max_length=128)
    mcp_endpoint_id: str = Field(min_length=1, max_length=128)
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    api_name: str | None = Field(default=None, min_length=1, max_length=80)
    api_path: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("api_name")
    @classmethod
    def validate_resource_name(cls, value: str | None) -> str | None:
        return PublicationCreate.validate_resource_name(value)

    @field_validator("api_path")
    @classmethod
    def validate_path(cls, value: str | None) -> str | None:
        candidate = PublicationCreate.validate_path(value)
        if candidate is not None and candidate.casefold().startswith(".well-known"):
            raise ValueError("An MCP server's path can't start with .well-known")
        return candidate


class McpPublicationUpdate(MosaicModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)


class McpModelCallerUpdate(MosaicModel):
    """The application principal an MCP server's tools call governed models as. See ADR 0025."""

    principal_id: str = Field(min_length=1, max_length=128)


class ConnectionOperation(MosaicModel):
    name: str
    method: str
    path: str


class KeySharedModel(MosaicModel):
    """Another model in a pool that a grant's key also unlocks (ADR 0024).

    A pool key serves every model its subject holds directly in the pool under one cost center,
    so rotating or deleting it affects each of them.
    """

    pool_model_id: str
    display_name: str
    public_name: str


class ModelConnection(MosaicModel):
    entitlement_id: str
    publication_id: str
    gateway_id: str
    endpoint: str
    deployment_name: str
    tenant_id: str
    runtime: EntitlementRuntime | None = None
    applied_methods: ModelAccessSettings | None = None
    entra_audience: str | None = None
    entra_scope: str | None = None
    entra_client_id: str | None = None
    subscription_header: str = "Ocp-Apim-Subscription-Key"
    api_shape: ApiShape | None = None
    operations: list[ConnectionOperation] = Field(default_factory=list)
    publication_limits: TokenEnforcement | None = None
    grant_limits: EntitlementEnforcement | None = None
    # The kind of principal the grant names. For a security-group grant this is securityGroup and
    # the caller is any member, a person or an agent.
    principal_kind: PrincipalKind | None = None
    # The app role an application or agent caller's token must carry in ``roles``. Entra doesn't
    # pass app roles through group membership to service principals, so an agent needs this role
    # assigned to itself, or inherited from its blueprint through the blueprint's inheritable
    # permissions, even when its access comes from a group.
    required_app_role: str | None = None
    # ``api://{audience}/.default``, for agents and applications that reach the model through a
    # security-group grant. Only set on security-group grants; ``entra_scope`` covers the rest.
    entra_application_scope: str | None = None
    # False for a security-group grant, which authorizes Entra tokens only.
    keys_available: bool = True
    via_group_id: str | None = None
    via_group_name: str | None = None
    # The cost center the grant charges. A caller holding grants under several cost centers names
    # this one with ``cost_center_header: <code>``; without it the gateway uses their default.
    cost_center: CostCenterRef | None = None
    cost_center_header: str = COST_CENTER_HEADER
    # Whether the grant's key exists. Keys are created on request and never by an apply.
    key_exists: bool = False
    # False when the cost center turned keys off. Keys work only when the publication accepts keys
    # and the cost center allows them.
    keys_allowed_by_cost_center: bool = True
    # Set for a grant on a pool model (ADR 0024). ``publication_id`` is then the pool's ID,
    # ``deployment_name`` is the model name callers send, and ``publication_limits`` is the
    # pool's safeguard on the model, which every caller of the model shares.
    pool_id: str | None = None
    pool_name: str | None = None
    pool_model_id: str | None = None
    # The pool's other models this grant's key unlocks, as the pool's last apply enforces them.
    key_shared_with: list[KeySharedModel] = Field(default_factory=list)
    # False when the gateway's tier can't count this model's tokens, so no token limit applies.
    # A pool model's ``publication_limits`` is also None when its pool has no safeguard, so this
    # tells the two apart.
    token_metering: bool = True


class McpConnection(MosaicModel):
    """How a grantee connects to a governed MCP server, and whether the gateway enforces it yet.

    ``enforced`` is true only for a server MOSAIC publishes whose grant is applied. Grants on an
    adopted server, or on a published one before its first apply, are recorded but not enforced,
    and ``status_message`` says which.
    """

    entitlement_id: str
    mcp_server_id: str
    publication_id: str | None = None
    gateway_id: str
    display_name: str
    tenant_id: str
    # ``https://{gateway}/{api_path}/mcp``. None until MOSAIC knows the gateway's URL.
    server_url: str | None = None
    transport: McpTransportType = McpTransportType.STREAMABLE
    enforced: bool = False
    status_message: str
    runtime: EntitlementRuntime | None = None
    entra_audience: str | None = None
    # ``api://{audience}/Mcp.Invoke``, for people and agent users.
    delegated_scope: str | None = None
    # ``api://{audience}/.default``, for agents and applications signing in as themselves.
    application_scope: str | None = None
    # ``Mcp.Invoke.Application``. Entra doesn't pass app roles to service principals through
    # group membership, so an agent needs it assigned to itself, or inherited from its blueprint
    # through the blueprint's inheritable permissions.
    required_app_role: str | None = None
    client_id: str | None = None
    principal_kind: PrincipalKind | None = None
    via_group_id: str | None = None
    via_group_name: str | None = None
    # ``https://{gateway}/.well-known/oauth-protected-resource/{api_path}/mcp``.
    resource_metadata_url: str | None = None
    limits: EntitlementEnforcement | None = None
    cost_center: CostCenterRef | None = None
    cost_center_header: str = COST_CENTER_HEADER


class KeyRevealRequest(MosaicModel):
    slot: Literal["primary", "secondary"] = "primary"


class KeyRevealResult(MosaicModel):
    entitlement_id: str
    subscription_name: str
    slot: Literal["primary", "secondary"]
    key: str = Field(repr=False)
    cost_center: CostCenterRef | None = None


class KeyRotateRequest(MosaicModel):
    slot: Literal["primary", "secondary"] = "primary"


class GrantKey(MosaicModel):
    """A grant's key as its holder manages it: whether it exists, never its value.

    The key is an API Management subscription with a deterministic name, so creating it again
    after a delete gives the same subscription new values, and no re-apply is needed.
    """

    entitlement_id: str
    subscription_name: str
    exists: bool
    cost_center: CostCenterRef | None = None
    # The slot a rotation regenerated.
    rotated: Literal["primary", "secondary"] | None = None
    # Set for a pool model grant, whose key is shared: the pool, and its other models the key
    # unlocks (ADR 0024).
    pool_id: str | None = None
    key_shared_with: list[KeySharedModel] = Field(default_factory=list)


class PublishRecoveryRequest(MosaicModel):
    run_id: str = Field(min_length=1, max_length=128)
    confirm_quiesced: bool = False


class PublicationLockInfo(MosaicModel):
    publication_id: str
    owner_id: str | None = None


class DeploymentCapability(StrEnum):
    """What a deployment does, as far as MOSAIC can tell from its ARM model and capability flags.

    Only a positively identified capability is ever used to refuse publishing. ``UNKNOWN`` covers
    deployments whose flags say nothing, which partner models often do, and stays publishable.
    """

    CHAT = "chat"
    RESPONSES = "responses"
    COMPLETION = "completion"
    EMBEDDINGS = "embeddings"
    IMAGE = "image"
    TRANSCRIPTION = "transcription"
    SPEECH = "speech"
    REALTIME = "realtime"
    VIDEO = "video"
    RERANK = "rerank"
    UNKNOWN = "unknown"


class VerdictLevel(StrEnum):
    ALLOWED = "allowed"
    WARNING = "warning"
    BLOCKED = "blocked"


class EnvironmentVerdict(MosaicModel):
    """Whether a gateway in one environment may front an endpoint in another, and why.

    Defined here rather than in ``environments`` so publishing models can carry it without an
    import cycle; ``mosaic_api.environments`` re-exports it with the compatibility rules.
    """

    level: VerdictLevel
    reason: str
    gateway_environment: str | None
    endpoint_environment: str | None
    via_exception: bool = False


class PublishableModel(MosaicModel):
    """A deployment on a registered endpoint that could be published through a given gateway.

    ``runtime_access`` is carried through unchanged rather than collapsed into a boolean, because
    ADR 0006's distinction between "the gateway cannot call this" and "MOSAIC could not evaluate
    whether the gateway can call this" has to survive into the publish experience.

    Every observed deployment is listed. One MOSAIC has no curated shape for is reported with
    ``publishable`` false and a reason rather than hidden, so an administrator can see why.
    """

    model_endpoint_id: str
    endpoint_name: str
    provider: ModelProvider
    deployment_name: str
    model_name: str | None = None
    model_version: str | None = None
    model_format: str | None = None
    model_publisher: str | None = None
    capability: DeploymentCapability = DeploymentCapability.UNKNOWN
    api_shape: ApiShape | None = None
    publishable: bool = True
    unpublishable_reason: str | None = None
    token_limits_supported: bool = True
    token_limits_note: str | None = None
    publication_id: str | None = None
    publication_status: PublicationStatus | None = None
    suggested_api_name: str = ""
    suggested_api_path: str = ""
    runtime_access: GatewayRuntimeAccess | None = None
    environment_verdict: EnvironmentVerdict
    # True when an administrator declared this deployment rather than MOSAIC reading it from Azure.
    declared: bool = False
