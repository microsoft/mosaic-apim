"""An in-process MCP server that speaks Streamable HTTP.

Behaviour is configurable per test so the client can be driven through every branch the
specification allows: JSON and SSE response bodies, sessions issued, absent, and expired,
authorization challenges, pagination, a server with no tools capability, a protocol counter-offer,
and a server that refuses ``DELETE``.
"""

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_PROTOCOL_VERSION = "2025-11-25"
SESSION_ID = "session-abc"

TOOL_READ_ONLY: dict[str, Any] = {
    "name": "search_docs",
    "title": "Search documents",
    "description": "Full text search over the corpus.",
    "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
    "outputSchema": {"type": "object", "properties": {"hits": {"type": "array"}}},
    "annotations": {"readOnlyHint": True, "openWorldHint": False},
}
# Deliberately annotation-free: the specification defaults destructiveHint and openWorldHint to
# true, so a tool that says nothing must never be recorded as if it claimed to be safe.
TOOL_UNANNOTATED: dict[str, Any] = {
    "name": "delete_record",
    "description": "Removes a record.",
    "inputSchema": {"type": "object"},
}
TOOL_ANNOTATION_TITLE_ONLY: dict[str, Any] = {
    "name": "rebuild_index",
    "inputSchema": {"type": "object"},
    # A title is a label, not a behavioural claim, so this tool still counts as unannotated.
    "annotations": {"title": "Rebuild the index"},
}


@dataclass
class RecordedRequest:
    method: str
    rpc_method: str | None
    headers: dict[str, str]


@dataclass
class FakeMcpServer:
    """A configurable MCP server reachable through :class:`httpx.MockTransport`."""

    protocol_version: str = DEFAULT_PROTOCOL_VERSION
    issue_session: bool = True
    respond_with_sse: bool = False
    supports_tools: bool = True
    tool_pages: list[list[dict[str, Any]]] = field(
        default_factory=lambda: [[TOOL_READ_ONLY, TOOL_UNANNOTATED]]
    )
    unauthorized: bool = False
    www_authenticate: str | None = (
        'Bearer resource_metadata="https://mcp.example.com/.well-known/'
        'oauth-protected-resource", scope="mcp.read"'
    )
    reject_streamable_post: bool = False
    expire_session_on_tools: bool = False
    tools_error: dict[str, Any] | None = None
    delete_status: int = 200
    instructions: str | None = "Use search_docs before anything else."

    requests: list[RecordedRequest] = field(default_factory=list)
    _tools_calls: int = 0
    _expired_once: bool = False

    # -- helpers ---------------------------------------------------------------------------

    def _envelope(self, request_id: Any, result: dict[str, Any]) -> httpx.Response:
        message = {"jsonrpc": "2.0", "id": request_id, "result": result}
        headers = {}
        if self.issue_session:
            headers["Mcp-Session-Id"] = SESSION_ID
        if self.respond_with_sse:
            # A comment line and an unrelated notification precede the answer, because the
            # specification permits both and a client must skip them.
            body = (
                ": keep-alive\n\n"
                'data: {"jsonrpc":"2.0","method":"notifications/message"}\n\n'
                f"event: message\ndata: {json.dumps(message)}\n\n"
            )
            headers["Content-Type"] = "text/event-stream"
            return httpx.Response(200, content=body.encode("utf-8"), headers=headers)
        headers["Content-Type"] = "application/json"
        return httpx.Response(200, content=json.dumps(message).encode("utf-8"), headers=headers)

    def _error(self, request_id: Any, code: int, message: str) -> httpx.Response:
        payload = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        return httpx.Response(
            200,
            content=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

    # -- transport -------------------------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        headers = {key.lower(): value for key, value in request.headers.items()}

        if request.method == "DELETE":
            self.requests.append(RecordedRequest("DELETE", None, headers))
            return httpx.Response(self.delete_status)

        payload = json.loads(request.content or b"{}")
        rpc_method = payload.get("method")
        request_id = payload.get("id")
        self.requests.append(RecordedRequest(request.method, rpc_method, headers))

        if self.unauthorized:
            response_headers = (
                {"WWW-Authenticate": self.www_authenticate} if self.www_authenticate else {}
            )
            return httpx.Response(401, headers=response_headers)

        if rpc_method == "initialize":
            if self.reject_streamable_post:
                return httpx.Response(405)
            capabilities: dict[str, Any] = {"logging": {}}
            if self.supports_tools:
                capabilities["tools"] = {"listChanged": False}
            return self._envelope(
                request_id,
                {
                    "protocolVersion": self.protocol_version,
                    "capabilities": capabilities,
                    "serverInfo": {
                        "name": "contoso-mcp",
                        "title": "Contoso MCP",
                        "version": "3.1.0",
                    },
                    **({"instructions": self.instructions} if self.instructions else {}),
                },
            )

        if rpc_method == "notifications/initialized":
            return httpx.Response(202)

        if rpc_method == "tools/list":
            if self.expire_session_on_tools and not self._expired_once:
                self._expired_once = True
                return httpx.Response(404)
            if self.tools_error is not None:
                return self._error(
                    request_id, self.tools_error["code"], self.tools_error["message"]
                )
            cursor = (payload.get("params") or {}).get("cursor")
            index = int(cursor.removeprefix("page-")) if isinstance(cursor, str) else 0
            page = self.tool_pages[index] if index < len(self.tool_pages) else []
            result: dict[str, Any] = {"tools": page}
            if index + 1 < len(self.tool_pages):
                result["nextCursor"] = f"page-{index + 1}"
            self._tools_calls += 1
            return self._envelope(request_id, result)

        return httpx.Response(400)

    # -- assertions ------------------------------------------------------------------------

    def rpc_methods(self) -> list[str | None]:
        return [entry.rpc_method for entry in self.requests if entry.method == "POST"]

    def header_on(self, rpc_method: str, name: str) -> str | None:
        for entry in self.requests:
            if entry.rpc_method == rpc_method:
                return entry.headers.get(name.lower())
        return None

    def headers_on(self, rpc_method: str, name: str) -> list[str | None]:
        return [
            entry.headers.get(name.lower())
            for entry in self.requests
            if entry.rpc_method == rpc_method
        ]

    def saw_delete(self) -> bool:
        return any(entry.method == "DELETE" for entry in self.requests)


def build_http_client(server: FakeMcpServer) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
