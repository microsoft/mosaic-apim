import asyncio
import hashlib
import json
import logging
import time

import httpx
import pytest
from azure.core.credentials import AccessToken

from federal_runtime import verify_backend as verifier
from support import FAKE_TOKEN, FakeCredential, model_env

AUDIENCES = [
    f"api://{digit * 8}-{digit * 4}-{digit * 4}-{digit * 4}-{digit * 12}"
    for digit in ("3", "4", "5")
]
CLIENT_ID = model_env()["AZURE_CLIENT_ID"]


def target_values() -> list[dict[str, str]]:
    return [
        {
            "url": f"https://backend-{environment}-{index}.example.eastus2.azurecontainerapps.io/mcp",
            "audience": audience,
        }
        for environment, audience in zip(("dev", "stg", "prod"), AUDIENCES, strict=True)
        for index in range(8)
    ]


def parsed_targets() -> verifier.Targets:
    return verifier.parse_targets(json.dumps(target_values()))


class BackendCredential(FakeCredential):
    async def get_token(self, *scopes, **kwargs):
        self.scopes.append(scopes)
        return AccessToken(
            f"private-backend-token-{AUDIENCES.index(scopes[0][:-9])}", time.time() + 60
        )


def test_canonical_hash_is_order_independent_and_contains_only_validated_targets():
    values = target_values()
    parsed = verifier.parse_targets(json.dumps(values))
    expected = hashlib.sha256(
        json.dumps(
            sorted(values, key=lambda value: (value["url"], value["audience"])),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
    ).hexdigest()
    assert parsed.sha256 == expected
    reversed_fields = [
        {"audience": value["audience"], "url": value["url"]} for value in reversed(values)
    ]
    assert verifier.parse_targets(json.dumps(reversed_fields)).sha256 == expected


@pytest.mark.anyio
async def test_exact_403_uses_three_current_audience_tokens_and_never_calls_models(monkeypatch):
    targets = parsed_targets()
    requests = []
    credential = BackendCredential()

    def forbidden_model(*args, **kwargs):
        raise AssertionError("Backend verification must never invoke a model.")

    monkeypatch.setattr("federal_runtime.model.BriefGateway.ask", forbidden_model)

    def respond(request):
        requests.append(request)
        assert request.method == "GET"
        target = next(target for target in targets.items if target.url == str(request.url))
        token = f"private-backend-token-{AUDIENCES.index(target.audience)}"
        assert request.headers["authorization"] == f"Bearer {token}"
        assert "x-mosaic-on-behalf-of" not in request.headers
        assert not request.url.query
        return httpx.Response(403, text="Private upstream body must never be reported.")

    report = await verifier.verify_targets(
        targets, CLIENT_ID, credential=credential, transport=httpx.MockTransport(respond)
    )
    assert report.success
    assert report.payload() == {
        "outcome": "ok",
        "targets_sha256": targets.sha256,
        "target_count": 24,
        "attempted": 24,
        "status_counts": {"403": 24},
        "error_counts": {},
    }
    assert credential.scopes == [(f"{audience}/.default",) for audience in AUDIENCES]
    assert credential.closed
    assert len(requests) == 24
    assert len({str(request.url) for request in requests}) == 24


@pytest.mark.anyio
@pytest.mark.parametrize("status", [200, 202, 204, 301, 302, 307, 401, 406, 429, 500, 503])
async def test_any_other_status_including_invalid_token_401_is_not_valid_client_denial(status):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            status,
            headers={"Location": "https://must-not-follow.example/private"},
            text=f"private body {FAKE_TOKEN}",
        )

    credential = FakeCredential()
    report = await verifier.verify_targets(
        parsed_targets(),
        CLIENT_ID,
        credential=credential,
        transport=httpx.MockTransport(respond),
    )
    assert not report.success
    assert report.payload()["outcome"] == "error"
    assert report.status_counts == {str(status): 24}
    assert report.error_counts == {"unexpected_status": 24}
    assert len(requests) == 24
    assert all(request.url.host.endswith(".azurecontainerapps.io") for request in requests)
    assert credential.closed


