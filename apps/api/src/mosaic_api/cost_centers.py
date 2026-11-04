"""Cost centers: who every grant, and so every call, is charged to. See ADR 0022.

A cost center spans gateways. Administrators author it as desired state: its name and unique code,
its owners, its Entra binding, whether its grants may use keys, per-person default limits for each
model or MCP server, and optional pooled monthly quotas. Every grant names one, so a call's cost
center is decided by the grant the gateway matched. A caller holding grants under several names
one with the ``x-mosaic-cost-center`` header.

Custom centers require a verified dedicated Entra group. General is exempt by its stable identity.
Neither a legacy local ``groupId`` nor a default preference proves custom-center membership.
"""

import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from mosaic_api.cost_center_directory import (
    CostCenterGroupSetup,
    CostCenterSetupState,
    EntraGroupBinding,
)
from mosaic_api.domain import (
    COST_CENTER_CODE_PATTERN,
    GENERAL_COST_CENTER_CODE,
    GENERAL_COST_CENTER_NAME,
    CostCenterRef,
    EntitlementEnforcement,
    EntitlementResource,
    EntitlementResourceKind,
    Entity,
    MosaicModel,
    Principal,
    PrincipalKind,
    QuotaPeriod,
    RequestEnforcement,
    TokenEnforcement,
    deterministic_id,
    general_cost_center_id,
    new_id,
    utc_now,
)

# Grant limits name the standard subscription counter, which the governed policy replaces with the
# grant's own counter. A cost center's per-person defaults become grant limits the same way.
SUBSCRIPTION_COUNTER = "@(context.Subscription.Id)"
_EMAIL = r"[^@\s]+@[^@\s]+\.[^@\s]+"
MAX_OWNERS = 20
MAX_MEMBERS = 500
MAX_LIMITS = 200
MAX_RESOURCE_ACCESS = 200
# More pending rechecks than this become one recheck of every grant under the cost center.
MAX_PENDING_RECHECKS = 50
# The lease every change to cost centers, the tenant default or a principal's default holds, so
# deleting a cost center can't race a change that makes it someone's default.
COST_CENTERS_SCOPE = "cost-centers"
COST_CENTERS_BUSY = "Another cost center change is in progress. Try again in a moment."


def normalize_code(value: str) -> str:
    candidate = value.strip()
    if not COST_CENTER_CODE_PATTERN.fullmatch(candidate):
        raise ValueError(
            "A cost center's code is 1 to 64 letters, digits, dots, hyphens and underscores"
        )
    return candidate


def _owners(values: list[str]) -> list[str]:
    owners: list[str] = []
    for value in values:
        candidate = value.strip()
        if not re.fullmatch(_EMAIL, candidate) or len(candidate) > 254:
            raise ValueError("Each owner is an email address")
        if candidate.casefold() not in {owner.casefold() for owner in owners}:
            owners.append(candidate)
    if len(owners) > MAX_OWNERS:
        raise ValueError(f"A cost center has at most {MAX_OWNERS} owners")
    return owners


class CostCenterMember(MosaicModel):
    """A principal that may charge the cost center: a person, application, agent or group."""

    principal_id: str
    added_at: datetime = Field(default_factory=utc_now)
    added_by: str | None = None


RecheckReason = Literal["memberRemoved", "defaultChanged", "tenantDefaultChanged"]


class PendingRecheck(MosaicModel):
    """Grants under the cost center to check again, because their subjects may have lost the right
    to charge it.

    MOSAIC records one before the change that may cause it, and removes it once every grant it
    covers has been checked: revoked if its subject can no longer charge the cost center, kept if
    it still can. Revoking a grant needs its publication's lock, which an apply holds while it
    runs. A grant that can't be revoked yet keeps the recheck pending, narrowed to the grants it
    hasn't finished, and applies leave every grant a pending recheck covers out of runtime access.
    See ADR 0022.
    """

    id: str = Field(default_factory=lambda: new_id("recheck"))
    reason: RecheckReason
    # The principal whose grants to check, or None for every grant under the cost center.
    subject_id: str | None = None
    # Once a check couldn't finish, the grants it has left; None until then.
    entitlement_ids: list[str] | None = None
    requested_at: datetime = Field(default_factory=utc_now)
    requested_by: str | None = None
    last_checked_at: datetime | None = None
    verification_error: str | None = None

    def covers(self, entitlement_id: str, subject_id: str) -> bool:
        if self.entitlement_ids is not None:
            return entitlement_id in self.entitlement_ids
        return self.subject_id is None or self.subject_id == subject_id


