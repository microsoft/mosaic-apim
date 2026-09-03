"""How MOSAIC reads a gateway's managed identity.

That principal is what must hold a data-plane role on a model endpoint, so misreading it produces a
confident and wrong answer about whether the gateway can call a model.
"""

import pytest
from apim_double import APIM_PRINCIPAL_ID, RESOURCE_ID, FakeApim
from conftest import build_gateway_service
from mosaic_api.domain import GatewayCreate
from mosaic_api.repositories import InMemoryGatewayRepository
from mosaic_api.services.directory import Actor

ACTOR = Actor(object_id="admin-object-id", tenant_id="tenant-test")
USER_ASSIGNED_PRINCIPAL = "22222222-2222-2222-2222-222222222222"


async def _register(fake: FakeApim, repository: InMemoryGatewayRepository):
    service = build_gateway_service(fake, repository)
    return await service.register(ACTOR, GatewayCreate(azure_resource_id=RESOURCE_ID))


@pytest.mark.asyncio
async def test_system_assigned_identity_is_captured(
    fake_apim: FakeApim, gateway_repository: InMemoryGatewayRepository
) -> None:
    gateway = await _register(fake_apim, gateway_repository)

    assert gateway.capabilities.principal_id == APIM_PRINCIPAL_ID
    assert gateway.capabilities.identity_observed is True


@pytest.mark.asyncio
async def test_user_assigned_identity_is_captured(
    fake_apim: FakeApim, gateway_repository: InMemoryGatewayRepository
) -> None:
    # ARM leaves the top-level principalId null for a user-assigned identity. Reading only that
    # would report a gateway that plainly has an identity as having none.
    fake_apim.identity = {
        "type": "UserAssigned",
        "principalId": None,
        "userAssignedIdentities": {
            "/subscriptions/x/resourceGroups/y/providers/Microsoft.ManagedIdentity"
            "/userAssignedIdentities/apim-mi": {
                "principalId": USER_ASSIGNED_PRINCIPAL,
                "clientId": "33333333-3333-3333-3333-333333333333",
            }
        },
    }

    gateway = await _register(fake_apim, gateway_repository)

    assert gateway.capabilities.principal_id == USER_ASSIGNED_PRINCIPAL
    assert gateway.capabilities.identity_observed is True
    assert not any("no managed identity" in note for note in gateway.capabilities.notes)


@pytest.mark.asyncio
async def test_multiple_identities_are_flagged(
    fake_apim: FakeApim, gateway_repository: InMemoryGatewayRepository
) -> None:
    fake_apim.identity = {
        "type": "SystemAssigned, UserAssigned",
        "principalId": APIM_PRINCIPAL_ID,
        "userAssignedIdentities": {
            "/subscriptions/x/.../apim-mi": {"principalId": USER_ASSIGNED_PRINCIPAL}
        },
    }

    gateway = await _register(fake_apim, gateway_repository)

    # Which identity a policy uses depends on its client ID, which the service description does
    # not reveal, so the ambiguity is surfaced rather than guessed at silently.
    assert any("managed identities" in note for note in gateway.capabilities.notes)


@pytest.mark.asyncio
async def test_absent_identity_is_recorded_as_observed_and_empty(
    fake_apim: FakeApim, gateway_repository: InMemoryGatewayRepository
) -> None:
    fake_apim.identity = None

    gateway = await _register(fake_apim, gateway_repository)

    assert gateway.capabilities.principal_id is None
    # Observed and genuinely absent, which is different from never having been read.
    assert gateway.capabilities.identity_observed is True
    assert any("no managed identity" in note for note in gateway.capabilities.notes)
