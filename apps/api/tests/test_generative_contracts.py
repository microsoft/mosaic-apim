"""Real registration, repository, routing and policy seams, with no external requests."""

import base64
import html
import json
import xml.etree.ElementTree as ET
from io import BytesIO
from typing import Any
from urllib.parse import parse_qs, quote

import pytest
from aoai_double import FakeCognitiveServices
from apim_double import RESOURCE_ID
from fastapi.testclient import TestClient
from mosaic_api.domain import (
    ApiShape,
    AuditEvent,
    CapabilitySupport,
    DeclaredDeploymentCreate,
    Gateway,
    GatewayCapabilities,
    GatewayTier,
    ModelAccessSettings,
    ModelAccessSnapshot,
    ModelEndpoint,
    ModelEndpointCreate,
    ModelEndpointUpdate,
    ModelProvider,
    Publication,
    PublicationCreate,
    PublishPlan,
)
from mosaic_api.errors import ConflictError, ValidationError
from mosaic_api.integrations.access_policy import (
    _operation_guard,
    governed_operations,
    render_governed_policy,
)
from mosaic_api.integrations.apim.model_apis import (
    assess_declared_deployment,
    required_data_actions,
    shape_operations,
)
from mosaic_api.integrations.apim.policy_capabilities import policy_support_on_tier
from mosaic_api.integrations.generative_protocols import (
    MAX_IMAGES,
    OAUTH_URL,
    STS_URL,
    VERTEX_MODEL,
    VERTEX_ORIGIN,
    admit_custom_chat_url,
    custom_operation_url,
    impersonation_request,
    jwt_bearer_exchange,
    service_account_claims,
    sts_exchange,
    usage_counts,
    validate_request,
    vertex_auth_headers,
    vertex_operation_url,
)
from mosaic_api.integrations.policy import render_publication_policy
from mosaic_api.repositories import InMemoryGatewayRepository, InMemoryModelEndpointRepository
from mosaic_api.services.directory import Actor
from mosaic_api.services.model_endpoints import ModelEndpointService
from mosaic_api.services.policy_capabilities import deployment_policy_capabilities
from mosaic_api.services.publishing import PublishingService
from PIL import Image

ACTOR = Actor(object_id="local-admin", tenant_id="tenant-test")
SECRET = "https://kv-fictional.vault.azure.net/secrets/custom-key"
ACCOUNT = "runtime@fictional-project.iam.gserviceaccount.com"
AUDIENCE = (
    "//iam.googleapis.com/projects/123456789/locations/global/"
    "workloadIdentityPools/fictional/providers/azure"
)


def custom_payload(**overrides: Any) -> dict[str, Any]:
    return {
        "provider": "openAiCompatible",
        "compatibleChat": True,
        "endpoint": "https://models.example.com/v1",
        "environment": "development",
        "credentialSecretUri": SECRET,
        "deployments": [
            {
                "deploymentName": "fictional/Model",
                "modelName": "fictional/Model",
                "apiShape": "openAiChat",
            }
        ],
        **overrides,
    }


def vertex_payload(mode: str = "workloadIdentityFederation", **overrides: Any) -> dict[str, Any]:
    return {
        "provider": "googleVertex",
        "endpoint": VERTEX_ORIGIN,
        "environment": "development",
        "vertex": {
            "projectId": "fictional-project",
            "location": "us",
            "apiVersion": "v1",
            "authMode": mode,
            "serviceAccount": ACCOUNT,
            **(
                {"workloadIdentityAudience": AUDIENCE}
                if mode == "workloadIdentityFederation"
                else {}
            ),
        },
        **({"credentialSecretUri": SECRET} if mode != "workloadIdentityFederation" else {}),
        "deployments": [
            {"deploymentName": VERTEX_MODEL, "modelName": VERTEX_MODEL, "apiShape": "vertexGemini"}
        ],
        **overrides,
    }


def _forbidden(*args: Any, **kwargs: Any) -> Any:
    pytest.fail("Offline external registration/publication must not construct any cloud client")


