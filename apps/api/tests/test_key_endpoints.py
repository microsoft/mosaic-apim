"""Key-authenticated Azure endpoints: registration, declared deployments and readiness (ADR 0018).

A Foundry resource MOSAIC's managed identity can't reach, such as one in another Microsoft Entra
tenant, is registered by URL with the Key Vault secret that holds its key. These drive the real
service against doubles of Key Vault's ARM surface, the key check and the secret read.
"""

import json
import logging
from typing import Any, cast

import httpx
import pytest
from aoai_double import AI_RESOURCE_ID, FakeCognitiveServices
from apim_double import APIM_PRINCIPAL_ID, RESOURCE_ID, SERVICE_NAME, FakeCredential
from azure.core.credentials_async import AsyncTokenCredential
from conftest import build_endpoint_service
from fastapi.testclient import TestClient
from key_vault_double import (
    KEY_VAULT_READER_ROLE_ID,
    KEY_VAULT_SECRETS_USER_ROLE_ID,
    SECRET_NAME,
    SECRET_SCOPE,
    SECRET_URI,
    VAULT_ID,
    VAULT_NAME,
    VAULT_RESOURCE_GROUP,
    VAULT_SUBSCRIPTION_ID,
    FakeKeyStore,
    FakeKeyVaultArm,
    vault_deny,
    vault_role_assignment,
)
from mosaic_api.config import Settings
from mosaic_api.domain import (
    AccessEvaluation,
    ApiShape,
    AzureAiEndpointUrl,
    DeclaredDeploymentCreate,
    EndpointAuthMode,
    Gateway,
    GatewayCapabilities,
    KeyVaultSecretId,
    ModelEndpoint,
    ModelEndpointCreate,
    ModelEndpointStatus,
    ModelEndpointUpdate,
    ModelProvider,
    NetworkReachability,
    RuntimeAccessEvaluation,
    RuntimeAccessReason,
    SuggestionSource,
    new_id,
)
from mosaic_api.errors import (
    ConflictError,
    NotFoundError,
    UpstreamAuthorizationError,
    UpstreamError,
    ValidationError,
)
from mosaic_api.integrations.aoai.backend_key_access import KeyVaultLocator
from mosaic_api.integrations.aoai.client import SubscriptionScanner
from mosaic_api.integrations.aoai.key_check import (
    MODELS_LIST_API_VERSION,
    EndpointKeyProbe,
    KeyCheckOutcome,
    KeyCheckResult,
)
from mosaic_api.integrations.apim import ArmClient
from mosaic_api.main import create_app
from mosaic_api.observed import AiBackendKind, ObservedBackend
from mosaic_api.repositories import InMemoryGatewayRepository, InMemoryModelEndpointRepository
from mosaic_api.services.directory import Actor
from mosaic_api.services.model_endpoints import ModelEndpointService
from structlog.testing import capture_logs

ACTOR = Actor(object_id="admin-object-id", tenant_id="tenant-test")
MOSAIC_PRINCIPAL_ID = "mosaic-managed-identity"
PROJECT_URL = "https://fabrikam-foundry.services.ai.azure.com/api/projects/partner-models"
ORIGIN = "https://fabrikam-foundry.services.ai.azure.com"
VERSION = "0123456789abcdef0123456789abcdef"
# Fictional, and deliberately recognisable: no test may find it anywhere MOSAIC writes.
KEY = "fictional-key-VALUE-never-logged-1234"
CLAUDE = DeclaredDeploymentCreate(
    deployment_name="claude-sonnet-4-5",
    model_name="claude-sonnet-4-5",
    api_shape=ApiShape.ANTHROPIC_MESSAGES,
)
GPT = DeclaredDeploymentCreate(
    deployment_name="gpt-4-1", model_name="gpt-4.1", api_shape=ApiShape.AZURE_OPENAI
)


async def _no_sleep(_seconds: float) -> None:
    return None


def keyed(**overrides: object) -> ModelEndpointCreate:
    payload: dict[str, object] = {
        "endpoint": PROJECT_URL,
        "credential_secret_uri": SECRET_URI,
        "name": "Fabrikam partner Foundry",
        "deployments": [CLAUDE, GPT],
    }
    payload.update(overrides)
    return ModelEndpointCreate.model_validate(payload)


def gateway(
    *, principal_id: str | None = APIM_PRINCIPAL_ID, observed: bool = True, name: str = SERVICE_NAME
) -> Gateway:
    return Gateway(
        id=new_id("gateway"),
        tenant_id=ACTOR.tenant_id,
        name=name,
        azure_resource_id=RESOURCE_ID,
        subscription_id=VAULT_SUBSCRIPTION_ID,
        resource_group="rg-contoso-dev",
        service_name=SERVICE_NAME,
        capabilities=GatewayCapabilities(
            principal_id=principal_id,
            identity_observed=observed,
            virtual_network_type="None",
        ),
    )


