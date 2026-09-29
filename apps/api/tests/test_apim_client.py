from typing import cast

import httpx
import pytest
from apim_double import RESOURCE_ID, FakeApim, FakeCredential
from azure.core.credentials_async import AsyncTokenCredential
from conftest import _no_sleep, build_arm_client
from mosaic_api.domain import ApimResourceId
from mosaic_api.errors import (
    UpstreamAuthorizationError,
    UpstreamConflictError,
    UpstreamError,
    UpstreamNotFoundError,
)
from mosaic_api.integrations.apim import ApimClient, ApimWriter, ArmClient


def _client(fake: FakeApim) -> ApimClient:
    return ApimClient(build_arm_client(fake), ApimResourceId.parse(RESOURCE_ID))


async def test_list_follows_next_link_across_pages(fake_apim: FakeApim) -> None:
    apis = await _client(fake_apim).list_apis()

    assert [api["name"] for api in apis] == ["chat-api", "echo-api", "orders-mcp"]


async def test_service_read_returns_sku_and_gateway_url(fake_apim: FakeApim) -> None:
    service = await _client(fake_apim).get_service()

    assert service is not None
    assert service["sku"]["name"] == "Developer"
    assert service["properties"]["gatewayUrl"].endswith(".azure-api.net")


async def test_missing_policy_is_absent_rather_than_an_error(fake_apim: FakeApim) -> None:
    assert await _client(fake_apim).get_api_policy("echo-api") is None


async def test_policy_is_requested_as_raw_xml(fake_apim: FakeApim) -> None:
    policy = await _client(fake_apim).get_api_policy("chat-api")

    assert policy is not None
    assert "llm-token-limit" in policy


async def test_forbidden_responses_map_to_an_authorization_error(fake_apim: FakeApim) -> None:
    fake_apim.fail_once("apis", 403)

    with pytest.raises(UpstreamAuthorizationError) as error:
        await _client(fake_apim).list_apis()

    assert error.value.status_code == 403
    assert error.value.code == "gateway_forbidden"


async def test_not_found_without_opt_in_maps_to_a_not_found_error(fake_apim: FakeApim) -> None:
    arm = build_arm_client(fake_apim)

    with pytest.raises(UpstreamNotFoundError):
        await arm.get(f"{RESOURCE_ID}/does-not-exist")


async def test_throttling_is_retried_then_succeeds(fake_apim: FakeApim) -> None:
    fake_apim.fail_once("products", 429)

    products = await _client(fake_apim).list_products()

    assert [product["name"] for product in products] == ["gold"]
    assert fake_apim.requests.count(f"{RESOURCE_ID}/products") == 2


async def test_server_errors_are_retried_then_reported(fake_apim: FakeApim) -> None:
    fake_apim.fail_always("backends", 503)

    with pytest.raises(UpstreamError):
        await _client(fake_apim).list_backends()

    assert fake_apim.requests.count(f"{RESOURCE_ID}/backends") == 4