def _publication(endpoint: ModelEndpoint) -> Publication:
    declaration = endpoint.declared_deployments[0]
    return Publication(
        id="blocked-publication",
        tenant_id=ACTOR.tenant_id,
        gateway_id="gateway",
        model_endpoint_id=endpoint.id,
        deployment_name=declaration.deployment_name,
        provider=endpoint.provider,
        api_shape=declaration.api_shape,
        shape_version="1.0",
        display_name="Fictional",
        api_name="mosaic-fictional",
        api_path="mosaic/fictional",
        backend_name="mosaic-fictional",
        fragment_name="mosaic-fictional",
        product_name="mosaic-fictional",
        subscription_name="mosaic-fictional",
    )


@pytest.mark.parametrize(
    "mode",
    [
        "workloadIdentityFederation",
        "serviceAccountKey",
        "apiKey",
    ],
)
async def test_native_registration_and_checks_are_offline_and_blocked(mode: str) -> None:
    endpoints = InMemoryModelEndpointRepository()
    gateways = InMemoryGatewayRepository()
    service = ModelEndpointService(
        endpoints,
        gateway_repository=gateways,
        client_factory=_forbidden,
        secret_resolver=_forbidden,
        identity_resolver=_forbidden,
        key_probe=_forbidden,
        connection_validator=_forbidden,
    )
    saved = await service.register(ACTOR, ModelEndpointCreate.model_validate(vertex_payload(mode)))
    assert saved.vertex and saved.vertex.auth_mode == mode
    assert str(saved.endpoint).rstrip("/") == VERTEX_ORIGIN
    assert saved.declared(VERTEX_MODEL)
    assert saved.access.can_read is False
    checked = await service.preflight(ACTOR, saved.id)
    assert checked.status == "pending" and checked.access.checked_at is None
    assert "unproved" in (checked.access.message or "")
    assert "schema recognition" in (checked.access.message or "")
    assert checked.runtime_access == []
    assert not gateways.publications
    with pytest.raises(ConflictError, match="Discovery is not supported"):
        await service.start_sync(ACTOR, saved.id)
    if mode != "workloadIdentityFederation":
        rotated = await service.update(
            ACTOR,
            saved.id,
            ModelEndpointUpdate(credential_secret_uri=SECRET + "-rotated"),
        )
        assert rotated.id == saved.id
        assert rotated.credential_reference_id == saved.credential_reference_id
        assert rotated.status == "pending"


async def test_custom_environment_credentials_and_rotation_are_isolated() -> None:
    endpoints = InMemoryModelEndpointRepository()
    gateways = InMemoryGatewayRepository()
    service = ModelEndpointService(
        endpoints,
        gateway_repository=gateways,
        client_factory=_forbidden,
        secret_resolver=_forbidden,
        key_probe=_forbidden,
        connection_validator=_forbidden,
    )
    development = await service.register(
        ACTOR, ModelEndpointCreate.model_validate(custom_payload())
    )
    production = await service.register(
        ACTOR,
        ModelEndpointCreate.model_validate(
            custom_payload(
                environment="production",
                credentialSecretUri=SECRET + "-production",
            )
        ),
    )
    assert development.id != production.id
    assert development.credential_reference_id != production.credential_reference_id
    assert production.credential_reference_id is not None
    before = await endpoints.get_credential(ACTOR.tenant_id, production.credential_reference_id)
    rotated = await service.update(
        ACTOR,
        development.id,
        ModelEndpointUpdate(
            credential_secret_uri=SECRET + "-rotated",
        ),
    )
    assert rotated.id == development.id
    assert rotated.credential_reference_id == development.credential_reference_id
    assert before == await endpoints.get_credential(
        ACTOR.tenant_id, production.credential_reference_id
    )
    with pytest.raises(ConflictError, match="exact environment"):
        await service.register(ACTOR, ModelEndpointCreate.model_validate(custom_payload()))
    with pytest.raises(ConflictError, match="exact environment"):
        await service.register(
            ACTOR,
            ModelEndpointCreate.model_validate(
                custom_payload(
                    endpoint="https://models.example.com",
                )
            ),
        )
    assert str(development.endpoint) == "https://models.example.com/v1"
    assert development.declared("fictional/model") is None
    with pytest.raises(ConflictError, match="case-only"):
        await service.declare_deployment(
            ACTOR,
            development.id,
            DeclaredDeploymentCreate(
                deployment_name="fictional/model",
                model_name="fictional/model",
                api_shape=ApiShape.OPENAI_CHAT,
            ),
        )


