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

    with pytest.raises(UpstreamError):
        await writer.put_backend("mosaic-test", url="https://contoso.openai.azure.com", title="t")
