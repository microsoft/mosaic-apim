"""Model pools: one vendor's deployments, on many endpoints, behind one API (ADR 0024).

A pool is desired state, like a publication. Saving one writes only to Cosmos; a reviewed plan and
an explicit apply write it to API Management. Callers send a pool model's public name, such as
``claude-opus-4-5``, to the pool's one base URL, and never learn which endpoint, region, or
deployment served them.

The code says ``ModelPool`` and ``model_pool`` rather than a bare "pool", because ADR 0022 already
calls a cost center's shared quota a pooled quota, and the two have nothing to do with each other.
"""

import hashlib
import re
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from mosaic_api.deployment_capacity import CapacityType, ProcessingScope
from mosaic_api.domain import (
    MOSAIC_RESOURCE_PREFIX,
    ApiShape,
    Entity,
    EnvironmentVerdict,
    ModelAccessSettings,
    ModelProvider,
    MosaicModel,
    PolicyFacet,
    PublicationStatus,
    PublishedResource,
    PublishedResourceKind,
    QuotaPeriod,
    apim_slug,
    deterministic_id,
)

# The access snapshot classes live in ``domain`` so a publish plan and run can carry one. They're
# re-exported here, beside the rest of the pool model.
from mosaic_api.domain import PoolAccessGrant as PoolAccessGrant
from mosaic_api.domain import PoolAccessSnapshot as PoolAccessSnapshot
from mosaic_api.domain import PoolModelQuota as PoolModelQuota

MODEL_POOL_ENTITY = "modelPool"
# API Management's limit on the members of one backend pool.
MAX_BACKEND_POOL_MEMBERS = 30
# No request makes more attempts than this, so a linear pool model has at most this many members.
MAX_ATTEMPTS = 10
DEFAULT_POOL_RETRIES = 3
# Each model adds a branch to the pool's policy, which API Management caps in size.
MAX_POOL_MODELS = 40
# What a client may send as a model name. Nothing that needs escaping in a policy expression or a
# URL path segment: the Azure OpenAI shape carries it in the route.
PUBLIC_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
POOL_API_PREFIX = f"{MOSAIC_RESOURCE_PREFIX}pool-"
POOL_PATH_PREFIX = "mosaic/pool-"
_RESOURCE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")
_API_PATH = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-/]*")


class ModelPoolType(StrEnum):
    """How a pool model spreads requests over its members."""

    # One backend pool over every member at one priority, weighted. Every member has a circuit
    # breaker, so one that throttles is skipped until its Retry-After passes.
    BREAKER = "breaker"
    # One backend pool with provisioned members at priority 1 and pay-as-you-go members at
    # priority 2. Provisioned capacity takes every request while any of it is available.
    PREFERENTIAL = "preferential"
    # One target per member, in the administrator's order, and no breakers. Every request starts
    # at the top and moves down the list on each cascading failure.
    LINEAR = "linear"


class BreakerPreset(StrEnum):
    """Which failures cascade to another member, and trip a member's circuit breaker."""

    THROTTLING = "throttling"
    THROTTLING_AND_ERRORS = "throttlingAndErrors"


class ModelPoolVisibility(StrEnum):
    """Whether the portal's catalog offers the pool's models (from phase 2)."""

    LISTED = "listed"
    HIDDEN = "hidden"


_CASCADE_STATUSES: dict[BreakerPreset, tuple[int, ...]] = {
    # 503 is API Management's answer for an open breaker or an exhausted pool, and an overloaded
    # model service's too.
    BreakerPreset.THROTTLING: (429, 503),
    BreakerPreset.THROTTLING_AND_ERRORS: (429, 500, 502, 503, 504),
}


def cascade_statuses(preset: BreakerPreset | str) -> tuple[int, ...]:
    """The response statuses that send a request on to another attempt."""

    return _CASCADE_STATUSES[BreakerPreset(preset)]