@pytest.mark.parametrize(
    "payload",
    [
        vertex_payload(endpoint="https://aiplatform.googleapis.com"),
        vertex_payload(credentialSecretUri=SECRET),
        vertex_payload("apiKey", credentialSecretUri=None),
        vertex_payload(
            vertex={"projectId": "fictional-project", "location": "global", "authMode": "apiKey"}
        ),
        custom_payload(compatibleChat=False),
        custom_payload(environment=None),
        custom_payload(
            deployments=[
                {
                    "deploymentName": "fictional/Model",
                    "modelName": "fictional/Model",
                    "apiShape": "openAiChat",
                },
                {
                    "deploymentName": "fictional/model",
                    "modelName": "fictional/model",
                    "apiShape": "openAiChat",
                },
            ]
        ),
    ],
)
def test_registration_refuses_unsupported_contracts(payload: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        ModelEndpointCreate.model_validate(payload)


@pytest.mark.parametrize(
    "url",
    [
        "http://models.example.com",
        "https://127.0.0.1",
        "https://169.254.169.254",
        "https://user:secret@models.example.com",
        "https://models.example.com?key=secret",
        "https://models.example.com#fragment",
        "https://models.example.com:8443",
        "https://metadata.google.internal",
        "https://models.example.com/other",
    ],
)
def test_custom_host_syntax_is_not_arbitrary_forwarding(url: str) -> None:
    with pytest.raises(ValueError):
        admit_custom_chat_url(url)


def test_slash_model_http_declaration_delete_is_exact_and_safe(
    endpoint_client: TestClient,
    fake_aoai: FakeCognitiveServices,
) -> None:
    response = endpoint_client.post("/api/v1/model-endpoints", json=custom_payload())
    assert response.status_code == 201, response.text
    endpoint = response.json()
    assert endpoint["compatibleChat"] is True
    url = f"/api/v1/model-endpoints/{endpoint['id']}/declared-deployments/"
    wrong = endpoint_client.delete(url + quote("fictional/model", safe=""))
    assert wrong.status_code == 404
    removed = endpoint_client.delete(url + quote("fictional/Model", safe=""))
    assert removed.status_code == 200, removed.text
    assert removed.json().get("declaredDeployments", []) == []
    assert fake_aoai.requests == []


def test_legacy_generic_opt_in_preserves_identity_without_cloud(
    endpoint_client: TestClient,
    fake_aoai: FakeCognitiveServices,
    endpoint_repository: InMemoryModelEndpointRepository,
) -> None:
    payload = custom_payload(compatibleChat=False, deployments=None)
    response = endpoint_client.post("/api/v1/model-endpoints", json=payload)
    assert response.status_code == 201, response.text
    old = response.json()
    assert "compatibleChat" not in old and "vertex" not in old
    assert not endpoint_repository.endpoints[old["id"]].uses_backend_key()
    changed = endpoint_client.patch(
        f"/api/v1/model-endpoints/{old['id']}", json={"compatibleChat": True}
    )
    assert changed.status_code == 200, changed.text
    new = changed.json()
    assert new["id"] == old["id"]
    assert new["credentialReferenceId"] == old["credentialReferenceId"]
    assert new["compatibleChat"] is True and new["status"] == "pending"
    assert endpoint_repository.endpoints[old["id"]].uses_backend_key()
    assert "egress enforcement" in new["access"]["message"]
    assert fake_aoai.requests == []


@pytest.mark.parametrize("payload", [custom_payload(), vertex_payload()])
async def test_draft_save_and_review_are_offline_but_apply_is_blocked(
    payload: dict[str, Any],
) -> None:
    endpoints = InMemoryModelEndpointRepository()
    gateways = InMemoryGatewayRepository()
    service = ModelEndpointService(
        endpoints,
        gateway_repository=gateways,
        client_factory=_forbidden,
    )
    endpoint = await service.register(ACTOR, ModelEndpointCreate.model_validate(payload))
    audit = AuditEvent(
        id="local-audit",
        tenant_id=ACTOR.tenant_id,
        action="test",
        resource_type="gateway",
        resource_id="gateway",
        actor_object_id=ACTOR.object_id,
    )
    gateway = Gateway(
        id="gateway",
        tenant_id=ACTOR.tenant_id,
        name="Fictional",
        azure_resource_id=RESOURCE_ID,
        subscription_id="fictional",
        resource_group="fictional",
        service_name="fictional",
        environment="development",
        capabilities=GatewayCapabilities(sku_name="Developer"),
    )
    await gateways.save_gateway(gateway, audit)
    publishing = PublishingService(
        gateways,
        endpoint_repository=endpoints,
        client_factory=_forbidden,
        writer_factory=_forbidden,
    )
    publication = await publishing.create(
        ACTOR,
        PublicationCreate(
            gateway_id=gateway.id,
            model_endpoint_id=endpoint.id,
            deployment_name=endpoint.declared_deployments[0].deployment_name,
        ),
    )
    plan = await publishing.plan(ACTOR, publication.id)
    assert plan.executable is False
    assert plan.execution_blocked_reasons and plan.steps == []
    assert plan.review is not None and plan.review.target.fields
    assert any("POST https://" in field.value for field in plan.review.target.fields)
    assert any("No APIM resources" in impact for impact in plan.review.impact)
    assert plan.model_dump(by_alias=True)["executable"] is False
    assert plan.model_dump(by_alias=True)["executionBlockedReasons"]
    assert PublishPlan.model_validate_json(plan.model_dump_json()).executable is False
    assert plan.digest == (await publishing.plan(ACTOR, publication.id)).digest
    with pytest.raises(ValidationError, match="no forwarding policy"):
        render_publication_policy(publication)
    with pytest.raises(ValidationError, match="no forwarding policy"):
        render_governed_policy(
            publication, ModelAccessSnapshot(version=1, settings=ModelAccessSettings())
        )
    with pytest.raises(ValidationError, match=r"blocked|unproved"):
        await publishing.apply(ACTOR, publication.id)
    await gateways.save_publish_plan(plan.model_copy(update={
        "executable": True, "execution_blocked_reasons": [],
    }))
    with pytest.raises(ValidationError, match=r"blocked|unproved"):
        await publishing.apply(ACTOR, publication.id, plan.id)
    recorded = await publishing.get_publication(ACTOR, publication.id)
    assert recorded.created_resources() == [] and recorded.applied_access is None
    candidates = await publishing.publishable_models(ACTOR, gateway.id)
    assert len(candidates) == 1
    assert candidates[0].publishable is False
    assert candidates[0].reviewable is True
    if endpoint.uses_backend_key():
        await service.update(
            ACTOR,
            endpoint.id,
            ModelEndpointUpdate(
                credential_secret_uri=SECRET + "-new-reference",
            ),
        )
        assert plan.digest != (await publishing.plan(ACTOR, publication.id)).digest
    await gateways.save_gateway(gateway.model_copy(update={"environment": "test"}), audit)
    with pytest.raises(ConflictError):
        await publishing.plan(ACTOR, publication.id)
    assert not (await publishing.publishable_models(ACTOR, gateway.id))[0].reviewable
    capability = deployment_policy_capabilities(gateway, endpoint, endpoint.declared_deployments[0])
    assert capability.policies["llm-token-limit"].support != CapabilitySupport.AVAILABLE


def _png() -> str:
    image = BytesIO()
    Image.new("RGB", (1, 1)).save(image, "PNG")
    return base64.b64encode(image.getvalue()).decode()


def test_native_payload_preserves_opaque_thinking_functions_images_and_defaults() -> None:
    body = {
        "contents": [
            {
                "role": "model",
                "parts": [
                    {
                        "functionCall": {"name": "lookup", "args": {"id": 1}},
                        "thoughtSignature": "opaque+signature==",
                    },
                    {"inlineData": {"mimeType": "image/png", "data": _png()}},
                ],
            },
            {
                "role": "user",
                "parts": [
                    {"functionResponse": {"name": "lookup", "response": {"result": "fictional"}}}
                ],
            },
        ],
        "tools": [{"functionDeclarations": [{"name": "lookup"}]}],
    }
    original = json.dumps(body)
    assert validate_request("vertexGemini", VERTEX_MODEL, body) is body
    assert json.dumps(body) == original and "generationConfig" not in body
    for part in [
        {"fileData": {"fileUri": "gs://bucket/image.png"}},
        {"inlineData": {"mimeType": "audio/wav", "data": _png()}},
    ]:
        with pytest.raises(ValueError):
            validate_request("vertexGemini", VERTEX_MODEL, {"contents": [{"parts": [part]}]})
    with pytest.raises(ValueError, match="function declarations"):
        validate_request(
            "vertexGemini",
            VERTEX_MODEL,
            {
                "contents": [{"parts": [{"text": "hello"}]}],
                "tools": [{"googleSearch": {}}],
            },
        )


def test_custom_payload_keeps_exact_ids_and_client_executed_tool_roundtrips() -> None:
    body = {
        "model": "fictional/Model",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + _png()}}
                ],
            },
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call", "content": "fictional result"},
        ],
        "tools": [{"type": "function", "function": {"name": "lookup"}}],
        "tool_choice": "auto",
    }
    assert validate_request("openAiChat", "fictional/Model", body) is body
    with pytest.raises(ValueError, match="exact declared"):
        validate_request("openAiChat", "fictional/model", body)
    body["messages"][0]["content"][0]["image_url"]["url"] = "https://example.com/image.png"
    with pytest.raises(ValueError, match="Remote media"):
        validate_request("openAiChat", "fictional/Model", body)