class KeyWorld:
    """MOSAIC with one Key Vault it can see, a key check, and a secret it may or may not read."""

    def __init__(self, *, known_vault: bool = True, key_store: FakeKeyStore | None = None) -> None:
        self.aoai = FakeCognitiveServices()
        self.vault = FakeKeyVaultArm()
        self.gateway_repository = InMemoryGatewayRepository()
        self.endpoint_repository = InMemoryModelEndpointRepository()
        self.key_store = key_store
        self.secret_reads: list[str] = []
        self.secret_value = KEY
        self.probes: list[tuple[str, bool]] = []
        self.probed_keys: list[str] = []
        self.secret_error: Exception | None = None
        self.outcome = KeyCheckResult(KeyCheckOutcome.ACCEPTED, 200)
        self.arm = ArmClient(
            cast(AsyncTokenCredential, FakeCredential()),
            client=httpx.AsyncClient(transport=httpx.MockTransport(self.vault.handler)),
            sleep=_no_sleep,
        )
        self.locator = KeyVaultLocator(
            self.arm,
            scanner=SubscriptionScanner(self.arm),
            known_vault_ids=[VAULT_ID] if known_vault else [],
        )
        self.service: ModelEndpointService = build_endpoint_service(
            self.aoai,
            repository=self.endpoint_repository,
            gateway_repository=self.gateway_repository,
            principal_id=MOSAIC_PRINCIPAL_ID,
            secret_resolver=self.read_secret,
            key_probe=self.check_key,
            vault_locator=self.locator,
            key_store=key_store,
        )

    async def read_secret(self, uri: str) -> str:
        self.secret_reads.append(uri)
        if self.secret_error is not None:
            raise self.secret_error
        stored = self.key_store.current(uri) if self.key_store is not None else None
        return stored if stored is not None else self.secret_value

    async def check_key(self, origin: str, key: str) -> KeyCheckResult:
        self.probes.append((origin, key == KEY))
        self.probed_keys.append(key)
        return self.outcome

    async def add_gateway(self, **kwargs: Any) -> Gateway:
        record = gateway(**kwargs)
        return await self.gateway_repository.create_gateway(
            record,
            _audit(record.id),
        )

    def grant(self, scope: str = VAULT_ID, role: str = KEY_VAULT_SECRETS_USER_ROLE_ID) -> None:
        self.vault.assignments.append(vault_role_assignment(role, scope, APIM_PRINCIPAL_ID))

    async def register(self, **overrides: object) -> ModelEndpoint:
        return await self.service.register(ACTOR, keyed(**overrides))


def _audit(resource_id: str) -> Any:
    from mosaic_api.domain import AuditEvent

    return AuditEvent(
        id=new_id("audit"),
        tenant_id=ACTOR.tenant_id,
        action="gateway.registered",
        resource_type="gateway",
        resource_id=resource_id,
        actor_object_id=ACTOR.object_id,
    )


@pytest.fixture
async def world() -> KeyWorld:
    built = KeyWorld()
    await built.add_gateway()
    return built


class TestEndpointUrl:
    @pytest.mark.parametrize(
        ("url", "host", "project"),
        [
            (PROJECT_URL, "fabrikam-foundry.services.ai.azure.com", "partner-models"),
            (f"{PROJECT_URL}/", "fabrikam-foundry.services.ai.azure.com", "partner-models"),
            (ORIGIN, "fabrikam-foundry.services.ai.azure.com", None),
            ("https://Fabrikam-Foundry.cognitiveservices.azure.com/", "fabrikam-foundry"
             ".cognitiveservices.azure.com", None),
            ("https://fabrikam-aoai.openai.azure.com", "fabrikam-aoai.openai.azure.com", None),
            (f"{ORIGIN}/anthropic", "fabrikam-foundry.services.ai.azure.com", None),
            ("https://fabrikam-aoai.openai.azure.com/openai/v1/", "fabrikam-aoai.openai.azure.com",
             None),
        ],
    )
    def test_accepts_resource_and_project_endpoints(
        self, url: str, host: str, project: str | None
    ) -> None:
        parsed = AzureAiEndpointUrl.parse(url)
        assert parsed.host == host
        assert parsed.origin == f"https://{host}"
        assert parsed.project_name == project

    def test_the_host_names_the_resource_kind(self) -> None:
        assert AzureAiEndpointUrl.parse(ORIGIN).provider == ModelProvider.AZURE_AI_FOUNDRY
        assert (
            AzureAiEndpointUrl.parse("https://x.cognitiveservices.azure.com").provider
            == ModelProvider.AZURE_AI_FOUNDRY
        )
        assert (
            AzureAiEndpointUrl.parse("https://x.openai.azure.com").provider
            == ModelProvider.AZURE_OPENAI
        )

    @pytest.mark.parametrize(
        "url",
        [
            "http://fabrikam-foundry.services.ai.azure.com",
            "https://fabrikam-foundry.services.ai.azure.com:8443",
            f"{ORIGIN}/?api-key=abc",
            f"{ORIGIN}#fragment",
            "https://user:secret@fabrikam-foundry.services.ai.azure.com",
            "https://eastus.api.cognitive.microsoft.com",
            "https://models.example.com",
            "https://a.b.services.ai.azure.com",
            "https://fabrikam-aoai.openai.azure.com/api/projects/partner-models",
            f"{ORIGIN}/openai/deployments/gpt-4o/chat/completions",
            f"{ORIGIN}/api/projects/partner-models/openai",
            f"{ORIGIN}/api/projects/../secrets",
        ],
    )
    def test_refuses_anything_else(self, url: str) -> None:
        with pytest.raises(ValueError):
            AzureAiEndpointUrl.parse(url)


