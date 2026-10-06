"""M-protected: M-tools' three tools on Azure Functions' MCP extension, for journey M1 to M10.

The MCP extension serves streamable HTTP at /runtime/webhooks/mcp. host.json sets the extension's
webhook authorization level to Anonymous, so the Functions system key isn't also required. Access
is decided before any of this code runs, by App Service Authentication (Easy Auth). The deployment
kit configures it to admit only an Entra token for this server's audience, obtained by API
Management's managed identity or MOSAIC API's.
"""

import json
import logging
import time
from collections.abc import Callable
from typing import Any

import azure.functions as func

import tools

app = func.FunctionApp()


def tool_properties(*properties: tuple[str, str, str]) -> str:
    return json.dumps(
        [
            {
                "propertyName": name,
                "propertyType": kind,
                "description": description,
                "isRequired": True,
            }
            for name, kind, description in properties
        ]
    )


def arguments(context: str) -> dict[str, Any]:
    """The tool call's arguments, from the context the MCP extension passes."""

    payload = json.loads(context)
    values = payload.get("arguments") if isinstance(payload, dict) else None
    return values if isinstance(values, dict) else {}


def run(name: str, call: Callable[[], str]) -> str:
    """Run a tool, logging only its name, outcome and duration."""

    started = time.perf_counter()
    outcome = "error"
    try:
        result = call()
        outcome = "ok"
        return result
    finally:
        duration = round((time.perf_counter() - started) * 1000)
        logging.info("tool=%s outcome=%s duration_ms=%d", name, outcome, duration)


@app.mcp_tool_trigger(
    arg_name="context",
    tool_name="echo",
    description="Return the text unchanged.",
    tool_properties=tool_properties(("text", "string", "The text to return.")),
)
def echo(context: str) -> str:
    return run("echo", lambda: tools.echo(arguments(context).get("text")))


@app.mcp_tool_trigger(
    arg_name="context",
    tool_name="utc_now",
    description=(
        "Return the current UTC time in ISO 8601, to the second, such as 2026-10-05T18:06:46Z."
    ),
    tool_properties=tool_properties(),
)
def utc_now(context: str) -> str:
    return run("utc_now", tools.utc_now)


@app.mcp_tool_trigger(
    arg_name="context",
    tool_name="add",
    description="Return the sum of two numbers.",
    tool_properties=tool_properties(
        ("a", "number", "The first number."), ("b", "number", "The second number.")
    ),
)
def add(context: str) -> str:
    values = arguments(context)
    return run("add", lambda: json.dumps(tools.add(values.get("a"), values.get("b"))))
