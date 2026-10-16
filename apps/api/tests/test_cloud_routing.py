"""Offline contracts for sovereign routing, identity isolation and transport confinement."""

import asyncio
import time
from dataclasses import FrozenInstanceError
from typing import Any, cast

import httpx
import jwt
import pytest
from aoai_double import AI_RESOURCE_ID, FakeCognitiveServices
from apim_double import RESOURCE_ID, FakeApim, FakeCredential
from azure.core.credentials import AccessToken
from azure.core.credentials_async import AsyncTokenCredential
from mosaic_api.clouds import (
    AzureCloud,
    AzureResourceContext,
    azure_ai_cloud,
    cloud_profile,
    validate_resource_connection,
)
from mosaic_api.domain import (
    ApimResourceId,
    AzureAiEndpointUrl,
    CognitiveServicesResourceId,
    Gateway,
    GatewayCreate,
    GatewayStatus,
    McpAuthMode,
    McpEndpoint,
    McpEndpointCreate,
    ModelEndpoint,
    ModelEndpointCreate,
    ModelProvider,
    new_id,
)
from mosaic_api.errors import ConflictError, UpstreamError, ValidationError
from mosaic_api.integrations.aoai import CognitiveServicesClient, SubscriptionScanner
from mosaic_api.integrations.apim import ApimClient, ApimWriter, ArmClient
from mosaic_api.integrations.apim.ai_detection import classify_url
from mosaic_api.integrations.apim.model_apis import anthropic_origin, is_azure_openai_host
from mosaic_api.integrations.policy import managed_identity_resource
from mosaic_api.observed import AiBackendKind
from mosaic_api.repositories import (
    InMemoryEntitlementRepository,
    InMemoryGatewayRepository,
    InMemoryMcpEndpointRepository,
    InMemoryModelEndpointRepository,
)
from mosaic_api.services.directory import Actor
from mosaic_api.services.gateways import GatewayService
from mosaic_api.services.mcp_endpoints import McpEndpointService
from mosaic_api.services.model_endpoints import ModelEndpointService

ACTOR = Actor(object_id="fictional-admin", tenant_id="fictional-mosaic-tenant")


class ScopedCredential(FakeCredential):
    def __init__(self, principal: str) -> None:
        super().__init__()
        self.principal = principal
        self.scopes: list[tuple[str, ...]] = []

    async def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        self.scopes.append(scopes)
        return AccessToken(
            jwt.encode({"oid": self.principal}, "fictional-test-signing-key-1234567"),
            int(time.time()) + 3600,
        )


async def valid_connection(tenant: str, connection: str | None, cloud: AzureCloud) -> None:
    assert tenant == ACTOR.tenant_id
    assert cloud in {"commercial", "government"}
    assert connection is None or connection.startswith("connection-")


def test_closed_profiles_and_cloud_qualified_resource_identity() -> None:
    government = cloud_profile("government")
    assert government.authority == "https://login.microsoftonline.us"
    assert government.arm_scope == "https://management.usgovcloudapi.net/.default"
    assert government.log_analytics_endpoint == "https://api.loganalytics.us"
    assert government.cognitive_audience == "https://cognitiveservices.azure.us"
    with pytest.raises(FrozenInstanceError):
        government.arm_endpoint = "https://untrusted.invalid"  # type: ignore[misc]
    with pytest.raises(ValueError, match="Unsupported"):
        cloud_profile(cast(AzureCloud, "arbitrary"))
    for parser, value in (
        (ApimResourceId.parse, RESOURCE_ID),
        (CognitiveServicesResourceId.parse, AI_RESOURCE_ID),
    ):
        commercial = parser(value)
        sovereign = parser(
            value, azure_cloud="government", azure_connection_id="connection-government"
        )
        assert commercial.dedupe_key == commercial.canonical.casefold()
        assert sovereign.dedupe_key == f"government:{commercial.dedupe_key}"
        assert sovereign.azure_connection_id == "connection-government"