class TestSecretIdentifier:
    def test_versioned_identifiers_are_kept_versionless(self) -> None:
        parsed = KeyVaultSecretId.parse(f"{SECRET_URI}/{VERSION.upper()}")
        assert parsed.version == VERSION
        assert parsed.versionless == SECRET_URI
        assert parsed.vault_name == VAULT_NAME

    @pytest.mark.parametrize("uri", [SECRET_URI, f"{SECRET_URI}/", SECRET_URI.upper()])
    def test_accepts_secret_identifiers(self, uri: str) -> None:
        assert KeyVaultSecretId.parse(uri).versionless.casefold() == SECRET_URI.casefold()

    @pytest.mark.parametrize(
        "uri",
        [
            f"http://{VAULT_NAME}.vault.azure.net/secrets/{SECRET_NAME}",
            f"https://{VAULT_NAME}.vault.azure.net:443/secrets/{SECRET_NAME}",
            f"https://{VAULT_NAME}.example.com/secrets/{SECRET_NAME}",
            f"https://kv.vault.azure.net/secrets/{SECRET_NAME}",
            f"https://-kv-contoso.vault.azure.net/secrets/{SECRET_NAME}",
            f"https://kv--contoso.vault.azure.net/secrets/{SECRET_NAME}",
            f"https://{VAULT_NAME}.vault.azure.net/keys/{SECRET_NAME}",
            f"https://{VAULT_NAME}.vault.azure.net/secrets",
            f"https://{VAULT_NAME}.vault.azure.net/secrets/{SECRET_NAME}/not-a-version",
            f"https://{VAULT_NAME}.vault.azure.net/secrets/{SECRET_NAME}/{VERSION}/extra",
            f"https://{VAULT_NAME}.vault.azure.net/secrets/bad_name",
            f"{SECRET_URI}?api-version=7.4",
            f"{SECRET_URI}#x",
            f"https://user:pass@{VAULT_NAME}.vault.azure.net/secrets/{SECRET_NAME}",
        ],
    )
    def test_refuses_anything_but_a_secret_identifier(self, uri: str) -> None:
        with pytest.raises(ValueError):
            KeyVaultSecretId.parse(uri)


class TestCreateRequest:
    def test_an_azure_url_with_a_secret_is_the_key_path(self) -> None:
        request = keyed()
        assert request.provider == ModelProvider.AZURE_AI_FOUNDRY
        assert request.deployments is not None and len(request.deployments) == 2

    def test_an_openai_host_is_an_azure_openai_resource(self) -> None:
        request = keyed(endpoint="https://fabrikam-aoai.openai.azure.com", deployments=[GPT])
        assert request.provider == ModelProvider.AZURE_OPENAI

    def test_a_key_path_needs_a_secret(self) -> None:
        with pytest.raises(ValueError, match="by its resource ID"):
            ModelEndpointCreate(endpoint=PROJECT_URL)

    def test_an_azure_host_is_not_openai_compatible(self) -> None:
        with pytest.raises(ValueError, match="That's an Azure OpenAI or Foundry endpoint"):
            keyed(provider=ModelProvider.OPENAI_COMPATIBLE, deployments=None)

    def test_the_secret_must_be_a_key_vault_secret(self) -> None:
        with pytest.raises(ValueError, match="Azure Key Vault"):
            keyed(credential_secret_uri="https://secrets.example.com/secrets/key")

    def test_an_openai_compatible_endpoint_declares_nothing(self) -> None:
        with pytest.raises(ValueError, match="Deployments can be declared only"):
            keyed(endpoint="https://models.example.com/v1")

    def test_a_resource_id_registration_declares_nothing(self) -> None:
        with pytest.raises(ValueError, match="none can be declared"):
            ModelEndpointCreate.model_validate(
                {"azure_resource_id": AI_RESOURCE_ID, "deployments": [GPT]}
            )

    def test_an_azure_openai_resource_serves_only_the_azure_openai_api(self) -> None:
        with pytest.raises(ValueError, match="serves only the Azure OpenAI API"):
            keyed(endpoint="https://fabrikam-aoai.openai.azure.com", deployments=[CLAUDE])

    def test_a_deployment_is_declared_once(self) -> None:
        with pytest.raises(ValueError, match="declared twice"):
            keyed(
                deployments=[
                    CLAUDE,
                    CLAUDE.model_copy(update={"deployment_name": "CLAUDE-SONNET-4-5"}),
                ]
            )

    @pytest.mark.parametrize(
        "name", ["", "-leading", "has space", "a/b", "a{b}", "x" * 65, '"quoted"']
    )
    def test_deployment_names_are_literal(self, name: str) -> None:
        with pytest.raises(ValueError):
            DeclaredDeploymentCreate(
                deployment_name=name, model_name="gpt-4.1", api_shape=ApiShape.AZURE_OPENAI
            )