async def test_transport_failures_surface_as_unreachable() -> None:
    def explode(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    arm = ArmClient(
        cast(AsyncTokenCredential, FakeCredential()),
        client=httpx.AsyncClient(transport=httpx.MockTransport(explode)),
        sleep=_no_sleep,
    )

    with pytest.raises(UpstreamError):
        await arm.get(RESOURCE_ID)


async def test_effective_permissions_are_returned(fake_apim: FakeApim) -> None:
    permissions = await _client(fake_apim).effective_permissions()

    assert permissions is not None
    assert "Microsoft.ApiManagement/service/*/read" in permissions[0]["actions"]


async def test_effective_permissions_degrade_to_none_when_denied() -> None:
    fake = FakeApim(permissions_status=403)

    assert await _client(fake).effective_permissions() is None


async def test_put_returns_the_written_resource(fake_apim: FakeApim) -> None:
    arm = build_arm_client(fake_apim)

    written = await arm.put(
        f"{RESOURCE_ID}/backends/mosaic-test",
        {"properties": {"url": "https://contoso.openai.azure.com", "protocol": "http"}},
        params={"api-version": "2024-05-01"},
    )

    assert written is not None
    assert written["name"] == "mosaic-test"
    assert fake_apim.write_paths("PUT") == ["backends/mosaic-test"]


async def test_delete_of_a_missing_resource_is_not_an_error(fake_apim: FakeApim) -> None:
    arm = build_arm_client(fake_apim)

    removed = await arm.delete(
        f"{RESOURCE_ID}/backends/never-created", params={"api-version": "2024-05-01"}
    )

    assert removed is False


async def test_delete_sends_an_if_match_header(fake_apim: FakeApim) -> None:
    writer = ApimWriter(build_arm_client(fake_apim), ApimResourceId.parse(RESOURCE_ID))
    await writer.put_backend("mosaic-test", url="https://contoso.openai.azure.com", title="t")

    assert await writer.delete_backend("mosaic-test") is True
    assert fake_apim.write_paths("DELETE") == ["backends/mosaic-test"]


async def test_a_precondition_failure_is_reported_as_a_conflict(fake_apim: FakeApim) -> None:
    fake_apim.fail_write("backends/mosaic-test", 412)
    arm = build_arm_client(fake_apim)

    with pytest.raises(UpstreamConflictError) as error:
        await arm.put(
            f"{RESOURCE_ID}/backends/mosaic-test",
            {"properties": {}},
            params={"api-version": "2024-05-01"},
            if_match='"stale"',
        )

    assert error.value.status_code == 409


async def test_a_throttled_write_is_retried(fake_apim: FakeApim) -> None:
    fake_apim.fail_write("backends/mosaic-test", 429)
    arm = build_arm_client(fake_apim)

    with pytest.raises(UpstreamError):
        await arm.put(
            f"{RESOURCE_ID}/backends/mosaic-test",
            {"properties": {}},
            params={"api-version": "2024-05-01"},
        )

    assert fake_apim.write_paths("PUT").count("backends/mosaic-test") == 4


async def test_an_in_progress_operation_is_polled_until_it_settles(fake_apim: FakeApim) -> None:
    fake_apim.make_async("backends/mosaic-test", polls=3)
    writer = ApimWriter(build_arm_client(fake_apim), ApimResourceId.parse(RESOURCE_ID))

    await writer.put_backend("mosaic-test", url="https://contoso.openai.azure.com", title="t")

    polls = [path for path in fake_apim.requests if "mosaic-test-operations" in path]
    assert len(polls) == 4


async def test_a_failed_operation_raises_rather_than_reporting_success(
    fake_apim: FakeApim,
) -> None:
    fake_apim.make_async("backends/mosaic-test", polls=0, result="Failed")
    writer = ApimWriter(build_arm_client(fake_apim), ApimResourceId.parse(RESOURCE_ID))

    with pytest.raises(UpstreamError) as error:
        await writer.put_backend("mosaic-test", url="https://contoso.openai.azure.com", title="t")

    assert error.value.message == (
        "The Azure operation did not succeed (Failed) and Azure returned no reason. Check the "
        "Azure activity log for this resource."
    )


FRAGMENT_NAME = "mosaic-retroburn-aoai-east-gpt-35-turbo"
FRAGMENT_URL = f"https://management.azure.com{RESOURCE_ID}/policyFragments/{FRAGMENT_NAME}"
POLL_URL = (
    f"{FRAGMENT_URL}?api-version=2024-05-01&azure-asyncId=6abb29da463461025c035d71&format=rawxml"
)
MISSING_BACKEND = (
    "Error in element 'set-backend-service' on line 3, column 4: Backend with id "
    f"'{FRAGMENT_NAME}' could not be found."
)
# What API Management's Location poll returned on the live gateway for a fragment written before
# its backend.
LIVE_ERROR = {
    "code": "ValidationError",
    "message": "One or more fields contain incorrect values:",
    "details": [
        {"code": "ValidationError", "target": "set-backend-service", "message": MISSING_BACKEND}
    ],
}


async def _failed_operation(poll_status: int, poll_body: object) -> UpstreamError:
    """Write a fragment that API Management accepts with 201 and a Location, then fails."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(201, json={"name": FRAGMENT_NAME}, headers={"Location": POLL_URL})
        return httpx.Response(poll_status, json=poll_body)

    arm = ArmClient(
        cast(AsyncTokenCredential, FakeCredential()),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        sleep=_no_sleep,
    )
    with pytest.raises(UpstreamError) as error:
        await arm.put(
            f"{RESOURCE_ID}/policyFragments/{FRAGMENT_NAME}",
            {"properties": {"format": "rawxml", "value": "<fragment />"}},
            params={"api-version": "2024-05-01"},
        )
    assert type(error.value) is UpstreamError
    return error.value


async def test_a_failed_operation_says_why_in_azures_words() -> None:
    error = await _failed_operation(200, {"status": "Failed", "error": LIVE_ERROR})

    reason = (
        f"ValidationError: One or more fields contain incorrect values. Detail: {MISSING_BACKEND}"
    )
    assert error.message == f"The Azure operation did not succeed (Failed). {reason}"
    # The resource is named, not just the poll, so the failure is attributable to it.
    assert error.details == {
        "url": FRAGMENT_URL,
        "pollUrl": POLL_URL,
        "status": "Failed",
        "code": "ValidationError",
        "reason": reason,
    }


async def test_the_most_specific_nested_detail_is_the_one_reported() -> None:
    # The shape the activity log records: ARM's wrapper around API Management's own error.
    wrapped = {
        "code": "ResourceOperationFailure",
        "message": "The resource operation completed with terminal provisioning state 'Failed'.",
        "details": [LIVE_ERROR],
    }

    error = await _failed_operation(200, {"status": "Failed", "error": wrapped})

    assert error.message == (
        "The Azure operation did not succeed (Failed). ResourceOperationFailure: The resource "
        "operation completed with terminal provisioning state 'Failed'. Detail: ValidationError: "
        f"{MISSING_BACKEND}"
    )


@pytest.mark.parametrize(
    ("count", "suffix"), [(2, " (+1 more detail)"), (3, " (+2 more details)")]
)
async def test_other_details_are_counted_and_a_target_is_named(count: int, suffix: str) -> None:
    details = [
        {"code": "ValidationError", "target": "properties.url", "message": "Not a valid URL."},
        {"code": "ValidationError", "target": "title", "message": "Too long."},
        {"code": "ValidationError", "target": "protocol", "message": "Not supported."},
    ][:count]
    body = {
        "status": "Failed",
        "error": {"code": "ValidationError", "message": "Invalid values.", "details": details},
    }

    error = await _failed_operation(200, body)

    assert error.message == (
        "The Azure operation did not succeed (Failed). ValidationError: Invalid values. Detail: "
        f"Not a valid URL. (target: properties.url){suffix}"
    )


async def test_an_error_on_a_failed_resource_body_is_reported() -> None:
    body = {
        "name": FRAGMENT_NAME,
        "properties": {
            "provisioningState": "Failed",
            "error": {"code": "BadRequest", "message": "The backend URL is not valid"},
        },
    }

    error = await _failed_operation(200, body)

    assert error.message == (
        "The Azure operation did not succeed (Failed). BadRequest: The backend URL is not valid."
    )


async def test_a_failed_resource_without_an_error_names_its_state_and_not_its_policy() -> None:
    body = {
        "name": FRAGMENT_NAME,
        "properties": {
            "provisioningState": "Failed",
            "format": "rawxml",
            "value": '<fragment>\n  <set-backend-service backend-id="secret-route" />\n</fragment>',
        },
    }

    error = await _failed_operation(200, body)

    assert error.message == (
        "The Azure operation did not succeed (Failed) and Azure returned no reason. Check the "
        "Azure activity log for this resource."
    )
    assert error.details["code"] is None
    assert error.details["reason"] is None
    assert "secret-route" not in f"{error.message} {error.details}"


async def test_a_client_error_from_the_poll_still_says_why() -> None:
    error = await _failed_operation(
        400, {"error": {"code": "InvalidRequest", "message": "The request is invalid."}}
    )

    assert error.message == (
        "The Azure operation did not succeed (HTTP 400). InvalidRequest: The request is invalid."
    )
    assert error.details["status"] == "HTTP 400"


@pytest.mark.parametrize(
    "markup",
    [
        '<set-backend-service backend-id="secret-route" />',
        '&lt;set-backend-service backend-id="secret-route" /&gt;',
        "@(context.Variables[\"secret-route\"])",
        '@{ return "secret-route"; }',
    ],
)
async def test_policy_markup_in_azures_reason_is_withheld(markup: str) -> None:
    body = {
        "status": "Failed",
        "error": {
            "code": "ValidationError",
            "message": f"Policy {markup} is not allowed here",
            "details": [{"code": "ValidationError", "message": f"Element {markup} is invalid"}],
        },
    }

    error = await _failed_operation(200, body)

    assert error.message == (
        "The Azure operation did not succeed (Failed). ValidationError: Policy [policy markup "
        "omitted]. Detail: Element [policy markup omitted]."
    )
    assert "secret-route" not in f"{error.message} {error.details}"


async def test_azures_reason_is_bounded() -> None:
    body = {
        "status": "Failed",
        "error": {
            "code": "C" * 500,
            "message": "m" * 5000,
            "details": [
                {"code": "D" * 500, "target": "t" * 500, "message": "d" * 5000},
                *({"message": "e" * 5000} for _ in range(500)),
            ],
        },
    }

    error = await _failed_operation(200, body)

    reason = error.details["reason"]
    assert len(reason) == 700
    assert reason.endswith("…")
    assert len(error.details["code"]) == 80
    assert error.message == f"The Azure operation did not succeed (Failed). {reason}"


async def test_a_deeply_nested_error_is_walked_only_so_far() -> None:
    nested: dict[str, object] = {"message": "Too deep to reach."}
    for level in range(50, 0, -1):
        nested = {"message": f"Level {level}.", "details": [nested]}
    outer = {"code": "Deep", "message": "Nested.", "details": [nested]}
    body = {"status": "Failed", "error": outer}

    error = await _failed_operation(200, body)

    assert error.message == (
        "The Azure operation did not succeed (Failed). Deep: Nested. Detail: Level 8."
    )


async def test_a_fragment_routed_to_a_missing_backend_fails_as_api_management_fails_it(
    fake_apim: FakeApim,
) -> None:
    writer = ApimWriter(build_arm_client(fake_apim), ApimResourceId.parse(RESOURCE_ID))
    fragment = (
        "<fragment>\n"
        '  <authentication-managed-identity resource="https://cognitiveservices.azure.com" />\n'
        '  <set-backend-service backend-id="mosaic-test" />\n'
        "</fragment>"
    )

    with pytest.raises(UpstreamError) as error:
        await writer.put_policy_fragment("mosaic-test", fragment, description="MOSAIC")

    assert error.value.message == (
        "The Azure operation did not succeed (Failed). ValidationError: One or more fields contain "
        "incorrect values. Detail: Error in element 'set-backend-service' on line 3, column 4: "
        "Backend with id 'mosaic-test' could not be found."
    )
    assert "policyFragments/mosaic-test" not in fake_apim.written

    await writer.put_backend("mosaic-test", url="https://contoso.openai.azure.com", title="t")
    await writer.put_policy_fragment("mosaic-test", fragment, description="MOSAIC")

    stored = fake_apim.written["policyFragments/mosaic-test"]
    assert stored["properties"]["value"] == fragment
    assert fake_apim.dangling_references == [
        ("policyFragments/mosaic-test", "backends/mosaic-test")
    ]