def with_recheck(pending: list[PendingRecheck], recheck: PendingRecheck) -> list[PendingRecheck]:
    """``pending`` with ``recheck`` added, unless a recheck already there covers what it does."""

    if any(
        item.entitlement_ids is None
        and (item.subject_id is None or item.subject_id == recheck.subject_id)
        for item in pending
    ):
        return pending
    if recheck.subject_id is None:
        return [recheck]
    combined = [*pending, recheck]
    if len(combined) > MAX_PENDING_RECHECKS:
        return [PendingRecheck(reason=recheck.reason, requested_by=recheck.requested_by)]
    return combined


class PersonLimits(MosaicModel):
    """The limits each person, application or agent granted a resource under the cost center gets.

    They apply to a grant that sets none of its own, on that grant's own counter. A security
    group's grant applies them to each member separately, as it does its own limits.
    """

    tokens_per_minute: int | None = Field(default=None, ge=1)
    token_quota: int | None = Field(default=None, ge=1)
    token_quota_period: QuotaPeriod | None = None
    calls_per_minute: int | None = Field(default=None, ge=1)
    call_quota: int | None = Field(default=None, ge=1)
    call_quota_period: QuotaPeriod | None = None

    @model_validator(mode="after")
    def validate_limits(self) -> Self:
        if all(
            value is None
            for value in (
                self.tokens_per_minute,
                self.token_quota,
                self.calls_per_minute,
                self.call_quota,
            )
        ):
            raise ValueError("Per-person limits need at least one rate or quota")
        if (self.token_quota is None) != (self.token_quota_period is None):
            raise ValueError("A token quota needs both a quota and a period")
        if (self.call_quota is None) != (self.call_quota_period is None):
            raise ValueError("A call quota needs both a quota and a period")
        return self

    @property
    def has_tokens(self) -> bool:
        return self.tokens_per_minute is not None or self.token_quota is not None

    def enforcement(self) -> EntitlementEnforcement:
        """These limits as a grant's own limits, counted on the grant's counter."""

        tokens = (
            TokenEnforcement(
                counter_key_expression=SUBSCRIPTION_COUNTER,
                tokens_per_minute=self.tokens_per_minute,
                token_quota=self.token_quota,
                token_quota_period=self.token_quota_period,
            )
            if self.has_tokens
            else None
        )
        requests = (
            RequestEnforcement(
                counter_key_expression=SUBSCRIPTION_COUNTER,
                calls=self.calls_per_minute,
                renewal_period_seconds=60 if self.calls_per_minute is not None else None,
                call_quota=self.call_quota,
                call_quota_period=self.call_quota_period,
            )
            if self.calls_per_minute is not None or self.call_quota is not None
            else None
        )
        return EntitlementEnforcement(tokens=tokens, requests=requests)


class PooledQuota(MosaicModel):
    """A monthly allowance every grant under the cost center on one resource shares.

    API Management counts it per gateway, and a model API or MCP server is on one gateway, so a
    pool is per model per gateway. Tokens need a gateway that can meter the model's tokens; MCP
    servers, and models it can't meter, are pooled by calls.
    """

    monthly_tokens: int | None = Field(default=None, ge=1)
    monthly_calls: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_present(self) -> Self:
        if self.monthly_tokens is None and self.monthly_calls is None:
            raise ValueError("A pooled quota needs monthly tokens or monthly calls")
        return self


class CostCenterLimit(MosaicModel):
    """A cost center's limits on one model API, MCP server, or model pool model."""

    resource: EntitlementResource
    person: PersonLimits | None = None
    pool: PooledQuota | None = None

    @model_validator(mode="after")
    def validate_resource(self) -> Self:
        if self.resource.kind not in {
            EntitlementResourceKind.MODEL_API,
            EntitlementResourceKind.MCP_SERVER,
            EntitlementResourceKind.POOL_MODEL,
        }:
            raise ValueError(
                "Cost center limits apply to model APIs, MCP servers, and model pool models"
            )
        if self.person is None and self.pool is None:
            raise ValueError("Set per-person limits, a pooled quota, or both")
        if self.resource.kind == EntitlementResourceKind.MCP_SERVER and (
            (self.person is not None and self.person.has_tokens)
            or (self.pool is not None and self.pool.monthly_tokens is not None)
        ):
            raise ValueError("MCP servers carry no tokens, so they're limited by calls only")
        return self


