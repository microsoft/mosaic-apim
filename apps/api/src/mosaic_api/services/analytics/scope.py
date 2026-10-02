"""What an analytics request may see, and how to name what it finds."""

import asyncio
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace

import structlog

from mosaic_api.domain import (
    CostCenterRef,
    DirectoryObject,
    Entitlement,
    EntitlementBinding,
    EntitlementResource,
    EntitlementResourceKind,
    EntitlementSubjectKind,
    Gateway,
    Group,
    Principal,
    PrincipalKind,
    subject_kind_for,
)
from mosaic_api.environments import EnvironmentCatalog
from mosaic_api.errors import DirectoryForbiddenError, NotFoundError, ValidationError
from mosaic_api.integrations.graph import DirectoryLookup
from mosaic_api.services.analytics.models import AnalyticsFilters, ConsumerKind
from mosaic_api.usage_telemetry import (
    AttributionRecord,
    RolledUpApi,
    SummaryDimension,
    UsageRollupState,
    subscription_attribution_key,
)

logger = structlog.get_logger()

_GUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
# Microsoft's own public clients, which callers commonly sign in through.
_KNOWN_APPS = {
    "04b07795-8ddb-461a-bbee-02f9e1bf7b46": "Azure CLI",
    "1950a258-227b-4e31-a9cf-717495945fc2": "Azure PowerShell",
    "aebc6443-996d-45c2-90f0-388ff96faa56": "Visual Studio Code",
}
_APPLICATION_KINDS = {
    PrincipalKind.SERVICE_PRINCIPAL,
    PrincipalKind.MANAGED_IDENTITY,
    PrincipalKind.AGENT_IDENTITY,
}
# Applications whose app-only tokens always name their own app ID.
_OWN_CLIENT_KINDS = {PrincipalKind.SERVICE_PRINCIPAL, PrincipalKind.MANAGED_IDENTITY}
_RESOURCE_LABELS = {
    EntitlementResourceKind.MODEL_API: "Model API",
    EntitlementResourceKind.MCP_SERVER: "MCP server",
    EntitlementResourceKind.MODEL_DEPLOYMENT: "Model deployment",
    EntitlementResourceKind.PRODUCT: "Product",
    EntitlementResourceKind.POOL_MODEL: "Pool model",
}
REMOVED_GATEWAY = "Removed gateway"
UNKNOWN_CALLER = "Unknown caller"
UNKNOWN_APPLICATION = "Unknown application"
UNCLASSIFIED = "Unclassified"


@dataclass(frozen=True)
class GrantInfo:
    """What one grant link stands for, from the attribution registry or a current binding."""

    key: str
    entitlement_id: str | None
    subject_kind: EntitlementSubjectKind | None
    subject_id: str | None
    subject_object_id: str | None
    subject_name: str | None
    resource_kind: EntitlementResourceKind | None
    resource_id: str | None
    resource_name: str | None
    gateway_id: str | None
    publication_id: str | None
    per_member: bool
    # The cost center the grant charges, with its code and name as last recorded.
    cost_center_id: str | None = None
    cost_center_code: str | None = None
    cost_center_name: str | None = None

    @property
    def api_resource_id(self) -> str | None:
        """The ID the governed API its calls reach knows it by.

        A pool model's grant reaches its pool's API, which the pool names.
        """

        if self.resource_kind == EntitlementResourceKind.POOL_MODEL:
            return self.publication_id
        return self.resource_id


def api_resource_id(resource: EntitlementResource) -> str:
    """The ID the governed API a granted resource's calls reach knows it by."""

    if resource.kind == EntitlementResourceKind.POOL_MODEL:
        return resource.scope_id or ""
    return resource.id


def _publication_id(resource: EntitlementResource, publication_id: str | None) -> str | None:
    """A grant's publication. A pool model's pool stands in when no publication is recorded."""

    if publication_id:
        return publication_id
    if resource.kind == EntitlementResourceKind.POOL_MODEL:
        return resource.scope_id or None
    return None


@dataclass(frozen=True)
class Name:
    label: str
    detail: str | None = None
    kind: ConsumerKind | None = None
    principal_id: str | None = None
    principal_kind: PrincipalKind | None = None


