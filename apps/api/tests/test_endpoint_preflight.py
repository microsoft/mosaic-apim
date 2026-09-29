"""Endpoint preflight reads key authentication from the account's ``disableLocalAuth``.

ARM leaves ``disableLocalAuth`` out of an account on which it was never set, and the default is
``false``: API keys work. Observed live on two Azure OpenAI accounts, which MOSAIC reported with no
key authentication setting at all although keys worked on both.
"""

from typing import Any

import pytest
from aoai_double import AI_RESOURCE_ID, OMITTED, FakeCognitiveServices
from conftest import build_aoai_arm_client
from mosaic_api.domain import CognitiveServicesResourceId, ModelEndpointCapabilities
from mosaic_api.integrations.aoai import CognitiveServicesClient, run_endpoint_preflight
from mosaic_api.integrations.aoai.preflight import _capabilities

KEY_AUTH_NOTE = (
    "Key authentication is enabled on this endpoint. Disabling it forces callers, including the "
    "gateway, onto managed identity."
)


async def _preflight(fake: FakeCognitiveServices) -> ModelEndpointCapabilities:
    client = CognitiveServicesClient(
        build_aoai_arm_client(fake), CognitiveServicesResourceId.parse(AI_RESOURCE_ID)
    )
    return (await run_endpoint_preflight(client)).capabilities


def _account(**properties: Any) -> dict[str, Any]:
    return {"id": AI_RESOURCE_ID, "kind": "OpenAI", "properties": properties}


@pytest.mark.parametrize(
    ("disable_local_auth", "expected"),
    [
        pytest.param(OMITTED, False, id="absent"),
        pytest.param(None, False, id="null"),
        pytest.param(False, False, id="explicit-false"),
        pytest.param(True, True, id="explicit-true"),
        pytest.param("false", None, id="not-a-bool"),
    ],
)
async def test_key_authentication_is_read_from_the_account(
    disable_local_auth: Any, expected: bool | None
) -> None:
    capabilities = await _preflight(FakeCognitiveServices(disable_local_auth=disable_local_auth))

    assert capabilities.kind == "OpenAI"
    assert capabilities.local_auth_disabled is expected
    assert (KEY_AUTH_NOTE in capabilities.notes) is (expected is False)


@pytest.mark.parametrize("account_status", [403, 404])
async def test_key_authentication_is_unknown_when_the_account_cannot_be_read(
    account_status: int,
) -> None:
    capabilities = await _preflight(FakeCognitiveServices(account_status=account_status))

    assert capabilities.kind is None
    assert capabilities.local_auth_disabled is None
    assert KEY_AUTH_NOTE not in capabilities.notes


@pytest.mark.parametrize(
    "account",
    [
        pytest.param(None, id="no-description"),
        pytest.param({}, id="empty-description"),
        pytest.param({"id": AI_RESOURCE_ID, "kind": "OpenAI"}, id="no-properties"),
        pytest.param(
            {"id": AI_RESOURCE_ID, "kind": "OpenAI", "properties": None}, id="null-properties"
        ),
        pytest.param(
            {"id": AI_RESOURCE_ID, "kind": "OpenAI", "properties": "Succeeded"},
            id="properties-not-an-object",
        ),
    ],
)
def test_key_authentication_is_unknown_without_readable_properties(
    account: dict[str, Any] | None,
) -> None:
    capabilities = _capabilities(account)

    assert capabilities.local_auth_disabled is None
    assert KEY_AUTH_NOTE not in capabilities.notes


def test_an_empty_properties_object_means_keys_are_enabled() -> None:
    capabilities = _capabilities(_account())

    assert capabilities.local_auth_disabled is False
    assert KEY_AUTH_NOTE in capabilities.notes