def test_image_count_and_mime_are_bounded_without_exposing_content() -> None:
    image = {"inlineData": {"mimeType": "image/jpeg", "data": _png()}}
    with pytest.raises(ValueError, match="MIME"):
        validate_request("vertexGemini", VERTEX_MODEL, {"contents": [{"parts": [image]}]})
    image["inlineData"]["mimeType"] = "image/png"
    with pytest.raises(ValueError, match="Too many"):
        validate_request(
            "vertexGemini", VERTEX_MODEL, {"contents": [{"parts": [image] * (MAX_IMAGES + 1)}]}
        )


def test_auth_protocol_contracts_are_fixed_independent_and_redact_repr() -> None:
    sts = sts_exchange(AUDIENCE, "fictional-subject")
    assert sts.url == STS_URL and "Authorization" not in sts.headers
    assert parse_qs(sts.body)["audience"] == [AUDIENCE]
    impersonation = impersonation_request(ACCOUNT, "fictional-sts")
    assert impersonation.headers["Authorization"] == "Bearer fictional-sts"
    assert impersonation.url.endswith(ACCOUNT + ":generateAccessToken")
    claims = service_account_claims(ACCOUNT, 1000)
    assert claims["aud"] == OAUTH_URL and claims["exp"] == 4600
    oauth = jwt_bearer_exchange("header.payload.signature")
    assert oauth.url == OAUTH_URL and "Authorization" not in oauth.headers
    assert "fictional-subject" not in repr(sts) and "fictional-sts" not in repr(impersonation)
    assert vertex_auth_headers("apiKey", "fictional-key") == {"x-goog-api-key": "fictional-key"}
    for mode in ("workloadIdentityFederation", "serviceAccountKey"):
        assert vertex_auth_headers(mode, "fictional-token") == {
            "Authorization": "Bearer fictional-token"
        }
    with pytest.raises(ValueError):
        impersonation_request("https://attacker.example.com", "token")


