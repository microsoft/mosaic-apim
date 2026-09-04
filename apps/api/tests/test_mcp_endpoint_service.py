import pytest
from conftest import build_mcp_service
from mcp_double import TOOL_READ_ONLY, TOOL_UNANNOTATED, FakeMcpServer, build_http_client
from mosaic_api.domain import (
    McpAuthMode,
    McpEndpointCreate,
    McpEndpointStatus,
    McpEndpointUpdate,
    canonical_mcp_url,
    mcp_endpoint_id,
)
from mosaic_api.errors import ConflictError, NotFoundError, UpstreamError, ValidationError
from mosaic_api.repositories import InMemoryMcpEndpointRepository
from mosaic_api.services import McpEndpointService
from mosaic_api.services.directory import Actor
from mosaic_api.services.mcp_endpoints import build_mcp_client_factory
from pydantic import ValidationError as PydanticValidationError

URL = "https://mcp.example.com/mcp"
ACTOR = Actor(object_id="admin-object-id", tenant_id="tenant-test")


def create(**kwargs: object) -> McpEndpointCreate:
    payload: dict[str, object] = {"endpoint": URL}
    payload.update(kwargs)
    return McpEndpointCreate.model_validate(payload)


async def register(service: McpEndpointService, **kwargs: object) -> object:
    return await service.register(ACTOR, create(**kwargs))


# -- registration ---------------------------------------------------------------------------


async def test_registration_runs_the_handshake_and_records_the_server(
    mcp_service: McpEndpointService, fake_mcp: FakeMcpServer
) -> None:
    endpoint = await mcp_service.register(ACTOR, create(name="Contoso"))

    assert endpoint.status == McpEndpointStatus.CONNECTED
    assert endpoint.access.can_discover is True
    assert endpoint.access.evaluation == "handshake"
    assert endpoint.capabilities.protocol_version == "2025-11-25"
    assert endpoint.capabilities.server_name == "contoso-mcp"
    assert endpoint.capabilities.supports_tools == "available"
    # Preflight is the handshake and nothing more; the catalogue waits for an explicit sync.
    assert "tools/list" not in fake_mcp.rpc_methods()
    assert endpoint.inventory.tools == 0


async def test_registration_is_idempotent_on_the_canonical_url(
    mcp_service: McpEndpointService,
) -> None:
    await mcp_service.register(ACTOR, create())
    with pytest.raises(ConflictError):
        await mcp_service.register(ACTOR, create(endpoint="HTTPS://MCP.Example.com/mcp/"))


async def test_the_record_id_is_derived_from_the_canonical_url(
    mcp_service: McpEndpointService,
) -> None:
    endpoint = await mcp_service.register(ACTOR, create())
    assert endpoint.id == mcp_endpoint_id("tenant-test", URL)


async def test_a_private_address_is_refused_at_registration(
    mcp_service: McpEndpointService,
) -> None:
    with pytest.raises(ValidationError):
        await mcp_service.register(ACTOR, create(endpoint="https://169.254.169.254/mcp"))


async def test_plaintext_http_is_refused_at_registration(
    mcp_service: McpEndpointService,
) -> None:
    with pytest.raises(ValidationError):
        await mcp_service.register(ACTOR, create(endpoint="http://mcp.example.com/mcp"))


# -- authentication -------------------------------------------------------------------------


async def test_a_key_authenticated_server_stores_only_the_secret_uri(
    fake_mcp: FakeMcpServer,
) -> None:
    repository = InMemoryMcpEndpointRepository()
    service = build_mcp_service(fake_mcp, repository=repository)
    endpoint = await service.register(
        ACTOR, create(credential_secret_uri="https://kv.vault.azure.net/secrets/mcp-token")
    )

    assert endpoint.auth_mode == McpAuthMode.API_KEY
    assert endpoint.credential_reference_id is not None
    stored = await repository.get_credential("tenant-test", endpoint.credential_reference_id)
    assert stored is not None
    assert str(stored.secret_uri) == "https://kv.vault.azure.net/secrets/mcp-token"
    # The resolved token is presented to the server but never written to the record.
    assert "vault-token" not in endpoint.model_dump_json()
    assert fake_mcp.header_on("initialize", "authorization") == "Bearer vault-token"


async def test_a_managed_identity_server_presents_a_token_for_the_stated_audience(
    fake_mcp: FakeMcpServer,
) -> None:
    service = build_mcp_service(fake_mcp)
    endpoint = await service.register(ACTOR, create(resource_audience="api://contoso-mcp"))

    assert endpoint.auth_mode == McpAuthMode.MANAGED_IDENTITY
    assert endpoint.resource_audience == "api://contoso-mcp"
    assert fake_mcp.header_on("initialize", "authorization") == "Bearer entra-token"