class TestRegistration:
    async def test_registers_the_resource_behind_a_project(self, world: KeyWorld) -> None:
        endpoint = await world.register(credential_secret_uri=f"{SECRET_URI}/{VERSION}")

        assert endpoint.auth_mode == EndpointAuthMode.API_KEY
        assert endpoint.provider == ModelProvider.AZURE_AI_FOUNDRY
        assert str(endpoint.endpoint).rstrip("/") == ORIGIN
        assert endpoint.account_name == "fabrikam-foundry"
        assert endpoint.project_name == "partner-models"
        assert endpoint.azure_resource_id is None
        assert [item.deployment_name for item in endpoint.declared_deployments] == [
            "claude-sonnet-4-5",
            "gpt-4-1",
        ]
        assert endpoint.declared_deployments[0].api_shape == ApiShape.ANTHROPIC_MESSAGES
        assert endpoint.declared_deployments[0].declared_by == ACTOR.object_id
        credential = world.endpoint_repository.credentials[endpoint.credential_reference_id or ""]
        # Stored without its version, so a rotated key reaches MOSAIC and API Management alike.
        assert str(credential.secret_uri) == SECRET_URI
        assert world.secret_reads == [SECRET_URI]
        assert world.probes == [(ORIGIN, True)]
        assert endpoint.status == ModelEndpointStatus.CONNECTED
        assert endpoint.access.can_read is True
        assert endpoint.access.evaluation == AccessEvaluation.PROBE

    async def test_a_resource_is_registered_once_whatever_host_names_it(
        self, world: KeyWorld
    ) -> None:
        first = await world.register()
        with pytest.raises(ConflictError, match="already registered with an API key") as error:
            await world.register(
                endpoint="https://fabrikam-foundry.openai.azure.com", deployments=[GPT]
            )
        assert error.value.details["id"] == first.id

    async def test_a_resource_mosaic_reads_by_id_needs_no_key(self, world: KeyWorld) -> None:
        by_id = await world.service.register(
            ACTOR, ModelEndpointCreate.model_validate({"azure_resource_id": AI_RESOURCE_ID})
        )
        with pytest.raises(ConflictError, match="doesn't need an API key") as error:
            await world.register(endpoint="https://contoso-aoai.services.ai.azure.com")
        assert error.value.details["id"] == by_id.id

    async def test_a_resource_reached_by_key_is_not_registered_again_by_id(
        self, world: KeyWorld
    ) -> None:
        keyed_endpoint = await world.register(
            endpoint="https://contoso-aoai.openai.azure.com", deployments=[GPT]
        )
        with pytest.raises(ConflictError, match="already reaches this resource with an API key"):
            await world.service.register(
                ACTOR, ModelEndpointCreate.model_validate({"azure_resource_id": AI_RESOURCE_ID})
            )
        assert await world.endpoint_repository.get_endpoint(ACTOR.tenant_id, keyed_endpoint.id)

    async def test_a_host_registered_as_openai_compatible_blocks_the_key_path(
        self, world: KeyWorld
    ) -> None:
        # Before the key path existed, an Azure host could only be registered as OpenAI-compatible.
        legacy = ModelEndpoint(
            id=new_id("endpoint"),
            tenant_id=ACTOR.tenant_id,
            name="Fabrikam as compatible",
            provider=ModelProvider.OPENAI_COMPATIBLE,
            endpoint="https://fabrikam-foundry.openai.azure.com/",
            auth_mode=EndpointAuthMode.API_KEY,
        )
        await world.endpoint_repository.create_endpoint(legacy, _audit(legacy.id))
        with pytest.raises(ConflictError, match="OpenAI-compatible endpoint Fabrikam"):
            await world.register()

    async def test_mosaic_without_access_to_the_secret_says_what_to_grant(
        self, world: KeyWorld
    ) -> None:
        world.secret_error = UpstreamAuthorizationError("denied")
        endpoint = await world.register()

        assert endpoint.status == ModelEndpointStatus.UNAUTHORIZED
        assert endpoint.access.can_read is False
        assert endpoint.access.remediation is not None
        assert endpoint.access.remediation.command == (
            "az role assignment create"
            f' --assignee-object-id "{MOSAIC_PRINCIPAL_ID}"'
            " --assignee-principal-type ServicePrincipal"
            ' --role "Key Vault Secrets User"'
            f' --scope "{VAULT_ID}"'
        )
        assert world.probes == []

    async def test_a_missing_secret_is_not_a_denial(self, world: KeyWorld) -> None:
        world.secret_error = ValidationError("That Key Vault secret does not exist.")
        endpoint = await world.register()
        assert endpoint.status == ModelEndpointStatus.DEGRADED
        assert "has no such secret" in (endpoint.access.message or "")

    async def test_an_unreachable_vault_is_recorded_not_raised(self, world: KeyWorld) -> None:
        world.secret_error = UpstreamError("unreachable")
        endpoint = await world.register()
        assert endpoint.status == ModelEndpointStatus.UNREACHABLE
        assert endpoint.access.evaluation == AccessEvaluation.NOT_EVALUATED

    async def test_a_refused_key_says_so(self, world: KeyWorld) -> None:
        world.outcome = KeyCheckResult(KeyCheckOutcome.REFUSED, 401)
        endpoint = await world.register()
        assert endpoint.status == ModelEndpointStatus.UNAUTHORIZED
        assert "refused the API key" in (endpoint.access.message or "")
        assert "HTTP 401" in (endpoint.access.message or "")

    async def test_an_unreachable_endpoint_is_not_a_denial(self, world: KeyWorld) -> None:
        world.outcome = KeyCheckResult(KeyCheckOutcome.UNREACHABLE)
        endpoint = await world.register()
        assert endpoint.status == ModelEndpointStatus.UNREACHABLE
        assert "isn't a denial" in (endpoint.access.message or "")

    @pytest.mark.parametrize("stored", [f"{KEY}\n", f"{KEY}\r\n", f" {KEY}"])
    async def test_a_key_stored_with_a_line_break_is_never_sent(
        self, world: KeyWorld, stored: str
    ) -> None:
        world.secret_value = stored
        endpoint = await world.register()
        assert endpoint.status == ModelEndpointStatus.DEGRADED
        assert endpoint.access.can_read is False
        assert "line break" in (endpoint.access.message or "")
        assert world.probes == []
        assert KEY not in endpoint.model_dump_json()

    async def test_without_key_vault_access_nothing_is_claimed(self) -> None:
        service = build_endpoint_service(FakeCognitiveServices())
        endpoint = await service.register(ACTOR, keyed())
        assert endpoint.status == ModelEndpointStatus.PENDING
        assert endpoint.access.evaluation == AccessEvaluation.NOT_EVALUATED

    async def test_an_api_key_cannot_list_deployments(self, world: KeyWorld) -> None:
        endpoint = await world.register()
        with pytest.raises(ConflictError, match="Declare the deployments"):
            await world.service.start_sync(ACTOR, endpoint.id)

    async def test_suggestions_know_a_resource_registered_with_a_key(
        self, world: KeyWorld
    ) -> None:
        [routing] = await world.gateway_repository.list_gateways(ACTOR.tenant_id)
        await world.gateway_repository.replace_observed(
            ACTOR.tenant_id,
            routing.id,
            [
                ObservedBackend(
                    id=new_id("obsBackend"),
                    tenant_id=ACTOR.tenant_id,
                    gateway_id=routing.id,
                    snapshot_id="snap-1",
                    name="fabrikam-backend",
                    url="https://fabrikam-foundry.openai.azure.com/openai",
                    ai_kind=AiBackendKind.AZURE_OPENAI,
                )
            ],
            "snap-1",
        )
        endpoint = await world.register()

        view = await world.service.suggestions(ACTOR)

        [suggestion] = [
            item for item in view.suggestions if item.source == SuggestionSource.GATEWAY_BACKEND
        ]
        assert suggestion.already_registered is True
        assert suggestion.model_endpoint_id == endpoint.id

    async def test_a_new_secret_is_kept_versionless_and_checked(self, world: KeyWorld) -> None:
        endpoint = await world.register()
        rotated = f"https://{VAULT_NAME}.vault.azure.net/secrets/fabrikam-foundry-key-2"
        world.outcome = KeyCheckResult(KeyCheckOutcome.REFUSED, 403)

        updated = await world.service.update(
            ACTOR, endpoint.id, ModelEndpointUpdate(credential_secret_uri=f"{rotated}/{VERSION}")
        )

        credential = world.endpoint_repository.credentials[endpoint.credential_reference_id or ""]
        assert str(credential.secret_uri) == rotated
        assert world.secret_reads[-1] == rotated
        assert updated.status == ModelEndpointStatus.UNAUTHORIZED
        # The declarations are the administrator's and survive the new check.
        assert len(updated.declared_deployments) == 2

    async def test_a_new_secret_must_be_a_key_vault_secret(self, world: KeyWorld) -> None:
        endpoint = await world.register()
        with pytest.raises(ValidationError, match="Azure Key Vault"):
            await world.service.update(
                ACTOR,
                endpoint.id,
                ModelEndpointUpdate(credential_secret_uri="https://secrets.example.com/secrets/k"),
            )


