"""host.json: the pinned MCP extension, served without the Functions system key, and quiet logs."""

import json
from pathlib import Path

HOST = json.loads((Path(__file__).resolve().parents[1] / "host.json").read_text(encoding="utf-8"))


def test_the_extension_bundle_is_pinned_to_the_4_38_line() -> None:
    # Bundle 4.38.1 carries the MCP extension 1.5.0, which serves streamable HTTP at
    # /runtime/webhooks/mcp. The README says how this was confirmed.
    assert HOST["extensionBundle"] == {
        "id": "Microsoft.Azure.Functions.ExtensionBundle",
        "version": "[4.38.1, 4.39.0)",
    }


def test_the_mcp_webhook_takes_no_functions_key() -> None:
    # MOSAIC's publication attaches only the managed identity's token. Easy Auth checks it.
    assert HOST["extensions"]["mcp"]["system"] == {"webhookAuthorizationLevel": "Anonymous"}


def test_only_the_tools_own_log_lines_are_kept_at_information() -> None:
    levels = HOST["logging"]["logLevel"]
    assert levels.pop("default") == "Warning"
    assert levels == {f"Function.{name}.User": "Information" for name in ("echo", "utc_now", "add")}