def exact_resource_key(resource: EntitlementResource) -> tuple[str, str | None, str]:
    """A pool-model offering is scoped to its pool even when another pool reuses its model ID."""

    return str(resource.kind), resource.scope_id, resource.id


class CostCenterResourceAccessMode(StrEnum):
    AUTOMATIC = "automatic"
    REQUEST_ONLY = "requestOnly"
    NOT_AVAILABLE = "notAvailable"


class CostCenterResourceAccess(MosaicModel):
    resource: EntitlementResource
    mode: CostCenterResourceAccessMode

    @model_validator(mode="after")
    def validate_resource(self) -> Self:
        if self.resource.kind not in {
            EntitlementResourceKind.MODEL_API,
            EntitlementResourceKind.MCP_SERVER,
            EntitlementResourceKind.POOL_MODEL,
        }:
            raise ValueError(
                "Cost center access names an exact model API, MCP server or pool model"
            )
        if not self.resource.id.strip():
            raise ValueError("Cost center access needs a resource ID")
        if self.resource.kind != EntitlementResourceKind.POOL_MODEL and self.resource.scope_id:
            raise ValueError("Only a pool-model access definition uses scopeId")
        return self


def _unique_resource_access(
    values: list[CostCenterResourceAccess],
) -> list[CostCenterResourceAccess]:
    if len({exact_resource_key(item.resource) for item in values}) != len(values):
        raise ValueError("Each exact resource appears once in cost center access")
    return values


class CostCenter(Entity):
    """Who calls are charged to. Desired state in ``desired-state``, saved with an audit event."""

    entity_type: Literal["costCenter"] = "costCenter"
    name: str
    code: str
    description: str | None = None
    owners: list[str] = Field(default_factory=list)
    entra_binding: EntraGroupBinding | None = None
    setup_state: CostCenterSetupState = CostCenterSetupState.SETUP_REQUIRED
    resource_access: list[CostCenterResourceAccess] = Field(
        default_factory=list, max_length=MAX_RESOURCE_ACCESS
    )
    # Legacy local roster/projection only. New Entra centers never embed directory members here.
    members: list[CostCenterMember] = Field(default_factory=list)
    keys_allowed: bool = True
    limits: list[CostCenterLimit] = Field(default_factory=list)
    # General, which every tenant has. It can be renamed and recoded, never deleted.
    built_in: bool = False
    group_id: str | None = None  # A MOSAIC local group ID, never an Entra object ID.
    membership_revision: int = Field(default=0, ge=0)
    # Grants to check again after a change that may have stopped their subjects charging it.
    pending_rechecks: list[PendingRecheck] = Field(default_factory=list)

    _validate_resource_access = field_validator("resource_access")(_unique_resource_access)

    @model_validator(mode="after")
    def validate_setup(self) -> Self:
        if self.is_general:
            if self.entra_binding is not None:
                raise ValueError("General is exempt from Entra group setup")
            self.setup_state = CostCenterSetupState.EXEMPT
        elif self.entra_binding is not None:
            if self.group_id is not None or self.members:
                raise ValueError(
                    "An Entra-backed center cannot also use the legacy local group or roster; "
                    "explicitly reconfigure or recreate the center"
                )
            self.setup_state = (
                CostCenterSetupState.READY
                if self.entra_binding.is_verified
                else CostCenterSetupState.SETUP_REQUIRED
            )
        else:
            self.setup_state = CostCenterSetupState.SETUP_REQUIRED
        return self

    @property
    def is_general(self) -> bool:
        return self.id == general_cost_center_id(self.tenant_id)

    @property
    def verified_group_object_id(self) -> str | None:
        binding = self.entra_binding
        return binding.group_object_id if binding is not None and binding.is_verified else None

    def ref(self) -> CostCenterRef:
        return CostCenterRef(id=self.id, name=self.name, code=self.code)

    def recheck_pending(self, entitlement_id: str, subject_id: str) -> bool:
        """Whether a pending recheck covers the grant, which keeps it out of every apply."""

        return any(item.covers(entitlement_id, subject_id) for item in self.pending_rechecks)

    def member_ids(self) -> set[str]:
        return {member.principal_id for member in self.members}

    def limit_for(self, resource: EntitlementResource) -> CostCenterLimit | None:
        return next(
            (
                limit
                for limit in self.limits
                if exact_resource_key(limit.resource) == exact_resource_key(resource)
            ),
            None,
        )

    def access_mode_for(self, resource: EntitlementResource) -> CostCenterResourceAccessMode:
        """Missing custom-center definitions are unavailable, never implicit automatic grants."""

        if self.is_general:
            return CostCenterResourceAccessMode.REQUEST_ONLY
        return next(
            (
                item.mode
                for item in self.resource_access
                if exact_resource_key(item.resource) == exact_resource_key(resource)
            ),
            CostCenterResourceAccessMode.NOT_AVAILABLE,
        )


