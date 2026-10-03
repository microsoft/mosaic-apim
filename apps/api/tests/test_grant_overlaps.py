from collections.abc import Sequence

from fastapi import FastAPI
from fastapi.testclient import TestClient
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from mosaic_api.domain import (
    AuditEvent,
    Entitlement,
    EntitlementResource,
    EntitlementSubject,
    ModelApi,
    Principal,
    PrincipalKind,
    new_id,
)
from mosaic_api.main import create_app
from mosaic_api.model_pools import (
    ModelPool,
    PoolModel,
    backend_pool_name,
    model_pool_id,
    pool_model_id,
)
from mosaic_api.repositories import (
    InMemoryDirectoryRepository,
    InMemoryEntitlementRepository,
    InMemoryGatewayRepository,
)
from mosaic_api.services.directory import Actor
from mosaic_api.services.overlaps import GrantOverlapService

TENANT = "tenant-test"
USER_OBJECT = "11111111-1111-1111-1111-111111111111"
GROUP_A_OBJECT = "22222222-2222-2222-2222-222222222222"
GROUP_B_OBJECT = "33333333-3333-3333-3333-333333333333"


class FakeLookup:
    def __init__(self, memberships: dict[str, set[str]]) -> None:
        self.memberships = memberships

    async def member_groups(self, object_id: str, group_object_ids: Sequence[str]) -> set[str]:
        return self.memberships.get(object_id, set()).intersection(group_object_ids)


def audit() -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id=TENANT,
        action="fixture",
        resource_type="fixture",
        resource_id="fixture",
        actor_object_id="admin",
    )


async def seed(
    directory: InMemoryDirectoryRepository,
    entitlements: InMemoryEntitlementRepository,
    gateways: InMemoryGatewayRepository,
) -> None:
    principals = [
        Principal(
            id="principal-user",
            tenant_id=TENANT,
            object_id=USER_OBJECT,
            kind=PrincipalKind.USER,
            label="Ada",
        ),
        Principal(
            id="group-a",
            tenant_id=TENANT,
            object_id=GROUP_A_OBJECT,
            kind=PrincipalKind.SECURITY_GROUP,
            label="Group A",
        ),
        Principal(
            id="group-b",
            tenant_id=TENANT,
            object_id=GROUP_B_OBJECT,
            kind=PrincipalKind.SECURITY_GROUP,
            label="Group B",
        ),
    ]
    for principal in principals:
        await directory.create_principal(principal, audit())
    await gateways.save_model_api(
        ModelApi(
            id="model-api",
            tenant_id=TENANT,
            gateway_id="gateway",
            api_name="chat",
            display_name="Chat",
            path="chat",
            imported_from_snapshot_id="snapshot",
        ),
        audit(),
    )
    resource = EntitlementResource(kind="modelApi", id="model-api")
    for entitlement in [
        Entitlement(
            id="direct",
            tenant_id=TENANT,
            subject=EntitlementSubject(kind="user", id="principal-user"),
            resource=resource,
        ),
        Entitlement(
            id="group-a-grant",
            tenant_id=TENANT,
            subject=EntitlementSubject(kind="securityGroup", id="group-a"),
            resource=resource,
        ),
        Entitlement(
            id="group-b-grant",
            tenant_id=TENANT,
            subject=EntitlementSubject(kind="securityGroup", id="group-b"),
            resource=resource,
        ),
    ]:
        await entitlements.create_entitlement(entitlement, audit())


async def test_overlap_report_covers_groups_direct_and_multiple_group_membership() -> None:
    directory = InMemoryDirectoryRepository()
    entitlements = InMemoryEntitlementRepository()
    gateways = InMemoryGatewayRepository()
    await seed(directory, entitlements, gateways)

    report = await GrantOverlapService(
        entitlements,
        directory_repository=directory,
        gateway_repository=gateways,
        directory_lookup=FakeLookup({USER_OBJECT: {GROUP_A_OBJECT, GROUP_B_OBJECT}}),
    ).list_overlaps(Actor(object_id="admin", tenant_id=TENANT))

    assert report.membership_checked is True
    assert {item.kind for item in report.overlaps} == {
        "groups",
        "directAndGroup",
        "multipleGroups",
    }
    assert next(item for item in report.overlaps if item.kind == "groups").principal_id is None


async def test_overlaps_on_a_pool_model_are_labelled_with_its_pool() -> None:
    directory = InMemoryDirectoryRepository()
    entitlements = InMemoryEntitlementRepository()
    gateways = InMemoryGatewayRepository()
    await seed(directory, entitlements, gateways)
    api_name = "mosaic-pool-anthropic"
    pool_id = model_pool_id(TENANT, "gateway", api_name)
    model_id = pool_model_id(pool_id, "claude-opus-4-5")
    await gateways.save_model_pool(
        ModelPool(
            id=pool_id,
            tenant_id=TENANT,
            gateway_id="gateway",
            display_name="Anthropic",
            vendor="Anthropic",
            api_name=api_name,
            api_path="mosaic/pool-anthropic",
            fragment_name=api_name,
            product_name=api_name,
            subscription_name=api_name,
            models=[
                PoolModel(
                    id=model_id,
                    public_name="claude-opus-4-5",
                    display_name="Claude Opus 4.5",
                    backend_pool_name=backend_pool_name(api_name, model_id, "claude-opus-4-5"),
                )
            ],
        ),
        audit(),
    )
    resource = EntitlementResource(kind="poolModel", id=model_id, scope_id=pool_id)
    for entitlement in [
        Entitlement(
            id="pool-direct",
            tenant_id=TENANT,
            subject=EntitlementSubject(kind="user", id="principal-user"),
            resource=resource,
        ),
        Entitlement(
            id="pool-group-a",
            tenant_id=TENANT,
            subject=EntitlementSubject(kind="securityGroup", id="group-a"),
            resource=resource,
        ),
    ]:
        await entitlements.create_entitlement(entitlement, audit())

    report = await GrantOverlapService(
        entitlements,
        directory_repository=directory,
        gateway_repository=gateways,
        directory_lookup=FakeLookup({USER_OBJECT: {GROUP_A_OBJECT}}),
    ).list_overlaps(Actor(object_id="admin", tenant_id=TENANT))

    [overlap] = [item for item in report.overlaps if item.resource.kind == "poolModel"]
    assert (overlap.kind, overlap.resource_label) == (
        "directAndGroup",
        "Claude Opus 4.5 in Anthropic",
    )


async def test_overlap_report_degrades_when_membership_lookup_is_unavailable() -> None:
    directory = InMemoryDirectoryRepository()
    entitlements = InMemoryEntitlementRepository()
    gateways = InMemoryGatewayRepository()
    await seed(directory, entitlements, gateways)

    report = await GrantOverlapService(
        entitlements,
        directory_repository=directory,
        gateway_repository=gateways,
        directory_lookup=None,
    ).list_overlaps(Actor(object_id="admin", tenant_id=TENANT))

    assert report.membership_checked is False
    assert [item.kind for item in report.overlaps] == ["groups"]
    assert report.skipped


def test_overlaps_route_is_declared_before_entitlement_id_route() -> None:
    app: FastAPI = create_app(
        Settings(
            environment=Environment.TEST,
            auth_mode=AuthMode.LOCAL,
            repository_backend=RepositoryBackend.MEMORY,
            tenant_id=TENANT,
        )
    )
    with TestClient(app) as client:
        response = client.get("/api/v1/entitlements/overlaps")
    assert response.status_code == 200, response.text
    assert "overlaps" in response.json()