class TestGatewayReadiness:
    async def test_a_gateway_holding_the_role_on_the_vault_can_read_the_key(
        self, world: KeyWorld
    ) -> None:
        world.grant()
        endpoint = await world.register()

        [row] = endpoint.runtime_access
        assert row.can_invoke is True
        assert row.reason == RuntimeAccessReason.GRANTED
        assert row.evaluation == RuntimeAccessEvaluation.ROLE_ASSIGNMENTS
        assert row.granted_role_name == "Key Vault Secrets User"
        assert row.assignment_scope == VAULT_ID
        assert row.inherited is False
        assert row.evaluated_scope == VAULT_ID
        assert row.required_data_actions == [
            "Microsoft.KeyVault/vaults/secrets/getSecret/action"
        ]
        assert row.network_reachability == NetworkReachability.REACHABLE
        assert row.remediation is None

    async def test_a_role_inherited_from_the_resource_group_counts(self, world: KeyWorld) -> None:
        world.grant(
            scope=f"/subscriptions/{VAULT_SUBSCRIPTION_ID}/resourceGroups/{VAULT_RESOURCE_GROUP}"
        )
        [row] = (await world.register()).runtime_access
        assert row.can_invoke is True
        assert row.inherited is True
        assert "inherited" in (row.message or "")

    async def test_a_role_on_the_secret_itself_counts_and_names_only_the_vault(
        self, world: KeyWorld
    ) -> None:
        world.grant(scope=SECRET_SCOPE)
        endpoint = await world.register()
        [row] = endpoint.runtime_access
        assert row.can_invoke is True
        assert row.assignment_scope == VAULT_ID
        assert SECRET_NAME not in endpoint.model_dump_json()

    async def test_a_role_on_another_secret_grants_nothing(self, world: KeyWorld) -> None:
        world.grant(scope=f"{VAULT_ID}/secrets/another-key")
        [row] = (await world.register()).runtime_access
        assert row.can_invoke is False
        assert row.reason == RuntimeAccessReason.MISSING_ROLE

    async def test_a_role_that_reads_only_metadata_is_not_enough(self, world: KeyWorld) -> None:
        world.grant(role=KEY_VAULT_READER_ROLE_ID)
        [row] = (await world.register()).runtime_access
        assert row.can_invoke is False
        assert row.reason == RuntimeAccessReason.MISSING_ROLE

    async def test_a_missing_role_comes_with_the_command_that_grants_it(
        self, world: KeyWorld
    ) -> None:
        [row] = (await world.register()).runtime_access
        assert row.can_invoke is False
        assert row.evaluation == RuntimeAccessEvaluation.ROLE_ASSIGNMENTS
        assert row.reason == RuntimeAccessReason.MISSING_ROLE
        assert row.remediation is not None
        assert row.remediation.command == (
            "az role assignment create"
            f' --assignee-object-id "{APIM_PRINCIPAL_ID}"'
            " --assignee-principal-type ServicePrincipal"
            ' --role "Key Vault Secrets User"'
            f' --scope "{VAULT_ID}"'
        )

    async def test_unreadable_assignments_are_not_a_denial(self, world: KeyWorld) -> None:
        world.vault.assignments_readable = False
        [row] = (await world.register()).runtime_access
        assert row.evaluation == RuntimeAccessEvaluation.NOT_EVALUATED
        assert row.reason == RuntimeAccessReason.ASSIGNMENTS_UNREADABLE
        assert "isn't a denial" in (row.message or "")
        assert row.remediation is not None

    async def test_a_vault_mosaic_cannot_find_is_resolved_by_the_command(self) -> None:
        world = KeyWorld(known_vault=False)
        world.vault.searchable = False
        await world.add_gateway()
        [row] = (await world.register()).runtime_access

        assert row.evaluation == RuntimeAccessEvaluation.NOT_EVALUATED
        assert row.reason == RuntimeAccessReason.ASSIGNMENTS_UNREADABLE
        assert row.remediation is not None
        assert row.remediation.command.endswith(
            f'--scope "$(az keyvault show --name {VAULT_NAME} --query id --output tsv)"'
        )

    async def test_a_vault_mosaic_can_read_is_found_by_name(self) -> None:
        world = KeyWorld(known_vault=False)
        world.grant()
        await world.add_gateway()
        [row] = (await world.register()).runtime_access
        assert row.can_invoke is True
        assert row.evaluated_scope == VAULT_ID

    async def test_an_access_policy_vault_is_judged_by_its_policies(self, world: KeyWorld) -> None:
        world.vault.properties["enableRbacAuthorization"] = False
        world.vault.properties["accessPolicies"] = []
        endpoint = await world.register()
        [row] = endpoint.runtime_access
        assert row.can_invoke is False
        assert row.remediation is not None
        assert row.remediation.command == (
            f'az keyvault set-policy --name "{VAULT_NAME}" --object-id "{APIM_PRINCIPAL_ID}" '
            "--secret-permissions get list"
        )

        world.vault.properties["accessPolicies"] = [
            {
                "tenantId": "00000000-0000-0000-0000-000000000000",
                "objectId": APIM_PRINCIPAL_ID,
                "permissions": {"secrets": ["Get", "List"]},
            }
        ]
        [row] = await world.service.runtime_access(ACTOR, endpoint.id)
        assert row.can_invoke is True
        assert row.reason == RuntimeAccessReason.GRANTED

    async def test_a_firewall_without_trusted_services_is_unverified(
        self, world: KeyWorld
    ) -> None:
        world.grant()
        world.vault.properties["networkAcls"] = {"defaultAction": "Deny", "bypass": "None"}
        [row] = (await world.register()).runtime_access
        assert row.can_invoke is False
        assert row.evaluation == RuntimeAccessEvaluation.NOT_EVALUATED
        assert row.reason == RuntimeAccessReason.NETWORK_UNVERIFIED
        assert "trusted Microsoft services" in (row.message or "")

    async def test_a_firewall_that_admits_trusted_services_lets_the_gateway_through(
        self, world: KeyWorld
    ) -> None:
        world.grant()
        world.vault.properties["networkAcls"] = {"defaultAction": "Deny", "bypass": "AzureServices"}
        [row] = (await world.register()).runtime_access
        assert row.can_invoke is True

    async def test_a_deny_assignment_overrides_the_role(self, world: KeyWorld) -> None:
        world.grant()
        world.vault.deny_assignments.append(vault_deny(APIM_PRINCIPAL_ID))
        [row] = (await world.register()).runtime_access
        assert row.can_invoke is False
        assert row.reason == RuntimeAccessReason.DENY_ASSIGNMENT

    async def test_a_gateway_without_an_identity_can_read_nothing(self) -> None:
        world = KeyWorld()
        await world.add_gateway(principal_id=None)
        await world.add_gateway(principal_id=None, observed=False, name="apim-unread")
        rows = {row.gateway_name: row for row in (await world.register()).runtime_access}
        assert rows[SERVICE_NAME].reason == RuntimeAccessReason.NO_GATEWAY_IDENTITY
        assert rows["apim-unread"].reason == RuntimeAccessReason.IDENTITY_NOT_OBSERVED

    async def test_rechecking_gateways_never_reads_the_key(self, world: KeyWorld) -> None:
        endpoint = await world.register()
        world.grant()
        reads = list(world.secret_reads)

        [row] = await world.service.runtime_access(ACTOR, endpoint.id)

        assert row.can_invoke is True
        assert world.secret_reads == reads
        stored = await world.endpoint_repository.get_endpoint(ACTOR.tenant_id, endpoint.id)
        assert stored is not None and stored.runtime_access[0].can_invoke is True