async def test_an_unauthenticated_server_sends_no_authorization_header(
    mcp_service: McpEndpointService, fake_mcp: FakeMcpServer
) -> None:
    endpoint = await mcp_service.register(ACTOR, create())

    assert endpoint.auth_mode == McpAuthMode.NONE
    assert fake_mcp.header_on("initialize", "authorization") is None


def test_managed_identity_without_an_audience_is_rejected() -> None:
    with pytest.raises(PydanticValidationError):
        create(auth_mode="managedIdentity")


def test_a_key_mode_without_a_secret_uri_is_rejected() -> None:
    with pytest.raises(PydanticValidationError):
        create(auth_mode="apiKey")


def test_an_unauthenticated_server_must_not_carry_a_credential() -> None:
    with pytest.raises(PydanticValidationError):
        create(auth_mode="none", resource_audience="api://contoso-mcp")


# -- access verdicts ------------------------------------------------------------------------


async def test_a_401_is_recorded_as_unauthorized_with_what_the_server_asked_for() -> None:
    service = build_mcp_service(FakeMcpServer(unauthorized=True))
    endpoint = await service.register(ACTOR, create())

    assert endpoint.status == McpEndpointStatus.UNAUTHORIZED
    assert endpoint.access.can_discover is False
    # "Needs authorization" must never read as "unreachable", and the challenge is kept so an
    # operator can act on it.
    assert endpoint.access.evaluation == "authorizationRequired"
    assert endpoint.access.challenge is not None
    assert endpoint.access.challenge.scope == "mcp.read"
    assert endpoint.access.challenge.resource_metadata_url is not None


async def test_a_modern_stateless_server_is_recorded_as_an_unsupported_protocol() -> None:
    service = build_mcp_service(FakeMcpServer(protocol_version="2026-07-28"))
    endpoint = await service.register(ACTOR, create())

    assert endpoint.status == McpEndpointStatus.UNSUPPORTED_PROTOCOL
    assert endpoint.access.can_discover is False
    assert endpoint.access.evaluation == "notEvaluated"


async def test_an_sse_only_server_is_recorded_as_an_unsupported_transport() -> None:
    service = build_mcp_service(FakeMcpServer(reject_streamable_post=True))
    endpoint = await service.register(ACTOR, create())

    assert endpoint.status == McpEndpointStatus.UNSUPPORTED_TRANSPORT


async def test_a_failed_handshake_still_registers_the_server() -> None:
    # Losing an administrator's intent because a server was briefly down would be worse than
    # holding a record that says so.
    service = build_mcp_service(FakeMcpServer(unauthorized=True))
    endpoint = await service.register(ACTOR, create())
    assert await service.get_endpoint(ACTOR, endpoint.id) is not None


async def test_preflight_reruns_the_handshake_and_updates_the_record(
    mcp_repository: InMemoryMcpEndpointRepository,
) -> None:
    server = FakeMcpServer(unauthorized=True)
    service = build_mcp_service(server, repository=mcp_repository)
    endpoint = await service.register(ACTOR, create())
    assert endpoint.status == McpEndpointStatus.UNAUTHORIZED

    server.unauthorized = False
    rechecked = await service.preflight(ACTOR, endpoint.id)
    assert rechecked.status == McpEndpointStatus.CONNECTED
    assert rechecked.access.can_discover is True


# -- discovery ------------------------------------------------------------------------------


async def test_sync_mirrors_tools_into_observed_state(
    mcp_service: McpEndpointService, mcp_repository: InMemoryMcpEndpointRepository
) -> None:
    endpoint = await mcp_service.register(ACTOR, create())
    run = await mcp_service.sync_now(ACTOR, endpoint.id)

    assert run.status == "succeeded"
    assert run.counts.tools == 2
    tools = await mcp_service.list_tools(ACTOR, endpoint.id)
    assert [tool.name for tool in tools] == ["delete_record", "search_docs"]
    stored = await mcp_repository.get_endpoint("tenant-test", endpoint.id)
    assert stored is not None
    assert stored.inventory.tools == 2
    assert stored.last_synced_at is not None
    assert stored.status == McpEndpointStatus.CONNECTED


async def test_sync_records_schemas_the_management_plane_cannot_provide(
    mcp_service: McpEndpointService,
) -> None:
    endpoint = await mcp_service.register(ACTOR, create())
    await mcp_service.sync_now(ACTOR, endpoint.id)

    tools = {tool.name: tool for tool in await mcp_service.list_tools(ACTOR, endpoint.id)}
    assert tools["search_docs"].input_schema is not None
    assert tools["search_docs"].output_schema is not None
    assert tools["search_docs"].annotations is not None
    assert tools["search_docs"].annotations.read_only_hint is True