@pytest.mark.anyio
async def test_partial_403_coverage_with_one_401_must_fail():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(401 if len(requests) == 1 else 403)

    report = await verifier.verify_targets(
        parsed_targets(),
        CLIENT_ID,
        credential=FakeCredential(),
        transport=httpx.MockTransport(respond),
    )
    assert not report.success
    assert report.status_counts == {"401": 1, "403": 23}
    assert report.error_counts == {"unexpected_status": 1}


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["failure", "empty", "expired"])
async def test_token_failure_never_sends_an_anonymous_request(kind):
    class Credential(FakeCredential):
        async def get_token(self, *scopes, **kwargs):
            self.scopes.append(scopes)
            if kind == "failure":
                raise RuntimeError(f"private credential {FAKE_TOKEN}")
            return AccessToken("" if kind == "empty" else FAKE_TOKEN, 0)

    requests = []
    credential = Credential()
    report = await verifier.verify_targets(
        parsed_targets(),
        CLIENT_ID,
        credential=credential,
        transport=httpx.MockTransport(lambda request: requests.append(request)),
    )
    assert not report.success
    assert report.attempted == 0
    assert report.status_counts == {}
    assert report.error_counts == {"token": 3}
    assert requests == []
    assert credential.closed


@pytest.mark.anyio
async def test_one_failed_audience_cannot_be_substituted_by_another_valid_token():
    class Credential(FakeCredential):
        async def get_token(self, *scopes, **kwargs):
            self.scopes.append(scopes)
            if scopes == (f"{AUDIENCES[0]}/.default",):
                raise RuntimeError("private audience failure")
            return AccessToken(FAKE_TOKEN, time.time() + 60)

    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(403)

    credential = Credential()
    report = await verifier.verify_targets(
        parsed_targets(), CLIENT_ID, credential=credential, transport=httpx.MockTransport(respond)
    )
    assert not report.success
    assert report.attempted == 16
    assert report.status_counts == {"403": 16}
    assert report.error_counts == {"token": 1}
    assert len(requests) == 16
    assert credential.closed


@pytest.mark.anyio
@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ConnectError])
async def test_network_failure_is_not_denial_and_does_not_retry(error_type):
    requests = []

    def fail(request):
        requests.append(request)
        raise error_type(f"private URL {request.url} token {FAKE_TOKEN}")

    credential = FakeCredential()
    report = await verifier.verify_targets(
        parsed_targets(), CLIENT_ID, credential=credential, transport=httpx.MockTransport(fail)
    )
    assert not report.success
    assert report.status_counts == {}
    assert report.error_counts == {"network": 24}
    assert len(requests) == 24
    assert credential.closed
    assert FAKE_TOKEN not in json.dumps(report.payload())


@pytest.mark.anyio
async def test_403_with_oversized_body_fails_without_exposing_body():
    report = await verifier.verify_targets(
        parsed_targets(),
        CLIENT_ID,
        credential=FakeCredential(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(403, content=b"x" * (verifier.MAX_RESPONSE_BYTES + 1))
        ),
    )
    assert not report.success
    assert report.status_counts == {"403": 24}
    assert report.error_counts == {"response_limit": 24}


@pytest.mark.anyio
async def test_total_deadline_interrupts_request_and_closes_both_resources(monkeypatch):
    monkeypatch.setattr(verifier, "TOTAL_DEADLINE_SECONDS", 0.02)
    credential = FakeCredential()

    class SlowTransport(httpx.MockTransport):
        closed = False

        async def aclose(self):
            self.closed = True

    async def slow(request):
        await asyncio.sleep(10)
        return httpx.Response(403)

    transport = SlowTransport(slow)
    report = await verifier.verify_targets(
        parsed_targets(), CLIENT_ID, credential=credential, transport=transport
    )
    assert not report.success
    assert report.error_counts == {"deadline": 1}
    assert report.attempted == 1
    assert credential.closed and transport.closed


