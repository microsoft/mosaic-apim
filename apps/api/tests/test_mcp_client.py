import httpx
import pytest
from mcp_double import (
    TOOL_ANNOTATION_TITLE_ONLY,
    TOOL_READ_ONLY,
    TOOL_UNANNOTATED,
    FakeMcpServer,
    build_http_client,
)
from mosaic_api.errors import ValidationError
from mosaic_api.integrations.mcp import (
    McpAuthorizationRequiredError,
    McpClient,
    McpToolCollector,
    McpToolListTruncatedError,
    McpUnreachableError,
    McpUnsupportedProtocolError,
    McpUnsupportedTransportError,
    admit_mcp_url,
)
from mosaic_api.integrations.mcp.client import MAX_TOOL_PAGES

URL = "https://mcp.example.com/mcp"


def client_for(server: FakeMcpServer) -> McpClient:
    return McpClient(url=URL, http=build_http_client(server))


async def test_handshake_reads_server_identity_and_capabilities() -> None:
    server = FakeMcpServer()
    async with client_for(server) as client:
        session = await client.initialize()

    assert session.protocol_version == "2025-11-25"
    assert session.server_name == "contoso-mcp"
    assert session.server_title == "Contoso MCP"
    assert session.server_version == "3.1.0"
    assert session.instructions == "Use search_docs before anything else."
    assert session.supports_tools is True
    assert session.session_managed is True


async def test_session_id_is_echoed_on_later_requests_but_not_on_initialize() -> None:
    server = FakeMcpServer()
    async with client_for(server) as client:
        await client.initialize()
        await client.notify_initialized()
        await client.list_tools()

    assert server.header_on("initialize", "mcp-session-id") is None
    assert server.header_on("tools/list", "mcp-session-id") == "session-abc"


async def test_protocol_version_header_is_absent_on_initialize_and_present_after() -> None:
    server = FakeMcpServer()
    async with client_for(server) as client:
        await client.initialize()
        await client.list_tools()

    assert server.header_on("initialize", "mcp-protocol-version") is None
    assert server.header_on("tools/list", "mcp-protocol-version") == "2025-11-25"


async def test_protocol_version_header_is_omitted_for_pre_2025_06_18_servers() -> None:
    # The header did not exist before 2025-06-18. Sending it invites a 400 for a header that
    # revision never defined.
    server = FakeMcpServer(protocol_version="2025-03-26")
    async with client_for(server) as client:
        await client.initialize()
        await client.list_tools()

    assert server.header_on("tools/list", "mcp-protocol-version") is None


async def test_accept_header_lists_both_content_types() -> None:
    server = FakeMcpServer()
    async with client_for(server) as client:
        await client.initialize()

    accept = server.header_on("initialize", "accept") or ""
    assert "application/json" in accept
    assert "text/event-stream" in accept


async def test_sse_response_body_is_parsed_and_unrelated_frames_skipped() -> None:
    server = FakeMcpServer(respond_with_sse=True)
    async with client_for(server) as client:
        session = await client.initialize()
        tools = await client.list_tools()

    assert session.server_name == "contoso-mcp"
    assert [tool.name for tool in tools] == ["search_docs", "delete_record"]


async def test_initialized_notification_is_sent_before_listing() -> None:
    server = FakeMcpServer()
    async with client_for(server) as client:
        await client.initialize()
        await client.notify_initialized()
        await client.list_tools()

    assert server.rpc_methods() == ["initialize", "notifications/initialized", "tools/list"]


async def test_tools_are_paged_until_the_cursor_runs_out() -> None:
    server = FakeMcpServer(
        tool_pages=[[TOOL_READ_ONLY], [TOOL_UNANNOTATED], [TOOL_ANNOTATION_TITLE_ONLY]]
    )
    async with client_for(server) as client:
        await client.initialize()
        tools = await client.list_tools()

    assert [tool.name for tool in tools] == ["search_docs", "delete_record", "rebuild_index"]


async def test_paging_stops_at_the_page_cap() -> None:
    # Every page advertises another, so only the cap can end the loop. A truncated read must not
    # be reported as a complete one — the caller would sweep everything past the cap.
    server = FakeMcpServer(tool_pages=[[TOOL_READ_ONLY]] * (MAX_TOOL_PAGES + 10))
    with pytest.raises(McpToolListTruncatedError):
        async with client_for(server) as client:
            await client.initialize()
            await client.list_tools()