async def test_sync_keeps_absent_annotations_absent(mcp_service: McpEndpointService) -> None:
    endpoint = await mcp_service.register(ACTOR, create())
    await mcp_service.sync_now(ACTOR, endpoint.id)

    tools = {tool.name: tool for tool in await mcp_service.list_tools(ACTOR, endpoint.id)}
    assert tools["delete_record"].annotations is None


async def test_sync_sweeps_tools_that_disappeared(
    mcp_repository: InMemoryMcpEndpointRepository,
) -> None:
    server = FakeMcpServer(tool_pages=[[TOOL_READ_ONLY, TOOL_UNANNOTATED]])
    service = build_mcp_service(server, repository=mcp_repository)
    endpoint = await service.register(ACTOR, create())
    await service.sync_now(ACTOR, endpoint.id)
    assert len(await service.list_tools(ACTOR, endpoint.id)) == 2

    server.tool_pages = [[TOOL_READ_ONLY]]
    run = await service.sync_now(ACTOR, endpoint.id)
    assert run.removed == 1
    assert [tool.name for tool in await service.list_tools(ACTOR, endpoint.id)] == ["search_docs"]


async def test_a_failed_tool_read_is_never_mistaken_for_a_deletion(
    mcp_repository: InMemoryMcpEndpointRepository,
) -> None:
    server = FakeMcpServer()
    service = build_mcp_service(server, repository=mcp_repository)
    endpoint = await service.register(ACTOR, create())
    await service.sync_now(ACTOR, endpoint.id)
    assert len(await service.list_tools(ACTOR, endpoint.id)) == 2

    server.tools_error = {"code": -32603, "message": "backend exploded"}
    run = await service.sync_now(ACTOR, endpoint.id)

    assert run.status == "partial"
    assert run.removed == 0
    # The tools MOSAIC could not read this time keep their previous documents.
    assert len(await service.list_tools(ACTOR, endpoint.id)) == 2
    stored = await mcp_repository.get_endpoint("tenant-test", endpoint.id)
    assert stored is not None
    assert stored.status == McpEndpointStatus.DEGRADED


async def test_a_server_with_no_tools_capability_syncs_successfully() -> None:
    service = build_mcp_service(FakeMcpServer(supports_tools=False))
    endpoint = await service.register(ACTOR, create())
    run = await service.sync_now(ACTOR, endpoint.id)

    assert run.status == "succeeded"
    assert run.counts.tools == 0
    assert await service.list_tools(ACTOR, endpoint.id) == []


async def test_sync_is_refused_until_the_server_is_reachable() -> None:
    service = build_mcp_service(FakeMcpServer(unauthorized=True))
    endpoint = await service.register(ACTOR, create())

    with pytest.raises(ConflictError):
        await service.start_sync(ACTOR, endpoint.id)


async def test_a_second_sync_is_refused_while_one_is_running(
    mcp_service: McpEndpointService,
) -> None:
    endpoint = await mcp_service.register(ACTOR, create())
    await mcp_service.start_sync(ACTOR, endpoint.id)
    try:
        with pytest.raises(ConflictError):
            await mcp_service.start_sync(ACTOR, endpoint.id)
    finally:
        await mcp_service.aclose()


async def test_a_sync_failure_is_recorded_on_the_run_and_the_record(
    mcp_repository: InMemoryMcpEndpointRepository,
) -> None:
    server = FakeMcpServer()
    service = build_mcp_service(server, repository=mcp_repository)
    endpoint = await service.register(ACTOR, create())

    server.unauthorized = True
    run = await service.sync_now(ACTOR, endpoint.id)

    assert run.status == "failed"
    assert run.errors
    stored = await mcp_repository.get_endpoint("tenant-test", endpoint.id)
    assert stored is not None
    assert stored.status == McpEndpointStatus.UNAUTHORIZED
    assert stored.last_sync_error is not None


async def test_a_sync_failure_moves_the_access_verdict_too(
    mcp_repository: InMemoryMcpEndpointRepository,
) -> None:
    server = FakeMcpServer()
    service = build_mcp_service(server, repository=mcp_repository)
    endpoint = await service.register(ACTOR, create())
    assert endpoint.access.can_discover is True

    server.unauthorized = True
    await service.sync_now(ACTOR, endpoint.id)

    stored = await mcp_repository.get_endpoint("tenant-test", endpoint.id)
    assert stored is not None
    # A stale "readable" verdict beside an unauthorized status would hide the challenge and keep
    # offering a sync the server just refused.
    assert stored.access.can_discover is False
    assert stored.access.evaluation == "authorizationRequired"
    assert stored.access.challenge is not None
    assert stored.access.challenge.scope == "mcp.read"

    with pytest.raises(ConflictError):
        await service.start_sync(ACTOR, endpoint.id)