@pytest.mark.parametrize("suffix", ["openai.azure.us", "cognitiveservices.azure.us"])
def test_verified_government_hosts_and_audience(suffix: str) -> None:
    url = f"https://fictional.{suffix}"
    assert AzureAiEndpointUrl.parse(url).origin == url
    assert azure_ai_cloud(url) == "government"
    assert managed_identity_resource(None, backend_url=url) == "https://cognitiveservices.azure.us"
    assert classify_url(url) != AiBackendKind.NONE
    assert is_azure_openai_host("https://fictional.openai.azure.us")
    assert azure_ai_cloud(f"{url}.attacker.invalid") is None
    with pytest.raises(ValidationError, match="Government"):
        anthropic_origin(url)
    with pytest.raises(ValueError, match="not verified"):
        managed_identity_resource("anthropicMessages", azure_cloud="government")


async def test_government_never_uses_implicit_host_connection() -> None:
    for connection in (None, "connection-government"):
        with pytest.raises(ValidationError):
            await validate_resource_connection(None, ACTOR.tenant_id, connection, "government")


async def test_government_registration_without_a_resolver_has_no_side_effects() -> None:
    gateways = InMemoryGatewayRepository()
    models = InMemoryModelEndpointRepository()
    tools = InMemoryMcpEndpointRepository()
    gateway_service = GatewayService(
        gateways, client_factory=lambda resource: pytest.fail("No ARM connection is available")
    )
    model_service = ModelEndpointService(
        models, gateway_repository=gateways,
        client_factory=lambda resource: pytest.fail("No ARM connection is available"),
    )
    mcp_service = McpEndpointService(
        tools, gateway_repository=gateways,
        entitlement_repository=InMemoryEntitlementRepository(),
        client_factory=lambda url, authorization: pytest.fail("No MCP connection is available"),
    )
    with pytest.raises(ValidationError, match="cannot resolve"):
        await gateway_service.register(ACTOR, GatewayCreate(
            azure_resource_id=RESOURCE_ID, azure_cloud="government",
            azure_connection_id="connection-government",
        ))
    with pytest.raises(ValidationError, match="cannot resolve"):
        await model_service.register(ACTOR, ModelEndpointCreate(
            azure_resource_id=AI_RESOURCE_ID, azure_cloud="government",
            azure_connection_id="connection-government",
        ))
    with pytest.raises(ValidationError, match="cannot resolve"):
        await mcp_service.register(ACTOR, McpEndpointCreate(
            endpoint="https://fictional.azure-api.us/mcp", azure_cloud="government",
            azure_connection_id="connection-government",
        ))
    assert not gateways.gateways
    assert not models.endpoints
    assert not tools.endpoints


async def test_simultaneous_cloud_transports_use_separate_scopes_and_principals() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": request.url.host})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        commercial_credential = ScopedCredential("commercial-principal")
        government_credential = ScopedCredential("government-principal")
        commercial = ArmClient(cast(AsyncTokenCredential, commercial_credential), client=http)
        profile = cloud_profile("government")
        government = ArmClient(
            cast(AsyncTokenCredential, government_credential),
            client=http,
            base_url=profile.arm_endpoint,
            scope=profile.arm_scope,
        )
        await asyncio.gather(commercial.get("/subscriptions"), government.get("/subscriptions"))
        assert await government.caller_object_id() == "government-principal"
        assert await commercial.caller_object_id() == "commercial-principal"
        assert {request.url.host for request in requests} == {
            "management.azure.com",
            "management.usgovcloudapi.net",
        }
        assert all(scopes == (profile.arm_scope,) for scopes in government_credential.scopes)
        assert all(
            scopes == ("https://management.azure.com/.default",)
            for scopes in commercial_credential.scopes
        )
        resource = ApimResourceId.parse(RESOURCE_ID, azure_cloud="government")
        with pytest.raises(ValueError, match="does not match"):
            ApimClient(commercial, resource)
        with pytest.raises(ValueError, match="does not match"):
            ApimWriter(commercial, resource)
        with pytest.raises(ValueError, match="does not match"):
            CognitiveServicesClient(
                commercial,
                CognitiveServicesResourceId.parse(AI_RESOURCE_ID, azure_cloud="government"),
            )