@pytest.mark.anyio
async def test_resource_cleanup_failure_cannot_report_success():
    class Credential(FakeCredential):
        async def close(self):
            self.closed = True
            raise RuntimeError(f"private cleanup {FAKE_TOKEN}")

    credential = Credential()
    report = await verifier.verify_targets(
        parsed_targets(),
        CLIENT_ID,
        credential=credential,
        transport=httpx.MockTransport(lambda request: httpx.Response(403)),
    )
    assert not report.success
    assert report.status_counts == {"403": 24}
    assert report.error_counts == {"cleanup": 1}
    assert credential.closed


@pytest.mark.parametrize(
    "url",
    [
        "http://backend.example.azurecontainerapps.io/mcp",
        "https://azurecontainerapps.io/mcp",
        "https://backend.azurecontainerapps.io.attacker.example/mcp",
        "https://attacker.example/mcp",
        "https://user:password@backend.azurecontainerapps.io/mcp",
        "https://backend.azurecontainerapps.io:443/mcp",
        "https://backend.azurecontainerapps.io/mcp?token=private",
        "https://backend.azurecontainerapps.io/mcp?",
        "https://backend.azurecontainerapps.io/mcp#fragment",
        "https://backend.azurecontainerapps.io/mcp/",
        "https://backend.azurecontainerapps.io/%6dcp",
        "https://backend.azurecontainerapps.io/other",
        "https://backend.azurecontainerapps.io/mcp\n",
        "https://backend.azurecontainerapps.io\\@attacker.example/mcp",
    ],
)
def test_invalid_target_urls_are_refused(url):
    values = target_values()
    values[0]["url"] = url
    with pytest.raises(verifier.TargetError):
        verifier.parse_targets(json.dumps(values))


@pytest.mark.parametrize(
    "audience",
    [
        "api://not-a-uuid",
        "api://33333333-3333-3333-3333-333333333333/.default",
        "https://33333333-3333-3333-3333-333333333333",
        "api://33333333-3333-3333-3333-333333333333?private",
        "",
    ],
)
def test_invalid_backend_audiences_are_refused(audience):
    values = target_values()
    values[0]["audience"] = audience
    with pytest.raises(verifier.TargetError):
        verifier.parse_targets(json.dumps(values))


@pytest.mark.parametrize("count", [0, 1, 23, 25])
def test_exactly_24_targets_required(count):
    values = (target_values() * 2)[:count]
    with pytest.raises(verifier.TargetError):
        verifier.parse_targets(json.dumps(values))


@pytest.mark.parametrize(
    "kind", ["duplicate", "two-audiences", "four-audiences", "unbalanced", "extra"]
)
def test_duplicate_urls_incorrect_inventory_and_unknown_fields_are_refused(kind):
    values = target_values()
    if kind == "duplicate":
        values[0]["url"] = values[1]["url"]
    elif kind == "two-audiences":
        for value in values:
            if value["audience"] == AUDIENCES[0]:
                value["audience"] = AUDIENCES[1]
    elif kind == "four-audiences":
        values[0]["audience"] = f"api://{'6' * 8}-6666-6666-6666-{'6' * 12}"
    elif kind == "unbalanced":
        values[0]["audience"] = AUDIENCES[1]
    else:
        values[0]["private"] = "must not be accepted"
    with pytest.raises(verifier.TargetError):
        verifier.parse_targets(json.dumps(values))


@pytest.mark.parametrize("raw", ["{}", "null", "not-json", " " * 16385, '[{"url":"a","url":"b"}]'])
def test_invalid_json_shape_size_or_duplicate_keys_are_refused(raw):
    with pytest.raises(verifier.TargetError):
        verifier.parse_targets(raw)


