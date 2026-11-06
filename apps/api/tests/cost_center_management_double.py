"""Fictional, lease-checking doubles shared by cost-center management tests."""

from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, cast

from azure.cosmos.aio import CosmosClient
from mosaic_api.cost_centers import CostCenter
from mosaic_api.domain import AuditEvent, DirectoryObject, Entitlement, PrincipalKind, new_id
from mosaic_api.integrations.graph import FakeDirectoryLookup, FakeGroupManagement, GraphGroup
from mosaic_api.repositories import (
    CostCenterRepository,
    InMemoryCostCenterRepository,
    InMemoryDirectoryRepository,
    InMemoryEntitlementRepository,
    InMemoryGatewayRepository,
)
from mosaic_api.repositories.cosmos_cost_centers import CosmosCostCenterRepository
from mosaic_api.services.cost_centers import CostCenterService
from mosaic_api.services.directory import Actor
from test_model_access_repositories import Container, Cosmos

TENANT = "10000000-0000-4000-8000-000000000001"
GROUP = "20000000-0000-4000-8000-000000000002"
USER = "30000000-0000-4000-8000-000000000003"
WORKLOAD = "40000000-0000-4000-8000-000000000004"
OTHER_GROUP = "50000000-0000-4000-8000-000000000005"
ADMIN = Actor(object_id="fictional-admin", tenant_id=TENANT)
BASELINE = ("User.ReadBasic.All", "GroupMember.Read.All", "Application.Read.All")
ALL_WRITES = (*BASELINE, "Group.Create", "GroupMember.ReadWrite.All", "Application.ReadWrite.All")


def audit() -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id=TENANT,
        action="test.seed",
        resource_type="costCenter",
        resource_id="center",
        actor_object_id=ADMIN.object_id,
    )


class DirectoryContainer(Container):
    async def query_items(
        self, *, query: str, parameters: list[dict[str, Any]], partition_key: str
    ) -> AsyncIterator[dict[str, Any]]:
        values = {item["name"]: item["value"] for item in parameters}
        items = [
            deepcopy(item)
            for (tenant_id, _), item in self.items.items()
            if tenant_id == partition_key
            and item.get("entityType") == values["@entityType"]
            and (
                "@costCenterId" not in values or item.get("costCenterId") == values["@costCenterId"]
            )
            and (
                "@memberObjectId" not in values
                or item.get("memberObjectId") == values["@memberObjectId"]
            )
            and ("c.state IN" not in query or item.get("state") in {"pending", "unknown"})
        ]
        if "ORDER BY" in query:
            items.sort(key=lambda item: (item["updatedAt"], item["id"]), reverse=True)
        for item in items[: values.get("@limit", len(items))]:
            yield item


class CheckedLookup(FakeDirectoryLookup):
    def __init__(
        self, gateways: InMemoryGatewayRepository, *, permissions: tuple[str, ...] = ALL_WRITES
    ) -> None:
        super().__init__(
            [
                DirectoryObject(object_id=USER, kind="user", display_name="Fictional person"),
                DirectoryObject(
                    object_id=WORKLOAD, kind="servicePrincipal", display_name="Fictional workload"
                ),
                DirectoryObject(
                    object_id=GROUP, kind="securityGroup", display_name="Fictional research"
                ),
                DirectoryObject(
                    object_id=OTHER_GROUP, kind="securityGroup", display_name="Fictional other"
                ),
            ],
            tenant_id=TENANT,
            application_permissions=permissions,
        )
        self.gateways = gateways
        self.reads = 0

    def outside_lease(self) -> None:
        assert not self.gateways.scope_leases, "External Graph I/O inside a Cosmos-only lease"
        self.reads += 1

    async def application_permissions(self) -> frozenset[str]:
        self.outside_lease()
        return await super().application_permissions()

    async def get_object(self, object_id: str) -> DirectoryObject | None:
        self.outside_lease()
        return await super().get_object(object_id)


class AccessChecks:
    def __init__(self, gateways: InMemoryGatewayRepository) -> None:
        self.gateways = gateways
        self.validated: list[CostCenter] = []
        self.synced: list[str] = []
        self.removed: list[tuple[str, str, PrincipalKind | None]] = []
        self.validation_error: Exception | None = None
        self.removal_error: Exception | None = None

    async def validate_cost_center_access(self, actor: Actor, proposed_center: CostCenter) -> None:
        assert not self.gateways.scope_leases
        self.validated.append(proposed_center.model_copy(deep=True))
        if self.validation_error is not None:
            raise self.validation_error

    async def sync_cost_center_access(self, actor: Actor, cost_center_id: str) -> list[Entitlement]:
        assert not self.gateways.scope_leases
        self.synced.append(cost_center_id)
        return []

    async def member_removed_from_cost_center(
        self,
        actor: Actor,
        cost_center_id: str,
        member_object_id: str,
        member_kind: PrincipalKind | None,
    ) -> None:
        assert not self.gateways.scope_leases
        if self.removal_error is not None:
            raise self.removal_error
        self.removed.append((cost_center_id, member_object_id, member_kind))

    async def resume_rechecks(self, actor: Actor, cost_center_id: str | None = None) -> list[str]:
        assert not self.gateways.scope_leases
        return []

    async def request_recheck(self, actor: Actor, cost_center_id: str, **kwargs: Any) -> None:
        return None


@dataclass
class Management:
    service: CostCenterService
    repository: CostCenterRepository
    local_directory: InMemoryDirectoryRepository
    gateways: InMemoryGatewayRepository
    entitlements: InMemoryEntitlementRepository
    lookup: CheckedLookup
    graph: FakeGroupManagement
    checks: AccessChecks
    cosmos: Cosmos | None


def management(
    backend: str = "memory",
    *,
    permissions: tuple[str, ...] = ALL_WRITES,
    groups: tuple[GraphGroup, ...] = (),
) -> Management:
    directory = InMemoryDirectoryRepository()
    gateways = InMemoryGatewayRepository()
    entitlements = InMemoryEntitlementRepository()
    cosmos = None
    repository: CostCenterRepository
    if backend == "cosmos":
        cosmos = Cosmos()
        cosmos.containers["desired"] = DirectoryContainer()
        repository = CosmosCostCenterRepository(
            cast(CosmosClient, cosmos), "db", "desired", "audit"
        )
    else:
        repository = InMemoryCostCenterRepository(directory)
    lookup = CheckedLookup(gateways, permissions=permissions)
    graph = FakeGroupManagement(lookup, groups=groups)
    checks = AccessChecks(gateways)
    service = CostCenterService(
        repository,
        directory_repository=directory,
        gateway_repository=gateways,
        entitlement_repository=entitlements,
        entitlements=checks,
        directory_lookup=lookup,
        group_management=graph,
        directory_tenant_id=TENANT,
    )
    return Management(
        service, repository, directory, gateways, entitlements, lookup, graph, checks, cosmos
    )
