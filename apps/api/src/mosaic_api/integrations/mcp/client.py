"""A minimal, read-only MCP client.

MOSAIC connects to a registered MCP server to learn what it offers and nothing else: it performs
``initialize``, sends ``notifications/initialized``, pages ``tools/list``, and ends the session.
There is no ``tools/call`` here, and there is no code path that could add one by accident.

**Protocol era.** The current MCP revision (2026-07-28) is stateless — it removed the handshake,
the session header, and the GET stream. Everything from 2025-11-25 back is the handshake era, and
the handshake era is what API Management speaks, so that is what this implements. A server that
negotiates outside :data:`MCP_SUPPORTED_PROTOCOL_VERSIONS` raises
:class:`McpUnsupportedProtocolError`, which callers record as a capability rather than a fault.

**Transport.** Streamable HTTP only. A server that rejects the initialize POST with 400, 404, or
405 is speaking the deprecated HTTP+SSE transport; that is reported as an unsupported transport
rather than half-supported. Note this is separate from the *response body*: a Streamable HTTP POST
may legally answer with either ``application/json`` or ``text/event-stream``, and both are handled.
"""

import json
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self

import httpx
import structlog

from mosaic_api.domain import (
    MCP_CLIENT_NAME,
    MCP_PROTOCOL_VERSION,
    MCP_PROTOCOL_VERSION_HEADER_MINIMUM,
    MCP_SUPPORTED_PROTOCOL_VERSIONS,
)
from mosaic_api.errors import DomainError

logger = structlog.get_logger()

JSON_RPC_VERSION = "2.0"
ACCEPT_HEADER = "application/json, text/event-stream"

# A misbehaving or hostile server must not be able to exhaust the API container. Every response is
# read against a byte budget, and paging stops at a fixed number of pages regardless of cursors.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_TOOL_PAGES = 50

METHOD_NOT_FOUND = -32601

# Status codes the specification names as "this is not a Streamable HTTP endpoint; try the
# deprecated HTTP+SSE transport". MOSAIC does not implement that transport.
_LEGACY_TRANSPORT_STATUSES: frozenset[int] = frozenset({400, 404, 405})


class McpError(DomainError):
    """Base for every failure talking to a registered MCP server."""

    status_code = 502
    code = "mcp_error"


class McpUnreachableError(McpError):
    code = "mcp_unreachable"


class McpProtocolError(McpError):
    """The server answered, but not with something this client can read."""

    code = "mcp_protocol_error"


class McpUnsupportedProtocolError(McpError):
    """The server negotiated a revision MOSAIC does not implement."""

    status_code = 501
    code = "mcp_unsupported_protocol"


class McpUnsupportedTransportError(McpError):
    """The server does not speak Streamable HTTP."""

    status_code = 501
    code = "mcp_unsupported_transport"


class McpAuthorizationRequiredError(McpError):
    """The server refused the credential MOSAIC presented, or wanted one it was not given."""

    status_code = 401
    code = "mcp_authorization_required"

    def __init__(
        self,
        message: str,
        *,
        scheme: str | None = None,
        resource_metadata_url: str | None = None,
        scope: str | None = None,
    ) -> None:
        self.scheme = scheme
        self.resource_metadata_url = resource_metadata_url
        self.scope = scope
        super().__init__(
            message,
            details={
                key: value
                for key, value in (
                    ("scheme", scheme),
                    ("resourceMetadataUrl", resource_metadata_url),
                    ("scope", scope),
                )
                if value
            },
        )


class McpToolListTruncatedError(McpError):
    """The page cap was reached while the server was still offering a cursor.

    Raised rather than returning a partial list, because a truncated read that looked successful
    would let the sweep delete every tool beyond the cap.
    """

    code = "mcp_tool_list_truncated"


class McpSessionExpiredError(McpError):
    """The server ended the session; the caller must initialize again."""

    status_code = 409
    code = "mcp_session_expired"


class McpJsonRpcError(McpError):
    """The server answered with a JSON-RPC error object."""

    code = "mcp_rpc_error"

    def __init__(self, message: str, *, rpc_code: int | None = None, data: object = None) -> None:
        self.rpc_code = rpc_code
        super().__init__(
            message,
            details={
                key: value
                for key, value in (("rpcCode", rpc_code), ("data", data))
                if value is not None
            },
        )


