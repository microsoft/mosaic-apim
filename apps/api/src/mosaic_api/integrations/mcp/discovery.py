"""Turns one MCP conversation into observed state.

The sibling of :class:`~mosaic_api.integrations.aoai.inventory.ModelInventoryCollector`: it runs
the handshake, pages the tools, and returns entities plus the set of entity types it could *not*
read. That last part is what keeps a failed read from being mistaken for a deletion — the sweep
exempts any type listed in ``incomplete_types``.
"""

from dataclasses import dataclass, field

import structlog

from mosaic_api.domain import (
    CapabilitySupport,
    McpEndpointCapabilities,
    McpInventorySummary,
    McpTransportType,
    deterministic_id,
    new_id,
)
from mosaic_api.integrations.mcp.client import (
    METHOD_NOT_FOUND,
    McpClient,
    McpJsonRpcError,
    McpSession,
    McpSessionExpiredError,
    McpToolDefinition,
    McpToolListTruncatedError,
)
from mosaic_api.observed import McpToolAnnotations, ObservedEndpointEntity, ObservedMcpTool

logger = structlog.get_logger()

TOOL_ENTITY_TYPE = "observedMcpTool"


@dataclass
class McpDiscoverySnapshot:
    snapshot_id: str
    session: McpSession
    tools: list[ObservedMcpTool] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    incomplete_types: set[str] = field(default_factory=set)

    def entities(self) -> list[ObservedEndpointEntity]:
        return list(self.tools)

    def summary(self) -> McpInventorySummary:
        return McpInventorySummary(
            tools=len(self.tools),
            # Counts only what a server actually claimed. There is no destructive count, because
            # ``destructiveHint`` defaults to true and a count built on that default would report
            # silence as a claim.
            read_only_tools=sum(
                1
                for tool in self.tools
                if tool.annotations is not None and tool.annotations.read_only_hint is True
            ),
            unannotated_tools=sum(
                1
                for tool in self.tools
                if tool.annotations is None or not tool.annotations.stated_anything()
            ),
        )

    def capabilities(self) -> McpEndpointCapabilities:
        return McpEndpointCapabilities(
            protocol_version=self.session.protocol_version,
            transport_type=McpTransportType.STREAMABLE,
            server_name=self.session.server_name,
            server_title=self.session.server_title,
            server_version=self.session.server_version,
            instructions=self.session.instructions,
            supports_tools=(
                CapabilitySupport.AVAILABLE
                if self.session.supports_tools
                else CapabilitySupport.UNAVAILABLE
            ),
            session_managed=self.session.session_managed,
        )


def observed_tool(
    tool: McpToolDefinition, *, tenant_id: str, endpoint_id: str, snapshot_id: str
) -> ObservedMcpTool:
    annotations = (
        McpToolAnnotations(
            title=tool.annotation_title,
            read_only_hint=tool.read_only_hint,
            destructive_hint=tool.destructive_hint,
            idempotent_hint=tool.idempotent_hint,
            open_world_hint=tool.open_world_hint,
        )
        if tool.had_annotations
        else None
    )
    return ObservedMcpTool(
        # Deterministic so a tool keeps its identity across syncs, the way a deployment does.
        id=deterministic_id("mcpTool", tenant_id, endpoint_id, tool.name),
        tenant_id=tenant_id,
        endpoint_id=endpoint_id,
        snapshot_id=snapshot_id,
        name=tool.name,
        display_name=tool.display_name,
        title=tool.title,
        description=tool.description,
        input_schema=tool.input_schema,
        output_schema=tool.output_schema,
        annotations=annotations,
    )


class McpToolCollector:
    """Runs one discovery pass against one registered MCP server."""

    def __init__(self, client: McpClient, *, tenant_id: str, endpoint_id: str) -> None:
        self._client = client
        self._tenant_id = tenant_id
        self._endpoint_id = endpoint_id

    async def collect(self) -> McpDiscoverySnapshot:
        session = await self._handshake()
        snapshot = McpDiscoverySnapshot(snapshot_id=new_id("mcpsnapshot"), session=session)

        if not session.supports_tools:
            # Calling an unnegotiated capability violates a client MUST. "This server offers no
            # tools" is a fact, not a failure, so nothing is marked incomplete.
            logger.info("mcp_server_declares_no_tools", endpoint_id=self._endpoint_id)
            return snapshot

        try:
            definitions = await self._list_tools()
        except McpToolListTruncatedError as error:
            # Keep the previous documents rather than writing a partial list: a truncated read is
            # a read MOSAIC could not complete, not a smaller catalogue.
            snapshot.errors.append(error.message)
            snapshot.incomplete_types.add(TOOL_ENTITY_TYPE)
            return snapshot
        except McpJsonRpcError as error:
            if error.rpc_code == METHOD_NOT_FOUND:
                snapshot.errors.append(
                    "This server advertised tools but does not implement tools/list."
                )
            else:
                snapshot.errors.append(f"Reading tools failed: {error.message}")
            snapshot.incomplete_types.add(TOOL_ENTITY_TYPE)
            return snapshot

        snapshot.tools = [
            observed_tool(
                definition,
                tenant_id=self._tenant_id,
                endpoint_id=self._endpoint_id,
                snapshot_id=snapshot.snapshot_id,
            )
            for definition in definitions
            if definition.name
        ]
        return snapshot

    async def _handshake(self) -> McpSession:
        session = await self._client.initialize()
        await self._client.notify_initialized()
        return session

    async def _list_tools(self) -> list[McpToolDefinition]:
        try:
            return await self._client.list_tools()
        except McpSessionExpiredError:
            # A 404 carrying a session ID means the server ended the session, and the
            # specification requires the client to start a new one. Retried exactly once so a
            # server that always 404s cannot spin.
            logger.info("mcp_session_expired_reinitializing", endpoint_id=self._endpoint_id)
            await self._handshake()
            return await self._client.list_tools()
