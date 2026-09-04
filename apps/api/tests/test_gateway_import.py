"""Discovering MCP servers and adopting gateway resources into MOSAIC.

Two rules run through every test here. Importing writes to Cosmos and never to Azure, and MOSAIC
imports only what it actually observed — a name it cannot see is an error rather than a quiet skip.
"""

import pytest
from apim_double import RESOURCE_ID, FakeApim
from conftest import build_gateway_service
from fastapi.testclient import TestClient
from mosaic_api.domain import (
    CapabilitySupport,
    CatalogEntryUpdate,
    CatalogVisibility,
    Gateway,
    GatewayCreate,
    GatewaySyncStatus,
    ImportRequest,
    ImportSelection,
    McpServerKind,
    McpTransportType,
)
from mosaic_api.errors import NotFoundError, ValidationError
from mosaic_api.observed import AiBackendKind
from mosaic_api.repositories import InMemoryGatewayRepository
from mosaic_api.services import GatewayService
from mosaic_api.services.directory import Actor

ACTOR = Actor(object_id="admin-object-id", tenant_id="tenant-test")
OTHER_ACTOR = Actor(object_id="other-admin", tenant_id="tenant-other")


async def _register(service: GatewayService) -> Gateway:
    return await service.register(ACTOR, GatewayCreate(azure_resource_id=RESOURCE_ID))


async def _registered_and_synced(service: GatewayService) -> Gateway:
    gateway = await _register(service)
    await service.sync_now(ACTOR, gateway.id)
    return await service.get_gateway(ACTOR, gateway.id)


# ---------------------------------------------------------------------------
# MCP discovery
# ---------------------------------------------------------------------------


async def test_sync_collects_mcp_servers_and_their_tools(
    gateway_service: GatewayService,
) -> None:
    gateway = await _registered_and_synced(gateway_service)

    servers = await gateway_service.list_observed_mcp_servers(ACTOR, gateway.id)

    assert [server.name for server in servers] == ["orders-mcp", "weather-mcp"]
    orders = servers[0]
    assert orders.kind == McpServerKind.REST_API_BACKED
    assert orders.tool_count == 1
    assert orders.tools[0].display_name == "listOrders"
    assert orders.tools[0].backing_api_name == "echo-api"
    assert orders.tools[0].backing_operation_name == "get-echo"


async def test_passthrough_transport_and_endpoints_are_recorded(
    gateway_service: GatewayService,
) -> None:
    gateway = await _registered_and_synced(gateway_service)

    servers = await gateway_service.list_observed_mcp_servers(ACTOR, gateway.id)
    weather = next(server for server in servers if server.name == "weather-mcp")

    assert weather.kind == McpServerKind.PASSTHROUGH
    assert weather.transport_type == McpTransportType.SSE
    assert [endpoint.name for endpoint in weather.endpoints] == ["sse", "message"]


async def test_passthrough_service_url_is_stripped_of_its_query_string(
    gateway_service: GatewayService,
) -> None:
    """Backend URLs routinely carry keys in the query string; ADR 0004 forbids storing them."""

    gateway = await _registered_and_synced(gateway_service)

    servers = await gateway_service.list_observed_mcp_servers(ACTOR, gateway.id)
    weather = next(server for server in servers if server.name == "weather-mcp")

    assert weather.service_url is not None
    assert weather.service_url.startswith("https://mcp.contoso.com")
    assert "PassthroughSecret" not in weather.service_url


async def test_mcp_servers_are_not_also_listed_as_ordinary_apis(
    gateway_service: GatewayService,
) -> None:
    gateway = await _registered_and_synced(gateway_service)

    apis = await gateway_service.list_apis(ACTOR, gateway.id)

    assert [api.name for api in apis] == ["chat-api", "echo-api"]


