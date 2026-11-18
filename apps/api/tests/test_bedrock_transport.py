"""Rendered policy and a real loopback JSON/SSE fixture, not an APIM runtime emulator."""

import json
import threading
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from mosaic_api.domain import ModelProvider
from mosaic_api.integrations.access_policy import render_governed_policy
from test_access_policy import _anthropic, _snapshot


def _compiled() -> tuple[ET.Element, ET.Element]:
    publication = _anthropic(
        provider=ModelProvider.AWS_BEDROCK,
        deployment_name="anthropic.claude-sonnet-5",
        backend_key_name="fictional-bedrock-key",
    )
    rendered = render_governed_policy(publication, _snapshot())
    return ET.fromstring(rendered.fragment_xml), ET.fromstring(rendered.api_policy_xml)


def test_native_route_owns_one_forward_without_changing_other_anthropic_routes() -> None:
    fragment, policy = _compiled()
    backend = policy.find("backend")
    assert [node.tag for node in backend] == ["forward-request"]
    assert backend[0].attrib == {"buffer-response": "false", "follow-redirects": "false"}
    assert not list(policy.iter("retry")) and not list(fragment.iter("retry"))
    assert not list(fragment.iter("send-request"))
    assert fragment.findall("set-body") == []
    assert fragment.find("rewrite-uri").attrib == {
        "template": "/anthropic/v1/messages", "copy-unmatched-params": "false",
    }
    keys = fragment.findall("set-header[@name='x-api-key']")
    assert keys[-1].find("value").text == "{{fictional-bedrock-key}}"
    assert keys[-1].attrib["exists-action"] == "override"
    version = fragment.find("set-header[@name='anthropic-version']")
    assert version.attrib["exists-action"] == "override"
    assert version.findtext("value") == "2023-06-01"
    for name in ("Authorization", "Host", "X-Amz-Security-Token", "X-Amz-Signature"):
        assert fragment.find(f"set-header[@name='{name}']").attrib["exists-action"] == "delete"
    outbound = policy.find("outbound")
    assert outbound.find("set-header[@name='Location']").attrib["exists-action"] == "delete"
    assert outbound.find("choose/when/return-response/set-status").attrib["code"] == "502"
    assert len(list(outbound.iter("set-body"))) == 1  # Redirect refusal only, not a stream rewrite.
    legacy = ET.fromstring(render_governed_policy(_anthropic(), _snapshot()).api_policy_xml)
    assert [node.tag for node in legacy.find("backend")] == ["base"]


def test_native_provider_bounds_do_not_become_generation_defaults_or_strip_features() -> None:
    fragment, _ = _compiled()
    guard = next(node.attrib["condition"] for node in fragment.iter("when")
                 if 'var maximum = body["max_tokens"]' in node.attrib.get("condition", ""))
    assert "(decimal)maximum > 128000" in guard
    assert 'thinking["budget_tokens"] != null' in guard
    assert '"adaptive"' in guard and '"disabled"' in guard
    assert 'body["service_tier"]' in guard and 'if (tier != null)' in guard
    assert "anthropic-beta" in guard
    for tool in (
        "computer_toolset_20260801", "browser_toolset_20260801", "bash_20250124",
        "text_editor_20250728", "memory_20250818",
    ):
        assert tool in guard
    assert not fragment.findall("set-body")
    assert list(fragment.iter("llm-token-limit"))  # Existing aggregate governance, separately.


@pytest.fixture
def native_fixture() -> Iterator[tuple[str, threading.Event, list[tuple[str, bytes]], list[bool]]]:
    observed = threading.Event()
    requests: list[tuple[str, bytes]] = []
    completed: list[bool] = []

    class NativeHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.path, body))
            if self.headers.get("x-api-key") != "fictional-key-not-a-credential":
                self.send_error(401)
                return
            payload = json.loads(body)
            if payload.get("redirect"):
                self.send_response(307)
                # Different origin; following would make a second credentialed request.
                self.send_header("Location", f"http://localhost:{self.server.server_port}/wrong")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            streaming = payload.get("stream")
            self.send_header(
                "Content-Type", "text/event-stream" if streaming else "application/json",
            )
            self.send_header("Connection", "close")
            self.end_headers()
            if streaming:
                self.wfile.write(b'event: message_start\ndata: {"type":"message_start"}\n\n')
                self.wfile.flush()
                completed.append(observed.wait(timeout=5))
                self.wfile.write(b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
            else:
                self.wfile.write(json.dumps({
                    "type": "message",
                    "content": [
                        {"type": "thinking", "thinking": "fictional", "signature": "unchanged"},
                        {"type": "tool_use", "id": "tool-demo", "name": "lookup", "input": {}},
                    ],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 2, "output_tokens": 3,
                              "cache_read_input_tokens": 1024, "cache_creation_input_tokens": 0},
                }).encode())
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), NativeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", observed, requests, completed
    finally:
        observed.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("streaming", [False, True])
def test_loopback_native_json_and_first_sse_data_arrive_before_completion(
    native_fixture: tuple[str, threading.Event, list[tuple[str, bytes]], list[bool]],
    streaming: bool,
) -> None:
    origin, observed, requests, completed = native_fixture
    fragment, policy = _compiled()
    forward = policy.find("backend/forward-request")
    path = fragment.find("rewrite-uri").attrib["template"]
    payload = {
        "model": "anthropic.claude-sonnet-5", "max_tokens": 128000, "stream": streaming,
        "thinking": {"type": "adaptive"},
        "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Fictional request", "cache_control": {"type": "ephemeral"}},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                       "data": "fictional-base64-not-a-provider-validation-test"}},
        ]}],
    }
    wire = json.dumps(payload).encode()
    with httpx.Client(
        trust_env=False, follow_redirects=forward.attrib["follow-redirects"] == "true", timeout=10,
    ) as client, client.stream(
        "POST", origin + path, content=wire,
        headers={"x-api-key": "fictional-key-not-a-credential", "Content-Type": "application/json"},
    ) as response:
        assert response.status_code == 200
        if streaming:
            lines = response.iter_lines()
            assert next(lines) == "event: message_start"
            assert next(lines) == 'data: {"type":"message_start"}'
            assert not completed  # Upstream cannot finish until this consumer sees first data.
            observed.set()
            assert 'data: {"type":"message_stop"}' in list(lines)
            assert completed == [True]
        else:
            result = json.loads(response.read())
            assert result["content"][0]["signature"] == "unchanged"
            assert result["content"][1]["type"] == "tool_use"
            assert result["usage"]["cache_read_input_tokens"] == 1024
    assert requests == [(path, wire)]


def test_loopback_does_not_follow_cross_origin_redirects(
    native_fixture: tuple[str, threading.Event, list[tuple[str, bytes]], list[bool]],
) -> None:
    origin, _, requests, _ = native_fixture
    _, policy = _compiled()
    forward = policy.find("backend/forward-request")
    with httpx.Client(
        trust_env=False, follow_redirects=forward.attrib["follow-redirects"] == "true",
    ) as client:
        response = client.post(origin + "/anthropic/v1/messages", json={"redirect": True},
                               headers={"x-api-key": "fictional-key-not-a-credential"})
    assert response.status_code == 307 and len(requests) == 1