class TestDeclaredDeployments:
    async def test_declares_and_removes_a_deployment(self, world: KeyWorld) -> None:
        endpoint = await world.register(deployments=[])
        declared = await world.service.declare_deployment(ACTOR, endpoint.id, CLAUDE)
        assert [item.deployment_name for item in declared.declared_deployments] == [
            "claude-sonnet-4-5"
        ]
        # A later check keeps what the administrator declared.
        checked = await world.service.preflight(ACTOR, endpoint.id)
        assert len(checked.declared_deployments) == 1

        removed = await world.service.remove_declared_deployment(
            ACTOR, endpoint.id, "claude-sonnet-4-5"
        )
        assert removed.declared_deployments == []

    async def test_a_check_that_raced_a_declaration_keeps_it(self, world: KeyWorld) -> None:
        endpoint = await world.register(deployments=[])
        # A check computed from the record as it was before the administrator declared one.
        stale = await world.service._apply_preflight(endpoint)
        await world.service.declare_deployment(ACTOR, endpoint.id, CLAUDE)

        recorded = await world.endpoint_repository.record_endpoint_state(stale)

        assert recorded is not None
        assert [item.deployment_name for item in recorded.declared_deployments] == [
            "claude-sonnet-4-5"
        ]

    async def test_a_declaration_is_made_once(self, world: KeyWorld) -> None:
        endpoint = await world.register()
        with pytest.raises(ConflictError, match="already declared"):
            await world.service.declare_deployment(
                ACTOR,
                endpoint.id,
                CLAUDE.model_copy(update={"deployment_name": "Claude-Sonnet-4-5"}),
            )

    async def test_an_azure_openai_resource_declares_only_azure_openai_deployments(
        self, world: KeyWorld
    ) -> None:
        endpoint = await world.register(
            endpoint="https://fabrikam-aoai.openai.azure.com", deployments=[GPT]
        )
        with pytest.raises(ValidationError, match="serves only the Azure OpenAI API"):
            await world.service.declare_deployment(ACTOR, endpoint.id, CLAUDE)

    async def test_an_endpoint_read_by_id_declares_nothing(self, world: KeyWorld) -> None:
        endpoint = await world.service.register(
            ACTOR, ModelEndpointCreate.model_validate({"azure_resource_id": AI_RESOURCE_ID})
        )
        with pytest.raises(ConflictError, match="none can be declared"):
            await world.service.declare_deployment(ACTOR, endpoint.id, GPT)

    async def test_removing_an_undeclared_deployment_is_not_found(self, world: KeyWorld) -> None:
        endpoint = await world.register()
        with pytest.raises(NotFoundError):
            await world.service.remove_declared_deployment(ACTOR, endpoint.id, "missing")


