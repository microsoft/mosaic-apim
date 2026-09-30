"""The portal offers a model or MCP server MOSAIC publishes only while its API is in API Management.

Unpublishing removes the API, so the gateway no longer serves it. The catalog leaves it out, a new
request for it is refused with the reason, and a grant or request already made for it says it isn't
available. Model APIs and MCP servers imported from a gateway MOSAIC didn't publish are unaffected.
"""

from datetime import UTC, datetime

import pytest
from conftest import reviewed_unpublish
from mosaic_api.domain import (
    AccessRequestCreate,
    CatalogVisibility,
    EntitlementResource,
    EntitlementSubject,
    McpPublication,
    McpServer,
    ModelApi,
    ModelProvider,
    Publication,
    PublicationStatus,
    PublishedResource,
    PublishedResourceKind,
    mcp_server_id,
)
from mosaic_api.errors import ConflictError, NotFoundError
from mosaic_api.repositories import InMemoryDirectoryRepository, InMemoryEntitlementRepository
from mosaic_api.services import EntitlementService, PortalService
from test_portal_api import ACTOR, TENANT, Harness, _audit
from test_publishing import ACTOR as ADMIN
from test_publishing import Harness as PublishingHarness

UNPUBLISHED_AT = datetime(2026, 9, 30, 11, 20, tzinfo=UTC)


def _api(name: str) -> PublishedResource:
    return PublishedResource(
        kind=PublishedResourceKind.API,
        name=name,
        resource_id=f"/gateways/gateway_1/apis/{name}",
        created_by_mosaic=True,
    )


async def _publish_model_api(
    harness: Harness,
    api_id: str,
    *,
    applied: bool,
    visibility: CatalogVisibility = CatalogVisibility.CATALOG,
    record_publication: bool = True,
) -> ModelApi:
    publication = Publication(
        id=f"publication-{api_id}",
        tenant_id=TENANT,
        gateway_id="gateway_1",
        model_endpoint_id="endpoint",
        deployment_name=api_id,
        provider=ModelProvider.AZURE_OPENAI,
        display_name=f"Model {api_id}",
        api_name=api_id,
        api_path=api_id,
        backend_name=api_id,
        fragment_name=api_id,
        product_name=api_id,
        subscription_name=api_id,
        shape_version="1",
        status=PublicationStatus.PUBLISHED if applied else PublicationStatus.DRAFT,
        resources=[_api(api_id)] if applied else [],
        last_applied_at=UNPUBLISHED_AT,
        unpublished_at=None if applied else UNPUBLISHED_AT,
        model_api_id=api_id,
    )
    if record_publication:
        await harness.gateways.save_publication(publication, _audit())
    record = ModelApi(
        id=api_id,
        tenant_id=TENANT,
        gateway_id="gateway_1",
        api_name=api_id,
        display_name=f"Model {api_id}",
        path=api_id,
        visibility=visibility,
        publication_id=publication.id,
    )
    await harness.gateways.save_model_api(record, _audit())
    return record


def _request(kind: str, resource_id: str) -> AccessRequestCreate:
    return AccessRequestCreate(resource=EntitlementResource(kind=kind, id=resource_id))


async def _publish_mcp_server(harness: Harness, name: str, *, applied: bool) -> McpServer:
    server = McpServer(
        id=mcp_server_id(TENANT, "gateway_1", name),
        tenant_id=TENANT,
        gateway_id="gateway_1",
        api_name=name,
        display_name=f"MCP {name}",
        path=name,
        publication_id=f"mcp-publication-{name}",
    )
    publication = McpPublication(
        id=f"mcp-publication-{name}",
        tenant_id=TENANT,
        gateway_id="gateway_1",
        mcp_endpoint_id=f"endpoint-{name}",
        display_name=f"MCP {name}",
        api_name=name,
        api_path=name,
        backend_name=name,
        fragment_name=name,
        metadata_api_name=f"{name}-prm",
        mcp_server_id=server.id,
        status=PublicationStatus.PUBLISHED if applied else PublicationStatus.DRAFT,
        access_state="applied" if applied else "pending",
        # The metadata API alone doesn't serve the server.
        resources=[_api(name), _api(f"{name}-prm")] if applied else [_api(f"{name}-prm")],
    )
    await harness.gateways.save_mcp_publication(publication, _audit())
    await harness.gateways.save_mcp_server(server, _audit())
    return server


@pytest.fixture
async def harness() -> Harness:
    built = Harness()
    await built.add_gateway()
    return built


async def test_the_catalog_offers_a_published_model_only_while_its_api_is_in_apim(
    harness: Harness,
) -> None:
    await harness.add_model_api("api-imported")
    await _publish_model_api(harness, "api-published", applied=True)
    await _publish_model_api(harness, "api-unpublished", applied=False)
    await _publish_model_api(harness, "api-removed", applied=True, record_publication=False)
    mismatched = await _publish_model_api(harness, "api-renamed", applied=True)
    await harness.gateways.save_model_api(
        mismatched.model_copy(update={"api_name": "api-other"}), _audit()
    )

    entries = await harness.service.catalog(ACTOR)

    assert [entry.id for entry in entries] == ["api-imported", "api-published"]