async def test_sync_counts_mcp_servers_and_reports_the_capability(
    gateway_service: GatewayService,
) -> None:
    gateway = await _register(gateway_service)

    run = await gateway_service.sync_now(ACTOR, gateway.id)
    refreshed = await gateway_service.get_gateway(ACTOR, gateway.id)

    assert run.status == GatewaySyncStatus.SUCCEEDED
    assert run.counts.mcp_servers == 2
    assert refreshed.capabilities.mcp_servers == CapabilitySupport.AVAILABLE


async def test_a_service_without_the_preview_contract_reports_unavailable_not_failure(
    gateway_repository: InMemoryGatewayRepository,
) -> None:
    """An older API Management service is not a broken one, so the sync must still succeed."""

    service = build_gateway_service(FakeApim(supports_mcp=False), gateway_repository)
    gateway = await _register(service)

    run = await service.sync_now(ACTOR, gateway.id)
    refreshed = await service.get_gateway(ACTOR, gateway.id)

    assert run.status == GatewaySyncStatus.SUCCEEDED
    assert run.errors == []
    assert run.counts.mcp_servers == 0
    assert refreshed.capabilities.mcp_servers == CapabilitySupport.UNAVAILABLE


async def test_a_failed_mcp_read_degrades_the_run_rather_than_reporting_none(
    gateway_repository: InMemoryGatewayRepository,
) -> None:
    fake = FakeApim()
    service = build_gateway_service(fake, gateway_repository)
    gateway = await _registered_and_synced(service)
    assert await service.list_observed_mcp_servers(ACTOR, gateway.id)

    fake.fail_always("apis", 500)
    run = await service.sync_now(ACTOR, gateway.id)
    refreshed = await service.get_gateway(ACTOR, gateway.id)

    assert run.status == GatewaySyncStatus.PARTIAL
    # The previous documents survive: a read MOSAIC could not complete must never look like a
    # deletion, and support stays at its last known value rather than flipping to unavailable.
    assert await service.list_observed_mcp_servers(ACTOR, gateway.id)
    assert refreshed.capabilities.mcp_servers == CapabilitySupport.AVAILABLE


async def test_preflight_does_not_forget_mcp_support(
    gateway_service: GatewayService,
) -> None:
    gateway = await _registered_and_synced(gateway_service)

    refreshed = await gateway_service.preflight(ACTOR, gateway.id)

    assert refreshed.capabilities.mcp_servers == CapabilitySupport.AVAILABLE


# ---------------------------------------------------------------------------
# Import candidates
# ---------------------------------------------------------------------------


async def test_model_fronting_apis_are_recommended_and_others_are_still_offered(
    gateway_service: GatewayService,
) -> None:
    gateway = await _registered_and_synced(gateway_service)

    result = await gateway_service.list_importable_apis(ACTOR, gateway.id)

    by_name = {candidate.api_name: candidate for candidate in result.candidates}
    assert set(by_name) == {"chat-api", "echo-api"}
    assert by_name["chat-api"].recommended is True
    assert by_name["chat-api"].ai_kind == AiBackendKind.AZURE_OPENAI
    assert by_name["chat-api"].ai_signals
    # An unrecognised API is listed so an administrator can adopt it anyway; it just starts
    # unchecked.
    assert by_name["echo-api"].recommended is False
    assert by_name["echo-api"].ai_kind == AiBackendKind.NONE


async def test_recommended_candidates_are_listed_first(
    gateway_service: GatewayService,
) -> None:
    gateway = await _registered_and_synced(gateway_service)

    result = await gateway_service.list_importable_apis(ACTOR, gateway.id)

    assert result.candidates[0].api_name == "chat-api"


async def test_candidates_report_what_is_already_adopted(
    gateway_service: GatewayService,
) -> None:
    gateway = await _registered_and_synced(gateway_service)
    await gateway_service.import_model_apis(
        ACTOR, gateway.id, ImportRequest(api_names=["chat-api"])
    )

    result = await gateway_service.list_importable_apis(ACTOR, gateway.id)
    by_name = {candidate.api_name: candidate for candidate in result.candidates}

    assert by_name["chat-api"].already_imported is True
    assert by_name["echo-api"].already_imported is False