class TestNoKeyAnywhere:
    async def test_the_key_never_reaches_a_record_a_response_an_audit_or_a_log(
        self, world: KeyWorld, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        world.grant()
        with capture_logs() as structured:
            endpoint = await world.register()
            await world.service.preflight(ACTOR, endpoint.id)
            await world.service.runtime_access(ACTOR, endpoint.id)
            world.outcome = KeyCheckResult(KeyCheckOutcome.REFUSED, 401)
            await world.service.preflight(ACTOR, endpoint.id)
            world.secret_error = UpstreamAuthorizationError("denied")
            await world.service.preflight(ACTOR, endpoint.id)

        written: list[str] = [
            *(item.model_dump_json() for item in world.endpoint_repository.endpoints.values()),
            *(item.model_dump_json() for item in world.endpoint_repository.credentials.values()),
            *(item.model_dump_json() for item in world.endpoint_repository.audit_events.values()),
            json.dumps(structured, default=str),
            caplog.text,
        ]
        assert all(KEY not in text for text in written)
        # The secret's name identifies it, so only the stored credential reference carries it.
        endpoint_json = world.endpoint_repository.endpoints[endpoint.id].model_dump_json()
        assert SECRET_NAME not in endpoint_json
        assert "vault.azure.net" not in endpoint_json


class TestHttpContract:
    def test_registers_declares_and_removes_over_http(self, settings: Settings) -> None:
        world = KeyWorld()
        app = create_app(settings)
        with TestClient(app) as client:
            app.state.model_endpoint_repository = world.endpoint_repository
            app.state.model_endpoint_service = world.service
            created = client.post(
                "/api/v1/model-endpoints",
                json={
                    "endpoint": PROJECT_URL,
                    "credentialSecretUri": SECRET_URI,
                    "name": "Fabrikam partner Foundry",
                    "deployments": [
                        {
                            "deploymentName": "claude-sonnet-4-5",
                            "modelName": "claude-sonnet-4-5",
                            "apiShape": "anthropicMessages",
                        }
                    ],
                },
            )
            assert created.status_code == 201, created.text
            body = created.json()
            endpoint_id = body["id"]
            assert body["authMode"] == "apiKey"
            assert body["provider"] == "azureAiFoundry"
            assert body["projectName"] == "partner-models"
            assert body["declaredDeployments"][0]["apiShape"] == "anthropicMessages"

            declared = client.post(
                f"/api/v1/model-endpoints/{endpoint_id}/declared-deployments",
                json={"deploymentName": "phi-4", "modelName": "Phi-4", "apiShape": "foundryModels"},
            )
            assert declared.status_code == 201, declared.text
            assert [item["deploymentName"] for item in declared.json()["declaredDeployments"]] == [
                "claude-sonnet-4-5",
                "phi-4",
            ]

            removed = client.delete(
                f"/api/v1/model-endpoints/{endpoint_id}/declared-deployments/phi-4"
            )
            assert removed.status_code == 200, removed.text
            missing = client.delete(
                f"/api/v1/model-endpoints/{endpoint_id}/declared-deployments/phi-4"
            )
            assert missing.status_code == 404
            assert client.post(f"/api/v1/model-endpoints/{endpoint_id}/sync").status_code == 409
            invalid = client.post(
                "/api/v1/model-endpoints",
                json={"endpoint": PROJECT_URL, "credentialSecretUri": "https://kv.example.com/x"},
            )
            assert invalid.status_code == 422
            responses = [created.text, declared.text, removed.text, invalid.text]
            assert all(KEY not in text for text in responses)


class TestRecordCompatibility:
    async def test_records_that_do_not_use_a_key_carry_no_new_field(
        self, world: KeyWorld
    ) -> None:
        by_id = await world.service.register(
            ACTOR, ModelEndpointCreate.model_validate({"azure_resource_id": AI_RESOURCE_ID})
        )
        document = by_id.model_dump(mode="json", by_alias=True)
        # The previous release forbids fields it doesn't know, so an unused one stays out.
        assert "declaredDeployments" not in document
        assert ModelEndpoint.model_validate(document).declared_deployments == []

        keyed_endpoint = await world.register()
        assert "declaredDeployments" in keyed_endpoint.model_dump(mode="json", by_alias=True)


class TestKeyProbe:
    async def test_checks_the_key_with_an_unbilled_read_on_the_resource_host(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"data": []})

        probe = EndpointKeyProbe(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        result = await probe.check(ORIGIN, KEY)

        assert result.outcome == KeyCheckOutcome.ACCEPTED
        [request] = seen
        assert request.method == "GET"
        assert request.url.host == "fabrikam-foundry.services.ai.azure.com"
        assert request.url.path == "/openai/models"
        assert request.url.params["api-version"] == MODELS_LIST_API_VERSION
        assert request.headers["api-key"] == KEY
        assert "authorization" not in request.headers

    @pytest.mark.parametrize(
        ("status", "outcome"),
        [
            (401, KeyCheckOutcome.REFUSED),
            (403, KeyCheckOutcome.REFUSED),
            (404, KeyCheckOutcome.INCONCLUSIVE),
            (429, KeyCheckOutcome.INCONCLUSIVE),
            (500, KeyCheckOutcome.INCONCLUSIVE),
        ],
    )
    async def test_reads_only_the_status(self, status: int, outcome: KeyCheckOutcome) -> None:
        probe = EndpointKeyProbe(
            httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(status, json={"error": {"message": KEY}})
                )
            )
        )
        result = await probe.check(ORIGIN, KEY)
        assert result == KeyCheckResult(outcome, status)

    async def test_an_unreachable_host_is_reported_without_the_key(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route", request=request)

        probe = EndpointKeyProbe(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        with capture_logs() as structured:
            result = await probe.check(ORIGIN, KEY)
        assert result.outcome == KeyCheckOutcome.UNREACHABLE
        assert KEY not in json.dumps(structured, default=str)

    @pytest.mark.parametrize(
        "origin",
        ["https://models.example.com", "http://fabrikam-foundry.services.ai.azure.com"],
    )
    async def test_a_key_is_sent_only_to_an_azure_ai_resource(self, origin: str) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200)

        probe = EndpointKeyProbe(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        result = await probe.check(origin, KEY)
        assert result.outcome == KeyCheckOutcome.INCONCLUSIVE
        assert seen == []