@dataclass(frozen=True)
class McpSession:
    """What ``initialize`` told MOSAIC about the server."""

    protocol_version: str
    server_name: str | None = None
    server_title: str | None = None
    server_version: str | None = None
    instructions: str | None = None
    supports_tools: bool = False
    session_managed: bool = False


@dataclass(frozen=True)
class McpToolDefinition:
    """One tool exactly as the server declared it.

    ``annotations`` keeps every hint tri-state. The specification defaults ``destructiveHint`` and
    ``openWorldHint`` to *true*, so an absent hint must never be flattened into a value here.
    """

    name: str
    title: str | None = None
    description: str | None = None
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    annotation_title: str | None = None
    read_only_hint: bool | None = None
    destructive_hint: bool | None = None
    idempotent_hint: bool | None = None
    open_world_hint: bool | None = None
    had_annotations: bool = False

    @property
    def display_name(self) -> str:
        """Per the specification's precedence: ``title``, then ``annotations.title``, then name."""

        return self.title or self.annotation_title or self.name


def _parse_www_authenticate(header: str | None) -> tuple[str | None, str | None, str | None]:
    """Pull the scheme, ``resource_metadata``, and ``scope`` out of a challenge header."""

    if not header:
        return None, None, None
    scheme, _, rest = header.strip().partition(" ")
    params: dict[str, str] = {}
    for chunk in rest.split(","):
        key, sep, value = chunk.strip().partition("=")
        if sep:
            params[key.strip().casefold()] = value.strip().strip('"')
    return (scheme or None), params.get("resource_metadata"), params.get("scope")


def _as_optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _as_optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _as_optional_object(value: object) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


@dataclass
class _Budget:
    """Bytes remaining across one response."""

    remaining: int = MAX_RESPONSE_BYTES

    def spend(self, amount: int) -> None:
        self.remaining -= amount
        if self.remaining < 0:
            raise McpProtocolError(
                "This MCP server returned more data than MOSAIC will read in one response.",
                details={"limitBytes": MAX_RESPONSE_BYTES},
            )