async def test_mcp_candidates_carry_the_gateway_support_state(
    gateway_service: GatewayService,
) -> None:
    gateway = await _registered_and_synced(gateway_service)

    result = await gateway_service.list_importable_mcp_servers(ACTOR, gateway.id)

    assert result.support == CapabilitySupport.AVAILABLE
    assert [candidate.api_name for candidate in result.candidates] == [
        "orders-mcp",
        "weather-mcp",
    ]


# ---------------------------------------------------------------------------
# Importing
# ---------------------------------------------------------------------------


async def test_importing_records_the_provenance_of_each_api(
    gateway_service: GatewayService,
) -> None:
    gateway = await _registered_and_synced(gateway_service)

    imported = await gateway_service.import_model_apis(
        ACTOR, gateway.id, ImportRequest(api_names=["chat-api"])
    )

    assert len(imported) == 1
    record = imported[0]
    assert record.api_name == "chat-api"
    assert record.gateway_id == gateway.id
    assert record.ai_kind == AiBackendKind.AZURE_OPENAI
    assert record.selection == ImportSelection.DETECTED
    assert record.imported_by == ACTOR.object_id
    assert record.imported_from_snapshot_id


async def test_adopting_an_undetected_api_is_recorded_as_a_manual_choice(
    gateway_service: GatewayService,
) -> None:
    gateway = await _registered_and_synced(gateway_service)

    imported = await gateway_service.import_model_apis(
        ACTOR, gateway.id, ImportRequest(api_names=["echo-api"])
    )

    assert imported[0].selection == ImportSelection.MANUAL


async def test_reimporting_updates_in_place_instead_of_duplicating(
    gateway_service: GatewayService,
) -> None:
    gateway = await _registered_and_synced(gateway_service)
    request = ImportRequest(api_names=["chat-api"])

    first = await gateway_service.import_model_apis(ACTOR, gateway.id, request)
    await gateway_service.sync_now(ACTOR, gateway.id)
    second = await gateway_service.import_model_apis(ACTOR, gateway.id, request)

    assert first[0].id == second[0].id
    assert len(await gateway_service.list_model_apis(ACTOR, gateway.id)) == 1


async def test_reimporting_preserves_administrator_authored_catalog_metadata(
    gateway_service: GatewayService,
) -> None:
    """Visibility and summary are authored, not discovered, so a re-sync must not reset them."""

    gateway = await _registered_and_synced(gateway_service)
    request = ImportRequest(api_names=["chat-api"])
    imported = await gateway_service.import_model_apis(ACTOR, gateway.id, request)
    await gateway_service.update_model_api_catalog(
        ACTOR,
        imported[0].id,
        CatalogEntryUpdate(visibility=CatalogVisibility.PRIVATE, summary="Restricted"),
    )

    await gateway_service.sync_now(ACTOR, gateway.id)
    reimported = await gateway_service.import_model_apis(ACTOR, gateway.id, request)

    assert reimported[0].visibility == CatalogVisibility.PRIVATE
    assert reimported[0].summary == "Restricted"


async def test_reimporting_preserves_mcp_catalog_metadata(
    gateway_service: GatewayService,
) -> None:
    gateway = await _registered_and_synced(gateway_service)
    request = ImportRequest(api_names=["orders-mcp"])
    imported = await gateway_service.import_mcp_servers(ACTOR, gateway.id, request)
    await gateway_service.update_mcp_server_catalog(
        ACTOR,
        imported[0].id,
        CatalogEntryUpdate(visibility=CatalogVisibility.PRIVATE, summary="Internal tooling"),
    )

    await gateway_service.sync_now(ACTOR, gateway.id)
    reimported = await gateway_service.import_mcp_servers(ACTOR, gateway.id, request)

    assert reimported[0].visibility == CatalogVisibility.PRIVATE
    assert reimported[0].summary == "Internal tooling"


