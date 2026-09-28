"""How MOSAIC reads a gateway's managed identity and network placement.

That principal is what must hold a data-plane role on a model endpoint, so misreading it produces a
confident and wrong answer about whether the gateway can call a model. The gateway's virtual network
and outbound addresses decide whether it can reach a private or firewalled endpoint at all.
"""

import pytest
from apim_double import APIM_PRINCIPAL_ID, APIM_PUBLIC_IP, RESOURCE_ID, FakeApim
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


@pytest.mark.asyncio
async def test_network_placement_and_egress_are_captured(
    fake_apim: FakeApim, gateway_repository: InMemoryGatewayRepository
) -> None:
    gateway = await _register(fake_apim, gateway_repository)

    # A classic tier outside any virtual network calls public endpoints from its public address.
    assert gateway.capabilities.virtual_network_type == "None"
    assert gateway.capabilities.egress_ip_addresses == [APIM_PUBLIC_IP]


@pytest.mark.asyncio
async def test_nat_gateway_addresses_replace_the_public_address(
    fake_apim: FakeApim, gateway_repository: InMemoryGatewayRepository
) -> None:
    fake_apim.outbound_public_ip_addresses = ["198.51.100.7"]

    gateway = await _register(fake_apim, gateway_repository)

    assert gateway.capabilities.egress_ip_addresses == ["198.51.100.7"]


@pytest.mark.asyncio
async def test_every_region_of_a_multi_region_gateway_is_captured(
    fake_apim: FakeApim, gateway_repository: InMemoryGatewayRepository
) -> None:
    fake_apim.additional_locations = [
        {"location": "westus2", "publicIPAddresses": ["203.0.113.20"]}
    ]

    gateway = await _register(fake_apim, gateway_repository)

    assert gateway.capabilities.egress_ip_addresses == [APIM_PUBLIC_IP, "203.0.113.20"]


@pytest.mark.asyncio
async def test_a_region_without_a_known_address_leaves_egress_unknown(
    fake_apim: FakeApim, gateway_repository: InMemoryGatewayRepository
) -> None:
    # A partial list cannot prove that an endpoint's firewall admits every call the gateway makes.
    fake_apim.additional_locations = [{"location": "westus2"}]

    gateway = await _register(fake_apim, gateway_repository)

    assert gateway.capabilities.egress_ip_addresses == []


@pytest.mark.asyncio
async def test_shared_tiers_have_no_deterministic_egress(
    fake_apim: FakeApim, gateway_repository: InMemoryGatewayRepository
) -> None:
    # The v2 tiers and Consumption run on shared infrastructure, so the public address they
    # report is not where their outbound calls come from.
    fake_apim.sku_name = "StandardV2"

    gateway = await _register(fake_apim, gateway_repository)

    assert gateway.capabilities.egress_ip_addresses == []


@pytest.mark.asyncio
async def test_an_unreported_network_type_is_not_assumed(
    fake_apim: FakeApim, gateway_repository: InMemoryGatewayRepository
) -> None:
    fake_apim.virtual_network_type = None

    gateway = await _register(fake_apim, gateway_repository)

    # Unknown, not "None": only a reported "None" lets MOSAIC say a private endpoint is unreachable.
    assert gateway.capabilities.virtual_network_type is None