def test_main_selects_job_uami_and_emits_only_bounded_json_without_sensitive_logs(
    monkeypatch, capsys
):
    credential = FakeCredential()
    selected = []
    values = target_values()
    private = [FAKE_TOKEN, CLIENT_ID, *AUDIENCES, *(value["url"] for value in values)]

    def identity(**kwargs):
        selected.append(kwargs)
        return credential

    def respond(request):
        logging.getLogger("azure.identity").warning(" ".join(private))
        return httpx.Response(403, text="private response body")

    monkeypatch.setenv("AZURE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(verifier, "ManagedIdentityCredential", identity)
    monkeypatch.setattr(
        verifier.httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(respond)
    )
    root = logging.getLogger()
    old_handlers, old_level = root.handlers[:], root.level
    try:
        assert verifier.main(["--targets-json", json.dumps(values)]) == 0
        output = capsys.readouterr()
    finally:
        root.handlers[:], root.level = old_handlers, old_level
    result = json.loads(output.out)
    assert result["outcome"] == "ok"
    assert result["status_counts"] == {"403": 24}
    assert result["targets_sha256"] == parsed_targets().sha256
    assert len(output.out.encode()) < 1024
    assert selected == [{"client_id": CLIENT_ID, "retry_total": 0}]
    assert credential.closed
    for secret in [*private, "private response body"]:
        assert secret not in output.out
        assert secret not in output.err


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--targets-json", "private-invalid-json"],
        ["--unrecognized", "https://private.example/secret"],
    ],
)
def test_cli_argument_errors_are_nonzero_json_not_argparse_value_echoes(argv, capsys):
    root = logging.getLogger()
    old_handlers, old_level = root.handlers[:], root.level
    try:
        assert verifier.main(argv) == 1
        output = capsys.readouterr()
    finally:
        root.handlers[:], root.level = old_handlers, old_level
    assert json.loads(output.out)["outcome"] == "error"
    assert "private" not in output.out + output.err


def test_main_missing_identity_reports_error_with_valid_targets_digest(monkeypatch, capsys):
    monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
    root = logging.getLogger()
    old_handlers, old_level = root.handlers[:], root.level
    try:
        assert verifier.main(["--targets-json", json.dumps(target_values())]) == 1
        result = json.loads(capsys.readouterr().out)
    finally:
        root.handlers[:], root.level = old_handlers, old_level
    assert result["outcome"] == "error"
    assert result["targets_sha256"] == parsed_targets().sha256
    assert result["error_counts"] == {"configuration": 1}
    assert result["attempted"] == 0


@pytest.mark.parametrize("kind", ["401", "token", "network"])
def test_main_runtime_failures_exit_nonzero_and_never_look_like_valid_client_denial(
    kind, monkeypatch, capsys
):
    credential = FakeCredential(fail=kind == "token")

    def respond(request):
        if kind == "network":
            raise httpx.ConnectError(f"private request {request.url} token {FAKE_TOKEN}")
        return httpx.Response(401, text=f"private invalid token {FAKE_TOKEN}")

    monkeypatch.setenv("AZURE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(verifier, "ManagedIdentityCredential", lambda **kwargs: credential)
    monkeypatch.setattr(
        verifier.httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(respond)
    )
    root = logging.getLogger()
    old_handlers, old_level = root.handlers[:], root.level
    try:
        assert verifier.main(["--targets-json", json.dumps(target_values())]) == 1
        output = capsys.readouterr()
    finally:
        root.handlers[:], root.level = old_handlers, old_level
    result = json.loads(output.out)
    assert result["outcome"] == "error"
    assert result["targets_sha256"] == parsed_targets().sha256
    assert result["status_counts"] == ({"401": 24} if kind == "401" else {})
    assert result["error_counts"]
    assert credential.closed
    assert FAKE_TOKEN not in output.out + output.err
    assert "private" not in output.out + output.err