def circuit_breaker(preset: BreakerPreset | str) -> dict[str, object]:
    """The ``circuitBreaker`` API Management applies to each member backend of a backend pool.

    Both presets honour the response's ``Retry-After``; the trip duration is what applies when a
    response sends none. ADR 0024 records these numbers as starting points for load testing.
    """

    if BreakerPreset(preset) == BreakerPreset.THROTTLING:
        rule: dict[str, object] = {
            "name": "mosaic-throttling",
            "failureCondition": {
                "count": 1,
                "interval": "PT1M",
                "statusCodeRanges": [{"min": 429, "max": 429}],
            },
            "tripDuration": "PT10S",
            "acceptRetryAfter": True,
        }
    else:
        rule = {
            "name": "mosaic-throttling-and-errors",
            "failureCondition": {
                "count": 3,
                "interval": "PT1M",
                "statusCodeRanges": [{"min": 429, "max": 429}, {"min": 500, "max": 599}],
            },
            "tripDuration": "PT30S",
            "acceptRetryAfter": True,
        }
    return {"rules": [rule]}


# What trips a member's breaker in a minute: how many failures, and whether server errors count
# as well as 429. The same numbers as :func:`circuit_breaker`, for reading the gateway's logs.
_TRIP_RULES: dict[BreakerPreset, tuple[int, bool]] = {
    BreakerPreset.THROTTLING: (1, False),
    BreakerPreset.THROTTLING_AND_ERRORS: (3, True),
}


def breaker_trip_rule(preset: BreakerPreset | str) -> tuple[int, bool]:
    """How many failures in a minute trip a member's breaker, and whether 5xx count with 429."""

    return _TRIP_RULES[BreakerPreset(preset)]


def uses_backend_pools(pool_type: ModelPoolType | str) -> bool:
    """Whether a pool model's members sit behind one balancing backend pool."""

    return ModelPoolType(pool_type) != ModelPoolType.LINEAR


def member_priority(pool_type: ModelPoolType | str, capacity_type: CapacityType | str) -> int:
    """The backend pool priority group a member joins. 1 is tried first.

    Only a preferential pool has two groups, and a member whose capacity MOSAIC can't read never
    joins the provisioned one.
    """

    if ModelPoolType(pool_type) != ModelPoolType.PREFERENTIAL:
        return 1
    return 1 if CapacityType(capacity_type) == CapacityType.PROVISIONED else 2


def _short_hash(*parts: str) -> str:
    value = "|".join(part.casefold() for part in parts)
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def _stem(api_name: str) -> str:
    return api_name[:40].rstrip("-") or "mosaic-pool"


def model_pool_id(tenant_id: str, gateway_id: str, api_name: str) -> str:
    """Deterministic on the gateway and API name, as ADR 0024 records."""

    return deterministic_id(MODEL_POOL_ENTITY, tenant_id, gateway_id, api_name)


def pool_model_id(pool_id: str, public_name: str) -> str:
    """Deterministic on the pool and the public name. A rename is a new pool model."""

    return deterministic_id("poolModel", pool_id, public_name)


def pool_key_name(tenant_id: str, pool_id: str, subject_id: str, cost_center_id: str) -> str:
    """The subscription that is one subject's key to a pool under one cost center.

    One key serves every model the subject holds directly in the pool under that cost center, so
    the name doesn't depend on any model or grant.
    """

    return deterministic_id(
        "mosaic-pool-key", tenant_id, pool_id, subject_id, cost_center_id
    ).replace("_", "-")


def default_pool_api_name(display_name: str) -> str:
    return f"{POOL_API_PREFIX}{apim_slug(display_name) or 'models'}"


def default_pool_api_path(display_name: str) -> str:
    return f"{POOL_PATH_PREFIX}{apim_slug(display_name) or 'models'}"


def member_backend_name(
    api_name: str, model_id: str, model_endpoint_id: str, deployment_name: str
) -> str:
    """One backend per member, owned by the pool. At most 74 characters; API Management takes 80."""

    slug = apim_slug(deployment_name, max_length=24) or "member"
    return f"{_stem(api_name)}-{slug}-{_short_hash(model_id, model_endpoint_id, deployment_name)}"