@dataclass
class McpClient:
    """One read-only conversation with one MCP server.

    Not reusable across servers: it holds the negotiated protocol version and session ID.
    """

    url: str
    http: httpx.AsyncClient
    authorization: str | None = None
    offered_protocol_version: str = MCP_PROTOCOL_VERSION
    _session_id: str | None = field(default=None, init=False)
    _negotiated_version: str | None = field(default=None, init=False)
    _request_id: int = field(default=0, init=False)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _headers(self, *, include_protocol_version: bool) -> dict[str, str]:
        headers = {"Accept": ACCEPT_HEADER, "Content-Type": "application/json"}
        if self.authorization:
            headers["Authorization"] = self.authorization
        # The header was introduced in 2025-06-18. Sending it to a server that negotiated an
        # earlier revision invites a 400 for a header that revision never defined.
        if (
            include_protocol_version
            and self._negotiated_version
            and self._negotiated_version >= MCP_PROTOCOL_VERSION_HEADER_MINIMUM
        ):
            headers["MCP-Protocol-Version"] = self._negotiated_version
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    async def _post(
        self, payload: dict[str, Any], *, expect_response: bool, initializing: bool = False
    ) -> dict[str, Any] | None:
        request_id = payload.get("id")
        try:
            async with self.http.stream(
                "POST",
                self.url,
                content=json.dumps(payload).encode("utf-8"),
                headers=self._headers(include_protocol_version=not initializing),
                # Never follow a redirect: it would carry the Authorization header to a host the
                # operator never registered.
                follow_redirects=False,
            ) as response:
                if initializing:
                    self._capture_session_id(response)
                self._raise_for_status(response, initializing=initializing)
                if not expect_response:
                    return None
                return await self._read_result(response, request_id)
        except httpx.HTTPError as error:
            raise McpUnreachableError(
                "MOSAIC could not reach this MCP server.",
                details={"url": self.url, "reason": str(error)},
            ) from error

    def _capture_session_id(self, response: httpx.Response) -> None:
        # httpx header lookup is case-insensitive, which the specification requires: 2025-06-18
        # writes ``Mcp-Session-Id`` and 2025-11-25 writes ``MCP-Session-Id``.
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id

    def _raise_for_status(self, response: httpx.Response, *, initializing: bool) -> None:
        status = response.status_code
        if status == 401:
            scheme, resource_metadata, scope = _parse_www_authenticate(
                response.headers.get("www-authenticate")
            )
            raise McpAuthorizationRequiredError(
                "This MCP server requires authorization that MOSAIC was not given.",
                scheme=scheme,
                resource_metadata_url=resource_metadata,
                scope=scope,
            )
        if status == 403:
            scheme, resource_metadata, scope = _parse_www_authenticate(
                response.headers.get("www-authenticate")
            )
            raise McpAuthorizationRequiredError(
                "This MCP server rejected MOSAIC's credential as insufficient.",
                scheme=scheme,
                resource_metadata_url=resource_metadata,
                scope=scope,
            )
        if initializing and status in _LEGACY_TRANSPORT_STATUSES:
            raise McpUnsupportedTransportError(
                "This MCP server does not accept Streamable HTTP requests. It is most likely on "
                "the deprecated HTTP+SSE transport, which MOSAIC does not implement.",
                details={"url": self.url, "status": status},
            )
        if status == 404:
            # Post-initialize only: the server ended the session. The specification requires the
            # client to start a *new* one without a session ID attached, so the dead ID is dropped
            # here rather than being re-sent on the next initialize.
            self._session_id = None
            raise McpSessionExpiredError(
                "This MCP server ended the session.", details={"url": self.url}
            )
        if status >= 400:
            raise McpProtocolError(
                "This MCP server returned an error MOSAIC could not interpret.",
                details={"url": self.url, "status": status},
            )

    async def _read_result(self, response: httpx.Response, request_id: object) -> dict[str, Any]:
        content_type = response.headers.get("content-type", "").split(";")[0].strip().casefold()
        if content_type == "text/event-stream":
            message = await self._read_sse_message(response, request_id)
        else:
            message = await self._read_json_message(response)
        return self._unwrap(message)

    async def _read_json_message(self, response: httpx.Response) -> dict[str, Any]:
        budget = _Budget()
        chunks: list[bytes] = []
        async for chunk in response.aiter_bytes():
            budget.spend(len(chunk))
            chunks.append(chunk)
        body = b"".join(chunks)
        try:
            payload = json.loads(body)
        except ValueError as error:
            raise McpProtocolError(
                "This MCP server returned a body that is not JSON.", details={"url": self.url}
            ) from error
        if not isinstance(payload, dict):
            raise McpProtocolError(
                "This MCP server returned a JSON value that is not a JSON-RPC message.",
                details={"url": self.url},
            )
        return payload

    async def _read_sse_message(
        self, response: httpx.Response, request_id: object
    ) -> dict[str, Any]:
        """Take the first SSE frame carrying the response to ``request_id``.

        The server may legitimately emit unrelated notifications first, so frames that are not the
        awaited response are skipped rather than treated as an error.
        """

        budget = _Budget()
        data_lines: list[str] = []

        def flush() -> dict[str, Any] | None:
            if not data_lines:
                return None
            raw = "\n".join(data_lines)
            data_lines.clear()
            try:
                candidate = json.loads(raw)
            except ValueError:
                return None
            if isinstance(candidate, dict) and candidate.get("id") == request_id:
                return candidate
            return None

        async for line in response.aiter_lines():
            budget.spend(len(line) + 1)
            line = line.rstrip("\r")
            if not line:
                found = flush()
                if found is not None:
                    return found
                continue
            if line.startswith(":"):
                continue
            name, separator, value = line.partition(":")
            if not separator:
                continue
            if value.startswith(" "):
                value = value[1:]
            if name == "data":
                data_lines.append(value)
        found = flush()
        if found is not None:
            return found
        raise McpProtocolError(
            "This MCP server closed its event stream without answering.",
            details={"url": self.url},
        )

    def _unwrap(self, message: dict[str, Any]) -> dict[str, Any]:
        error = message.get("error")
        if isinstance(error, dict):
            raise McpJsonRpcError(
                _as_optional_str(error.get("message")) or "This MCP server returned an error.",
                rpc_code=error.get("code") if isinstance(error.get("code"), int) else None,
                data=error.get("data"),
            )
        result = message.get("result")
        if not isinstance(result, dict):
            raise McpProtocolError(
                "This MCP server returned a message with no result.", details={"url": self.url}
            )
        return result

    async def initialize(self) -> McpSession:
        result = await self._post(
            {
                "jsonrpc": JSON_RPC_VERSION,
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": self.offered_protocol_version,
                    # Empty on purpose. Declaring roots, sampling, or elicitation would promise to
                    # service server-initiated requests MOSAIC has no intention of answering.
                    "capabilities": {},
                    "clientInfo": {"name": MCP_CLIENT_NAME, "version": "1.0.0"},
                },
            },
            expect_response=True,
            initializing=True,
        )
        assert result is not None
        negotiated = _as_optional_str(result.get("protocolVersion"))
        if negotiated is None:
            raise McpProtocolError(
                "This MCP server did not state a protocol version.", details={"url": self.url}
            )
        if negotiated not in MCP_SUPPORTED_PROTOCOL_VERSIONS:
            raise McpUnsupportedProtocolError(
                f"This MCP server speaks protocol revision {negotiated}, which MOSAIC does not "
                "implement.",
                details={
                    "negotiated": negotiated,
                    "supported": sorted(MCP_SUPPORTED_PROTOCOL_VERSIONS),
                },
            )
        self._negotiated_version = negotiated
        capabilities = _as_optional_object(result.get("capabilities")) or {}
        server_info = _as_optional_object(result.get("serverInfo")) or {}
        return McpSession(
            protocol_version=negotiated,
            server_name=_as_optional_str(server_info.get("name")),
            server_title=_as_optional_str(server_info.get("title")),
            server_version=_as_optional_str(server_info.get("version")),
            instructions=_as_optional_str(result.get("instructions")),
            supports_tools="tools" in capabilities,
            session_managed=self._session_id is not None,
        )

    async def notify_initialized(self) -> None:
        await self._post(
            {"jsonrpc": JSON_RPC_VERSION, "method": "notifications/initialized"},
            expect_response=False,
        )

    async def list_tools(self) -> list[McpToolDefinition]:
        """Every tool the server declares, following ``nextCursor`` to a fixed page cap."""

        tools: list[McpToolDefinition] = []
        cursor: str | None = None
        for _ in range(MAX_TOOL_PAGES):
            params = {"cursor": cursor} if cursor else {}
            result = await self._post(
                {
                    "jsonrpc": JSON_RPC_VERSION,
                    "id": self._next_id(),
                    "method": "tools/list",
                    "params": params,
                },
                expect_response=True,
            )
            assert result is not None
            entries = result.get("tools")
            if isinstance(entries, list):
                tools.extend(_tool_from(entry) for entry in entries if isinstance(entry, dict))
            # Cursors are opaque and must never be persisted across sessions.
            cursor = _as_optional_str(result.get("nextCursor"))
            if not cursor:
                return tools
        # The server still has more to give. Returning what was collected would report an
        # incomplete read as a complete one, and the caller would sweep everything past the cap.
        raise McpToolListTruncatedError(
            "This MCP server offered more tool pages than MOSAIC will read in one sync.",
            details={"url": self.url, "pages": MAX_TOOL_PAGES, "read": len(tools)},
        )

    async def close(self) -> None:
        """Best-effort session termination.

        The specification makes DELETE a SHOULD and lets a server refuse with 405, so every
        outcome here is success as far as the caller is concerned.
        """

        if not self._session_id:
            return
        try:
            await self.http.request(
                "DELETE",
                self.url,
                headers=self._headers(include_protocol_version=True),
                follow_redirects=False,
            )
        except httpx.HTTPError:
            logger.debug("mcp_session_delete_failed", url=self.url)
        finally:
            self._session_id = None


def _tool_from(entry: dict[str, Any]) -> McpToolDefinition:
    name = _as_optional_str(entry.get("name")) or ""
    annotations = _as_optional_object(entry.get("annotations"))
    hints = annotations or {}
    return McpToolDefinition(
        name=name,
        title=_as_optional_str(entry.get("title")),
        description=_as_optional_str(entry.get("description")),
        input_schema=_as_optional_object(entry.get("inputSchema")),
        output_schema=_as_optional_object(entry.get("outputSchema")),
        annotation_title=_as_optional_str(hints.get("title")),
        read_only_hint=_as_optional_bool(hints.get("readOnlyHint")),
        destructive_hint=_as_optional_bool(hints.get("destructiveHint")),
        idempotent_hint=_as_optional_bool(hints.get("idempotentHint")),
        open_world_hint=_as_optional_bool(hints.get("openWorldHint")),
        had_annotations=annotations is not None,
    )
