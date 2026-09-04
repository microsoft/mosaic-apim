"""Read-only Model Context Protocol integration.

MOSAIC connects to a registered MCP server to record what it offers. It performs the handshake,
lists tools, and ends the session. It never calls a tool.
"""

from mosaic_api.integrations.mcp.client import (
    McpAuthorizationRequiredError,
    McpClient,
    McpError,
    McpJsonRpcError,
    McpProtocolError,
    McpSession,
    McpSessionExpiredError,
    McpToolDefinition,
    McpToolListTruncatedError,
    McpUnreachableError,
    McpUnsupportedProtocolError,
    McpUnsupportedTransportError,
)
from mosaic_api.integrations.mcp.credentials import (
    EntraTokenProvider,
    KeyVaultSecretReader,
    scope_for_audience,
)
from mosaic_api.integrations.mcp.discovery import (
    TOOL_ENTITY_TYPE,
    McpDiscoverySnapshot,
    McpToolCollector,
)
from mosaic_api.integrations.mcp.guard import admit_mcp_url

__all__ = [
    "TOOL_ENTITY_TYPE",
    "EntraTokenProvider",
    "KeyVaultSecretReader",
    "McpAuthorizationRequiredError",
    "McpClient",
    "McpDiscoverySnapshot",
    "McpError",
    "McpJsonRpcError",
    "McpProtocolError",
    "McpSession",
    "McpSessionExpiredError",
    "McpToolCollector",
    "McpToolDefinition",
    "McpToolListTruncatedError",
    "McpUnreachableError",
    "McpUnsupportedProtocolError",
    "McpUnsupportedTransportError",
    "admit_mcp_url",
    "scope_for_audience",
]