async def test_importing_an_unobserved_name_is_rejected(
    gateway_service: GatewayService,
) -> None:
    gateway = await _registered_and_synced(gateway_service)

    with pytest.raises(ValidationError) as error:
        await gateway_service.import_model_apis(
            ACTOR, gateway.id, ImportRequest(api_names=["chat-api", "invented-api"])
        )

    assert error.value.details["unknown"] == ["invented-api"]
    # Nothing is adopted when part of the selection is unresolvable.
    assert await gateway_service.list_model_apis(ACTOR, gateway.id) == []


async def test_importing_before_a_sync_is_refused(
    gateway_service: GatewayService,
) -> None:
    gateway = await _register(gateway_service)

    with pytest.raises(ValidationError):
        await gateway_service.import_model_apis(
            ACTOR, gateway.id, ImportRequest(api_names=["chat-api"])
        )


async def test_importing_mcp_servers_keeps_transport_and_tools(
    gateway_service: GatewayService,
) -> None:
    gateway = await _registered_and_synced(gateway_service)

    imported = await gateway_service.import_mcp_servers(
        ACTOR, gateway.id, ImportRequest(api_names=["orders-mcp", "weather-mcp"])
    )

    by_name = {record.api_name: record for record in imported}
    assert by_name["orders-mcp"].tool_count == 1
    assert by_name["orders-mcp"].tools[0].backing_api_name == "echo-api"
    assert by_name["weather-mcp"].transport_type == McpTransportType.SSE
    assert len(by_name["weather-mcp"].endpoints) == 2


async def test_duplicate_names_in_one_request_import_once(
    gateway_service: GatewayService,
) -> None:
    gateway = await _registered_and_synced(gateway_service)

    imported = await gateway_service.import_model_apis(
        ACTOR, gateway.id, ImportRequest(api_names=["chat-api", "chat-api"])
    )

    assert len(imported) == 1


# ---------------------------------------------------------------------------
# Lifecycle and isolation
# ---------------------------------------------------------------------------


async def test_removing_an_import_leaves_the_gateway_untouched(
    gateway_service: GatewayService,
) -> None:
    gateway = await _registered_and_synced(gateway_service)
    imported = await gateway_service.import_model_apis(
        ACTOR, gateway.id, ImportRequest(api_names=["chat-api"])
    )

    await gateway_service.delete_model_api(ACTOR, imported[0].id)

    assert await gateway_service.list_model_apis(ACTOR, gateway.id) == []
    assert await gateway_service.get_gateway(ACTOR, gateway.id)
    assert await gateway_service.list_apis(ACTOR, gateway.id)


async def test_removing_an_unknown_import_is_not_found(
    gateway_service: GatewayService,
) -> None:
    with pytest.raises(NotFoundError):
        await gateway_service.delete_model_api(ACTOR, "modelApi_missing")


async def test_deleting_a_gateway_takes_its_imports_with_it(
    gateway_service: GatewayService,
) -> None:
    gateway = await _registered_and_synced(gateway_service)
    await gateway_service.import_model_apis(
        ACTOR, gateway.id, ImportRequest(api_names=["chat-api"])
    )
    await gateway_service.import_mcp_servers(
        ACTOR, gateway.id, ImportRequest(api_names=["orders-mcp"])
    )

    await gateway_service.delete(ACTOR, gateway.id)

    assert await gateway_service.list_model_apis(ACTOR) == []
    assert await gateway_service.list_mcp_servers(ACTOR) == []


async def test_imports_are_not_visible_to_another_tenant(
    gateway_service: GatewayService,
) -> None:
    gateway = await _registered_and_synced(gateway_service)
    await gateway_service.import_model_apis(
        ACTOR, gateway.id, ImportRequest(api_names=["chat-api"])
    )

    assert await gateway_service.list_model_apis(OTHER_ACTOR) == []


async def test_importing_emits_an_audit_event(
    gateway_service: GatewayService,
    gateway_repository: InMemoryGatewayRepository,
) -> None:
    gateway = await _registered_and_synced(gateway_service)

    imported = await gateway_service.import_model_apis(
        ACTOR, gateway.id, ImportRequest(api_names=["chat-api"])
    )

    events = [
        event
        for event in gateway_repository.audit_events.values()
        if event.resource_id == imported[0].id
    ]
    assert [event.action for event in events] == ["modelApi.imported"]
    assert events[0].resource_type == "modelApi"
    assert events[0].actor_object_id == ACTOR.object_id


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