def backend_pool_name(api_name: str, model_id: str, public_name: str) -> str:
    """The backend pool that balances one pool model's members. At most 79 characters."""

    slug = apim_slug(public_name, max_length=24) or "model"
    return f"{_stem(api_name)}-{slug}-pool-{_short_hash(model_id)}"


def _validate_resource_name(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    if not _RESOURCE_NAME.fullmatch(candidate):
        raise ValueError(
            "API Management resource names must start with a letter or digit and contain only "
            "letters, digits, and hyphens"
        )
    return candidate


def _validate_api_path(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip().strip("/")
    if not candidate or not _API_PATH.fullmatch(candidate):
        raise ValueError(
            "An API path may contain only letters, digits, hyphens, and forward slashes"
        )
    return candidate


def _validate_public_name(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    if not PUBLIC_NAME_PATTERN.fullmatch(candidate):
        raise ValueError(
            "A model name starts with a letter or digit and contains only letters, digits, "
            "periods, underscores, and hyphens, up to 64 characters"
        )
    return candidate


class PoolSafeguard(MosaicModel):
    """A token limit every caller of one pool model shares, counted per pool model.

    It protects the members' capacity from the pool's own callers. Per-person limits come from
    grants in phase 2, and the safeguard still counts last.
    """

    tokens_per_minute: int | None = Field(default=None, ge=1)
    token_quota: int | None = Field(default=None, ge=1)
    token_quota_period: QuotaPeriod | None = None

    @model_validator(mode="after")
    def validate_limits(self) -> Self:
        if self.tokens_per_minute is None and self.token_quota is None:
            raise ValueError("A safeguard needs a token rate, a token quota, or both")
        if (self.token_quota is None) != (self.token_quota_period is None):
            raise ValueError("A token quota needs both a quota and a quota period")
        return self


class PoolMember(MosaicModel):
    """One deployment on one registered model endpoint that serves a pool model.

    Region, capacity type, and processing scope aren't intent, so they aren't stored here: each
    plan reads them from inventory. ``backend_name`` is deterministic, so a member keeps its
    backend, and its breaker, across edits.
    """

    model_endpoint_id: str
    deployment_name: str
    weight: int = Field(default=1, ge=1, le=100)
    drained: bool = False
    backend_name: str


class PoolModel(MosaicModel):
    """A model the pool offers, under the name clients send."""

    id: str
    public_name: str
    display_name: str
    # Every member serves the same model, by name and format. Filled in from inventory when the
    # model is saved, and checked again by every plan.
    model_name: str | None = None
    model_format: str | None = None
    expected_version: str | None = None
    # A version mismatch between members is refused unless the administrator accepted it.
    allow_mixed_versions: bool = False
    # Whether the portal's catalog shows this model (from phase 2).
    listed: bool = True
    backend_pool_name: str
    members: list[PoolMember] = Field(default_factory=list)

    def active_members(self) -> list[PoolMember]:
        return [member for member in self.members if not member.drained]


class ModelPool(Entity):
    """An administrator's intent to serve one vendor's models through one API on one gateway.

    It has no environment of its own: it takes its gateway's, and each plan judges every member's
    endpoint against it (ADR 0014).
    """

    entity_type: Literal["modelPool"] = "modelPool"
    gateway_id: str
    display_name: str
    description: str | None = None
    visibility: ModelPoolVisibility = ModelPoolVisibility.LISTED
    # Whether users see a capacity badge, such as "Provisioned", on the pool's models.
    show_capacity: bool = True
    pool_type: ModelPoolType = ModelPoolType.BREAKER
    breaker_preset: BreakerPreset = BreakerPreset.THROTTLING
    # Breaker and preferential pools only: how many times a request is sent again after a
    # cascading failure. A linear pool tries each member once.
    max_retries: int = Field(default=DEFAULT_POOL_RETRIES, ge=0, le=MAX_ATTEMPTS - 1)
    # Unset until the pool's first model is added. Every member must be assessed to it.
    api_shape: ApiShape | None = None
    # The vendor: the members' model format, such as "OpenAI" or "Anthropic".
    vendor: str | None = None
    api_name: str
    api_path: str
    fragment_name: str
    product_name: str
    subscription_name: str
    safeguard: PoolSafeguard | None = None
    models: list[PoolModel] = Field(default_factory=list)
    status: PublicationStatus = PublicationStatus.DRAFT
    resources: list[PublishedResource] = Field(default_factory=list)
    last_plan_id: str | None = None
    last_plan_digest: str | None = None
    last_run_id: str | None = None
    last_applied_at: datetime | None = None
    # What the gateway was asked to run when an apply last succeeded. The console compares it with
    # the saved intent to tell an administrator their changes aren't on the gateway yet.
    applied_intent_digest: str | None = None
    # Digests of the API's policy and the pool's fragment as API Management returned them right
    # after the last apply succeeded. A plan compares them with what it reads, to warn that someone
    # changed them outside MOSAIC. Every run clears them until it succeeds.
    applied_policy_sha256: str | None = None
    applied_fragment_sha256: str | None = None
    unpublished_at: datetime | None = None
    last_error: str | None = None
    # Governed access (phase 2). None until an administrator opts the pool in. Then its policy
    # authorizes every call against grants on its models, and its bootstrap subscription is
    # suspended. A pool can't go back.
    governed_access: ModelAccessSettings | None = None
    applied_access: PoolAccessSnapshot | None = None
    access_state: Literal["pending", "applying", "applied", "failed", "unknown"] = "pending"
    # The models the gateway serves, as of the last successful apply. An unpublish clears them.
    applied_model_ids: list[str] = Field(default_factory=list)

    def created_resources(self) -> list[PublishedResource]:
        """The subset rollback and unpublish are allowed to delete."""

        return [resource for resource in self.resources if resource.created_by_mosaic]

    def has_applied_api(self) -> bool:
        """Whether MOSAIC's record says the pool's API is in API Management."""

        return any(
            item.kind == PublishedResourceKind.API
            and item.name == self.api_name
            and item.created_by_mosaic
            for item in self.resources
        )

    def pool_model(self, pool_model_id: str) -> PoolModel | None:
        return next((model for model in self.models if model.id == pool_model_id), None)

    def serves_model(self, pool_model_id: str) -> bool:
        """Whether the gateway serves the pool model.

        It does when the pool's last apply wrote the model, the API is still there, and the model
        is still part of the pool.
        """

        return (
            self.has_applied_api()
            and pool_model_id in self.applied_model_ids
            and self.pool_model(pool_model_id) is not None
        )

    def owns_key(self, key_name: str) -> bool:
        """Whether the pool's record says it created this key."""

        return any(
            item.kind == PublishedResourceKind.SUBSCRIPTION
            and item.name == key_name
            and item.created_by_mosaic
            for item in self.resources
        )

    def may_own_gateway_state(self) -> bool:
        """Whether API Management may hold something this pool is responsible for.

        True while MOSAIC-created resources are recorded, while a run is or may be in flight, while
        an interrupted apply left the runtime state unknown, and while any applied grant is still
        enabled. Only a pool for which this is False may be forgotten.
        """

        return bool(
            self.created_resources()
            or self.status == PublicationStatus.APPLYING
            or self.access_state in {"applying", "unknown"}
            or (
                self.applied_access
                and any(grant.enabled for grant in self.applied_access.grants)
            )
        )

    def owned_backends(self) -> set[str]:
        """The member backends and backend pools the pool's record says it created."""

        return {
            item.name
            for item in self.created_resources()
            if item.kind in {PublishedResourceKind.BACKEND, PublishedResourceKind.BACKEND_POOL}
        }

    def member_endpoint_ids(self, *, applied_only: bool = False) -> set[str]:
        """The endpoints the pool's members use. ``applied_only`` keeps those it may have written.

        A member may have been written when its backend is among the pool's created resources.
        """

        owned = self.owned_backends()
        return {
            member.model_endpoint_id
            for model in self.models
            for member in model.members
            if not applied_only or member.backend_name in owned
        }

    def live_endpoint_ids(self) -> set[str]:
        """The endpoints API Management may route this pool to.

        That is every member's endpoint while a run is applying, and otherwise the endpoints whose
        backends the pool created.
        """

        return self.member_endpoint_ids(applied_only=self.status != PublicationStatus.APPLYING)

    def deployments_on(self, endpoint_id: str) -> list[str]:
        """The deployment names the pool's members use on one endpoint, in model order."""

        names: list[str] = []
        for model in self.models:
            for member in model.members:
                if member.model_endpoint_id == endpoint_id and member.deployment_name not in names:
                    names.append(member.deployment_name)
        return names

    def _uses(self, member: PoolMember, endpoint_id: str, deployment_name: str | None) -> bool:
        return member.model_endpoint_id == endpoint_id and (
            deployment_name is None or member.deployment_name == deployment_name
        )

    def uses(self, endpoint_id: str, deployment_name: str | None = None) -> bool:
        """Whether a member uses the endpoint, or the endpoint's deployment when one is named."""

        return any(
            self._uses(member, endpoint_id, deployment_name)
            for model in self.models
            for member in model.members
        )

    def blocks_endpoint_removal(self, endpoint_id: str, deployment_name: str | None = None) -> bool:
        """Whether the gateway may hold a backend this pool wrote for one of the endpoint's members.

        While a run is applying, any member may be being written.
        """

        owned = self.owned_backends()
        return any(
            self._uses(member, endpoint_id, deployment_name)
            and (self.status == PublicationStatus.APPLYING or member.backend_name in owned)
            for model in self.models
            for member in model.members
        )

    def without_endpoint(self, endpoint_id: str, deployment_name: str | None = None) -> Self:
        """The pool without the endpoint's members (or one deployment's), and any model left empty.

        The pool's last plan no longer describes it, so it must be planned again.
        """

        models = [
            model.model_copy(
                update={
                    "members": [
                        member
                        for member in model.members
                        if not self._uses(member, endpoint_id, deployment_name)
                    ]
                }
            )
            for model in self.models
        ]
        return self.model_copy(
            update={
                "models": [model for model in models if model.members],
                "status": (
                    PublicationStatus.DRAFT
                    if self.status == PublicationStatus.PLANNED
                    else self.status
                ),
                "last_plan_id": None,
                "last_plan_digest": None,
            }
        )


class PoolMemberSpec(MosaicModel):
    model_endpoint_id: str = Field(min_length=1, max_length=128)
    deployment_name: str = Field(min_length=1, max_length=64)
    weight: int = Field(default=1, ge=1, le=100)
    drained: bool = False


class PoolModelSpec(MosaicModel):
    """A pool model as an administrator authors it. MOSAIC derives the rest from inventory."""

    # Defaults to the members' shared deployment name. Required when they don't share one.
    public_name: str | None = Field(default=None, min_length=1, max_length=64)
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    listed: bool = True
    allow_mixed_versions: bool = False
    members: list[PoolMemberSpec] = Field(min_length=1, max_length=MAX_BACKEND_POOL_MEMBERS)

    _public_name = field_validator("public_name")(_validate_public_name)

    @model_validator(mode="after")
    def validate_members(self) -> Self:
        seen: set[tuple[str, str]] = set()
        for member in self.members:
            key = (member.model_endpoint_id, member.deployment_name.casefold())
            if key in seen:
                raise ValueError(
                    f"Deployment {member.deployment_name} on one endpoint is listed twice"
                )
            seen.add(key)
        return self

    def resolved_public_name(self) -> str | None:
        """The public name, or the members' shared deployment name when none was given."""

        if self.public_name is not None:
            return self.public_name
        names = {member.deployment_name for member in self.members}
        if len(names) != 1:
            return None
        return _validate_public_name_or_none(next(iter(names)))


def _validate_public_name_or_none(value: str) -> str | None:
    try:
        return _validate_public_name(value)
    except ValueError:
        return None


def _unique_public_names(models: list[PoolModelSpec] | None) -> None:
    if not models:
        return
    seen: set[str] = set()
    for model in models:
        name = model.resolved_public_name()
        if name is None:
            continue
        if name.casefold() in seen:
            raise ValueError(f"Two models in the pool are both named {name}")
        seen.add(name.casefold())


class ModelPoolCreate(MosaicModel):
    gateway_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    visibility: ModelPoolVisibility = ModelPoolVisibility.LISTED
    show_capacity: bool = True
    pool_type: ModelPoolType = ModelPoolType.BREAKER
    breaker_preset: BreakerPreset = BreakerPreset.THROTTLING
    max_retries: int = Field(default=DEFAULT_POOL_RETRIES, ge=0, le=MAX_ATTEMPTS - 1)
    api_name: str | None = Field(default=None, min_length=1, max_length=80)
    api_path: str | None = Field(default=None, min_length=1, max_length=200)
    product_name: str | None = Field(default=None, min_length=1, max_length=80)
    safeguard: PoolSafeguard | None = None
    models: list[PoolModelSpec] = Field(default_factory=list, max_length=MAX_POOL_MODELS)

    _names = field_validator("api_name", "product_name")(_validate_resource_name)
    _path = field_validator("api_path")(_validate_api_path)

    @model_validator(mode="after")
    def validate_models(self) -> Self:
        _unique_public_names(self.models)
        return self


class ModelPoolUpdate(MosaicModel):
    """A change to a pool's intent. ``models`` replaces the whole list when it's given."""

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    visibility: ModelPoolVisibility | None = None
    show_capacity: bool | None = None
    pool_type: ModelPoolType | None = None
    breaker_preset: BreakerPreset | None = None
    max_retries: int | None = Field(default=None, ge=0, le=MAX_ATTEMPTS - 1)
    safeguard: PoolSafeguard | None = None
    models: list[PoolModelSpec] | None = Field(default=None, max_length=MAX_POOL_MODELS)
    # Opting in is one-way: once set, it can change but not be cleared.
    governed_access: ModelAccessSettings | None = None

    @model_validator(mode="after")
    def validate_models(self) -> Self:
        _unique_public_names(self.models)
        return self


class PoolMemberView(MosaicModel):
    """One member as a plan or the console sees it, with what inventory says about it."""

    model_endpoint_id: str
    endpoint_name: str | None = None
    deployment_name: str
    backend_name: str
    weight: int = 1
    drained: bool = False
    # Linear pools: the member's position, from 1. Backend pools: its priority group, or for a
    # member reached with an API key, which no backend pool can hold, its position among those
    # tried after the backend pool.
    order: int | None = None
    priority: int | None = None
    region: str | None = None
    environment: str | None = None
    model_name: str | None = None
    model_version: str | None = None
    sku_name: str | None = None
    sku_capacity: int | None = None
    capacity_type: CapacityType = CapacityType.UNKNOWN
    processing_scope: ProcessingScope = ProcessingScope.UNKNOWN
    spillover_deployment_name: str | None = None
    provisioning_state: str | None = None
    # Whether MOSAIC saw the deployment in Azure, or an administrator declared it on an endpoint
    # an API key can't list.
    observed: bool = False
    declared: bool = False
    # Whether the gateway reaches the member with an API key rather than its managed identity.
    api_key: bool = False
    # Who serves the member, such as AWS Bedrock outside Azure. None once its endpoint is gone.
    provider: ModelProvider | None = None
    readiness: Literal["ready", "notConfirmed", "cannotInvoke"] = "notConfirmed"
    readiness_message: str | None = None
    environment_verdict: EnvironmentVerdict | None = None


class PoolCandidateDeployment(MosaicModel):
    """A deployment a pool could use on a gateway, grouped under the model it serves."""

    model_endpoint_id: str
    endpoint_name: str
    region: str | None = None
    environment: str | None = None
    deployment_name: str
    model_version: str | None = None
    sku_name: str | None = None
    sku_capacity: int | None = None
    capacity_type: CapacityType = CapacityType.UNKNOWN
    processing_scope: ProcessingScope = ProcessingScope.UNKNOWN
    spillover_deployment_name: str | None = None
    readiness: Literal["ready", "notConfirmed", "cannotInvoke"] = "notConfirmed"
    environment_verdict: EnvironmentVerdict
    eligible: bool
    reason: str | None = None
    # The pools on this gateway that already use the deployment.
    pool_ids: list[str] = Field(default_factory=list)
    declared: bool = False
    api_key: bool = False
    provider: ModelProvider | None = None


class PoolCandidateModel(MosaicModel):
    """One model, by name and format, and every deployment of it a gateway could pool."""

    model_name: str
    model_format: str | None = None
    api_shape: ApiShape | None = None
    deployments: list[PoolCandidateDeployment] = Field(default_factory=list)


class PoolCandidates(MosaicModel):
    gateway_id: str
    gateway_environment: str | None = None
    pool_types: dict[str, str | None] = Field(
        default_factory=dict,
        description="Each pool type, and why the gateway can't run it, or null when it can.",
    )
    models: list[PoolCandidateModel] = Field(default_factory=list)


class PoolModelView(MosaicModel):
    id: str
    public_name: str
    display_name: str
    model_name: str | None = None
    model_format: str | None = None
    expected_version: str | None = None
    listed: bool = True
    backend_pool_name: str | None = None
    capacity: Literal["provisioned", "payAsYouGo", "provisionedWithOverflow", "unknown"] = "unknown"
    members: list[PoolMemberView] = Field(default_factory=list)


class ModelPoolDetail(MosaicModel):
    """A pool as the console shows it: its intent, and what inventory says about its members."""

    pool: ModelPool
    gateway_name: str | None = None
    gateway_environment: str | None = None
    base_url: str | None = None
    models: list[PoolModelView] = Field(default_factory=list)
    # What stops the pool being planned now. Each is a sentence an administrator can act on.
    problems: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    facets: list[PolicyFacet] = Field(default_factory=list)
    # Whether saved changes differ from what the pool's last successful apply wrote.
    unapplied_changes: bool = False


class ModelPoolSummary(MosaicModel):
    """A pool as the console's list shows it, with its active members counted."""

    pool: ModelPool
    gateway_name: str | None = None
    gateway_environment: str | None = None
    # Active members by capacity type, such as {"provisioned": 2, "payAsYouGo": 3}.
    capacity: dict[str, int] = Field(default_factory=dict)
    # Active members by readiness, such as {"ready": 4, "cannotInvoke": 1}.
    readiness: dict[str, int] = Field(default_factory=dict)
    problem_count: int = 0
    warning_count: int = 0
    unapplied_changes: bool = False


class EndpointPoolDeployment(MosaicModel):
    """A deployment on an endpoint that a pool sends one of its models' calls to."""

    deployment_name: str
    pool_model_id: str
    public_name: str
    model_display_name: str
    drained: bool = False
    # Why portal users would see the model twice: the deployment is also published on its own,
    # and the catalog lists both.
    warning: str | None = None


class EndpointPoolUse(MosaicModel):
    """A pool that uses deployments on one endpoint, as the endpoint's page lists it."""

    pool: ModelPool
    gateway_name: str | None = None
    deployments: list[EndpointPoolDeployment] = Field(default_factory=list)


class PoolSuggestionModel(MosaicModel):
    """A model a suggested pool would serve, and the deployments of it the pool could use."""

    model_name: str
    model_format: str | None = None
    deployment_count: int
    endpoint_count: int
    regions: list[str] = Field(default_factory=list)


class PoolReference(MosaicModel):
    id: str
    display_name: str


class PoolSuggestion(MosaicModel):
    """A pool an administrator could create: one vendor's models, each deployed on two or more
    endpoints a gateway can front, that no pool on the gateway serves yet."""

    gateway_id: str
    gateway_name: str
    gateway_environment: str | None = None
    vendor: str | None = None
    api_shape: ApiShape
    models: list[PoolSuggestionModel] = Field(default_factory=list)
    # Across every model: the endpoints the deployments are on, and their regions.
    endpoint_count: int
    regions: list[str] = Field(default_factory=list)
    # Pools on the gateway that already serve the vendor through the same API, which could take
    # these models instead of a new pool.
    family_pools: list[PoolReference] = Field(default_factory=list)


class PoolMemberHealth(MosaicModel):
    """How one member answered the attempts the gateway sent it over a window."""

    model_endpoint_id: str
    endpoint_name: str | None = None
    deployment_name: str
    backend_name: str
    region: str | None = None
    drained: bool = False
    api_key: bool = False
    # Whether the member takes requests only once others can't: a later member of a linear pool,
    # a pay-as-you-go member of a preferential pool, or one reached with an API key.
    overflow: bool = False
    attempts: int = 0
    succeeded: int = 0
    throttled: int = 0
    # Server errors, and attempts that got no response.
    failed: int = 0
    client_errors: int = 0
    # Calls whose last attempt it answered, and how many of those it answered successfully.
    served: int = 0
    served_ok: int = 0
    # Minutes in which its breaker would have tripped: an estimate, because the breaker counts
    # over a rolling minute. None when the member has no breaker.
    tripped_minutes: int | None = None
    last_seen: datetime | None = None


class PoolModelHealth(MosaicModel):
    """How the calls to one pool model ended, and how its members answered them."""

    model_id: str
    public_name: str
    display_name: str
    requests: int = 0
    succeeded: int = 0
    # Calls that ended on a 429, a server error, or no response.
    unavailable: int = 0
    client_errors: int = 0
    # Calls that took more than one attempt.
    retried: int = 0
    # Successful calls that an overflow member answered.
    overflowed: int = 0
    # Attempts the backend pool answered itself, because none of its members was available.
    exhausted: int = 0
    # Attempts MOSAIC couldn't place on a member.
    unplaced: int = 0
    members: list[PoolMemberHealth] = Field(default_factory=list)


class PoolHealth(MosaicModel):
    """A pool's health over a window of whole hours, read from its attempt traces."""

    status: Literal["ok", "noData", "notPublished", "notConfigured", "accessDenied", "error"]
    message: str | None = None
    # A command that fixes what stops MOSAIC reading the logs, when there is one.
    command: str | None = None
    hours: int
    start: datetime | None = None
    end: datetime | None = None
    # Calls the gateway logged with no attempt trace, sent by a policy from before traces.
    untraced: int = 0
    models: list[PoolModelHealth] = Field(default_factory=list)


def capacity_label(
    kinds: Iterable[CapacityType],
) -> Literal["provisioned", "payAsYouGo", "provisionedWithOverflow", "unknown"]:
    """What users are told about a pool model's capacity, from its active members' types.

    Unknown when there's no active member, or when MOSAIC can't tell what one of them is.
    """

    found = set(kinds)
    if not found or CapacityType.UNKNOWN in found:
        return "unknown"
    if found == {CapacityType.PROVISIONED}:
        return "provisioned"
    if found == {CapacityType.PAY_AS_YOU_GO}:
        return "payAsYouGo"
    if found == {CapacityType.PROVISIONED, CapacityType.PAY_AS_YOU_GO}:
        return "provisionedWithOverflow"
    return "unknown"


def capacity_badge(
    members: list[PoolMemberView],
) -> Literal["provisioned", "payAsYouGo", "provisionedWithOverflow", "unknown"]:
    """What users are told about a pool model's capacity, when the pool shows it."""

    return capacity_label(member.capacity_type for member in members if not member.drained)