def test_native_inline_jpeg_is_preserved() -> None:
    image = BytesIO()
    Image.new("RGB", (1, 1)).save(image, "JPEG")
    body = {
        "contents": [
            {
                "parts": [
                    {
                        "inlineData": {
                            "mimeType": "image/jpeg",
                            "data": base64.b64encode(image.getvalue()).decode(),
                        }
                    }
                ]
            }
        ]
    }
    assert validate_request("vertexGemini", VERTEX_MODEL, body) is body


def test_fixed_native_operations_query_allowlist_and_custom_json_not_path() -> None:
    ordinary = vertex_operation_url("fictional-project", VERTEX_MODEL, streaming=False)
    streamed = vertex_operation_url("fictional-project", VERTEX_MODEL, streaming=True)
    assert ordinary == (
        VERTEX_ORIGIN + "/v1/projects/fictional-project/locations/us/publishers/google/"
        "models/gemini-3.8-flash:generateContent"
    )
    assert streamed.endswith(":streamGenerateContent?alt=sse")
    with pytest.raises(ValueError, match="alt=sse"):
        vertex_operation_url(
            "fictional-project",
            VERTEX_MODEL,
            streaming=True,
            query={"alt": "sse", "key": "credential"},
        )
    assert custom_operation_url("https://models.example.com/v1") == (
        "https://models.example.com/v1/chat/completions"
    )
    assert (
        "fictional/Model"
        not in shape_operations(ApiShape.OPENAI_CHAT, "fictional/Model")[0].url_template
    )
    for shape in (ApiShape.OPENAI_CHAT, ApiShape.VERTEX_GEMINI):
        assert required_data_actions(shape) == ()
        assert policy_support_on_tier("llm-token-limit", shape, GatewayTier.V2) == "unknown"
    assert not assess_declared_deployment(
        ModelProvider.GOOGLE_VERTEX,
        ApiShape.VERTEX_GEMINI,
        endpoint=VERTEX_ORIGIN,
        gateway_sku="StandardV2",
    ).publishable


