"""Reuse the unchanged M-agent settings, error types and privacy-preserving logging helpers."""

import sys
from importlib.util import find_spec
from pathlib import Path

# The image copies only these shared modules, not the E2E server. A source checkout
# resolves the same modules without requiring installation of an unrelated test package.
if find_spec("m_agent") is None:
    helpers = Path(__file__).resolve().parents[3] / "e2e" / "mcp-servers" / "m-agent"
    if not helpers.is_dir():
        raise ImportError("M-agent helpers are missing; use the repository or the runtime image.")
    sys.path.insert(0, str(helpers))

from m_agent.gateway import (
    COST_CENTER_HEADER,
    ON_BEHALF_HEADER,
    ModelCallError,
    ModelSettings,
)
from m_agent.logs import RequestLog, configure_logging, logger, tool_call

__all__ = [
    "COST_CENTER_HEADER",
    "ON_BEHALF_HEADER",
    "ModelCallError",
    "ModelSettings",
    "RequestLog",
    "configure_logging",
    "logger",
    "tool_call",
]