class CostCenterSettings(Entity):
    """Which cost center new principals are charged to, and everyone may charge."""

    entity_type: Literal["costCenterSettings"] = "costCenterSettings"
    default_cost_center_id: str


def cost_center_settings_id(tenant_id: str) -> str:
    return deterministic_id("costCenterSettings", tenant_id)


def general_cost_center(tenant_id: str) -> CostCenter:
    return CostCenter(
        id=general_cost_center_id(tenant_id),
        tenant_id=tenant_id,
        name=GENERAL_COST_CENTER_NAME,
        code=GENERAL_COST_CENTER_CODE,
        description="Everyone may charge the tenant's default cost center.",
        built_in=True,
    )


def default_settings(tenant_id: str) -> CostCenterSettings:
    return CostCenterSettings(
        id=cost_center_settings_id(tenant_id),
        tenant_id=tenant_id,
        default_cost_center_id=general_cost_center_id(tenant_id),
    )


class CostCenterCreate(MosaicModel):
    name: str = Field(min_length=1, max_length=120)
    code: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=1000)
    owners: list[str] = Field(default_factory=list)
    keys_allowed: bool = True
    group_setup: CostCenterGroupSetup | None = None
    resource_access: list[CostCenterResourceAccess] = Field(
        default_factory=list, max_length=MAX_RESOURCE_ACCESS
    )

    _validate_code = field_validator("code")(normalize_code)
    _validate_owners = field_validator("owners")(_owners)
    _validate_resource_access = field_validator("resource_access")(_unique_resource_access)