def _register_and_sync(client: TestClient) -> str:
    created = client.post("/api/v1/gateways", json={"azureResourceId": RESOURCE_ID})
    assert created.status_code == 201, created.text
    gateway_id = str(created.json()["id"])
    started = client.post(f"/api/v1/gateways/{gateway_id}/sync")
    assert started.status_code == 202, started.text
    run_id = started.json()["id"]
    for _ in range(100):
        run = client.get(f"/api/v1/gateways/{gateway_id}/sync-runs/{run_id}").json()
        if run["status"] != "running":
            return gateway_id
    raise AssertionError("sync did not finish")


def test_import_endpoints_round_trip(gateway_client: TestClient) -> None:
    gateway_id = _register_and_sync(gateway_client)

    candidates = gateway_client.get(f"/api/v1/gateways/{gateway_id}/importable-apis")
    assert candidates.status_code == 200, candidates.text
    body = candidates.json()
    assert body["gatewayId"] == gateway_id
    assert body["candidates"][0]["recommended"] is True

    imported = gateway_client.post(
        f"/api/v1/gateways/{gateway_id}/import-apis", json={"apiNames": ["chat-api"]}
    )
    assert imported.status_code == 201, imported.text
    record = imported.json()[0]
    assert record["apiName"] == "chat-api"
    assert record["aiKind"] == "azureOpenAi"

    listed = gateway_client.get("/api/v1/model-apis", params={"gateway": gateway_id})
    assert [item["id"] for item in listed.json()] == [record["id"]]

    removed = gateway_client.delete(f"/api/v1/model-apis/{record['id']}")
    assert removed.status_code == 204
    assert gateway_client.get("/api/v1/model-apis").json() == []


def test_mcp_import_endpoints_round_trip(gateway_client: TestClient) -> None:
    gateway_id = _register_and_sync(gateway_client)

    candidates = gateway_client.get(f"/api/v1/gateways/{gateway_id}/importable-mcp-servers")
    assert candidates.status_code == 200, candidates.text
    assert candidates.json()["support"] == "available"

    imported = gateway_client.post(
        f"/api/v1/gateways/{gateway_id}/import-mcp-servers",
        json={"apiNames": ["weather-mcp"]},
    )
    assert imported.status_code == 201, imported.text
    record = imported.json()[0]
    assert record["transportType"] == "sse"

    listed = gateway_client.get("/api/v1/mcp-servers")
    assert [item["id"] for item in listed.json()] == [record["id"]]

    removed = gateway_client.delete(f"/api/v1/mcp-servers/{record['id']}")
    assert removed.status_code == 204


def test_observed_mcp_servers_are_exposed_for_the_gateway(
    gateway_client: TestClient,
) -> None:
    gateway_id = _register_and_sync(gateway_client)

    response = gateway_client.get(f"/api/v1/gateways/{gateway_id}/mcp-servers")

    assert response.status_code == 200
    assert [item["name"] for item in response.json()] == ["orders-mcp", "weather-mcp"]


def test_an_empty_selection_is_rejected(gateway_client: TestClient) -> None:
    gateway_id = _register_and_sync(gateway_client)

    response = gateway_client.post(
        f"/api/v1/gateways/{gateway_id}/import-apis", json={"apiNames": []}
    )

    assert response.status_code == 422


def test_importing_an_unobserved_name_returns_a_validation_error(
    gateway_client: TestClient,
) -> None:
    gateway_id = _register_and_sync(gateway_client)

    response = gateway_client.post(
        f"/api/v1/gateways/{gateway_id}/import-apis", json={"apiNames": ["invented"]}
    )

    assert response.status_code == 422
    assert response.json()["details"]["unknown"] == ["invented"]