def test_missing_usage_remains_unknown_and_native_thought_tokens_are_not_invented() -> None:
    assert usage_counts("vertexGemini", {}) == (None, None)
    assert usage_counts("openAiChat", {"usage": None}) == (None, None)
    assert usage_counts("vertexGemini", {"usageMetadata": {"promptTokenCount": 0}}) == (0, None)
    assert usage_counts(
        "vertexGemini",
        {
            "usageMetadata": {
                "promptTokenCount": 3,
                "candidatesTokenCount": 4,
                "thoughtsTokenCount": 20,
            }
        },
    ) == (3, 4)


@pytest.mark.parametrize(
    "shape",
    [
        ApiShape.AZURE_OPENAI,
        ApiShape.FOUNDRY_MODELS,
        ApiShape.ANTHROPIC_MESSAGES,
        ApiShape.OPENAI_CHAT,
    ],
)
def test_neighboring_nondeployment_shapes_keep_exact_body_model_guard(shape: ApiShape) -> None:
    endpoint = ModelEndpoint(
        id="endpoint",
        tenant_id=ACTOR.tenant_id,
        name="Fictional",
        provider=ModelProvider.OPENAI_COMPATIBLE,
        endpoint="https://models.example.com",
    )
    publication = _publication(
        endpoint.model_copy(
            update={
                "declared_deployments": [
                    DeclaredDeploymentCreate(
                        deployment_name="Exact", model_name="Exact", api_shape=shape
                    )
                ]
            }
        )
    )
    fragment = ET.Element("fragment")
    _operation_guard(fragment, publication, governed_operations(publication))
    policy = html.unescape(ET.tostring(fragment, encoding="unicode"))
    assert 'body["model"]' in policy
    assert "StringComparison.Ordinal" in policy
    assert "model == null || model.Type != JTokenType.String" in policy


def test_native_guard_binds_operation_route_and_rejects_body_override() -> None:
    endpoint = ModelEndpoint(
        id="endpoint",
        tenant_id=ACTOR.tenant_id,
        name="Fictional",
        provider=ModelProvider.GOOGLE_VERTEX,
        endpoint=VERTEX_ORIGIN,
    )
    publication = _publication(
        endpoint.model_copy(
            update={
                "declared_deployments": [
                    DeclaredDeploymentCreate(
                        deployment_name=VERTEX_MODEL,
                        model_name=VERTEX_MODEL,
                        api_shape=ApiShape.VERTEX_GEMINI,
                    )
                ]
            }
        )
    )
    fragment = ET.Element("fragment")
    _operation_guard(fragment, publication, governed_operations(publication))
    policy = ET.tostring(fragment, encoding="unicode")
    assert "OriginalUrl.Path.EndsWith" in policy
    assert "/models/gemini-3.8-flash:generateContent" in policy
    assert "/models/gemini-3.8-flash:streamGenerateContent" in policy
    assert "model != null" in policy and "StringComparison.Ordinal" in policy
