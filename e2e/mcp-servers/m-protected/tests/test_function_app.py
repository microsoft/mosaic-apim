"""The function app as the Functions host indexes it, called as the MCP extension calls it."""

import json
import logging
import re
from typing import Any

import pytest

import function_app
import tools

FUNCTIONS = {
    function.get_function_name(): function for function in function_app.app.get_functions()
}


def trigger(name: str) -> dict[str, Any]:
    [binding] = json.loads(FUNCTIONS[name].get_function_json())["bindings"]
    return binding


def call(name: str, arguments: dict[str, Any]) -> str:
    context = json.dumps({"name": name, "arguments": arguments})
    return FUNCTIONS[name].get_user_function()(context)


def test_the_app_has_one_mcp_tool_trigger_per_tool() -> None:
    assert set(FUNCTIONS) == {"echo", "utc_now", "add"}
    for name in FUNCTIONS:
        binding = trigger(name)
        assert binding["type"] == "mcpToolTrigger"
        assert binding["direction"] == "IN"
        assert binding["name"] == "context"
        assert binding["toolName"] == name
        assert binding["description"]


def test_each_tool_declares_its_required_properties() -> None:
    def properties(name: str) -> list[tuple[str, str, bool]]:
        return [
            (item["propertyName"], item["propertyType"], item["isRequired"])
            for item in json.loads(trigger(name)["toolProperties"])
        ]

    assert properties("echo") == [("text", "string", True)]
    assert properties("utc_now") == []
    assert properties("add") == [("a", "number", True), ("b", "number", True)]


def test_the_tools_answer_as_m_tools_does() -> None:
    assert call("echo", {"text": "through the trigger"}) == "through the trigger"
    assert call("add", {"a": 2, "b": 3}) == "5"
    assert call("add", {"a": 1.5, "b": 2}) == "3.5"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", call("utc_now", {}))


@pytest.mark.parametrize(
    ("name", "arguments"), [("echo", {}), ("add", {"a": 1}), ("add", {"a": True, "b": 2})]
)
def test_bad_arguments_fail_the_call(name: str, arguments: dict[str, Any]) -> None:
    with pytest.raises(tools.ToolInputError):
        call(name, arguments)


def test_a_context_without_arguments_is_read_as_no_arguments() -> None:
    assert function_app.arguments(json.dumps({"name": "utc_now"})) == {}
    assert function_app.arguments(json.dumps(["not", "an", "object"])) == {}


def test_the_tools_log_only_their_name_outcome_and_duration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    call("echo", {"text": "private words"})
    with pytest.raises(tools.ToolInputError):
        call("add", {"a": "x", "b": 1})

    messages = [entry.getMessage() for entry in caplog.records]
    assert len(messages) == 2
    assert re.fullmatch(r"tool=echo outcome=ok duration_ms=\d+", messages[0])
    assert re.fullmatch(r"tool=add outcome=error duration_ms=\d+", messages[1])
