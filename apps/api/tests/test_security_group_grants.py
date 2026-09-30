from collections.abc import Sequence

import pytest
from mosaic_api.domain import (
    AuditEvent,
    EntitlementCreate,
    EntitlementEnforcement,
    EntitlementResource,
    EntitlementSubject,
    ModelApi,
    Principal,
    PrincipalKind,
    RequestEnforcement,
    new_id,
)
from mosaic_api.repositories import (
    InMemoryDirectoryRepository,
    InMemoryEntitlementRepository,
    InMemoryGatewayRepository,
    InMemoryModelEndpointRepository,
)
from mosaic_api.services.directory import Actor
from mosaic_api.services.entitlements import EntitlementService
from mosaic_api.services.portal_access import PortalAccessService

TENANT = "tenant-test"
USER_OBJECT = "11111111-1111-1111-1111-111111111111"
GROUP_OBJECT = "22222222-2222-2222-2222-222222222222"


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


class Harness:
    def __init__(self) -> None:
        self.directory = InMemoryDirectoryRepository()
        self.entitlements = InMemoryEntitlementRepository()
        self.gateways = InMemoryGatewayRepository()
        self.actor = Actor(object_id="admin", tenant_id=TENANT)
        self.lookup = FakeLookup({USER_OBJECT: {GROUP_OBJECT}})
        self.service = EntitlementService(
            self.entitlements,
            directory_repository=self.directory,
            gateway_repository=self.gateways,
            endpoint_repository=InMemoryModelEndpointRepository(),
            directory_lookup=self.lookup,
        )

    async def seed(self) -> tuple[Principal, Principal]:
        user = Principal(
            id="principal-user",
            tenant_id=TENANT,
            object_id=USER_OBJECT,
            kind=PrincipalKind.USER,
            label="Ada",
        )
        group = Principal(
            id="principal-group",
            tenant_id=TENANT,
            object_id=GROUP_OBJECT,
            kind=PrincipalKind.SECURITY_GROUP,
            label="Platform",
        )
        await self.directory.create_principal(user, audit())
        await self.directory.create_principal(group, audit())
        await self.gateways.save_model_api(
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
        return user, group


async def test_security_group_grant_validates_kind_and_resolves_through_lookup() -> None:
    harness = Harness()
    user, group = await harness.seed()

    mismatched = EntitlementCreate(
        subject=EntitlementSubject(kind="user", id=group.id),
        resource=EntitlementResource(kind="modelApi", id="model-api"),
    )
    with pytest.raises(Exception, match="securityGroup"):
        await harness.service.create_entitlement(harness.actor, mismatched)

    grant = await harness.service.create_entitlement(
        harness.actor,
        EntitlementCreate(
            subject=EntitlementSubject(kind="securityGroup", id=group.id),
            resource=EntitlementResource(kind="modelApi", id="model-api"),
            enforcement=EntitlementEnforcement(
                requests=RequestEnforcement(
                    counter_key_expression="@(context.Subscription.Id)",
                    calls=10,
                    renewal_period_seconds=60,
                )
            ),
        ),
    )

    resolved = await harness.service.resolve_for_principal(harness.actor, user.id)
    assert [(item.entitlement.id, item.via, item.effective) for item in resolved] == [
        (grant.id, "securityGroup", True)
    ]
    assert resolved[0].via_group_id == group.id
    assert resolved[0].via_group_name == "Platform"


async def test_portal_resolution_uses_token_groups_without_graph_lookup() -> None:
    harness = Harness()
    _, group = await harness.seed()
    await harness.service.create_entitlement(
        harness.actor,
        EntitlementCreate(
            subject=EntitlementSubject(kind="securityGroup", id=group.id),
            resource=EntitlementResource(kind="modelApi", id="model-api"),
        ),
    )

    portal_items = await harness.service.resolve_for_object_id(
        Actor(object_id=USER_OBJECT, tenant_id=TENANT),
        USER_OBJECT,
        group_object_ids=frozenset({GROUP_OBJECT}),
    )
    assert len(portal_items) == 1
    assert portal_items[0].effective is True

    no_member = await harness.service.resolve_for_object_id(
        Actor(object_id=USER_OBJECT, tenant_id=TENANT),
        USER_OBJECT,
        group_object_ids=frozenset(),
    )
    assert no_member == []


async def test_portal_list_includes_security_group_grants_for_recorded_group_members() -> None:
    harness = Harness()
    _, group = await harness.seed()
    grant = await harness.service.create_entitlement(
        harness.actor,
        EntitlementCreate(
            subject=EntitlementSubject(kind="securityGroup", id=group.id),
            resource=EntitlementResource(kind="modelApi", id="model-api"),
        ),
    )
    portal = PortalAccessService(
        harness.service,
        repository=harness.entitlements,
        directory_repository=harness.directory,
        gateway_repository=harness.gateways,
        credential_factory=lambda _resource: None,  # type: ignore[arg-type,return-value]
    )

    listed = await portal.list_for_caller(
        Actor(object_id=USER_OBJECT, tenant_id=TENANT, group_ids=frozenset({GROUP_OBJECT}))
    )
    assert [item.id for item in listed] == [grant.id]