async def test_a_truncated_tool_list_exempts_tools_from_the_sweep() -> None:
    server = FakeMcpServer(tool_pages=[[TOOL_READ_ONLY]] * (MAX_TOOL_PAGES + 10))
    async with client_for(server) as client:
        snapshot = await McpToolCollector(
            client, tenant_id="tenant-test", endpoint_id="mcpEndpoint:1"
        ).collect()

    assert snapshot.tools == []
    assert snapshot.incomplete_types == {"observedMcpTool"}
    assert snapshot.errors


async def test_absent_annotations_are_recorded_as_absent_not_as_false() -> None:
    server = FakeMcpServer(tool_pages=[[TOOL_UNANNOTATED]])
    async with client_for(server) as client:
        await client.initialize()
        tools = await client.list_tools()

    tool = tools[0]
    assert tool.had_annotations is False
    # The specification defaults destructiveHint and openWorldHint to true. Recording them as
    # False here would invent a safety claim the server never made.
    assert tool.destructive_hint is None
    assert tool.open_world_hint is None
    assert tool.read_only_hint is None


async def test_stated_annotations_are_preserved_exactly() -> None:
    server = FakeMcpServer(tool_pages=[[TOOL_READ_ONLY]])
    async with client_for(server) as client:
        await client.initialize()
        tools = await client.list_tools()

    tool = tools[0]
    assert tool.had_annotations is True
    assert tool.read_only_hint is True
    assert tool.open_world_hint is False
    assert tool.destructive_hint is None
    assert tool.input_schema == {"type": "object", "properties": {"query": {"type": "string"}}}
    assert tool.output_schema is not None


async def test_display_name_follows_title_then_annotation_title_then_name() -> None:
    server = FakeMcpServer(
        tool_pages=[[TOOL_READ_ONLY, TOOL_ANNOTATION_TITLE_ONLY, TOOL_UNANNOTATED]]
    )
    async with client_for(server) as client:
        await client.initialize()
        tools = await client.list_tools()

    assert [tool.display_name for tool in tools] == [
        "Search documents",
        "Rebuild the index",
        "delete_record",
    ]


async def test_unsupported_protocol_revision_is_reported_as_such() -> None:
    # The stateless era. MOSAIC implements the handshake era only.
    server = FakeMcpServer(protocol_version="2026-07-28")
    with pytest.raises(McpUnsupportedProtocolError) as error:
        async with client_for(server) as client:
            await client.initialize()

    assert error.value.details["negotiated"] == "2026-07-28"


async def test_streamable_rejection_is_reported_as_an_unsupported_transport() -> None:
    server = FakeMcpServer(reject_streamable_post=True)
    with pytest.raises(McpUnsupportedTransportError):
        async with client_for(server) as client:
            await client.initialize()


async def test_challenge_details_are_kept_so_401_is_not_reported_as_unreachable() -> None:
    server = FakeMcpServer(unauthorized=True)
    with pytest.raises(McpAuthorizationRequiredError) as error:
        async with client_for(server) as client:
            await client.initialize()

    assert error.value.scheme == "Bearer"
    assert error.value.scope == "mcp.read"
    assert error.value.resource_metadata_url == (
        "https://mcp.example.com/.well-known/oauth-protected-resource"
    )