async def test_credentials_in_the_url_are_refused_rather_than_stored(
    mcp_service: McpEndpointService,
) -> None:
    with pytest.raises(ValidationError):
        await mcp_service.register(
            ACTOR, create(endpoint="https://alice:hunter2@mcp.example.com/mcp")
        )
    assert await mcp_service.list_endpoints(ACTOR) == []


async def test_the_stored_url_is_the_canonical_one(mcp_service: McpEndpointService) -> None:
    endpoint = await mcp_service.register(ACTOR, create(endpoint="HTTPS://MCP.Example.com/mcp/"))
    assert str(endpoint.endpoint) == URL


async def test_a_key_vault_outage_records_the_fault_instead_of_losing_the_registration(
    fake_mcp: FakeMcpServer, mcp_repository: InMemoryMcpEndpointRepository
) -> None:
    async def unreachable_vault(_uri: str) -> str:
        raise UpstreamError("Key Vault is unreachable")

    service = McpEndpointService(
        mcp_repository,
        client_factory=build_mcp_client_factory(build_http_client(fake_mcp)),
        secret_resolver=unreachable_vault,
        token_resolver=None,
    )

    endpoint = await service.register(
        ACTOR, create(credential_secret_uri="https://kv.vault.azure.net/secrets/mcp-token")
    )

    assert endpoint.status == McpEndpointStatus.DEGRADED
    assert endpoint.access.can_discover is False
    # The administrator's intent survives a transient fault.
    assert await service.get_endpoint(ACTOR, endpoint.id) is not None


# -- lifecycle ------------------------------------------------------------------------------


async def test_update_renames_without_reconnecting(
    mcp_service: McpEndpointService, fake_mcp: FakeMcpServer
) -> None:
    endpoint = await mcp_service.register(ACTOR, create(name="Contoso"))
    before = len(fake_mcp.requests)

    updated = await mcp_service.update(ACTOR, endpoint.id, McpEndpointUpdate(name="Renamed"))

    assert updated.name == "Renamed"
    assert len(fake_mcp.requests) == before


async def test_a_secret_uri_cannot_be_set_on_a_server_that_uses_no_key(
    mcp_service: McpEndpointService,
) -> None:
    endpoint = await mcp_service.register(ACTOR, create())
    with pytest.raises(ValidationError):
        await mcp_service.update(
            ACTOR,
            endpoint.id,
            McpEndpointUpdate.model_validate(
                {"credentialSecretUri": "https://kv.vault.azure.net/secrets/x"}
            ),
        )


async def test_deleting_a_server_removes_its_tools(
    mcp_service: McpEndpointService, mcp_repository: InMemoryMcpEndpointRepository
) -> None:
    endpoint = await mcp_service.register(ACTOR, create())
    await mcp_service.sync_now(ACTOR, endpoint.id)
    assert mcp_repository.observed

    await mcp_service.delete(ACTOR, endpoint.id)

    assert mcp_repository.observed == {}
    with pytest.raises(NotFoundError):
        await mcp_service.get_endpoint(ACTOR, endpoint.id)


async def test_a_restart_marks_orphaned_runs_failed_rather_than_pending_forever(
    mcp_service: McpEndpointService, mcp_repository: InMemoryMcpEndpointRepository
) -> None:
    endpoint = await mcp_service.register(ACTOR, create())
    run = await mcp_service.start_sync(ACTOR, endpoint.id)
    await mcp_service.aclose()
    await mcp_repository.save_endpoint_sync_run(run.model_copy(update={"status": "running"}))

    reaped = await mcp_service.reap_stale_sync_runs("tenant-test")

    assert reaped == 1
    stored = await mcp_repository.get_endpoint_sync_run("tenant-test", run.id)
    assert stored is not None
    assert stored.status == "failed"


async def test_another_tenant_cannot_see_a_registered_server(
    mcp_service: McpEndpointService,
) -> None:
    endpoint = await mcp_service.register(ACTOR, create())
    other = Actor(object_id="admin-object-id", tenant_id="tenant-other")

    with pytest.raises(NotFoundError):
        await mcp_service.get_endpoint(other, endpoint.id)


def test_canonical_url_lowercases_and_trims() -> None:
    assert canonical_mcp_url("HTTPS://MCP.Example.com:443/mcp/#frag") == (
        "https://mcp.example.com:443/mcp"
    )
