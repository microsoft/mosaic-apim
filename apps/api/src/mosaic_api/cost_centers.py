"""Cost centers: who every grant, and so every call, is charged to. See ADR 0021.

A cost center spans gateways. Administrators author it as desired state: its name and unique code,
its owners, its members, whether its grants may use keys, per-person default limits for each model
or MCP server, and optional pooled monthly quotas. Every grant names one, so a call's cost center
is decided by the grant the gateway matched, and a caller who holds grants under several names one
with the ``x-mosaic-cost-center`` header.

Who may charge a cost center:

- everyone may charge the tenant's default cost center;
- a principal may charge its own default cost center;
- a member may charge the cost center that lists it, and so may every member of a security group
  it lists.
"""

import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

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
    utc_now,
)

# Grant limits name the standard subscription counter, which the governed policy replaces with the
# grant's own counter. A cost center's per-person defaults become grant limits the same way.
SUBSCRIPTION_COUNTER = "@(context.Subscription.Id)"
_EMAIL = r"[^@\s]+@[^@\s]+\.[^@\s]+"
MAX_OWNERS = 20
MAX_MEMBERS = 500
MAX_LIMITS = 200


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
    """A cost center's limits on one model API or MCP server."""

    resource: EntitlementResource
    person: PersonLimits | None = None
    pool: PooledQuota | None = None

    @model_validator(mode="after")
    def validate_resource(self) -> Self:
        if self.resource.kind not in {
            EntitlementResourceKind.MODEL_API,
            EntitlementResourceKind.MCP_SERVER,
        }:
            raise ValueError("Cost center limits apply to model APIs and MCP servers")
        if self.person is None and self.pool is None:
            raise ValueError("Set per-person limits, a pooled quota, or both")
        if self.resource.kind == EntitlementResourceKind.MCP_SERVER and (
            (self.person is not None and self.person.has_tokens)
            or (self.pool is not None and self.pool.monthly_tokens is not None)
        ):
            raise ValueError("MCP servers carry no tokens, so they're limited by calls only")
        return self


class CostCenter(Entity):
    """Who calls are charged to. Desired state in ``desired-state``, saved with an audit event."""

    entity_type: Literal["costCenter"] = "costCenter"
    name: str
    code: str
    description: str | None = None
    owners: list[str] = Field(default_factory=list)
    members: list[CostCenterMember] = Field(default_factory=list)
    keys_allowed: bool = True
    limits: list[CostCenterLimit] = Field(default_factory=list)
    # General, which every tenant has. It can be renamed and recoded, never deleted.
    built_in: bool = False

    def ref(self) -> CostCenterRef:
        return CostCenterRef(id=self.id, name=self.name, code=self.code)

    def member_ids(self) -> set[str]:
        return {member.principal_id for member in self.members}

    def limit_for(self, resource: EntitlementResource) -> CostCenterLimit | None:
        return next(
            (
                limit
                for limit in self.limits
                if limit.resource.kind == resource.kind and limit.resource.id == resource.id
            ),
            None,
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

    _validate_code = field_validator("code")(normalize_code)
    _validate_owners = field_validator("owners")(_owners)


class CostCenterUpdate(MosaicModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    code: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=1000)
    owners: list[str] | None = None
    keys_allowed: bool | None = None

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
        seen: set[tuple[str, str]] = set()
        for limit in self.limits:
            key = (str(limit.resource.kind), limit.resource.id)
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
) -> bool:
    """Whether a principal may charge a cost center. ``principal`` None is someone not onboarded.

    ``group_principal_ids`` are the principal IDs of the security groups the principal is in, as
    their token or Microsoft Graph says. A security group may charge only a cost center that lists
    it, or the tenant's default.
    """

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
        self, principal: Principal | None, group_principal_ids: Iterable[str] = ()
    ) -> list[CostCenter]:
        groups = list(group_principal_ids)
        return sorted(
            (
                item
                for item in self.by_id.values()
                if may_charge(
                    item,
                    principal=principal,
                    settings=self.settings,
                    group_principal_ids=groups,
                )
            ),
            key=lambda item: (item.name.casefold(), item.code.casefold()),
        )


def refs_by_id(book: CostCenterBook) -> Mapping[str, CostCenterRef]:
    return {key: value.ref() for key, value in book.by_id.items()}