async def test_the_catalog_offers_a_published_mcp_server_only_while_its_api_is_in_apim(
    harness: Harness,
) -> None:
    await harness.add_mcp_server("imported-mcp")
    applied = await _publish_mcp_server(harness, "applied-mcp", applied=True)
    await _publish_mcp_server(harness, "draft-mcp", applied=False)

    entries = {entry.id: entry for entry in await harness.service.catalog(ACTOR)}

    assert set(entries) == {"imported-mcp", applied.id}
    assert entries[applied.id].enforced is True
    assert entries["imported-mcp"].enforced is False


async def test_a_request_for_an_unpublished_model_is_refused_with_the_reason(
    harness: Harness,
) -> None:
    await _publish_model_api(harness, "api-unpublished", applied=False)
    await _publish_model_api(
        harness, "api-hidden", applied=False, visibility=CatalogVisibility.PRIVATE
    )
    await _publish_model_api(harness, "api-published", applied=True)
    await harness.add_model_api("api-imported")

    with pytest.raises(ConflictError) as refused:
        await harness.service.create_access_request(ACTOR, _request("modelApi", "api-unpublished"))
    # A private resource gets the same answer as one that doesn't exist, published or not.
    with pytest.raises(NotFoundError):
        await harness.service.create_access_request(ACTOR, _request("modelApi", "api-hidden"))

    assert refused.value.message == (
        "Model api-unpublished isn't published right now, so MOSAIC can't take access requests "
        "for it. Try again once an administrator publishes it."
    )
    assert refused.value.details == {
        "reason": "notPublished",
        "resourceKind": "modelApi",
        "resourceId": "api-unpublished",
    }
    assert await harness.service.my_access_requests(ACTOR) == []
    for resource_id in ("api-published", "api-imported"):
        created = await harness.service.create_access_request(
            ACTOR, _request("modelApi", resource_id)
        )
        assert created.resource_summary is not None
        assert created.resource_summary.available is True


async def test_a_request_for_an_mcp_server_never_applied_is_refused(harness: Harness) -> None:
    draft = await _publish_mcp_server(harness, "draft-mcp", applied=False)

    with pytest.raises(ConflictError, match="MCP draft-mcp isn't published right now"):
        await harness.service.create_access_request(ACTOR, _request("mcpServer", draft.id))


async def test_grants_and_requests_for_an_unpublished_model_say_it_is_not_available(
    harness: Harness,
) -> None:
    principal = await harness.add_principal("caller-object-id")
    model = await _publish_model_api(harness, "api-model", applied=True)
    await harness.grant(
        EntitlementSubject(kind="user", id=principal.id),
        EntitlementResource(kind="modelApi", id=model.id),
    )
    other = await _publish_model_api(harness, "api-other", applied=True)
    await harness.service.create_access_request(ACTOR, _request("modelApi", other.id))
    for publication_id in (model.publication_id, other.publication_id):
        assert publication_id is not None
        publication = await harness.gateways.get_publication(TENANT, publication_id)
        assert publication is not None
        await harness.gateways.record_publication_state(
            publication.model_copy(
                update={
                    "status": PublicationStatus.DRAFT,
                    "resources": [],
                    "unpublished_at": UNPUBLISHED_AT,
                }
            )
        )

    [grant] = await harness.service.my_entitlements(ACTOR)
    [request] = await harness.service.my_access_requests(ACTOR)

    for summary, name in (
        (grant.resource_summary, "Model api-model"),
        (request.resource_summary, "Model api-other"),
    ):
        assert summary is not None
        assert summary.available is False
        # Still named, with its gateway, so the person can tell what it was.
        assert summary.display_name == name
        assert summary.gateway_name == "Development gateway"
    assert await harness.service.catalog(ACTOR) == []


async def test_unpublishing_takes_a_model_out_of_the_catalog_until_it_is_published_again() -> None:
    publishing = PublishingHarness()
    await publishing.setup()
    entitlements = EntitlementService(
        InMemoryEntitlementRepository(),
        directory_repository=InMemoryDirectoryRepository(),
        gateway_repository=publishing.gateway_repository,
        endpoint_repository=publishing.endpoint_repository,
    )
    portal = PortalService(
        entitlements,
        directory_repository=InMemoryDirectoryRepository(),
        gateway_repository=publishing.gateway_repository,
    )
    person = ADMIN
    publication_id = await publishing.publish()
    await publishing.apply(publication_id)
    publication = await publishing.service.get_publication(ADMIN, publication_id)
    assert publication.model_api_id
    request = _request("modelApi", publication.model_api_id)
    assert [entry.id for entry in await portal.catalog(person)] == [publication.model_api_id]

    await reviewed_unpublish(publishing.service, ADMIN, publication_id)
    await publishing.service.wait_for_idle()

    assert await portal.catalog(person) == []
    with pytest.raises(ConflictError) as refused:
        await portal.create_access_request(person, request)
    assert refused.value.details["reason"] == "notPublished"

    await publishing.apply(publication_id)

    assert [entry.id for entry in await portal.catalog(person)] == [publication.model_api_id]
    created = await portal.create_access_request(person, request)
    assert created.resource_summary is not None
    assert created.resource_summary.available is True