def binding_links(binding: EntitlementBinding) -> list[str]:
    """The grant keys a binding links calls by, as summaries record them."""

    keys: list[str] = []
    if binding.attribution_key:
        keys.append(f"trace:{binding.attribution_key.casefold()}")
    if binding.apim_subscription_name:
        key = subscription_attribution_key(binding.gateway_id, binding.apim_subscription_name)
        keys.append(f"subscription:{key}")
    return keys


def consumer_kind(kind: PrincipalKind | str) -> ConsumerKind:
    subject = subject_kind_for(PrincipalKind(kind))
    if subject == EntitlementSubjectKind.USER:
        return "person"
    if subject == EntitlementSubjectKind.SECURITY_GROUP:
        return "group"
    return "application"


class NameCache:
    """Directory objects looked up to name callers MOSAIC doesn't record, kept for an hour.

    Only object IDs are looked up, a request looks up at most ``limit`` of them, and a failed
    lookup is not remembered. After Graph refuses MOSAIC's identity, lookups pause for ten minutes
    rather than failing once per caller.
    """

    def __init__(
        self,
        lookup: DirectoryLookup | None,
        *,
        ttl_seconds: float = 3600,
        max_entries: int = 5000,
        concurrency: int = 25,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lookup = lookup
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._concurrency = concurrency
        self._clock = clock
        self._entries: dict[str, tuple[float, DirectoryObject | None]] = {}
        self._paused_until = 0.0

    async def resolve(
        self, object_ids: Iterable[str], *, limit: int = 200
    ) -> dict[str, DirectoryObject]:
        now = self._clock()
        found: dict[str, DirectoryObject] = {}
        missing: list[str] = []
        # In the caller's order, so the busiest unknown callers are looked up first.
        for object_id in dict.fromkeys(value.casefold() for value in object_ids if value):
            if not _GUID.fullmatch(object_id):
                continue
            cached = self._entries.get(object_id)
            if cached is not None and now - cached[0] < self._ttl:
                if cached[1] is not None:
                    found[object_id] = cached[1]
            else:
                missing.append(object_id)
        lookup = self._lookup
        if lookup is None or not missing or now < self._paused_until:
            return found
        semaphore = asyncio.Semaphore(self._concurrency)
        failed: set[str] = set()

        async def one(object_id: str) -> DirectoryObject | None:
            async with semaphore:
                try:
                    return await lookup.get_object(object_id)
                except DirectoryForbiddenError:
                    self._paused_until = self._clock() + 600
                except Exception:
                    logger.warning("analytics_name_lookup_failed")
                failed.add(object_id)
                return None

        batch = missing[:limit]
        results = await asyncio.gather(*(one(object_id) for object_id in batch))
        if len(self._entries) + len(batch) > self._max_entries:
            self._entries.clear()
        for object_id, value in zip(batch, results, strict=True):
            if object_id in failed:
                continue
            self._entries[object_id] = (now, value)
            if value is not None:
                found[object_id] = value
        return found


@dataclass
class Scope:
    tenant_id: str
    filters: AnalyticsFilters
    # Every current gateway, for naming, and those the filters select.
    all_gateways: dict[str, Gateway]
    gateways: dict[str, Gateway]
    # None reads every gateway's figures, including those of gateways since removed.
    gateway_ids: list[str] | None
    states: dict[str, UsageRollupState]
    governed: dict[str, list[RolledUpApi]]
    apis: dict[tuple[str, str], RolledUpApi]
    allowed_apis: set[tuple[str, str]] | None
    resource_ids: set[str] | None
    environments: dict[str, str]
    grants: dict[str, GrantInfo]
    links_by_entitlement: dict[str, set[str]]
    entitlements: dict[str, Entitlement]
    principals_by_object: dict[str, Principal]
    principals_by_id: dict[str, Principal]
    principals_by_app: dict[str, Principal]
    groups: dict[str, Group]
    subject_names: dict[str, str]
    directory: dict[str, DirectoryObject] = field(default_factory=dict)
    # Every cost center that exists now, by ID, for naming grants by their current name.
    cost_centers: dict[str, CostCenterRef] = field(default_factory=dict)

    # -- which gateways count ---------------------------------------------------------------

    def in_scope(self, gateway_id: str | None) -> bool:
        if self.gateway_ids is None:
            return True
        return gateway_id is not None and gateway_id in self.gateway_ids

    def live_gateways(self) -> list[Gateway]:
        """Current gateways in scope that MOSAIC reads telemetry for."""

        return [gateway for gateway in self.gateways.values() if self.governed.get(gateway.id)]

    def live_states(self) -> list[UsageRollupState]:
        return [
            state for gateway in self.live_gateways() if (state := self.states.get(gateway.id))
        ]

    # -- filters ----------------------------------------------------------------------------

    def api_allowed(self, gateway_id: str, api_name: str) -> bool:
        return self.allowed_apis is None or (gateway_id, api_name) in self.allowed_apis

    def entry_allowed(self, gateway_id: str, dimension: SummaryDimension, key: str) -> bool:
        if dimension in {"grant", "grantCaller"}:
            return self.grant_allowed(key.partition("|")[0])
        if self.allowed_apis is None:
            return True
        if dimension == "api":
            return self.api_allowed(gateway_id, key)
        if dimension in {"model", "clientApp"}:
            return self.api_allowed(gateway_id, key.rpartition("|")[2])
        if dimension == "denial":
            parts = key.split("|", 3)
            return len(parts) == 4 and self.api_allowed(gateway_id, parts[3])
        if dimension == "unattributed":
            return self.api_allowed(gateway_id, key.partition("|")[0])
        if dimension == "deployment":
            return any(
                key in api.deployment_keys()
                for (api_gateway, _), api in self.apis.items()
                if api_gateway == gateway_id and self.api_allowed(api_gateway, api.api_name)
            )
        return False

    def grant_allowed(self, grant_key: str) -> bool:
        if (
            self.resource_ids is None
            and self.filters.subject_kind is None
            and self.filters.cost_center_id is None
        ):
            return True
        grant = self.grants.get(grant_key)
        if grant is None:
            return False
        if self.resource_ids is not None and not (
            grant.resource_id in self.resource_ids or grant.publication_id in self.resource_ids
        ):
            return False
        if (
            self.filters.cost_center_id is not None
            and grant.cost_center_id != self.filters.cost_center_id
        ):
            return False
        return self.filters.subject_kind is None or grant.subject_kind == self.filters.subject_kind

    def entitlement_gateway(self, entitlement: Entitlement) -> str | None:
        if entitlement.binding is not None:
            return entitlement.binding.gateway_id
        resource_id = api_resource_id(entitlement.resource)
        for (gateway_id, _), api in self.apis.items():
            if api.resource_id == resource_id:
                return gateway_id
        return None

    def entitlement_allowed(self, entitlement: Entitlement) -> bool:
        if not self.in_scope(self.entitlement_gateway(entitlement)):
            return False
        if (
            self.resource_ids is not None
            and api_resource_id(entitlement.resource) not in self.resource_ids
        ):
            return False
        if (
            self.filters.cost_center_id is not None
            and entitlement.cost_center_id != self.filters.cost_center_id
        ):
            return False
        return (
            self.filters.subject_kind is None
            or entitlement.subject.kind == self.filters.subject_kind
        )

    def grant_api(self, gateway_id: str, grant: GrantInfo | None) -> RolledUpApi | None:
        """The governed API a grant's calls reached on a gateway, when it grants one."""

        resource_id = grant.api_resource_id if grant is not None else None
        if resource_id is None:
            return None
        return next(
            (
                api
                for (api_gateway, _), api in self.apis.items()
                if api_gateway == gateway_id
                and resource_id in {api.resource_id, api.publication_id}
            ),
            None,
        )

    def cost_center(self, grant: GrantInfo | None) -> CostCenterRef | None:
        """The cost center a grant charges, by its current name if it still exists."""

        if grant is None or not grant.cost_center_id:
            return None
        current = self.cost_centers.get(grant.cost_center_id)
        if current is not None:
            return current
        return CostCenterRef(
            id=grant.cost_center_id,
            name=grant.cost_center_name or grant.cost_center_code or "Removed cost center",
            code=grant.cost_center_code or "",
        )

    # -- names ------------------------------------------------------------------------------

    def gateway_name(self, gateway_id: str | None) -> str:
        gateway = self.all_gateways.get(gateway_id or "")
        return gateway.name if gateway else REMOVED_GATEWAY

    def environment_name(self, key: str | None) -> str:
        if key is None:
            return UNCLASSIFIED
        return self.environments.get(key, key)

    def api_label(self, gateway_id: str, api_name: str) -> str:
        api = self.apis.get((gateway_id, api_name))
        return api.display_name if api else api_name

    def caller(self, object_id: str | None) -> Name:
        if not object_id:
            return Name(UNKNOWN_CALLER)
        principal = self.principals_by_object.get(object_id.casefold())
        if principal is not None:
            return Name(
                principal.label or principal.detail or object_id,
                principal.detail,
                consumer_kind(principal.kind),
                principal.id,
                PrincipalKind(principal.kind),
            )
        found = self.directory.get(object_id.casefold())
        if found is not None:
            return Name(
                found.display_name or found.detail or object_id,
                found.detail,
                consumer_kind(found.kind),
                found.principal_id,
                found.kind,
            )
        name = self.subject_names.get(object_id.casefold())
        if name is not None:
            return Name(name, object_id)
        return Name(UNKNOWN_CALLER, object_id)

    def application(self, app_id: str | None, callers: Iterable[str] = ()) -> Name:
        """An app ID's name, from the applications MOSAIC knows or Microsoft's public clients.

        MOSAIC keeps no app IDs for service principals and managed identities. But when one of
        them is among ``callers``, the object IDs that signed in with this app ID, its app-only
        token named its own app ID, so the app ID is that application's.
        """

        if not app_id:
            return Name(UNKNOWN_APPLICATION)
        principal = self.principals_by_app.get(app_id.casefold())
        if principal is None:
            principal = next(
                (
                    found
                    for caller in sorted(callers)
                    if (found := self.principals_by_object.get(caller.casefold())) is not None
                    and found.kind in _OWN_CLIENT_KINDS
                ),
                None,
            )
        if principal is not None:
            return Name(principal.label or app_id, app_id, "application", principal.id)
        known = _KNOWN_APPS.get(app_id.casefold())
        return Name(known or UNKNOWN_APPLICATION, app_id, "application")

    def subject(self, grant: GrantInfo | None) -> Name:
        if grant is None:
            return Name("Unknown grant")
        if grant.subject_id is not None:
            principal = self.principals_by_id.get(grant.subject_id)
            if principal is not None:
                return Name(
                    principal.label or principal.detail or principal.object_id,
                    principal.detail,
                    consumer_kind(principal.kind),
                    principal.id,
                    PrincipalKind(principal.kind),
                )
            group = self.groups.get(grant.subject_id)
            if group is not None:
                return Name(group.name, "MOSAIC group", "group")
        return Name(grant.subject_name or "Unknown subject", grant.subject_object_id)

    def entitlement_subject(self, entitlement: Entitlement) -> Name:
        return self.subject(grant_for(entitlement, self))

    def resource_label(
        self, kind: EntitlementResourceKind | str | None, resource_id: str | None, name: str | None
    ) -> str:
        if name:
            return name
        for api in self.apis.values():
            if api.resource_id == resource_id:
                return api.display_name
        if not kind:
            return "Resource"
        return _RESOURCE_LABELS.get(EntitlementResourceKind(kind), "Resource")


def grant_for(entitlement: Entitlement, scope: Scope) -> GrantInfo:
    """The grant an entitlement stands for, named from its current records."""

    for key in scope.links_by_entitlement.get(entitlement.id, set()):
        grant = scope.grants.get(key)
        if grant is not None:
            return grant
    object_id, name = subject_identity(entitlement, scope.principals_by_id, scope.groups)
    return GrantInfo(
        key=f"entitlement:{entitlement.id}",
        entitlement_id=entitlement.id,
        subject_kind=EntitlementSubjectKind(entitlement.subject.kind),
        subject_id=entitlement.subject.id,
        subject_object_id=object_id,
        subject_name=name,
        resource_kind=EntitlementResourceKind(entitlement.resource.kind),
        resource_id=entitlement.resource.id,
        resource_name=None,
        gateway_id=scope.entitlement_gateway(entitlement),
        publication_id=_publication_id(
            entitlement.resource,
            entitlement.runtime.publication_id if entitlement.runtime else None,
        ),
        per_member=entitlement.subject.kind == EntitlementSubjectKind.SECURITY_GROUP,
        cost_center_id=entitlement.cost_center_id,
        cost_center_code=(current.code if (current := scope.cost_centers.get(
            entitlement.cost_center_id
        )) else None),
        cost_center_name=current.name if current else None,
    )


def subject_identity(
    entitlement: Entitlement, principals: dict[str, Principal], groups: dict[str, Group]
) -> tuple[str | None, str | None]:
    principal = principals.get(entitlement.subject.id)
    if principal is not None:
        return principal.object_id.casefold(), principal.label or principal.detail
    group = groups.get(entitlement.subject.id)
    return None, group.name if group else None


def build_grants(
    records: Iterable[AttributionRecord],
    entitlements: Iterable[Entitlement],
    principals: dict[str, Principal],
    groups: dict[str, Group],
) -> dict[str, GrantInfo]:
    entitlements = list(entitlements)
    by_id = {entitlement.id: entitlement for entitlement in entitlements}
    grants: dict[str, GrantInfo] = {}
    for record in records:
        link = "trace" if record.kind == "grant" else "subscription"
        key = f"{link}:{record.key}"
        grants[key] = GrantInfo(
            key=key,
            entitlement_id=record.entitlement_id,
            subject_kind=EntitlementSubjectKind(record.subject.kind),
            subject_id=record.subject.id,
            subject_object_id=(record.subject_object_id or "").casefold() or None,
            subject_name=record.subject_name,
            resource_kind=EntitlementResourceKind(record.resource.kind),
            resource_id=record.resource.id,
            resource_name=record.resource_name,
            gateway_id=record.gateway_id,
            publication_id=_publication_id(record.resource, record.publication_id),
            per_member=record.per_member,
            cost_center_id=record.cost_center_id,
            cost_center_code=record.cost_center_code,
            cost_center_name=record.cost_center_name,
        )
    for entitlement in entitlements:
        binding = entitlement.binding
        if binding is None:
            continue
        object_id, name = subject_identity(entitlement, principals, groups)
        for key in binding_links(binding):
            grants.setdefault(
                key,
                GrantInfo(
                    key=key,
                    entitlement_id=entitlement.id,
                    subject_kind=EntitlementSubjectKind(entitlement.subject.kind),
                    subject_id=entitlement.subject.id,
                    subject_object_id=object_id,
                    subject_name=name,
                    resource_kind=EntitlementResourceKind(entitlement.resource.kind),
                    resource_id=entitlement.resource.id,
                    resource_name=None,
                    gateway_id=binding.gateway_id,
                    publication_id=_publication_id(
                        entitlement.resource,
                        entitlement.runtime.publication_id if entitlement.runtime else None,
                    ),
                    per_member=binding.attribution_per_member,
                    cost_center_id=entitlement.cost_center_id,
                ),
            )
    # A record copied before cost centers names none; the grant it stands for still does.
    for key, grant in list(grants.items()):
        if grant.cost_center_id or not grant.entitlement_id:
            continue
        current = by_id.get(grant.entitlement_id)
        if current is not None:
            grants[key] = replace(grant, cost_center_id=current.cost_center_id)
    return grants


def resolve_gateways(
    filters: AnalyticsFilters, gateways: dict[str, Gateway], catalog: EnvironmentCatalog
) -> tuple[dict[str, Gateway], list[str] | None]:
    """The current gateways a request covers, and the IDs to read figures for."""

    selected = dict(gateways)
    narrowed = False
    if filters.gateway_id is not None:
        gateway = gateways.get(filters.gateway_id)
        if gateway is None:
            raise NotFoundError("Gateway not found", details={"gatewayId": filters.gateway_id})
        selected = {gateway.id: gateway}
        narrowed = True
    if filters.environment is not None:
        keys = {environment.key for environment in catalog.environments}
        if filters.environment not in keys:
            raise ValidationError(
                "Unknown environment", details={"environment": filters.environment}
            )
        selected = {
            gateway_id: gateway
            for gateway_id, gateway in selected.items()
            if gateway.environment == filters.environment
        }
        narrowed = True
    return selected, (sorted(selected) if narrowed else None)


def resolve_resource(
    resource_id: str, apis: dict[tuple[str, str], RolledUpApi]
) -> tuple[set[tuple[str, str]], set[str]]:
    matches = {
        key
        for key, api in apis.items()
        if resource_id in {api.resource_id, api.publication_id}
    }
    if not matches:
        raise NotFoundError(
            "No governed model API, model pool, or MCP server has that ID",
            details={"resourceId": resource_id},
        )
    ids = {resource_id}
    for key in matches:
        api = apis[key]
        ids.add(api.resource_id)
        if api.publication_id:
            ids.add(api.publication_id)
    return matches, ids


def is_application(principal: Principal) -> bool:
    return principal.kind in _APPLICATION_KINDS