async def test_a_transport_failure_is_reported_as_unreachable() -> None:
    def explode(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    http = httpx.AsyncClient(transport=httpx.MockTransport(explode))
    with pytest.raises(McpUnreachableError):
        async with McpClient(url=URL, http=http) as client:
            await client.initialize()


async def test_authorization_header_is_sent_on_every_request() -> None:
    server = FakeMcpServer()
    client = McpClient(url=URL, http=build_http_client(server), authorization="Bearer secret")
    async with client:
        await client.initialize()
        await client.list_tools()

    assert server.header_on("initialize", "authorization") == "Bearer secret"
    assert server.header_on("tools/list", "authorization") == "Bearer secret"


async def test_session_is_deleted_on_close_and_a_405_is_still_success() -> None:
    server = FakeMcpServer(delete_status=405)
    async with client_for(server) as client:
        await client.initialize()

    assert server.saw_delete() is True


async def test_no_delete_is_sent_when_no_session_was_issued() -> None:
    server = FakeMcpServer(issue_session=False)
    async with client_for(server) as client:
        session = await client.initialize()

    assert session.session_managed is False
    assert server.saw_delete() is False


async def test_collector_skips_tools_list_when_the_capability_is_absent() -> None:
    server = FakeMcpServer(supports_tools=False)
    async with client_for(server) as client:
        snapshot = await McpToolCollector(
            client, tenant_id="tenant-test", endpoint_id="mcpEndpoint:1"
        ).collect()

    assert snapshot.tools == []
    # Not a failure: a server with no tools capability is a fact, so nothing is exempted from the
    # sweep and no error is recorded.
    assert snapshot.errors == []
    assert snapshot.incomplete_types == set()
    assert "tools/list" not in server.rpc_methods()
    assert snapshot.capabilities().supports_tools == "unavailable"


async def test_collector_exempts_tools_from_the_sweep_when_the_read_fails() -> None:
    server = FakeMcpServer(tools_error={"code": -32603, "message": "backend exploded"})
    async with client_for(server) as client:
        snapshot = await McpToolCollector(
            client, tenant_id="tenant-test", endpoint_id="mcpEndpoint:1"
        ).collect()

    assert snapshot.tools == []
    assert snapshot.incomplete_types == {"observedMcpTool"}
    assert "backend exploded" in snapshot.errors[0]


async def test_collector_reinitializes_once_when_the_session_expires() -> None:
    server = FakeMcpServer(expire_session_on_tools=True)
    async with client_for(server) as client:
        snapshot = await McpToolCollector(
            client, tenant_id="tenant-test", endpoint_id="mcpEndpoint:1"
        ).collect()

    assert [tool.name for tool in snapshot.tools] == ["search_docs", "delete_record"]
    assert server.rpc_methods().count("initialize") == 2
    # The specification requires the new session to be started *without* the dead session ID.
    assert server.headers_on("initialize", "mcp-session-id") == [None, None]


async def test_collector_summary_counts_only_what_the_server_stated() -> None:
    server = FakeMcpServer(
        tool_pages=[[TOOL_READ_ONLY, TOOL_UNANNOTATED, TOOL_ANNOTATION_TITLE_ONLY]]
    )
    async with client_for(server) as client:
        snapshot = await McpToolCollector(
            client, tenant_id="tenant-test", endpoint_id="mcpEndpoint:1"
        ).collect()

    summary = snapshot.summary()
    assert summary.tools == 3
    assert summary.read_only_tools == 1
    # ``rebuild_index`` carries an annotations block holding only a title. A title is a label, not
    # a behavioural claim, so it counts as unannotated alongside ``delete_record``.
    assert summary.unannotated_tools == 2


async def test_collector_gives_tools_stable_identities_across_snapshots() -> None:
    ids: list[str] = []
    for _ in range(2):
        server = FakeMcpServer(tool_pages=[[TOOL_READ_ONLY]])
        async with client_for(server) as client:
            snapshot = await McpToolCollector(
                client, tenant_id="tenant-test", endpoint_id="mcpEndpoint:1"
            ).collect()
        ids.append(snapshot.tools[0].id)

    assert ids[0] == ids[1]


@pytest.mark.parametrize(
    "url",
    [
        "https://169.254.169.254/mcp",
        "https://127.0.0.1/mcp",
        "https://localhost/mcp",
        "https://10.1.2.3/mcp",
        "https://192.168.0.5/mcp",
        "https://[::1]/mcp",
        "https://[fd00::1]/mcp",
    ],
)
def test_guard_refuses_private_and_metadata_addresses(url: str) -> None:
    with pytest.raises(ValidationError):
        admit_mcp_url(url)


def test_guard_refuses_plaintext_http() -> None:
    with pytest.raises(ValidationError):
        admit_mcp_url("http://mcp.example.com/mcp")


def test_guard_refuses_a_non_http_scheme() -> None:
    with pytest.raises(ValidationError):
        admit_mcp_url("file:///etc/passwd")


def test_guard_refuses_credentials_embedded_in_the_url() -> None:
    # Silently stripping them would store and echo the credential while never sending it.
    with pytest.raises(ValidationError):
        admit_mcp_url("https://alice:hunter2@mcp.example.com/mcp")


def test_guard_allows_a_public_https_endpoint() -> None:
    assert admit_mcp_url(URL) == URL


def test_guard_can_be_relaxed_for_local_development() -> None:
    assert admit_mcp_url(
        "http://localhost:8080/mcp", require_https=False, allow_private=True
    ).endswith("/mcp")