class CostCenterUpdate(MosaicModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    code: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=1000)
    owners: list[str] | None = None
    keys_allowed: bool | None = None
    group_setup: CostCenterGroupSetup | None = None
    resource_access: list[CostCenterResourceAccess] | None = Field(
        default=None, max_length=MAX_RESOURCE_ACCESS
    )

    @field_validator("resource_access")
    @classmethod
    def validate_resource_access(
        cls, value: list[CostCenterResourceAccess] | None
    ) -> list[CostCenterResourceAccess] | None:
        return None if value is None else _unique_resource_access(value)

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str | None) -> str | None:
        return None if value is None else normalize_code(value)

    @field_validator("owners")
    @classmethod
    def validate_owners(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _owners(value)


class CostCenterLimitsUpdate(MosaicModel):
    """Replaces every limit a cost center sets."""

    limits: list[CostCenterLimit] = Field(default_factory=list, max_length=MAX_LIMITS)

    @model_validator(mode="after")
    def validate_unique(self) -> Self:
        seen: set[tuple[str, str | None, str]] = set()
        for limit in self.limits:
            key = exact_resource_key(limit.resource)
            if key in seen:
                raise ValueError("Each model or MCP server appears once")
            seen.add(key)
        return self


class CostCenterSettingsUpdate(MosaicModel):
    default_cost_center_id: str = Field(min_length=1, max_length=128)


class CostCenterMemberView(MosaicModel):
    principal_id: str
    object_id: str | None = None
    label: str | None = None
    kind: PrincipalKind | None = None
    # ``member`` is listed; ``default`` charges here when naming no cost center, which lets them
    # charge it without being listed. A principal can be both.
    explicit: bool = True
    is_default: bool = False
    added_at: datetime | None = None


class CostCenterView(CostCenter):
    """A cost center as the console lists it."""

    is_tenant_default: bool = False
    member_details: list[CostCenterMemberView] = Field(default_factory=list)
    grant_count: int = 0
    enabled_grant_count: int = 0
    # Principals whose own default this is.
    default_for: int = 0


class PortalCostCenter(MosaicModel):
    """One cost center the caller may charge, as the request dropdown lists it."""

    id: str
    name: str
    code: str
    is_default: bool = False
    keys_allowed: bool = True


def tenant_default_id(settings: CostCenterSettings | None, tenant_id: str) -> str:
    return settings.default_cost_center_id if settings else general_cost_center_id(tenant_id)


def effective_default_id(
    principal: Principal | None, settings: CostCenterSettings | None, tenant_id: str
) -> str:
    """The cost center a principal's calls are charged to when they name none."""

    if principal is not None and principal.default_cost_center_id:
        return principal.default_cost_center_id
    return tenant_default_id(settings, tenant_id)


def may_charge(
    cost_center: CostCenter,
    *,
    principal: Principal | None,
    settings: CostCenterSettings | None,
    group_principal_ids: Iterable[str] = (),
    group_object_ids: Iterable[str] = (),
) -> bool:
    """Whether a principal may charge a cost center. ``principal`` None is someone not onboarded.

    Custom centers require the verified primary group's actual Entra object ID. Default settings
    only choose between eligible centers, and local rosters never establish Entra membership.
    General preserves its existing default/member rules.
    """

    if not cost_center.is_general:
        binding = cost_center.entra_binding
        group_id = cost_center.verified_group_object_id
        if (
            binding is None
            or group_id is None
            or binding.directory_tenant_id.casefold() != cost_center.tenant_id.casefold()
        ):
            return False
        if principal is not None and principal.kind == PrincipalKind.SECURITY_GROUP:
            return principal.object_id.casefold() == group_id.casefold()
        return group_id.casefold() in {item.casefold() for item in group_object_ids}
    if cost_center.id == tenant_default_id(settings, cost_center.tenant_id):
        return True
    if principal is None:
        members = cost_center.member_ids()
        return any(group in members for group in group_principal_ids)
    if principal.kind != PrincipalKind.SECURITY_GROUP and (
        principal.default_cost_center_id == cost_center.id
    ):
        return True
    members = cost_center.member_ids()
    if principal.id in members:
        return True
    if principal.kind == PrincipalKind.SECURITY_GROUP:
        return False
    return any(group in members for group in group_principal_ids)


class CostCenterBook:
    """A tenant's cost centers and settings, read once and shared by one operation's lookups."""

    def __init__(
        self,
        tenant_id: str,
        cost_centers: Iterable[CostCenter],
        settings: CostCenterSettings | None,
    ) -> None:
        self.tenant_id = tenant_id
        self.by_id: dict[str, CostCenter] = {item.id: item for item in cost_centers}
        general = general_cost_center_id(tenant_id)
        if general not in self.by_id:
            self.by_id[general] = general_cost_center(tenant_id)
        self.settings = settings

    @property
    def tenant_default_id(self) -> str:
        return tenant_default_id(self.settings, self.tenant_id)

    def get(self, cost_center_id: str | None) -> CostCenter | None:
        return self.by_id.get(cost_center_id or "")

    def ref(self, cost_center_id: str | None) -> CostCenterRef | None:
        found = self.get(cost_center_id)
        return found.ref() if found else None

    def default_for(self, principal: Principal | None) -> str:
        return effective_default_id(principal, self.settings, self.tenant_id)

    def chargeable(
        self,
        principal: Principal | None,
        group_principal_ids: Iterable[str] = (),
        *,
        group_object_ids: Iterable[str] = (),
    ) -> list[CostCenter]:
        groups = list(group_principal_ids)
        objects = list(group_object_ids)
        return sorted(
            (
                item
                for item in self.by_id.values()
                if may_charge(
                    item,
                    principal=principal,
                    settings=self.settings,
                    group_principal_ids=groups,
                    group_object_ids=objects,
                )
            ),
            key=lambda item: (item.name.casefold(), item.code.casefold()),
        )


def refs_by_id(book: CostCenterBook) -> Mapping[str, CostCenterRef]:
    return {key: value.ref() for key, value in book.by_id.items()}