@pytest.mark.parametrize("operation", ["page", "poll"])
@pytest.mark.parametrize(
    "hostile",
    [
        "https://management.azure.com/next",
        "https://untrusted.invalid/next",
        "//untrusted.invalid/next",
        "http://management.usgovcloudapi.net/next",
        "https://management.usgovcloudapi.net:444/next",
        "https://user:password@management.usgovcloudapi.net/next",
    ],
)
async def test_foreign_page_and_poll_links_never_receive_credentials(
    operation: str, hostile: str
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if operation == "page":
            return httpx.Response(200, json={"value": [], "nextLink": hostile})
        return httpx.Response(202, headers={"Location": hostile})

    credential = ScopedCredential("government-principal")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        profile = cloud_profile("government")
        arm = ArmClient(
            cast(AsyncTokenCredential, credential), client=http, base_url=profile.arm_endpoint
        )
        with pytest.raises(UpstreamError, match="untrusted"):
            if operation == "page":
                await arm.list("/resources", params={})
            else:
                await arm.put("/resources", {}, params={})
    assert len(requests) == 1
    assert len(credential.scopes) == 1


async def test_redirects_are_disabled_even_with_a_redirecting_shared_client() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(307, headers={"Location": "https://untrusted.invalid/next"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as http:
        arm = ArmClient(cast(AsyncTokenCredential, ScopedCredential("principal")), client=http)
        with pytest.raises(UpstreamError, match="redirects"):
            await arm.get("/resources")
    assert len(requests) == 1


async def test_government_accepts_trusted_paging_and_operation_urls() -> None:
    profile = cloud_profile("government")
    requests: list[httpx.Request] = []

    async def no_sleep(seconds: float) -> None:
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "PUT":
            return httpx.Response(
                202, headers={"Azure-AsyncOperation": f"{profile.arm_endpoint}/operations/one"}
            )
        if request.url.path == "/operations/one":
            return httpx.Response(200, json={"status": "Succeeded"})
        if request.url.path == "/resources/page2":
            return httpx.Response(200, json={"value": [{"id": "second"}]})
        return httpx.Response(200, json={
            "value": [{"id": "first"}], "nextLink": "/resources/page2",
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        arm = ArmClient(
            cast(AsyncTokenCredential, ScopedCredential("government-principal")),
            client=http, base_url=profile.arm_endpoint, sleep=no_sleep,
        )
        assert await arm.list("/resources") == [{"id": "first"}, {"id": "second"}]
        await arm.put("/resource", {}, params={})
        for bad_base, scope in (
            ("https://untrusted.invalid", profile.arm_scope),
            (profile.arm_endpoint, "https://management.azure.com/.default"),
        ):
            with pytest.raises(ValueError, match="supported Azure cloud"):
                ArmClient(
                    cast(AsyncTokenCredential, ScopedCredential("principal")), client=http,
                    base_url=bad_base, scope=scope,
                )
    assert len(requests) == 4
    assert all(request.url.host == "management.usgovcloudapi.net" for request in requests)


async def test_cloud_qualified_gateway_deduplication_and_observation_fields() -> None:
    repository = InMemoryGatewayRepository()
    fake = FakeApim()
    credential = ScopedCredential("selected-government-principal")

    def handler(request: httpx.Request) -> httpx.Response:
        response = fake.handler(request)
        if request.url.host == "management.usgovcloudapi.net" and request.url.path == RESOURCE_ID:
            payload = response.json()
            payload["properties"]["gatewayUrl"] = "https://fictional.azure-api.us"
            return httpx.Response(response.status_code, json=payload)
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        arms = {
            cloud: ArmClient(
                cast(AsyncTokenCredential, credential),
                client=http,
                base_url=cloud_profile(cloud).arm_endpoint,
            )
            for cloud in ("commercial", "government")
        }
        service = GatewayService(
            repository,
            client_factory=lambda resource: ApimClient(arms[resource.azure_cloud], resource),
            principal_id="host-cached-principal",
            connection_validator=valid_connection,
        )
        commercial = await service.register(ACTOR, GatewayCreate(azure_resource_id=RESOURCE_ID))
        government = await service.register(
            ACTOR,
            GatewayCreate(
                azure_resource_id=RESOURCE_ID,
                azure_cloud="government",
                azure_connection_id="connection-one",
            ),
        )
        assert commercial.id != government.id
        assert government.arm_resource().azure_connection_id == "connection-one"
        assert government.access.remediation is not None
        assert government.access.remediation.principal_id == "selected-government-principal"
        assert "AzureUSGovernment" in government.access.remediation.command
        assert "host-cached-principal" not in government.access.remediation.command
        assert (
            await repository.find_gateway_by_resource_id(ACTOR.tenant_id, RESOURCE_ID) == commercial
        )
        assert (
            await repository.find_gateway_by_resource_id(
                ACTOR.tenant_id, RESOURCE_ID, azure_cloud="government"
            )
            == government
        )
        with pytest.raises(ConflictError):
            await service.register(
                ACTOR,
                GatewayCreate(
                    azure_resource_id=RESOURCE_ID,
                    azure_cloud="government",
                    azure_connection_id="connection-two",
                ),
            )
        observed = government.model_copy(
            update={
                "azure_cloud": "commercial",
                "azure_connection_id": None,
                "status": GatewayStatus.DEGRADED,
            }
        )
        recorded = await repository.record_gateway_state(observed)
        assert recorded is not None
        assert recorded.azure_cloud == "government"
        assert recorded.azure_connection_id == "connection-one"
        assert recorded.status == GatewayStatus.DEGRADED


async def test_wrong_cloud_gateway_hostname_is_not_registered() -> None:
    fake = FakeApim()
    repository = InMemoryGatewayRepository()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as http:
        arm = ArmClient(
            cast(AsyncTokenCredential, ScopedCredential("government-principal")),
            client=http, base_url=cloud_profile("government").arm_endpoint,
        )
        service = GatewayService(
            repository, client_factory=lambda resource: ApimClient(arm, resource),
            connection_validator=valid_connection,
        )
        with pytest.raises(ValidationError, match="gateway hostname"):
            await service.register(ACTOR, GatewayCreate(
                azure_resource_id=RESOURCE_ID, azure_cloud="government",
                azure_connection_id="connection-one",
            ))
    assert not repository.gateways


async def test_model_registration_and_selected_scans_keep_cloud_context() -> None:
    fake = FakeCognitiveServices()
    repository = InMemoryModelEndpointRepository()
    contexts: list[AzureResourceContext] = []

    def handler(request: httpx.Request) -> httpx.Response:
        response = fake.handler(request)
        if response.status_code == 200 and request.url.host == "management.usgovcloudapi.net":
            payload = response.json()
            if request.url.path == AI_RESOURCE_ID:
                payload["properties"]["endpoint"] = "https://fictional.openai.azure.us"
            elif request.url.path.endswith("/providers/Microsoft.CognitiveServices/accounts"):
                for account in payload["value"]:
                    account["properties"]["endpoint"] = "https://fictional.openai.azure.us"
            return httpx.Response(200, json=payload)
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        arms = {
            cloud: ArmClient(
                cast(AsyncTokenCredential, ScopedCredential(f"{cloud}-principal")),
                client=http,
                base_url=cloud_profile(cloud).arm_endpoint,
            )
            for cloud in ("commercial", "government")
        }

        def scanner(context: AzureResourceContext) -> SubscriptionScanner:
            contexts.append(context)
            return SubscriptionScanner(arms[context.azure_cloud])

        service = ModelEndpointService(
            repository,
            gateway_repository=InMemoryGatewayRepository(),
            client_factory=lambda resource: CognitiveServicesClient(
                arms[resource.azure_cloud], resource
            ),
            connection_validator=valid_connection,
            scanner_factory=scanner,
        )
        commercial = await service.register(
            ACTOR, ModelEndpointCreate(azure_resource_id=AI_RESOURCE_ID)
        )
        suggestions = await service.suggestions(
            ACTOR, azure_cloud="government", azure_connection_id="connection-one"
        )
        assert contexts == [AzureResourceContext("government", "connection-one")]
        assert suggestions.suggestions
        suggestion = suggestions.suggestions[0]
        assert suggestion.azure_cloud == "government"
        assert suggestion.azure_connection_id == "connection-one"
        assert suggestion.already_registered is False
        fake.permissions = []
        government = await service.register(
            ACTOR,
            ModelEndpointCreate(
                azure_resource_id=AI_RESOURCE_ID,
                azure_cloud="government",
                azure_connection_id="connection-one",
            ),
        )
        assert government.id != commercial.id
        assert government.arm_resource().azure_cloud == "government"
        assert government.access.remediation is not None
        assert government.access.remediation.principal_id == "government-principal"
        with pytest.raises(ConflictError):
            await service.register(
                ACTOR,
                ModelEndpointCreate(
                    azure_resource_id=AI_RESOURCE_ID,
                    azure_cloud="government",
                    azure_connection_id="connection-two",
                ),
            )


async def test_wrong_cloud_endpoint_is_rejected_before_azure_operations() -> None:
    service = ModelEndpointService(
        InMemoryModelEndpointRepository(),
        gateway_repository=InMemoryGatewayRepository(),
        client_factory=lambda resource: pytest.fail("Must not create an Azure client"),
    )
    with pytest.raises(ValidationError, match="different Azure cloud"):
        await service.register(
            ACTOR,
            ModelEndpointCreate(
                azure_resource_id=AI_RESOURCE_ID,
                endpoint="https://fictional.openai.azure.us",
            ),
        )


async def test_mcp_discovery_uses_selected_connection_not_host_token() -> None:
    resolved: list[tuple[str | None, str]] = []

    async def sovereign_token(endpoint: McpEndpoint, audience: str) -> str:
        resolved.append((endpoint.azure_connection_id, audience))
        return "fictional-government-token"

    async def host_token(audience: str) -> str:
        pytest.fail("Government discovery must never use the host token resolver")

    service = McpEndpointService(
        InMemoryMcpEndpointRepository(),
        gateway_repository=InMemoryGatewayRepository(),
        entitlement_repository=InMemoryEntitlementRepository(),
        client_factory=lambda url, authorization: pytest.fail("No discovery connection expected"),
        token_resolver=host_token,
        connection_token_resolver=sovereign_token,
        connection_validator=valid_connection,
    )
    endpoint = McpEndpoint(
        id=new_id("mcpEndpoint"),
        tenant_id=ACTOR.tenant_id,
        name="Fictional Government tool",
        endpoint="https://fictional.azure-api.us/mcp",
        azure_cloud="government",
        azure_connection_id="connection-one",
        auth_mode=McpAuthMode.MANAGED_IDENTITY,
        resource_audience="https://fictional-government-resource.invalid",
    )
    assert await service._authorization_for(endpoint) == "Bearer fictional-government-token"
    assert resolved == [("connection-one", "https://fictional-government-resource.invalid")]


def test_endpoint_helpers_preserve_legacy_defaults() -> None:
    gateway = Gateway(
        id="legacy-gateway",
        tenant_id=ACTOR.tenant_id,
        name="Legacy",
        azure_resource_id=RESOURCE_ID,
        subscription_id="subscription",
        resource_group="group",
        service_name="service",
    )
    endpoint = ModelEndpoint(
        id="legacy-endpoint",
        tenant_id=ACTOR.tenant_id,
        name="Legacy model",
        provider=ModelProvider.AZURE_OPENAI,
        endpoint="https://fictional.openai.azure.com",
        azure_resource_id=AI_RESOURCE_ID,
    )
    assert gateway.arm_resource().azure_cloud == "commercial"
    assert endpoint.arm_resource().azure_connection_id is None
