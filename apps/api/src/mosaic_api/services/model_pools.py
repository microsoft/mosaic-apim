"""Plan, apply, and unpublish model pools in API Management (ADR 0024).

A pool's apply writes, in order: one backend per active member, one backend pool per model in a
breaker or preferential pool, the policy fragment, the API and its operations, the API policy, a
product, the product's link to the API, and the pool's subscription. Everything it writes is
recorded as the pool's, so rollback and unpublish remove exactly that and nothing else, and a plan
refuses to take over anything MOSAIC didn't create.

A governed pool (phase 2) has no open subscription. Its plan compiles the grants on its models into
an access snapshot, and its apply runs in stages that keep the pool fail-closed: every call is
denied and every key suspended, the reviewed policy goes in, and only then are the keys of enabled
grants activated. A governed apply that fails isn't rolled back. The pool stays denied, and gets
back only the access both the last applied and the reviewed snapshot allow.
"""

import asyncio
import hashlib
import json
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import structlog

from mosaic_api.budgets import BLOCKED_COST_CENTERS_NAMED_VALUE
from mosaic_api.deployment_capacity import CapacityType, ProcessingScope
from mosaic_api.domain import (
    ApimResourceId,
    ApiShape,
    AuditEvent,
    BindingSource,
    CapabilitySupport,
    EntitlementBinding,
    EntitlementSubjectKind,
    EnvironmentVerdict,
    Gateway,
    GatewayTier,
    ManagementMode,
    McpPublication,
    ModelEndpoint,
    ModelProvider,
    Publication,
    PublicationStatus,
    PublishAction,
    PublishedResource,
    PublishedResourceKind,
    PublishPlan,
    PublishPlanStep,
    PublishRun,
    PublishRunStatus,
    PublishStepResult,
    PublishStepStatus,
    RuntimeAccessEvaluation,
    RuntimeAccessReason,
    VerdictLevel,
    gateway_tier,
    grant_precedence_key,
    new_id,
    subject_kind_for,
    utc_now,
)
from mosaic_api.environments import EnvironmentCatalog, compatibility_fingerprint, permits
from mosaic_api.errors import ConflictError, NotFoundError, ValidationError
from mosaic_api.integrations.apim import ApimClient
from mosaic_api.integrations.apim.model_apis import (
    DeploymentFit,
    OperationSpec,
    assess_deployment,
    backend_origin,
    token_limits_note,
)
from mosaic_api.integrations.apim.writer import DEFAULT_SUBSCRIPTION_KEY_NAMES, ApimWriter
from mosaic_api.integrations.policy import PublicationPolicy
from mosaic_api.integrations.pool_policy import (
    PoolRoute,
    PoolTarget,
    body_routed,
    governed_pool_operations,
    member_backend_url,
    pool_grant_counter_identity,
    pool_grant_counter_key_expression,
    pool_operations,
    render_governed_pool_policy,
    render_pool_policy,
    template_parameters,
)
from mosaic_api.model_pools import (
    MAX_ATTEMPTS,
    MAX_BACKEND_POOL_MEMBERS,
    ModelPool,
    ModelPoolCreate,
    ModelPoolDetail,
    ModelPoolSummary,
    ModelPoolType,
    ModelPoolUpdate,
    PoolAccessGrant,
    PoolAccessSnapshot,
    PoolCandidateDeployment,
    PoolCandidateModel,
    PoolCandidates,
    PoolMember,
    PoolMemberView,
    PoolModel,
    PoolModelQuota,
    PoolModelSpec,
    PoolModelView,
    backend_pool_name,
    capacity_badge,
    circuit_breaker,
    default_pool_api_name,
    default_pool_api_path,
    member_backend_name,
    member_priority,
    model_pool_id,
    pool_model_id,
    uses_backend_pools,
)
from mosaic_api.observed import ObservedApi, ObservedModelDeployment
from mosaic_api.repositories import (
    CostCenterRepository,
    DirectoryRepository,
    EntitlementRepository,
    EnvironmentRepository,
    GatewayRepository,
    ModelEndpointRepository,
)
from mosaic_api.services.budget_gate import BlockedListGate, ensure_blocked_list, is_blocked_list
from mosaic_api.services.cost_centers import load_book
from mosaic_api.services.directory import Actor
from mosaic_api.services.environments import load_environment_catalog
from mosaic_api.services.model_access import (
    cost_center_intent,
    effective_enforcement,
    entitlement_intent_digest,
    environment_guard,
    grant_key_display_name,
    local_mutation_active,
    publication_lock,
)
from mosaic_api.services.pool_access import (
    denied_pool_access_snapshot,
    entitlement_key_name,
    is_pool_model_entitlement,
    safe_pool_access_snapshot,
)
from mosaic_api.services.publishing import _KIND_NOUNS as _KIND_NOUNS
from mosaic_api.services.publishing import (
    DENY_ALL_FRAGMENT,
    DENY_ALL_POLICY,
    STALE_RUN_MESSAGE,
    RecoveryJournal,
)
from mosaic_api.services.publishing import _merge_resources as _merge_resources

logger = structlog.get_logger()

ClientFactory = Callable[[ApimResourceId], ApimClient]
WriterFactory = Callable[[ApimResourceId], ApimWriter]
Readiness = Literal["ready", "notConfirmed", "cannotInvoke"]

POOL_CREATE_ORDER: tuple[PublishedResourceKind, ...] = (
    PublishedResourceKind.NAMED_VALUE,
    PublishedResourceKind.BACKEND,
    PublishedResourceKind.BACKEND_POOL,
    PublishedResourceKind.POLICY_FRAGMENT,
    PublishedResourceKind.API,
    PublishedResourceKind.API_OPERATION,
    PublishedResourceKind.API_POLICY,
    PublishedResourceKind.PRODUCT,
    PublishedResourceKind.PRODUCT_API,
    PublishedResourceKind.SUBSCRIPTION,
)
_ORDER: dict[PublishedResourceKind, int] = {
    kind: index for index, kind in enumerate(POOL_CREATE_ORDER)
}
# What a plan refuses to take over when it exists and the pool's record doesn't say MOSAIC made it.
# Operations, the API policy, and the product link live inside an API or product, so they're
# covered by their parent.
_CLAIMED_KINDS = frozenset(
    {
        PublishedResourceKind.BACKEND,
        PublishedResourceKind.BACKEND_POOL,
        PublishedResourceKind.POLICY_FRAGMENT,
        PublishedResourceKind.API,
        PublishedResourceKind.PRODUCT,
        PublishedResourceKind.SUBSCRIPTION,
    }
)
_MEMBER_KINDS = frozenset({PublishedResourceKind.BACKEND, PublishedResourceKind.BACKEND_POOL})
_DENIED_REASONS: dict[RuntimeAccessReason, str] = {
    RuntimeAccessReason.MISSING_ROLE: (
        "The gateway's managed identity has no role that lets it call this endpoint. Grant the "
        "role shown on the endpoint."
    ),
    RuntimeAccessReason.NARROWER_SCOPE: (
        "The gateway's managed identity holds a role on part of this endpoint, not the "
        "deployment's scope. Grant the role shown on the endpoint."
    ),
    RuntimeAccessReason.NO_GATEWAY_IDENTITY: (
        "The gateway has no managed identity to call this endpoint with."
    ),
    RuntimeAccessReason.NETWORK_UNREACHABLE: "The gateway has no network path to this endpoint.",
}
_NOT_CONFIRMED = (
    "MOSAIC couldn't confirm that the gateway's managed identity can call this endpoint. Check "
    "the endpoint's gateway access."
)
_DENY_ASSIGNMENT = "A deny assignment stops the gateway's managed identity calling this endpoint."
_STALE_PLAN = "This plan is out of date. Re-plan the pool and review the changes again."
_APPLYING = "This pool is being applied. Wait for the run to finish first."
_UNKNOWN_ACCESS = (
    "An interrupted apply left this pool's access unknown. Recover the run before changing it."
)


@dataclass(frozen=True)
class _Resource:
    """One API Management resource a pool wants, with what decides its content."""

    kind: PublishedResourceKind
    name: str
    resource_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    operation: OperationSpec | None = None


@dataclass(frozen=True)
class _Inventory:
    endpoint: ModelEndpoint
    # Keyed on the casefolded deployment name.
    deployments: dict[str, ObservedModelDeployment]


@dataclass
class _Member:
    """One member, with what inventory says about it and what stops or worries a plan."""

    model: PoolModel
    member: PoolMember
    endpoint: ModelEndpoint | None
    deployment: ObservedModelDeployment | None
    fit: DeploymentFit | None
    view: PoolMemberView
    fingerprint: str | None
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Assessment:
    """A pool judged against today's inventory, environments, and gateway."""

    gateway: Gateway
    members: list[_Member]
    shape: ApiShape | None
    vendor: str | None
    problems: list[str]
    warnings: list[str]

    def find(self, model_id: str, backend_name: str) -> _Member | None:
        return next(
            (
                item
                for item in self.members
                if item.model.id == model_id and item.member.backend_name == backend_name
            ),
            None,
        )

    def fingerprints(self) -> list[str]:
        """What a plan's digest covers about the active members' environments."""

        return sorted(
            item.fingerprint
            for item in self.members
            if item.fingerprint is not None and not item.member.drained
        )


def _readiness(endpoint: ModelEndpoint, gateway_id: str) -> tuple[Readiness, str | None]:
    """Whether the gateway's managed identity can call the endpoint, as far as MOSAIC knows.

    Something MOSAIC couldn't read is never presented as a denial.
    """

    entry = next((item for item in endpoint.runtime_access if item.gateway_id == gateway_id), None)
    if entry is None:
        return "notConfirmed", _NOT_CONFIRMED
    unevaluated = entry.evaluation == RuntimeAccessEvaluation.NOT_EVALUATED
    if entry.reason is None:
        if unevaluated:
            return "notConfirmed", _NOT_CONFIRMED
        if entry.can_invoke:
            return "ready", None
        return "cannotInvoke", _DENIED_REASONS[RuntimeAccessReason.MISSING_ROLE]
    if entry.reason == RuntimeAccessReason.GRANTED:
        return ("ready", None) if entry.can_invoke else ("notConfirmed", _NOT_CONFIRMED)
    if entry.reason in _DENIED_REASONS:
        return "cannotInvoke", _DENIED_REASONS[entry.reason]
    if entry.reason == RuntimeAccessReason.DENY_ASSIGNMENT:
        return (
            ("notConfirmed", _NOT_CONFIRMED)
            if unevaluated
            else (
                "cannotInvoke",
                _DENY_ASSIGNMENT,
            )
        )
    return "notConfirmed", _NOT_CONFIRMED


def _pool_type_problem(gateway: Gateway, pool_type: ModelPoolType | str) -> str | None:
    """Why the gateway can't run a pool type, or None when it can."""

    if (
        uses_backend_pools(pool_type)
        and gateway_tier(gateway.capabilities.sku_name) == GatewayTier.CONSUMPTION
    ):
        return (
            "The Consumption tier has no backend pools, so this gateway can run only linear pools."
        )
    return None


def _digest(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def _names(pool: ModelPool) -> list[str]:
    return [
        pool.api_name,
        pool.api_path,
        pool.fragment_name,
        pool.product_name,
        pool.subscription_name,
    ]


def _recorded(pool: ModelPool) -> list[list[str]]:
    return sorted(
        [str(item.kind), item.name, str(item.created_by_mosaic)] for item in pool.resources
    )


def pool_digest(
    pool: ModelPool,
    shape: str,
    policy: PublicationPolicy,
    desired: list[_Resource],
    fingerprints: list[str],
    snapshot: PoolAccessSnapshot | None = None,
) -> str:
    """What a publish plan reviewed. Apply refuses a plan whose digest no longer matches."""

    payload: dict[str, Any] = {
        "id": pool.id,
        "gatewayId": pool.gateway_id,
        "names": _names(pool),
        "displayName": pool.display_name,
        "description": pool.description,
        "shape": shape,
        "policy": policy.content_sha256,
        "desired": [[str(item.kind), item.name, item.payload] for item in desired],
        "resources": _recorded(pool),
        "fingerprints": fingerprints,
    }
    if snapshot is not None:
        # Only a governed pool's digest has these, so an ungoverned plan's digest is unchanged.
        payload["accessSnapshot"] = snapshot.model_dump(mode="json")
        payload["previousAccessVersion"] = (
            pool.applied_access.version if pool.applied_access else None
        )
    return _digest(payload)


def _is_governed(pool: ModelPool) -> bool:
    """Whether the pool's access is governed, or was by an apply that went through."""

    return pool.governed_access is not None or pool.applied_access is not None


def unpublish_digest(pool: ModelPool) -> str:
    payload: dict[str, Any] = {
        "id": pool.id,
        "gatewayId": pool.gateway_id,
        "names": _names(pool),
        "resources": _recorded(pool),
    }
    if _is_governed(pool):
        # Only a governed pool's digest has these, so an ungoverned plan's digest is unchanged.
        payload["appliedAccess"] = (
            pool.applied_access.model_dump(mode="json") if pool.applied_access else None
        )
        payload["accessState"] = pool.access_state
    return _digest(payload)


def intent_digest(pool: ModelPool) -> str:
    """What the administrator asked the gateway to run.

    Settings only the portal reads, such as visibility and model display names, are left out, so
    changing them isn't a change waiting to be applied.
    """

    payload: dict[str, Any] = {
        "gatewayId": pool.gateway_id,
        "names": _names(pool),
        "displayName": pool.display_name,
        "description": pool.description,
        "poolType": str(pool.pool_type),
        "breakerPreset": str(pool.breaker_preset),
        "maxRetries": pool.max_retries,
        "safeguard": pool.safeguard.model_dump(mode="json") if pool.safeguard else None,
        "models": [
            [
                model.public_name,
                model.backend_pool_name,
                [
                    [
                        member.model_endpoint_id,
                        member.deployment_name,
                        member.weight,
                        member.drained,
                        member.backend_name,
                    ]
                    for member in model.members
                ],
            ]
            for model in pool.models
        ],
    }
    if pool.governed_access is not None:
        # Only a governed pool's digest has it, so an ungoverned pool's digest is unchanged.
        payload["governedAccess"] = pool.governed_access.model_dump(mode="json")
    return _digest(payload)


def has_unapplied_changes(pool: ModelPool) -> bool:
    """Whether the pool's saved intent differs from what its last successful apply wrote."""

    return (
        pool.status != PublicationStatus.APPLYING
        and pool.has_applied_api()
        and pool.applied_intent_digest is not None
        and pool.applied_intent_digest != intent_digest(pool)
    )


def _routes(pool: ModelPool) -> list[PoolRoute]:
    """How the gateway serves each model with an active member."""

    routes: list[PoolRoute] = []
    for model in pool.models:
        active = model.active_members()
        if not active:
            continue
        if uses_backend_pools(pool.pool_type):
            targets: tuple[PoolTarget, ...] = (
                PoolTarget(model.backend_pool_name, active[0].deployment_name),
            )
            attempts = max(1, min(pool.max_retries + 1, len(active)))
        else:
            targets = tuple(
                PoolTarget(member.backend_name, member.deployment_name) for member in active
            )
            attempts = len(active)
        routes.append(
            PoolRoute(
                model_id=model.id,
                public_name=model.public_name,
                targets=targets,
                attempts=attempts,
            )
        )
    return routes


def _render(pool: ModelPool, shape: str) -> PublicationPolicy:
    return render_pool_policy(
        pool_id=pool.id,
        fragment_name=pool.fragment_name,
        shape=shape,
        routes=_routes(pool),
        preset=pool.breaker_preset,
        safeguard=pool.safeguard,
    )


def _policy(pool: ModelPool, shape: str, snapshot: PoolAccessSnapshot | None) -> PublicationPolicy:
    """The pool's policy: governed by a reviewed access snapshot, or open to its subscription."""

    if snapshot is None:
        return _render(pool, shape)
    return render_governed_pool_policy(
        pool=pool,
        snapshot=snapshot,
        shape=shape,
        routes=_routes(pool),
        preset=pool.breaker_preset,
        safeguard=pool.safeguard,
    )


def _base_url(gateway: Gateway, pool: ModelPool) -> str | None:
    url = gateway.capabilities.gateway_url
    if url is None:
        return None
    return f"{str(url).rstrip('/')}/{pool.api_path.strip('/')}"


def _noun(kind: PublishedResourceKind) -> str:
    return _KIND_NOUNS.get(kind, str(kind))


def _customized_key_names(live: dict[str, Any] | None) -> bool:
    """Whether a live API reads keys from other parameters than the ones governed access uses."""

    key_names = ((live or {}).get("properties") or {}).get("subscriptionKeyParameterNames")
    return bool(key_names) and (
        not isinstance(key_names, dict)
        or any(
            key_names.get(kind, default) != default
            for kind, default in DEFAULT_SUBSCRIPTION_KEY_NAMES.items()
        )
    )


def _segment(pool: ModelPool, kind: PublishedResourceKind, name: str) -> str:
    """Where a pool's resource lives, relative to its API Management service."""

    match kind:
        case PublishedResourceKind.NAMED_VALUE:
            return f"namedValues/{name}"
        case PublishedResourceKind.BACKEND | PublishedResourceKind.BACKEND_POOL:
            return f"backends/{name}"
        case PublishedResourceKind.POLICY_FRAGMENT:
            return f"policyFragments/{name}"
        case PublishedResourceKind.API:
            return f"apis/{name}"
        case PublishedResourceKind.API_OPERATION:
            return f"apis/{pool.api_name}/operations/{name}"
        case PublishedResourceKind.API_POLICY:
            return f"apis/{pool.api_name}/policies/policy"
        case PublishedResourceKind.PRODUCT:
            return f"products/{name}"
        case PublishedResourceKind.PRODUCT_API:
            return f"products/{pool.product_name}/apis/{pool.api_name}"
        case PublishedResourceKind.SUBSCRIPTION:
            return f"subscriptions/{name}"
    raise ValueError(f"A pool has no {kind} resources")


def _grant_identities(grant: PoolAccessGrant) -> set[str]:
    """What a pool's policy needs unique across its grants, besides their entitlements."""

    scope = f"{grant.cost_center_id}|{grant.pool_model_id}"
    return {f"o|{grant.object_id}|{scope}".casefold(), f"s|{grant.subject.id}|{scope}".casefold()}


def _grant_label(grants: list[PoolAccessGrant]) -> str:
    ids = sorted(grant.entitlement_id for grant in grants)
    return f"grant {ids[0]}" if len(ids) == 1 else f"grants {', '.join(ids)}"


def _key_display_name(pool: ModelPool, grants: list[PoolAccessGrant]) -> str:
    """What API Management calls a key: whose it is and the cost center it charges.

    Every grant sharing a key has the same subject and cost center, so any of them says it.
    """

    if not grants:
        return pool.display_name[:100]
    first = min(grants, key=lambda grant: grant.entitlement_id)
    return grant_key_display_name(first.display_name, first.cost_center_code)


def _live_display_name(live: dict[str, Any]) -> str | None:
    name = (live.get("properties") or {}).get("displayName")
    return name if isinstance(name, str) and name else None


def _check_linear(pool: ModelPool) -> None:
    if pool.pool_type != ModelPoolType.LINEAR:
        return
    for model in pool.models:
        active = len(model.active_members())
        if active > MAX_ATTEMPTS:
            raise ValidationError(
                f"A linear pool tries at most {MAX_ATTEMPTS} deployments of one model, and "
                f"{model.public_name} has {active} active. Drain some, or use a breaker pool.",
                details={"modelId": model.id, "active": active},
            )


def _check_granted_models(previous: ModelPool, pool: ModelPool) -> None:
    """Refuse to drop a model whose grants the gateway still enforces.

    ADR 0024 has its grants revoked, and the revocation applied, before the model can leave the
    pool, so a live grant never names a model the pool no longer has.
    """

    if previous.applied_access is None:
        return
    kept = {model.id for model in pool.models}
    held = sorted(
        {
            grant.pool_model_id
            for grant in previous.applied_access.grants
            if grant.enabled and grant.pool_model_id not in kept
        }
    )
    if held:
        names = [
            model.public_name if (model := previous.pool_model(model_id)) else model_id
            for model_id in held
        ]
        verb = "has" if len(names) == 1 else "have"
        raise ConflictError(
            f"{', '.join(names)} still {verb} applied grants. Revoke them and apply the pool "
            "before removing the model.",
            details={"poolModelIds": held},
        )


def _desired(
    pool: ModelPool, gateway: Gateway, assessment: _Assessment, shape: str
) -> list[_Resource]:
    """Everything the pool writes, in the order apply writes it."""

    base = ApimResourceId.parse(gateway.azure_resource_id).canonical
    breakers = uses_backend_pools(pool.pool_type)
    resources: list[_Resource] = []
    for model in pool.models:
        services: list[dict[str, Any]] = []
        for member in model.active_members():
            item = assessment.find(model.id, member.backend_name)
            if item is None or item.endpoint is None or item.deployment is None:
                continue
            origin = backend_origin(shape, str(item.endpoint.endpoint))
            resources.append(
                _Resource(
                    PublishedResourceKind.BACKEND,
                    member.backend_name,
                    f"{base}/backends/{member.backend_name}",
                    payload={
                        "url": member_backend_url(shape, origin, member.deployment_name),
                        "title": (
                            f"{pool.display_name}: {model.public_name} on {item.endpoint.name}"
                        )[:300],
                        "circuitBreaker": (
                            circuit_breaker(pool.breaker_preset) if breakers else None
                        ),
                    },
                )
            )
            services.append(
                {
                    "backend": member.backend_name,
                    "priority": member_priority(pool.pool_type, item.deployment.capacity_type),
                    "weight": member.weight,
                }
            )
        if breakers and services:
            resources.append(
                _Resource(
                    PublishedResourceKind.BACKEND_POOL,
                    model.backend_pool_name,
                    f"{base}/backends/{model.backend_pool_name}",
                    payload={
                        "title": f"{pool.display_name}: {model.public_name}"[:300],
                        "services": services,
                    },
                )
            )
    resources.append(
        _Resource(
            PublishedResourceKind.POLICY_FRAGMENT,
            pool.fragment_name,
            f"{base}/policyFragments/{pool.fragment_name}",
            payload={
                "description": (
                    f"MOSAIC model pool {pool.display_name}: finds the model a request names "
                    "and applies the pool's safeguard."
                )[:1000]
            },
        )
    )
    resources.append(
        _Resource(
            PublishedResourceKind.API,
            pool.api_name,
            f"{base}/apis/{pool.api_name}",
            payload={
                "displayName": pool.display_name,
                "path": pool.api_path,
                "description": pool.description
                or f"The models in the {pool.display_name} pool, served by MOSAIC.",
            },
        )
    )
    operations = (
        pool_operations(shape)
        if pool.governed_access is None
        # A governed pool serves only the operations its access policy meters; the others an
        # earlier, open apply wrote are deleted as stale.
        else governed_pool_operations(shape)
    )
    for operation in operations:
        resources.append(
            _Resource(
                PublishedResourceKind.API_OPERATION,
                operation.name,
                f"{base}/apis/{pool.api_name}/operations/{operation.name}",
                payload={
                    "displayName": operation.display_name,
                    "method": operation.method,
                    "urlTemplate": operation.url_template,
                    "description": operation.description,
                    "templateParameters": template_parameters(operation),
                },
                operation=operation,
            )
        )
    resources.extend(
        [
            _Resource(
                PublishedResourceKind.API_POLICY,
                "policy",
                f"{base}/apis/{pool.api_name}/policies/policy",
            ),
            _Resource(
                PublishedResourceKind.PRODUCT,
                pool.product_name,
                f"{base}/products/{pool.product_name}",
                payload={
                    "displayName": pool.display_name,
                    "description": f"Access to the {pool.display_name} model pool.",
                },
            ),
            _Resource(
                PublishedResourceKind.PRODUCT_API,
                pool.api_name,
                f"{base}/products/{pool.product_name}/apis/{pool.api_name}",
            ),
        ]
    )
    if pool.governed_access is None:
        # A governed pool's callers use their own keys. One left by an earlier, open apply stays
        # the pool's, suspended, until it's unpublished.
        resources.append(
            _Resource(
                PublishedResourceKind.SUBSCRIPTION,
                pool.subscription_name,
                f"{base}/subscriptions/{pool.subscription_name}",
                payload={
                    "displayName": f"{pool.display_name[:90]} (pool)",
                    "product": pool.product_name,
                },
            )
        )
    resources.sort(key=lambda item: _ORDER[item.kind])
    return resources


class ModelPoolService:
    def __init__(
        self,
        repository: GatewayRepository,
        *,
        endpoint_repository: ModelEndpointRepository,
        client_factory: ClientFactory,
        writer_factory: WriterFactory,
        environment_repository: EnvironmentRepository | None = None,
        directory_repository: DirectoryRepository | None = None,
        entitlement_repository: EntitlementRepository | None = None,
        cost_center_repository: CostCenterRepository | None = None,
        model_runtime_client_id: str | None = None,
        security_group_claims: bool = True,
        blocked_list: BlockedListGate | None = None,
    ) -> None:
        self._repository = repository
        self._endpoints = endpoint_repository
        self._client_factory = client_factory
        self._writer_factory = writer_factory
        # None only in tests that don't exercise environments; the built-in seeds apply then.
        self._environments = environment_repository
        # Governed access (phase 2) needs the directory and entitlements; without them a governed
        # pool can't be planned.
        self._directory = directory_repository
        self._entitlements = entitlement_repository
        self._cost_centers = cost_center_repository
        self._runtime_audience = model_runtime_client_id
        self._security_group_claims = security_group_claims
        # What a new blocked list holds. Without it, a list MOSAIC creates blocks nothing until the
        # budget check writes it.
        self._blocked_list = blocked_list
        self._active: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._task_failures: list[BaseException] = []

    async def aclose(self) -> None:
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._active.clear()

    @staticmethod
    def _audit(
        actor: Actor,
        action: str,
        resource_id: str,
        details: dict[str, Any] | None = None,
        *,
        resource_type: str = "modelPool",
    ) -> AuditEvent:
        return AuditEvent(
            id=new_id("audit"),
            tenant_id=actor.tenant_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            actor_object_id=actor.object_id,
            details=details or {},
        )

    async def list_pools(self, actor: Actor, gateway_id: str | None = None) -> list[ModelPool]:
        return await self._repository.list_model_pools(actor.tenant_id, gateway_id=gateway_id)

    async def summaries(
        self, actor: Actor, gateway_id: str | None = None
    ) -> list[ModelPoolSummary]:
        """Every pool, with its active members counted by capacity type and readiness."""

        gateways: dict[str, Gateway | None] = {}
        summaries: list[ModelPoolSummary] = []
        for pool in await self.list_pools(actor, gateway_id):
            if pool.gateway_id not in gateways:
                gateways[pool.gateway_id] = await self._repository.get_gateway(
                    actor.tenant_id, pool.gateway_id
                )
            gateway = gateways[pool.gateway_id]
            if gateway is None:
                summaries.append(
                    ModelPoolSummary(
                        pool=pool, problem_count=1, unapplied_changes=has_unapplied_changes(pool)
                    )
                )
                continue
            assessment = await self._assess(actor, pool, gateway)
            capacity: dict[str, int] = {}
            readiness: dict[str, int] = {}
            for item in assessment.members:
                if item.member.drained:
                    continue
                kind = str(item.view.capacity_type)
                capacity[kind] = capacity.get(kind, 0) + 1
                readiness[item.view.readiness] = readiness.get(item.view.readiness, 0) + 1
            summaries.append(
                ModelPoolSummary(
                    pool=pool,
                    gateway_name=gateway.name,
                    gateway_environment=gateway.environment,
                    capacity=capacity,
                    readiness=readiness,
                    problem_count=len(assessment.problems),
                    warning_count=len(assessment.warnings),
                    unapplied_changes=has_unapplied_changes(pool),
                )
            )
        return summaries

    async def get_pool(self, actor: Actor, pool_id: str) -> ModelPool:
        pool = await self._repository.get_model_pool(actor.tenant_id, pool_id)
        if pool is None:
            raise NotFoundError("Model pool was not found", details={"id": pool_id})
        return pool

    async def get_lock_owner(self, actor: Actor, pool_id: str) -> str | None:
        return await self._repository.get_publication_lock(actor.tenant_id, pool_id)

    async def get_plan(self, actor: Actor, plan_id: str) -> PublishPlan:
        plan = await self._repository.get_publish_plan(actor.tenant_id, plan_id)
        if plan is None or plan.target != "pool":
            raise NotFoundError("Pool plan was not found", details={"id": plan_id})
        return plan

    async def _load_gateway(self, actor: Actor, gateway_id: str) -> Gateway:
        gateway = await self._repository.get_gateway(actor.tenant_id, gateway_id)
        if gateway is None:
            raise NotFoundError("Gateway was not found", details={"id": gateway_id})
        return gateway

    @staticmethod
    def _require_writable(gateway: Gateway) -> None:
        if gateway.management_mode != ManagementMode.MANAGE:
            raise ConflictError(
                "This gateway is in observe mode. Switch it to managed first.",
                details={"gatewayId": gateway.id, "managementMode": str(gateway.management_mode)},
            )
        if not gateway.access.can_write:
            raise ConflictError(
                "MOSAIC cannot write to this gateway. Grant the role shown on the gateway and "
                "re-run the access check.",
                details={"gatewayId": gateway.id, "missingActions": gateway.access.missing_actions},
            )

    @staticmethod
    def _owns(pool: ModelPool, kind: PublishedResourceKind, name: str) -> bool:
        return any(
            item.kind == kind and item.name == name and item.created_by_mosaic
            for item in pool.resources
        )

    async def _access_snapshot(
        self, pool: ModelPool, gateway: Gateway, shape: str
    ) -> tuple[PoolAccessSnapshot | None, list[str]]:
        """Compile the grants on the pool's models into the snapshot its policy enforces.

        A grant the gateway can't enforce exactly as given stays out, with a warning that says
        why: leaving it out denies access, where bending it would widen access.
        """

        settings = pool.governed_access
        if settings is None:
            if pool.applied_access is not None:
                raise ValidationError("An applied governed pool can't return to open access.")
            return None, []
        if self._directory is None or self._entitlements is None:
            raise ValidationError("Governed access repositories are not configured")
        if gateway.capabilities.ai_gateway_policies == CapabilitySupport.UNAVAILABLE:
            raise ValidationError(
                "This gateway doesn't support the AI gateway policies governed access needs."
            )
        warnings = [
            "This is a pool-wide batch: every direct grant and authentication-method change "
            "listed in this snapshot will be applied together. MOSAIC-group grants remain "
            "desired-state only.",
            "Existing generic, product, all-API and all-access keys will not authorize this pool. "
            "Its bootstrap subscription is suspended when present.",
            "Governed key authentication uses Ocp-Apim-Subscription-Key (header) or "
            "subscription-key (query); each apply explicitly enforces these parameter names.",
        ]
        book = await load_book(self._cost_centers, pool.tenant_id)
        metered = token_limits_note(shape, gateway.capabilities.sku_name) is None
        routed = {route.model_id for route in _routes(pool)}
        grants: list[PoolAccessGrant] = []
        quotas: dict[tuple[str, str], PoolModelQuota] = {}
        saw_security_group = False
        for model in pool.models:
            entitlements = await self._entitlements.list_entitlements(
                pool.tenant_id, resource_id=model.id
            )
            for entitlement in entitlements:
                if (
                    not is_pool_model_entitlement(entitlement)
                    or entitlement.resource.scope_id != pool.id
                ):
                    continue
                if entitlement.subject.kind == EntitlementSubjectKind.GROUP:
                    warnings.append(
                        f"Grant {entitlement.id} is not a supported direct pool grant and will "
                        "not be enforced by this apply."
                    )
                    continue
                if model.id not in routed:
                    # The gateway can't serve it, so the grant would authorize nothing.
                    if entitlement.enabled:
                        warnings.append(
                            f"Grant {entitlement.id}'s model {model.public_name} has no active "
                            "deployment in this pool; it is excluded from runtime access."
                        )
                    continue
                is_security_group = (
                    entitlement.subject.kind == EntitlementSubjectKind.SECURITY_GROUP
                )
                saw_security_group = saw_security_group or is_security_group
                principal = await self._directory.get_principal(
                    pool.tenant_id, entitlement.subject.id
                )
                intent = cost_center_intent(entitlement, principal, book)
                if intent is None:
                    # Charged to a cost center MOSAIC no longer has, so it can't be charged at
                    # all: it stays out rather than charge nobody.
                    warnings.append(
                        f"Grant {entitlement.id}'s cost center no longer exists; it is excluded "
                        "from runtime access."
                    )
                    continue
                charged = book.get(entitlement.cost_center_id)
                if charged is not None and charged.recheck_pending(
                    entitlement.id, entitlement.subject.id
                ):
                    warnings.append(
                        f"Grant {entitlement.id} is waiting for MOSAIC to check that its subject "
                        f"may still charge {charged.name} ({charged.code}); it is excluded from "
                        "runtime access until then. Open the cost center and choose Check grants "
                        "again."
                    )
                    continue
                enforcement = effective_enforcement(entitlement, intent)
                if not metered and enforcement is not None and enforcement.tokens is not None:
                    # Granting access without the token limits an administrator set would widen
                    # it, so the grant stays out until its limits fit what the gateway can apply.
                    warnings.append(
                        f"Grant {entitlement.id} sets token limits, itself or through its cost "
                        "center's per-person limits, which this gateway can't apply to this "
                        "pool; it is excluded from runtime access. Use call limits instead."
                    )
                    continue
                if (
                    not metered
                    and intent.pool is not None
                    and intent.pool.monthly_tokens is not None
                ):
                    warnings.append(
                        f"Grant {entitlement.id}'s cost center pools tokens on this model, which "
                        "this gateway can't meter; it is excluded from runtime access until the "
                        "pool counts calls instead."
                    )
                    continue
                if principal is None or entitlement.subject.kind != subject_kind_for(
                    principal.kind
                ):
                    warnings.append(
                        f"Grant {entitlement.id} has a missing or mismatched principal; it is "
                        "excluded from runtime access."
                    )
                    continue
                if is_security_group and not settings.entra_enabled:
                    warning = (
                        "Grants to security groups need Entra access. Turn on Entra sign-in for "
                        "this pool to apply them."
                    )
                    if warning not in warnings:
                        warnings.append(warning)
                    continue
                grants.append(
                    PoolAccessGrant(
                        entitlement_id=entitlement.id,
                        pool_model_id=model.id,
                        subject=entitlement.subject,
                        object_id=(
                            principal.object_id.casefold()
                            if is_security_group
                            else principal.object_id
                        ),
                        display_name=principal.label or principal.object_id,
                        key_name=entitlement_key_name(pool, entitlement),
                        enabled=entitlement.enabled,
                        enforcement=enforcement,
                        intent_digest=entitlement_intent_digest(entitlement, principal, intent),
                        cost_center_id=intent.cost_center_id,
                        cost_center_code=intent.code,
                        default_cost_center=intent.default_for_subject,
                        granted_at=entitlement.created_at,
                        keys_allowed=intent.keys_allowed,
                        revoked=entitlement.revocation is not None,
                    )
                )
                if intent.pool is not None:
                    quotas[(model.id, intent.cost_center_id)] = PoolModelQuota(
                        cost_center_id=intent.cost_center_id,
                        cost_center_code=intent.code,
                        monthly_tokens=intent.pool.monthly_tokens,
                        monthly_calls=intent.pool.monthly_calls,
                        pool_model_id=model.id,
                    )
        if saw_security_group and not self._security_group_claims:
            warnings.append(
                "Group claims aren't configured for this deployment, so the gateway can't match "
                "grants to security groups."
            )
        for model in pool.models:
            group_grants = [
                grant
                for grant in grants
                if grant.enabled and grant.is_group_grant and grant.pool_model_id == model.id
            ]
            if len(group_grants) >= 2:
                ordered = sorted(
                    group_grants,
                    key=lambda grant: grant_precedence_key(grant.enforcement, grant.entitlement_id),
                )
                warnings.append(
                    "Members of more than one of these groups get only the most generous grant "
                    f"to {model.public_name}: "
                    f"{', '.join(grant.display_name for grant in ordered)}. "
                    f"{ordered[0].display_name} wins."
                )
        # An applied grant this snapshot no longer has stays in it, off, so the apply takes its
        # access away and the key it shared is suspended rather than forgotten.
        included = {grant.entitlement_id for grant in grants}
        identities = {identity for grant in grants for identity in _grant_identities(grant)}
        for grant in pool.applied_access.grants if pool.applied_access else []:
            if grant.entitlement_id in included or (
                grant.is_group_grant and not settings.entra_enabled
            ):
                continue
            if identities & _grant_identities(grant):
                # A newer grant to the same model under the same cost center replaces it.
                continue
            grants.append(grant.model_copy(update={"enabled": False}))
            identities |= _grant_identities(grant)
        grants.sort(key=lambda grant: grant.entitlement_id)
        return (
            PoolAccessSnapshot(
                version=pool.applied_access.version + 1 if pool.applied_access else 1,
                settings=settings,
                audience=self._runtime_audience,
                token_metering=metered,
                grants=grants,
                quotas=sorted(
                    quotas.values(), key=lambda quota: (quota.pool_model_id, quota.cost_center_id)
                ),
            ),
            warnings,
        )

    @staticmethod
    def _validate_subscription_scope(
        pool: ModelPool, resource: ApimResourceId, name: str, live: dict[str, Any]
    ) -> None:
        """Refuse a key whose scope moved: MOSAIC changes only the keys it scoped to the pool."""

        properties = live.get("properties") or {}
        relative = (
            f"/products/{pool.product_name}"
            if name == pool.subscription_name
            else f"/apis/{pool.api_name}"
        )
        expected = {relative.casefold(), f"{resource.canonical}{relative}".casefold()}
        if str(properties.get("scope", "")).rstrip("/").casefold() not in expected:
            raise ConflictError(
                "The pool's subscription's live scope changed. Resolve it before applying.",
                details={"subscriptionName": name},
            )

    async def create(self, actor: Actor, request: ModelPoolCreate) -> ModelPool:
        api_name = request.api_name or default_pool_api_name(request.display_name)
        pool_id = model_pool_id(actor.tenant_id, request.gateway_id, api_name)
        endpoint_ids = sorted(
            {member.model_endpoint_id for model in request.models for member in model.members}
        )
        async with environment_guard(
            self._repository,
            actor.tenant_id,
            gateway_ids=(request.gateway_id,),
            endpoint_ids=endpoint_ids,
            publication_ids=(pool_id,),
        ):
            gateway = await self._load_gateway(actor, request.gateway_id)
            if await self._repository.get_model_pool(actor.tenant_id, pool_id) is not None:
                raise ConflictError(
                    "This gateway already has a pool with that API name. Choose another name.",
                    details={"id": pool_id, "apiName": api_name},
                )
            if (problem := _pool_type_problem(gateway, request.pool_type)) is not None:
                raise ValidationError(problem, details={"poolType": str(request.pool_type)})
            pool = ModelPool(
                id=pool_id,
                tenant_id=actor.tenant_id,
                gateway_id=gateway.id,
                display_name=request.display_name,
                description=request.description,
                visibility=request.visibility,
                show_capacity=request.show_capacity,
                pool_type=request.pool_type,
                breaker_preset=request.breaker_preset,
                max_retries=request.max_retries,
                api_name=api_name,
                api_path=request.api_path or default_pool_api_path(request.display_name),
                fragment_name=api_name,
                product_name=request.product_name or api_name,
                subscription_name=api_name,
                safeguard=request.safeguard,
            )
            if request.models:
                pool = await self._with_models(actor, pool, gateway, request.models)
            _check_linear(pool)
            await self._reject_desired_collisions(actor, pool)
            await self._repository.save_model_pool(
                pool, self._audit(actor, "modelPool.created", pool.id)
            )
            return pool

    async def update(self, actor: Actor, pool_id: str, request: ModelPoolUpdate) -> ModelPool:
        current = await self.get_pool(actor, pool_id)
        endpoint_ids = current.member_endpoint_ids() | {
            member.model_endpoint_id for model in request.models or [] for member in model.members
        }
        async with environment_guard(
            self._repository,
            actor.tenant_id,
            gateway_ids=(current.gateway_id,),
            endpoint_ids=sorted(endpoint_ids),
            publication_ids=(pool_id,),
        ):
            pool = await self.get_pool(actor, pool_id)
            if pool.status == PublicationStatus.APPLYING:
                raise ConflictError(_APPLYING, details={"id": pool_id})
            if pool.access_state == "unknown":
                raise ConflictError(_UNKNOWN_ACCESS, details={"id": pool_id})
            if (
                "governed_access" in request.model_fields_set
                and request.governed_access is None
                and (pool.governed_access is not None or pool.applied_access is not None)
            ):
                raise ValidationError(
                    "Governed access cannot be cleared. Disable keys and Entra independently "
                    "instead."
                )
            changes: dict[str, Any] = {}
            for name in (
                "display_name",
                "visibility",
                "show_capacity",
                "pool_type",
                "breaker_preset",
                "max_retries",
                "governed_access",
            ):
                value = getattr(request, name)
                if value is not None:
                    changes[name] = value
            for name in ("description", "safeguard"):
                if name in request.model_fields_set:
                    changes[name] = getattr(request, name)
            pool = pool.model_copy(update=changes)
            gateway = await self._load_gateway(actor, pool.gateway_id)
            if (problem := _pool_type_problem(gateway, pool.pool_type)) is not None:
                raise ValidationError(problem, details={"poolType": str(pool.pool_type)})
            if request.models is not None:
                previous = pool
                pool = await self._with_models(actor, pool, gateway, request.models)
                _check_granted_models(previous, pool)
            _check_linear(pool)
            pool = pool.model_copy(
                update={
                    "status": (
                        PublicationStatus.DRAFT
                        if pool.status == PublicationStatus.PLANNED
                        else pool.status
                    ),
                    "last_plan_id": None,
                    "last_plan_digest": None,
                    "updated_at": utc_now(),
                }
            )
            await self._repository.save_model_pool(
                pool, self._audit(actor, "modelPool.updated", pool.id)
            )
            return pool

    async def delete(self, actor: Actor, pool_id: str) -> None:
        async with publication_lock(self._repository, actor.tenant_id, pool_id):
            pool = await self.get_pool(actor, pool_id)
            if pool.may_own_gateway_state():
                raise ConflictError(
                    "This pool still has resources in API Management. Unpublish it first.",
                    details={"id": pool_id},
                )
            await self._repository.delete_model_pool(
                pool, self._audit(actor, "modelPool.removed", pool.id)
            )

    async def _inventory(self, actor: Actor, endpoint_ids: Iterable[str]) -> dict[str, _Inventory]:
        inventory: dict[str, _Inventory] = {}
        for endpoint_id in sorted(set(endpoint_ids)):
            endpoint = await self._endpoints.get_endpoint(actor.tenant_id, endpoint_id)
            if endpoint is None:
                continue
            observed = await self._endpoints.list_observed_for_endpoint(
                ObservedModelDeployment, actor.tenant_id, endpoint_id, "observedModelDeployment"
            )
            inventory[endpoint_id] = _Inventory(
                endpoint, {item.deployment_name.casefold(): item for item in observed}
            )
        return inventory

    async def _with_models(
        self, actor: Actor, pool: ModelPool, gateway: Gateway, specs: list[PoolModelSpec]
    ) -> ModelPool:
        """The pool with ``specs`` as its models, resolved and checked against inventory."""

        inventory = await self._inventory(
            actor, (member.model_endpoint_id for spec in specs for member in spec.members)
        )
        previous = {model.id: model for model in pool.models}
        models: list[PoolModel] = []
        shapes: set[ApiShape] = set()
        vendors: dict[str, str] = {}
        for spec in specs:
            public_name = spec.resolved_public_name()
            if public_name is None:
                raise ValidationError(
                    "Give this model a public name: its deployments don't share one callers "
                    "could send.",
                    details={"deployments": sorted({m.deployment_name for m in spec.members})},
                )
            model_id = pool_model_id(pool.id, public_name)
            members: list[PoolMember] = []
            names: dict[str, str] = {}
            formats: dict[str, str] = {}
            versions: set[str] = set()
            for member_spec in spec.members:
                entry = inventory.get(member_spec.model_endpoint_id)
                if entry is None:
                    raise ValidationError(
                        "Model endpoint was not found",
                        details={"id": member_spec.model_endpoint_id},
                    )
                endpoint = entry.endpoint
                label = f"{member_spec.deployment_name} on {endpoint.name}"
                details = {"endpointId": endpoint.id, "deployment": member_spec.deployment_name}
                if (
                    endpoint.provider == ModelProvider.OPENAI_COMPATIBLE
                    or endpoint.uses_backend_key()
                ):
                    raise ValidationError(
                        f"{label}: pools can't use endpoints MOSAIC reaches with an API key yet.",
                        details=details,
                    )
                deployment = entry.deployments.get(member_spec.deployment_name.casefold())
                if deployment is None:
                    raise ValidationError(
                        f"MOSAIC hasn't observed {label}. Sync the endpoint if it was just "
                        "deployed.",
                        details=details,
                    )
                if not deployment.model_name:
                    raise ValidationError(
                        f"MOSAIC can't tell which model {label} serves.", details=details
                    )
                if deployment.capacity_type == CapacityType.BATCH:
                    raise ValidationError(
                        f"{label} is a batch deployment, which can't serve requests as they "
                        "arrive.",
                        details=details,
                    )
                fit = assess_deployment(
                    endpoint.provider,
                    model_name=deployment.model_name,
                    model_format=deployment.model_format,
                    capabilities=deployment.capabilities,
                    endpoint=str(endpoint.endpoint),
                    gateway_sku=gateway.capabilities.sku_name,
                )
                if fit.api_shape is None or not fit.publishable:
                    reason = fit.unpublishable_reason or "MOSAIC has no API it can serve it with."
                    raise ValidationError(f"{label} can't be pooled: {reason}", details=details)
                shapes.add(fit.api_shape)
                names.setdefault(deployment.model_name.casefold(), deployment.model_name)
                model_format = deployment.model_format or ""
                formats.setdefault(model_format.casefold(), model_format)
                if deployment.model_version:
                    versions.add(deployment.model_version)
                members.append(
                    PoolMember(
                        model_endpoint_id=endpoint.id,
                        deployment_name=deployment.deployment_name,
                        weight=member_spec.weight,
                        drained=member_spec.drained,
                        backend_name=member_backend_name(
                            pool.api_name, model_id, endpoint.id, deployment.deployment_name
                        ),
                    )
                )
            if len(names) > 1 or len(formats) > 1:
                raise ValidationError(
                    f"Every deployment of {public_name} must serve the same model from the same "
                    "vendor.",
                    details={
                        "models": sorted(names.values()),
                        "formats": sorted(formats.values()),
                    },
                )
            if len(versions) > 1 and not spec.allow_mixed_versions:
                raise ValidationError(
                    f"The deployments of {public_name} serve different versions "
                    f"({', '.join(sorted(versions))}). Choose deployments of one version, or "
                    "allow mixed versions for this model.",
                    details={"versions": sorted(versions)},
                )
            vendor = next(iter(formats.values()))
            if vendor:
                vendors.setdefault(vendor.casefold(), vendor)
            earlier = previous.get(model_id)
            models.append(
                PoolModel(
                    id=model_id,
                    public_name=public_name,
                    display_name=spec.display_name
                    or (earlier.display_name if earlier else public_name),
                    model_name=next(iter(names.values())),
                    model_format=vendor or None,
                    expected_version=next(iter(versions)) if len(versions) == 1 else None,
                    allow_mixed_versions=spec.allow_mixed_versions,
                    listed=spec.listed,
                    backend_pool_name=backend_pool_name(pool.api_name, model_id, public_name),
                    members=members,
                )
            )
        if len(shapes) > 1:
            raise ValidationError(
                "A pool serves its models through one API, and these models need different "
                "ones. Put them in separate pools.",
                details={"apiShapes": sorted(shapes)},
            )
        if len(vendors) > 1:
            raise ValidationError(
                "A pool serves one vendor's models. Put each vendor's models in its own pool.",
                details={"vendors": sorted(vendors.values())},
            )
        shape = next(iter(shapes), None)
        vendor_name = next(iter(vendors.values()), None)
        if pool.has_applied_api():
            if pool.api_shape is not None and shape is not None and shape != pool.api_shape:
                raise ValidationError(
                    f"This pool's API is published for {pool.api_shape} requests, and these "
                    f"models need {shape}. Unpublish the pool before changing what it serves.",
                    details={"apiShape": str(pool.api_shape), "requested": str(shape)},
                )
            if (
                pool.vendor is not None
                and vendor_name is not None
                and vendor_name.casefold() != pool.vendor.casefold()
            ):
                raise ValidationError(
                    f"This pool's API is published for {pool.vendor} models. Unpublish the pool "
                    "before changing its vendor.",
                    details={"vendor": pool.vendor, "requested": vendor_name},
                )
            shape = pool.api_shape or shape
            vendor_name = pool.vendor or vendor_name
        return pool.model_copy(update={"models": models, "api_shape": shape, "vendor": vendor_name})

    async def _catalog(self, actor: Actor) -> EnvironmentCatalog:
        return await load_environment_catalog(self._environments, actor.tenant_id)

    async def _assess(
        self, actor: Actor, pool: ModelPool, gateway: Gateway | None = None
    ) -> _Assessment:
        """Judge the pool against today's inventory. Problems stop a plan; warnings don't."""

        gateway = gateway or await self._load_gateway(actor, pool.gateway_id)
        catalog = await self._catalog(actor)
        inventory = await self._inventory(actor, pool.member_endpoint_ids())
        others = [
            item
            for item in await self._repository.list_model_pools(
                actor.tenant_id, gateway_id=pool.gateway_id
            )
            if item.id != pool.id
        ]
        problems: list[str] = []
        warnings: list[str] = []
        if (problem := _pool_type_problem(gateway, pool.pool_type)) is not None:
            problems.append(problem)
        if not pool.models:
            problems.append("Add at least one model to the pool.")
        members: list[_Member] = []
        shapes: set[ApiShape] = set()
        vendors: dict[str, str] = {}
        for model in pool.models:
            active = model.active_members()
            if not active:
                problems.append(
                    f"{model.public_name} has no active deployments. Undrain one, or remove the "
                    "model."
                )
            elif pool.pool_type == ModelPoolType.LINEAR and len(active) > MAX_ATTEMPTS:
                problems.append(
                    f"A linear pool tries at most {MAX_ATTEMPTS} deployments of one model, and "
                    f"{model.public_name} has {len(active)} active."
                )
            elif uses_backend_pools(pool.pool_type) and len(active) > MAX_BACKEND_POOL_MEMBERS:
                problems.append(
                    f"An API Management backend pool holds at most {MAX_BACKEND_POOL_MEMBERS} "
                    f"backends, and {model.public_name} has {len(active)} active deployments."
                )
            order = 0
            current: list[_Member] = []
            for member in model.members:
                if not member.drained:
                    order += 1
                item = self._member(
                    pool,
                    gateway,
                    catalog,
                    inventory,
                    others,
                    model,
                    member,
                    order=(
                        order
                        if pool.pool_type == ModelPoolType.LINEAR and not member.drained
                        else None
                    ),
                )
                current.append(item)
                if member.drained:
                    continue
                problems.extend(item.problems)
                warnings.extend(item.warnings)
                if item.fit is not None and item.fit.api_shape is not None:
                    shapes.add(item.fit.api_shape)
                if item.deployment is not None and item.deployment.model_format:
                    vendors.setdefault(
                        item.deployment.model_format.casefold(), item.deployment.model_format
                    )
            members.extend(current)
            self._check_model(pool, model, current, problems, warnings)
        if len(shapes) > 1:
            problems.append(
                "The pool's deployments need different APIs. A pool serves its models through "
                "one API, so put them in separate pools."
            )
        if len(vendors) > 1:
            problems.append(
                f"The pool's deployments serve models from {', '.join(sorted(vendors.values()))}. "
                "A pool serves one vendor's models."
            )
        shape = next(iter(shapes)) if len(shapes) == 1 else pool.api_shape
        if (
            pool.has_applied_api()
            and pool.api_shape is not None
            and shape is not None
            and shape != pool.api_shape
        ):
            problems.append(
                f"The pool's API is published for {pool.api_shape} requests, and its deployments "
                f"now need {shape}. Unpublish the pool before changing what it serves."
            )
        if pool.safeguard is not None and shape is not None:
            note = token_limits_note(shape, gateway.capabilities.sku_name)
            if note is not None:
                problems.append(
                    f"This gateway can't apply the pool's safeguard. {note} Remove the safeguard "
                    "to publish the pool here."
                )
        if gateway.capabilities.gateway_url is None:
            warnings.append(
                "MOSAIC hasn't read this gateway's URL, so it can't show callers where to "
                "connect. Re-run the gateway's access check."
            )
        vendor = next(iter(vendors.values())) if len(vendors) == 1 else pool.vendor
        return _Assessment(
            gateway=gateway,
            members=members,
            shape=shape,
            vendor=vendor,
            problems=list(dict.fromkeys(problems)),
            warnings=list(dict.fromkeys(warnings)),
        )

    @staticmethod
    def _check_model(
        pool: ModelPool,
        model: PoolModel,
        members: list[_Member],
        problems: list[str],
        warnings: list[str],
    ) -> None:
        deployments = [
            item.deployment
            for item in members
            if item.deployment is not None and not item.member.drained
        ]
        versions = {item.model_version for item in deployments if item.model_version}
        if len(versions) > 1 and not model.allow_mixed_versions:
            problems.append(
                f"The deployments of {model.public_name} now serve different versions "
                f"({', '.join(sorted(versions))}). Align them, or allow mixed versions for this "
                "model."
            )
        elif (
            model.expected_version is not None
            and versions
            and versions != {model.expected_version}
            and not model.allow_mixed_versions
        ):
            warnings.append(
                f"{model.public_name} was added at version {model.expected_version} and its "
                f"deployments now serve {', '.join(sorted(versions))}."
            )
        scopes = {
            item.processing_scope
            for item in deployments
            if item.processing_scope != ProcessingScope.UNKNOWN
        }
        if len(scopes) > 1:
            warnings.append(
                f"The deployments of {model.public_name} process data in different scopes "
                f"({', '.join(sorted(str(scope) for scope in scopes))}). A request may be served "
                "by any of them."
            )
        shapes = {
            item.fit.api_shape
            for item in members
            if item.fit is not None and item.fit.api_shape is not None and not item.member.drained
        }
        names = {
            item.member.deployment_name.casefold() for item in members if not item.member.drained
        }
        if (
            uses_backend_pools(pool.pool_type)
            and len(names) > 1
            and any(body_routed(shape) for shape in shapes)
        ):
            problems.append(
                f"The request body names the deployment, so every active deployment of "
                f"{model.public_name} in a breaker or preferential pool must have the same "
                "deployment name. Rename them, or use a linear pool."
            )

    def _member(
        self,
        pool: ModelPool,
        gateway: Gateway,
        catalog: EnvironmentCatalog,
        inventory: dict[str, _Inventory],
        others: list[ModelPool],
        model: PoolModel,
        member: PoolMember,
        *,
        order: int | None,
    ) -> _Member:
        entry = inventory.get(member.model_endpoint_id)
        endpoint = entry.endpoint if entry is not None else None
        deployment = (
            entry.deployments.get(member.deployment_name.casefold()) if entry is not None else None
        )
        label = (
            f"{member.deployment_name} on {endpoint.name}"
            if endpoint is not None
            else member.deployment_name
        )
        problems: list[str] = []
        warnings: list[str] = []
        fit: DeploymentFit | None = None
        readiness: Readiness = "notConfirmed"
        message: str | None = None
        verdict: EnvironmentVerdict | None = None
        fingerprint: str | None = None
        if endpoint is None:
            problems.append(
                f"{model.public_name}: the endpoint behind {label} is no longer registered. "
                "Remove the deployment from the pool."
            )
        else:
            verdict = permits(catalog, gateway.environment, endpoint.environment)
            fingerprint = (
                f"{member.backend_name}:"
                f"{compatibility_fingerprint(catalog, gateway.environment, endpoint.environment)}"
            )
            readiness, message = _readiness(endpoint, gateway.id)
            if endpoint.provider == ModelProvider.OPENAI_COMPATIBLE or endpoint.uses_backend_key():
                problems.append(
                    f"{label}: pools can't use endpoints MOSAIC reaches with an API key yet."
                )
            if deployment is None:
                problems.append(
                    f"MOSAIC no longer sees {label}. Sync the endpoint, or remove the deployment "
                    "from the pool."
                )
            else:
                state = deployment.provisioning_state
                if state and state.casefold() != "succeeded":
                    problems.append(f"{label} is {state}, not ready to serve requests.")
                if deployment.capacity_type == CapacityType.BATCH:
                    problems.append(
                        f"{label} is a batch deployment, which can't serve requests as they arrive."
                    )
                if (model.model_name or "").casefold() != (
                    deployment.model_name or ""
                ).casefold() or (model.model_format or "").casefold() != (
                    deployment.model_format or ""
                ).casefold():
                    problems.append(
                        f"{label} now serves {deployment.model_name or 'an unknown model'}, not "
                        f"{model.model_name or model.public_name}."
                    )
                if deployment.model_name:
                    fit = assess_deployment(
                        endpoint.provider,
                        model_name=deployment.model_name,
                        model_format=deployment.model_format,
                        capabilities=deployment.capabilities,
                        endpoint=str(endpoint.endpoint),
                        gateway_sku=gateway.capabilities.sku_name,
                    )
                    if fit.api_shape is None or not fit.publishable:
                        reason = fit.unpublishable_reason or "MOSAIC has no API to serve it with."
                        problems.append(f"{label} can't be pooled: {reason}")
                if deployment.spillover_deployment_name:
                    warnings.append(
                        f"{label} spills over to {deployment.spillover_deployment_name}, which "
                        "isn't in the pool, when it runs out of capacity."
                    )
            if readiness == "cannotInvoke":
                problems.append(f"{label}: {message}")
            elif readiness == "notConfirmed":
                warnings.append(f"{label}: {message}")
            if verdict.level == VerdictLevel.BLOCKED:
                problems.append(f"{label}: {verdict.reason}")
            elif verdict.level == VerdictLevel.WARNING and verdict.reason:
                warnings.append(f"{label}: {verdict.reason}")
        shared = sorted(
            other.display_name
            for other in others
            if other.uses(member.model_endpoint_id, member.deployment_name)
        )
        if shared:
            warnings.append(
                f"{label} also serves {', '.join(shared)}, so those pools share its capacity."
            )
        view = PoolMemberView(
            model_endpoint_id=member.model_endpoint_id,
            endpoint_name=endpoint.name if endpoint is not None else None,
            deployment_name=member.deployment_name,
            backend_name=member.backend_name,
            weight=member.weight,
            drained=member.drained,
            order=order,
            priority=(
                member_priority(pool.pool_type, deployment.capacity_type)
                if deployment is not None
                and uses_backend_pools(pool.pool_type)
                and not member.drained
                else None
            ),
            region=endpoint.capabilities.location if endpoint is not None else None,
            environment=endpoint.environment if endpoint is not None else None,
            model_name=deployment.model_name if deployment is not None else None,
            model_version=deployment.model_version if deployment is not None else None,
            sku_name=deployment.sku_name if deployment is not None else None,
            sku_capacity=deployment.sku_capacity if deployment is not None else None,
            capacity_type=(
                deployment.capacity_type if deployment is not None else CapacityType.UNKNOWN
            ),
            processing_scope=(
                deployment.processing_scope if deployment is not None else ProcessingScope.UNKNOWN
            ),
            spillover_deployment_name=(
                deployment.spillover_deployment_name if deployment is not None else None
            ),
            provisioning_state=deployment.provisioning_state if deployment is not None else None,
            observed=deployment is not None,
            readiness=readiness,
            readiness_message=message,
            environment_verdict=verdict,
        )
        return _Member(
            model=model,
            member=member,
            endpoint=endpoint,
            deployment=deployment,
            fit=fit,
            view=view,
            fingerprint=fingerprint,
            problems=problems,
            warnings=warnings,
        )

    async def detail(self, actor: Actor, pool_id: str) -> ModelPoolDetail:
        pool = await self.get_pool(actor, pool_id)
        gateway = await self._repository.get_gateway(actor.tenant_id, pool.gateway_id)
        if gateway is None:
            return ModelPoolDetail(
                pool=pool,
                problems=["The pool's gateway is no longer registered with MOSAIC."],
                unapplied_changes=has_unapplied_changes(pool),
            )
        assessment = await self._assess(actor, pool, gateway)
        models: list[PoolModelView] = []
        for model in pool.models:
            views = [item.view for item in assessment.members if item.model.id == model.id]
            models.append(
                PoolModelView(
                    id=model.id,
                    public_name=model.public_name,
                    display_name=model.display_name,
                    model_name=model.model_name,
                    model_format=model.model_format,
                    expected_version=model.expected_version,
                    listed=model.listed,
                    backend_pool_name=(
                        model.backend_pool_name if uses_backend_pools(pool.pool_type) else None
                    ),
                    capacity=capacity_badge(views),
                    members=views,
                )
            )
        facets = []
        if assessment.shape is not None:
            try:
                facets = _render(pool, assessment.shape).facets
            except ValueError:
                facets = []
        return ModelPoolDetail(
            pool=pool,
            gateway_name=gateway.name,
            gateway_environment=gateway.environment,
            base_url=_base_url(gateway, pool),
            models=models,
            problems=assessment.problems,
            warnings=assessment.warnings,
            facets=facets,
            unapplied_changes=has_unapplied_changes(pool),
        )

    async def candidates(self, actor: Actor, gateway_id: str) -> PoolCandidates:
        """Every deployment a pool on the gateway could use, grouped by the model it serves."""

        gateway = await self._load_gateway(actor, gateway_id)
        catalog = await self._catalog(actor)
        pools = await self._repository.list_model_pools(actor.tenant_id, gateway_id=gateway_id)
        # One group per model and API: deployments of one model that need different APIs, such as
        # gpt-4o on an Azure OpenAI resource and on a Foundry resource, can't share a pool.
        groups: dict[tuple[str, str, str], tuple[str, str | None, ApiShape | None]] = {}
        deployments: dict[tuple[str, str, str], list[PoolCandidateDeployment]] = {}
        for endpoint in await self._endpoints.list_endpoints(actor.tenant_id):
            if endpoint.provider == ModelProvider.OPENAI_COMPATIBLE:
                continue
            observed = await self._endpoints.list_observed_for_endpoint(
                ObservedModelDeployment, actor.tenant_id, endpoint.id, "observedModelDeployment"
            )
            verdict = permits(catalog, gateway.environment, endpoint.environment)
            readiness, message = _readiness(endpoint, gateway.id)
            for deployment in observed:
                if not deployment.model_name:
                    continue
                fit = assess_deployment(
                    endpoint.provider,
                    model_name=deployment.model_name,
                    model_format=deployment.model_format,
                    capabilities=deployment.capabilities,
                    endpoint=str(endpoint.endpoint),
                    gateway_sku=gateway.capabilities.sku_name,
                )
                reason = self._ineligible(endpoint, deployment, fit, verdict, readiness, message)
                key = (
                    (deployment.model_format or "").casefold(),
                    deployment.model_name.casefold(),
                    str(fit.api_shape or ""),
                )
                groups.setdefault(
                    key, (deployment.model_name, deployment.model_format, fit.api_shape)
                )
                deployments.setdefault(key, []).append(
                    PoolCandidateDeployment(
                        model_endpoint_id=endpoint.id,
                        endpoint_name=endpoint.name,
                        region=endpoint.capabilities.location,
                        environment=endpoint.environment,
                        deployment_name=deployment.deployment_name,
                        model_version=deployment.model_version,
                        sku_name=deployment.sku_name,
                        sku_capacity=deployment.sku_capacity,
                        capacity_type=deployment.capacity_type,
                        processing_scope=deployment.processing_scope,
                        spillover_deployment_name=deployment.spillover_deployment_name,
                        readiness=readiness,
                        environment_verdict=verdict,
                        eligible=reason is None,
                        reason=reason,
                        pool_ids=sorted(
                            pool.id
                            for pool in pools
                            if pool.uses(endpoint.id, deployment.deployment_name)
                        ),
                    )
                )
        models = [
            PoolCandidateModel(
                model_name=name,
                model_format=model_format,
                api_shape=shape,
                deployments=sorted(
                    deployments[key],
                    key=lambda item: (item.endpoint_name.casefold(), item.deployment_name),
                ),
            )
            for key, (name, model_format, shape) in sorted(groups.items())
        ]
        return PoolCandidates(
            gateway_id=gateway.id,
            gateway_environment=gateway.environment,
            pool_types={str(kind): _pool_type_problem(gateway, kind) for kind in ModelPoolType},
            models=models,
        )

    @staticmethod
    def _ineligible(
        endpoint: ModelEndpoint,
        deployment: ObservedModelDeployment,
        fit: DeploymentFit,
        verdict: EnvironmentVerdict,
        readiness: Readiness,
        message: str | None,
    ) -> str | None:
        if endpoint.uses_backend_key():
            return "MOSAIC reaches this endpoint with an API key, which pools can't use yet."
        if deployment.capacity_type == CapacityType.BATCH:
            return "Batch deployments can't serve requests as they arrive."
        state = deployment.provisioning_state
        if state and state.casefold() != "succeeded":
            return f"The deployment is {state}."
        if fit.api_shape is None or not fit.publishable:
            return (
                fit.unpublishable_reason or "MOSAIC has no API it can serve this deployment with."
            )
        if verdict.level == VerdictLevel.BLOCKED:
            return verdict.reason or "The gateway's environment doesn't permit this endpoint."
        if readiness == "cannotInvoke":
            return message
        return None

    async def _reject_desired_collisions(self, actor: Actor, pool: ModelPool) -> None:
        """Refuse an API name, path, or product another MOSAIC record on the gateway uses."""

        name = pool.api_name.casefold()
        path = pool.api_path.strip("/").casefold()
        for other in await self._repository.list_model_pools(
            actor.tenant_id, gateway_id=pool.gateway_id
        ):
            if other.id == pool.id:
                continue
            if other.api_name.casefold() == name or other.api_path.strip("/").casefold() == path:
                raise ConflictError(
                    f"The {other.display_name} pool on this gateway already uses that API name or "
                    "path. Choose another.",
                    details={"conflictingPoolId": other.id, "apiName": pool.api_name},
                )
            if other.product_name.casefold() == pool.product_name.casefold():
                raise ConflictError(
                    f"The {other.display_name} pool on this gateway already uses product "
                    f"{pool.product_name}. Choose another product name.",
                    details={"conflictingPoolId": other.id, "productName": pool.product_name},
                )
        publications: list[Publication | McpPublication] = [
            *await self._repository.list_publications(actor.tenant_id, gateway_id=pool.gateway_id),
            *await self._repository.list_mcp_publications(
                actor.tenant_id, gateway_id=pool.gateway_id
            ),
        ]
        for publication in publications:
            if (
                publication.api_name.casefold() == name
                or publication.api_path.strip("/").casefold() == path
            ):
                raise ConflictError(
                    "A publication on this gateway already uses that API name or path. Choose "
                    "another.",
                    details={"conflictingPublicationId": publication.id, "apiName": pool.api_name},
                )

    async def _reject_collisions(
        self, actor: Actor, pool: ModelPool, gateway: Gateway, client: ApimClient
    ) -> None:
        """Refuse to take over an API, or a path, MOSAIC didn't create."""

        live = await client.get_api(pool.api_name)
        if live is not None and not self._owns(pool, PublishedResourceKind.API, pool.api_name):
            raise ConflictError(
                "An API with this name already exists in the gateway and MOSAIC did not create "
                "it. Choose a different name rather than replacing it.",
                details={"apiName": pool.api_name, "gatewayId": gateway.id},
            )
        observed = await self._repository.list_observed(
            ObservedApi, actor.tenant_id, gateway.id, "observedApi"
        )
        wanted = pool.api_path.strip("/").casefold()
        clash = next(
            (
                item
                for item in observed
                if item.path.strip("/").casefold() == wanted
                and item.name.casefold() != pool.api_name.casefold()
            ),
            None,
        )
        if clash is not None:
            raise ConflictError(
                "Another API in this gateway is already served at that path.",
                details={"apiPath": pool.api_path, "conflictingApi": clash.name},
            )

    async def _exists(
        self, client: ApimClient, pool: ModelPool, kind: PublishedResourceKind, name: str
    ) -> bool:
        match kind:
            case PublishedResourceKind.NAMED_VALUE:
                return await client.get_named_value(name) is not None
            case PublishedResourceKind.POLICY_FRAGMENT:
                return await client.get_policy_fragment_resource(name) is not None
            case PublishedResourceKind.BACKEND | PublishedResourceKind.BACKEND_POOL:
                return await client.get_backend(name) is not None
            case PublishedResourceKind.API:
                return await client.get_api(name) is not None
            case PublishedResourceKind.API_OPERATION:
                return await client.get_api_operation(pool.api_name, name) is not None
            case PublishedResourceKind.API_POLICY:
                return await client.get_api_policy(pool.api_name) is not None
            case PublishedResourceKind.PRODUCT:
                return await client.get_product(name) is not None
            case PublishedResourceKind.PRODUCT_API:
                linked = await client.list_product_apis(pool.product_name)
                return any(entry.get("name") == pool.api_name for entry in linked)
            case PublishedResourceKind.SUBSCRIPTION:
                return await client.get_subscription(name) is not None

    async def _steps(
        self, client: ApimClient, pool: ModelPool, desired: list[_Resource]
    ) -> list[PublishPlanStep]:
        steps: list[PublishPlanStep] = []
        for item in desired:
            existed = await self._exists(client, pool, item.kind, item.name)
            if (
                existed
                and item.kind in _CLAIMED_KINDS
                and not self._owns(pool, item.kind, item.name)
            ):
                raise ConflictError(
                    f"API Management already has a {_noun(item.kind)} named {item.name}, and "
                    "MOSAIC didn't create it. MOSAIC won't replace it, so choose other names.",
                    details={"kind": str(item.kind), "name": item.name},
                )
            action = PublishAction.UPDATE if existed else PublishAction.CREATE
            verb = "Update" if existed else "Create"
            steps.append(
                PublishPlanStep(
                    kind=item.kind,
                    name=item.name,
                    action=action,
                    reason=f"{verb} the pool's {_noun(item.kind)}.",
                    resource_id=item.resource_id,
                    existed=existed,
                )
            )
        wanted = {(item.kind, item.name) for item in desired}
        stale = sorted(
            (
                resource
                for resource in pool.created_resources()
                if (resource.kind, resource.name) not in wanted
            ),
            key=lambda resource: -_ORDER[resource.kind],
        )
        steps.extend(
            PublishPlanStep(
                kind=resource.kind,
                name=resource.name,
                action=PublishAction.DELETE,
                reason=f"The pool no longer uses this {_noun(resource.kind)}.",
                resource_id=resource.resource_id,
                existed=True,
            )
            for resource in stale
        )
        return steps

    async def _governed_steps(
        self,
        pool: ModelPool,
        snapshot: PoolAccessSnapshot,
        client: ApimClient,
        resource: ApimResourceId,
        desired: list[_Resource],
    ) -> list[PublishPlanStep]:
        """A governed apply's steps, in stages that keep the pool closed while they run.

        Prepare denies every call, suspends every key, and writes what the policy routes to.
        Policy installs the reviewed access policy. Activate turns on only what it allows.
        """

        steps: list[PublishPlanStep] = [
            # First of all: every governed policy reads the gateway's blocked list (ADR 0023).
            BlockedListGate.plan_step(
                resource,
                exists=await client.get_named_value(BLOCKED_COST_CENTERS_NAMED_VALUE) is not None,
            )
        ]
        base: dict[tuple[PublishedResourceKind, str], PublishPlanStep] = {}
        for item in desired:
            existed = await self._exists(client, pool, item.kind, item.name)
            if (
                existed
                and item.kind in _CLAIMED_KINDS
                and not self._owns(pool, item.kind, item.name)
            ):
                raise ConflictError(
                    f"API Management already has a {_noun(item.kind)} named {item.name}, and "
                    "MOSAIC didn't create it. Governed access won't replace it, so choose other "
                    "names.",
                    details={"kind": str(item.kind), "name": item.name},
                )
            verb = "Update" if existed else "Create"
            base[(item.kind, item.name)] = PublishPlanStep(
                kind=item.kind,
                name=item.name,
                action=PublishAction.UPDATE if existed else PublishAction.CREATE,
                reason=f"{verb} the pool's {_noun(item.kind)}.",
                resource_id=item.resource_id,
                existed=existed,
                stage="prepare",
            )
        api = base[(PublishedResourceKind.API, pool.api_name)]
        api_policy = base[(PublishedResourceKind.API_POLICY, "policy")]
        if api.existed:
            steps.append(
                api_policy.model_copy(
                    update={
                        "stage": "prepare",
                        "reason": "Temporarily deny all calls while this pool-wide batch applies.",
                    }
                )
            )
        routing = {
            PublishedResourceKind.NAMED_VALUE,
            PublishedResourceKind.BACKEND,
            PublishedResourceKind.BACKEND_POOL,
        }
        steps.extend(base[(item.kind, item.name)] for item in desired if item.kind in routing)
        steps.append(
            api.model_copy(
                update={
                    "reason": "Require a subscription until the governed policy is installed, "
                    "and reset key parameter names to Ocp-Apim-Subscription-Key/subscription-key."
                }
            )
        )
        if not api.existed:
            steps.append(
                api_policy.model_copy(
                    update={
                        "stage": "prepare",
                        "reason": "Deny every call until the reviewed access policy is installed.",
                    }
                )
            )
            api_policy = api_policy.model_copy(
                update={"action": PublishAction.UPDATE, "existed": True}
            )

        keys = snapshot.key_grants()
        names = set(keys)
        names.update(
            item.name
            for item in pool.created_resources()
            if item.kind == PublishedResourceKind.SUBSCRIPTION
        )
        suspensions: list[PublishPlanStep] = []
        deletions: list[PublishPlanStep] = []
        for name in sorted(names):
            live = await client.get_subscription(name)
            # Keys are created on request, by a grant's holder or an administrator, and never by
            # an apply. A grant without one keeps none; the policy already knows its name, so a
            # key created later works without another apply.
            if live is None:
                continue
            if not self._owns(pool, PublishedResourceKind.SUBSCRIPTION, name):
                raise ConflictError(
                    "A key this pool's grants use already exists, and MOSAIC didn't create it. "
                    "Governed access won't take it over.",
                    details={"subscriptionName": name},
                )
            self._validate_subscription_scope(pool, resource, name, live)
            served = keys.get(name, [])
            resource_id = f"{resource.canonical}/subscriptions/{name}"
            if served and all(grant.revoked for grant in served):
                deletions.append(
                    PublishPlanStep(
                        kind=PublishedResourceKind.SUBSCRIPTION,
                        name=name,
                        action=PublishAction.DELETE,
                        reason=(
                            f"Delete the key of {_grant_label(served)}, revoked when its subject "
                            "left its cost center."
                        ),
                        resource_id=resource_id,
                        existed=True,
                        entitlement_id=served[0].entitlement_id,
                        stage="prepare",
                    )
                )
                continue
            if served:
                reason = f"Suspend the key of {_grant_label(served)} while access is applied."
            elif name == pool.subscription_name:
                reason = (
                    "Suspend the pool's bootstrap subscription; governed callers use their own "
                    "keys."
                )
            else:
                reason = "Suspend this key; none of its grants is in this snapshot."
            suspensions.append(
                PublishPlanStep(
                    kind=PublishedResourceKind.SUBSCRIPTION,
                    name=name,
                    action=PublishAction.UPDATE,
                    reason=reason,
                    resource_id=resource_id,
                    existed=True,
                    entitlement_id=served[0].entitlement_id if served else None,
                    subscription_state="suspended",
                    stage="prepare",
                )
            )
        steps.extend(deletions)
        steps.extend(suspensions)
        placed = routing | {
            PublishedResourceKind.API,
            PublishedResourceKind.API_POLICY,
            PublishedResourceKind.POLICY_FRAGMENT,
        }
        steps.extend(base[(item.kind, item.name)] for item in desired if item.kind not in placed)
        # The reviewed fragment goes in only once nothing can reach it: the API's policy denies
        # every call until the next step replaces it.
        steps.append(
            base[(PublishedResourceKind.POLICY_FRAGMENT, pool.fragment_name)].model_copy(
                update={"stage": "policy"}
            )
        )
        steps.append(
            api_policy.model_copy(
                update={
                    "stage": "policy",
                    "reason": "Install the reviewed, fail-closed access policy.",
                }
            )
        )
        steps.append(
            api.model_copy(
                update={
                    "stage": "activate",
                    "reason": (
                        "Allow token-only calls after installing the reviewed authorization policy."
                        if snapshot.settings.entra_enabled
                        else "Keep subscription authentication required."
                    ),
                }
            )
        )
        for step in suspensions:
            usable = [
                grant
                for grant in keys.get(step.name, [])
                if grant.enabled and grant.keys_allowed
            ]
            if usable and snapshot.settings.keys_enabled:
                steps.append(
                    step.model_copy(
                        update={
                            "stage": "activate",
                            "subscription_state": "active",
                            "reason": f"Activate key access for {_grant_label(usable)}.",
                        }
                    )
                )
        wanted = {(item.kind, item.name) for item in desired}
        stale = sorted(
            (
                item
                for item in pool.created_resources()
                # A key outlives the open access it was made for, suspended, until unpublish.
                if (item.kind, item.name) not in wanted
                and item.kind != PublishedResourceKind.SUBSCRIPTION
            ),
            key=lambda item: -_ORDER[item.kind],
        )
        steps.extend(
            PublishPlanStep(
                kind=item.kind,
                name=item.name,
                action=PublishAction.DELETE,
                reason=f"The pool no longer uses this {_noun(item.kind)}.",
                resource_id=item.resource_id,
                existed=True,
            )
            for item in stale
        )
        return steps

    @staticmethod
    def _require_shape(pool: ModelPool, assessment: _Assessment) -> ApiShape:
        if assessment.problems:
            raise ConflictError(
                "This pool can't be published until its problems are fixed.",
                details={"id": pool.id, "problems": assessment.problems},
            )
        if assessment.shape is None:
            raise ConflictError(
                "MOSAIC can't tell which API this pool's deployments need.",
                details={"id": pool.id},
            )
        return assessment.shape

    async def plan(self, actor: Actor, pool_id: str) -> PublishPlan:
        """Work out every write a publish makes, without making any."""

        async with publication_lock(self._repository, actor.tenant_id, pool_id):
            pool = await self.get_pool(actor, pool_id)
            if pool.status == PublicationStatus.APPLYING:
                raise ConflictError(_APPLYING, details={"id": pool_id})
            if pool.access_state == "unknown":
                raise ConflictError(_UNKNOWN_ACCESS, details={"id": pool_id})
            gateway = await self._load_gateway(actor, pool.gateway_id)
            self._require_writable(gateway)
            assessment = await self._assess(actor, pool, gateway)
            shape = self._require_shape(pool, assessment)
            await self._reject_desired_collisions(actor, pool)
            resource = ApimResourceId.parse(gateway.azure_resource_id)
            client = self._client_factory(resource)
            await self._reject_collisions(actor, pool, gateway, client)
            snapshot, access_warnings = await self._access_snapshot(pool, gateway, shape)
            policy = _policy(pool, shape, snapshot)
            desired = _desired(pool, gateway, assessment, shape)
            if snapshot is None:
                steps = await self._steps(client, pool, desired)
            else:
                steps = await self._governed_steps(pool, snapshot, client, resource, desired)
                if _customized_key_names(await client.get_api(pool.api_name)):
                    access_warnings.append(
                        "This API's customized subscription key names will be reset to "
                        "Ocp-Apim-Subscription-Key (header) and subscription-key (query). "
                        "Callers must use these governed defaults."
                    )
            plan = PublishPlan(
                id=new_id("publishplan"),
                tenant_id=actor.tenant_id,
                publication_id=pool.id,
                target="pool",
                operation="publish",
                gateway_id=gateway.id,
                digest=pool_digest(
                    pool, shape, policy, desired, assessment.fingerprints(), snapshot
                ),
                steps=steps,
                facets=policy.facets,
                policy_content_sha256=policy.content_sha256,
                warnings=[*assessment.warnings, *access_warnings],
                actor_object_id=actor.object_id,
                pool_access_snapshot=snapshot,
                previous_access_version=(
                    pool.applied_access.version if pool.applied_access else None
                ),
            )
            await self._repository.save_publish_plan(plan)
            await self._repository.record_model_pool_state(
                pool.model_copy(
                    update={
                        "status": (
                            PublicationStatus.PUBLISHED
                            if pool.status == PublicationStatus.PUBLISHED
                            else PublicationStatus.PLANNED
                        ),
                        "api_shape": shape,
                        "vendor": assessment.vendor,
                        "last_plan_id": plan.id,
                        "last_plan_digest": plan.digest,
                        "updated_at": utc_now(),
                    }
                )
            )
            return plan

    async def _resolve_plan(
        self, actor: Actor, pool: ModelPool, plan_id: str | None
    ) -> PublishPlan:
        resolved = plan_id or pool.last_plan_id
        if not resolved:
            raise ConflictError(
                "Plan this pool before applying it, so the changes are reviewed first.",
                details={"id": pool.id, "reason": "planRequired"},
            )
        plan = await self._repository.get_publish_plan(actor.tenant_id, resolved)
        if plan is None or plan.target != "pool" or plan.publication_id != pool.id:
            raise NotFoundError("Pool plan was not found", details={"id": resolved})
        if plan.operation != "publish":
            raise ConflictError(
                "That plan unpublishes this pool, and apply runs only publish plans.",
                details={"planId": plan.id, "operation": plan.operation},
            )
        if plan.id != pool.last_plan_id:
            raise ConflictError(_STALE_PLAN, details={"planId": plan.id, "reason": "stalePlan"})
        return plan

    async def apply(self, actor: Actor, pool_id: str, plan_id: str | None = None) -> PublishRun:
        owner = new_id("publishrun")
        await self._repository.acquire_publication_lock(actor.tenant_id, pool_id, owner)
        self._active.add(pool_id)
        try:
            return await self._apply_locked(actor, pool_id, plan_id, owner)
        except BaseException:
            self._active.discard(pool_id)
            await self._repository.release_publication_lock(actor.tenant_id, pool_id, owner)
            raise

    async def _apply_locked(
        self, actor: Actor, pool_id: str, plan_id: str | None, owner: str
    ) -> PublishRun:
        pool = await self.get_pool(actor, pool_id)
        if pool.access_state == "unknown":
            raise ConflictError(_UNKNOWN_ACCESS, details={"id": pool_id})
        gateway = await self._load_gateway(actor, pool.gateway_id)
        self._require_writable(gateway)
        plan = await self._resolve_plan(actor, pool, plan_id)
        assessment = await self._assess(actor, pool, gateway)
        shape = self._require_shape(pool, assessment)
        snapshot, _ = await self._access_snapshot(pool, gateway, shape)
        policy = _policy(pool, shape, snapshot)
        desired = _desired(pool, gateway, assessment, shape)
        if (
            pool_digest(pool, shape, policy, desired, assessment.fingerprints(), snapshot)
            != plan.digest
        ):
            raise ConflictError(_STALE_PLAN, details={"planId": plan.id, "reason": "stalePlan"})
        run = self._claim(actor, pool, plan, owner)
        await self._repository.save_publish_run(run)
        await self._mark_applying(pool, run.id)
        client = self._client_factory(ApimResourceId.parse(gateway.azure_resource_id))
        if snapshot is None:
            self._spawn(self._run_apply(pool, gateway, client, plan, policy, desired, run))
        else:
            self._spawn(
                self._run_governed_apply(pool, gateway, client, plan, policy, desired, shape, run)
            )
        return run

    @staticmethod
    def _claim(actor: Actor, pool: ModelPool, plan: PublishPlan, owner: str) -> PublishRun:
        return PublishRun(
            id=owner,
            tenant_id=pool.tenant_id,
            publication_id=pool.id,
            target="pool",
            gateway_id=pool.gateway_id,
            plan_id=plan.id,
            plan_digest=plan.digest,
            actor_object_id=actor.object_id,
            pool_access_snapshot=plan.pool_access_snapshot,
        )

    def _spawn(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task: asyncio.Task[None] = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._task_finished)

    def _task_finished(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            self._task_failures.append(error)

    async def _assert_lock(self, pool: ModelPool, run: PublishRun) -> None:
        if await self._repository.get_publication_lock(pool.tenant_id, pool.id) != run.id:
            raise ConflictError("The pool's write lock is no longer owned by this run")

    async def _release_run(self, pool: ModelPool, run: PublishRun) -> None:
        await self._repository.release_publication_lock(pool.tenant_id, pool.id, run.id)

    async def _record_state(
        self,
        pool: ModelPool,
        *,
        status: PublicationStatus,
        resources: list[PublishedResource],
        run_id: str | None,
        error: str | None,
        applied: bool = False,
        unpublished: bool = False,
        access_snapshot: PoolAccessSnapshot | None = None,
        access_state: Literal["pending", "applying", "applied", "failed", "unknown"]
        | None = None,
        applied_model_ids: list[str] | None = None,
    ) -> None:
        """Re-read before writing, so a run never brings back a pool that was removed.

        The applied snapshot, access state, and served models change only when given.
        """

        current = await self._repository.get_model_pool(pool.tenant_id, pool.id)
        if current is None:
            raise ConflictError("The pool was removed while a run was writing it")
        now = utc_now()
        update: dict[str, Any] = {
            "status": status,
            "resources": list(resources),
            "last_run_id": run_id,
            "last_error": error,
            "updated_at": now,
        }
        if access_snapshot is not None:
            update["applied_access"] = access_snapshot
        if access_state is not None:
            update["access_state"] = access_state
        if applied_model_ids is not None:
            update["applied_model_ids"] = list(applied_model_ids)
        if applied:
            update.update(
                {
                    "last_applied_at": now,
                    "unpublished_at": None,
                    "applied_intent_digest": intent_digest(current),
                }
            )
        if unpublished:
            update.update(
                {
                    "unpublished_at": now,
                    "last_plan_id": None,
                    "last_plan_digest": None,
                    "applied_intent_digest": None,
                    "applied_model_ids": [],
                }
            )
        await self._repository.record_model_pool_state(current.model_copy(update=update))

    async def _mark_applying(self, pool: ModelPool, run_id: str) -> None:
        await self._record_state(
            pool,
            status=PublicationStatus.APPLYING,
            resources=pool.resources,
            run_id=run_id,
            error=None,
            access_state="applying" if pool.governed_access else None,
        )

    async def _progress(
        self,
        pool: ModelPool,
        run: PublishRun,
        results: list[PublishStepResult],
        resources: list[PublishedResource],
    ) -> None:
        await self._assert_lock(pool, run)
        await self._repository.save_publish_run(run.model_copy(update={"steps": list(results)}))
        await self._record_state(
            pool,
            status=PublicationStatus.APPLYING,
            resources=resources,
            run_id=run.id,
            error=None,
        )

    async def _interrupted(self, pool: ModelPool, run: PublishRun, message: str) -> None:
        # Best effort. The non-expiring lock stays even if storage is unavailable, so no other
        # instance applies or unpublishes until an administrator recovers the run.
        try:
            current = await self._repository.get_publish_run(run.tenant_id, run.id) or run
            await self._repository.save_publish_run(
                current.model_copy(
                    update={
                        "status": PublishRunStatus.INTERRUPTED,
                        "errors": [*current.errors, message],
                        "updated_at": utc_now(),
                    }
                )
            )
            latest = await self._repository.get_model_pool(pool.tenant_id, pool.id)
            if latest is not None and latest.last_run_id == run.id:
                await self._repository.record_model_pool_state(
                    latest.model_copy(
                        update={
                            "status": PublicationStatus.FAILED,
                            "last_error": message,
                            "updated_at": utc_now(),
                            # Nobody knows what a governed run left in force, so nothing plans
                            # again until an administrator recovers it.
                            "access_state": (
                                "unknown"
                                if latest.governed_access or latest.applied_access
                                else latest.access_state
                            ),
                        }
                    )
                )
        except Exception:
            logger.exception("pool_interruption_not_recorded", pool_id=pool.id)

    async def _run_apply(
        self,
        pool: ModelPool,
        gateway: Gateway,
        client: ApimClient,
        plan: PublishPlan,
        policy: PublicationPolicy,
        desired: list[_Resource],
        run: PublishRun,
    ) -> None:
        started = utc_now()
        writer = self._writer_factory(ApimResourceId.parse(gateway.azure_resource_id))
        resources = {(item.kind, item.name): item for item in desired}
        tracked = list(pool.resources)
        results: list[PublishStepResult] = []
        # Stale resources the run couldn't remove. They don't fail a pool that now works.
        leftovers: list[PublishedResource] = []
        cleanup: list[str] = []
        failure: str | None = None
        try:
            for step in plan.steps:
                await self._assert_lock(pool, run)
                result = PublishStepResult(
                    kind=step.kind,
                    name=step.name,
                    action=step.action,
                    resource_id=step.resource_id,
                )
                if step.action == PublishAction.DELETE:
                    # The plan puts removals last, after the new routing is in place.
                    try:
                        await self._progress(pool, run, [*results, result], tracked)
                        await self._remove(writer, pool, step.kind, step.name)
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        result.status = PublishStepStatus.FAILED
                        result.error = str(error)
                        cleanup.append(f"{_noun(step.kind)} {step.name}: {error}")
                        leftovers.extend(
                            item
                            for item in tracked
                            if item.kind == step.kind and item.name == step.name
                        )
                    else:
                        result.status = PublishStepStatus.SUCCEEDED
                        tracked = [
                            item
                            for item in tracked
                            if not (item.kind == step.kind and item.name == step.name)
                        ]
                    results.append(result)
                    await self._progress(pool, run, results, tracked)
                    continue
                item = resources.get((step.kind, step.name))
                if item is None:
                    continue
                try:
                    observed = await self._exists(client, pool, item.kind, item.name)
                    result.created_by_mosaic = not observed
                    if not step.existed and observed:
                        raise ConflictError(
                            f"The {_noun(step.kind)} {step.name} appeared after the plan was "
                            "made. MOSAIC won't overwrite something it didn't create. Re-plan "
                            "to review it.",
                            details={"kind": str(step.kind), "name": step.name},
                        )
                    await self._progress(pool, run, [*results, result], tracked)
                    await self._write(writer, pool, policy, item)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    result.status = PublishStepStatus.FAILED
                    result.error = str(error)
                    results.append(result)
                    failure = f"{_noun(step.kind)} {step.name}: {error}"
                    break
                result.status = PublishStepStatus.SUCCEEDED
                results.append(result)
                tracked = _merge_resources(
                    tracked,
                    [
                        PublishedResource(
                            kind=result.kind,
                            name=result.name,
                            resource_id=result.resource_id,
                            created_by_mosaic=result.created_by_mosaic,
                        )
                    ],
                )
                await self._progress(pool, run, results, tracked)
        except asyncio.CancelledError:
            await self._interrupted(pool, run, STALE_RUN_MESSAGE)
            self._active.discard(pool.id)
            raise
        except Exception as error:
            logger.exception("pool_apply_failed", pool_id=pool.id)
            failure = str(error)

        try:
            if failure is None:
                await self._finish_success(
                    pool, run, results, tracked, desired, started, cleanup, leftovers
                )
            else:
                await self._rollback(pool, writer, run, results, tracked, started, failure)
            await self._release_run(pool, run)
        except asyncio.CancelledError:
            await self._interrupted(pool, run, STALE_RUN_MESSAGE)
            raise
        except Exception as error:
            await self._interrupted(pool, run, f"Publish result was not recorded: {error}")
            raise
        finally:
            self._active.discard(pool.id)

    async def _run_governed_apply(
        self,
        pool: ModelPool,
        gateway: Gateway,
        client: ApimClient,
        plan: PublishPlan,
        policy: PublicationPolicy,
        desired: list[_Resource],
        shape: str,
        run: PublishRun,
    ) -> None:
        """Run a reviewed governed plan, stage by stage, keeping the pool closed until it's done.

        Nothing is rolled back: a failure leaves the pool with no more access than its last
        applied snapshot allowed, as :meth:`_governed_failure` describes.
        """

        started = utc_now()
        resource = ApimResourceId.parse(gateway.azure_resource_id)
        writer = self._writer_factory(resource)
        items = {(item.kind, item.name): item for item in desired}
        owned = list(pool.resources)
        results: list[PublishStepResult] = []
        # Stale resources the run couldn't remove. They don't fail a pool that now works.
        leftovers: list[PublishedResource] = []
        cleanup: list[str] = []
        failure: str | None = None
        try:
            snapshot = plan.pool_access_snapshot
            if snapshot is None:
                raise ConflictError("A governed apply needs the access snapshot its plan reviewed")
            try:
                for step in plan.steps:
                    await self._assert_lock(pool, run)
                    result = PublishStepResult(
                        kind=step.kind,
                        name=step.name,
                        action=step.action,
                        resource_id=step.resource_id,
                        stage=step.stage,
                    )
                    results.append(result)
                    if (
                        step.action == PublishAction.DELETE
                        and step.kind != PublishedResourceKind.SUBSCRIPTION
                    ):
                        # Stale, and last: the policy no longer routes to it.
                        try:
                            await self._progress(pool, run, results, owned)
                            await self._remove(writer, pool, step.kind, step.name)
                        except asyncio.CancelledError:
                            raise
                        except Exception as error:
                            result.status = PublishStepStatus.FAILED
                            result.error = str(error)
                            cleanup.append(f"{_noun(step.kind)} {step.name}: {error}")
                            leftovers.extend(
                                item
                                for item in owned
                                if item.kind == step.kind and item.name == step.name
                            )
                        else:
                            result.status = PublishStepStatus.SUCCEEDED
                            owned = [
                                item
                                for item in owned
                                if not (item.kind == step.kind and item.name == step.name)
                            ]
                        await self._progress(pool, run, results, owned)
                        continue
                    write_started = False
                    try:
                        if is_blocked_list(step.kind, step.name):
                            await self._progress(pool, run, results, owned)
                            await ensure_blocked_list(
                                self._blocked_list, client, writer, pool.tenant_id
                            )
                        elif step.action == PublishAction.DELETE:
                            await self._progress(pool, run, results, owned)
                            owned = await self._delete_revoked_key(
                                pool, client, writer, step.name, owned
                            )
                        else:
                            exists = await self._exists(client, pool, step.kind, step.name)
                            if step.kind == PublishedResourceKind.SUBSCRIPTION and not exists:
                                # Deleted since the plan: there's nothing left to suspend or
                                # activate, and an apply never creates a key.
                                result.status = PublishStepStatus.SKIPPED
                                await self._progress(pool, run, results, owned)
                                continue
                            if (
                                exists
                                and step.kind in _CLAIMED_KINDS
                                and not self._owns(
                                    pool.model_copy(update={"resources": owned}),
                                    step.kind,
                                    step.name,
                                )
                            ):
                                raise ConflictError(
                                    f"The {_noun(step.kind)} {step.name} appeared without MOSAIC "
                                    "ownership; the reviewed plan can't overwrite it.",
                                    details={"kind": str(step.kind), "name": step.name},
                                )
                            if step.kind == PublishedResourceKind.SUBSCRIPTION:
                                live = await client.get_subscription(step.name)
                                self._validate_subscription_scope(
                                    pool, resource, step.name, live or {}
                                )
                            result.created_by_mosaic = not exists
                            await self._progress(pool, run, results, owned)
                            write_started = True
                            await self._write_governed_step(
                                writer,
                                pool,
                                policy,
                                step,
                                items.get((step.kind, step.name)),
                                snapshot,
                            )
                            owned = _merge_resources(
                                owned,
                                [
                                    PublishedResource(
                                        kind=step.kind,
                                        name=step.name,
                                        resource_id=step.resource_id,
                                        created_by_mosaic=result.created_by_mosaic,
                                    )
                                ],
                            )
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        result.status = PublishStepStatus.FAILED
                        result.error = str(error)
                        failure = f"{_noun(step.kind)} {step.name}: {error}"
                        if (
                            write_started
                            and result.created_by_mosaic
                            and await self._exists(client, pool, step.kind, step.name)
                        ):
                            # A write that failed may still have made it; it's the pool's.
                            owned = _merge_resources(
                                owned,
                                [
                                    PublishedResource(
                                        kind=step.kind,
                                        name=step.name,
                                        resource_id=step.resource_id,
                                        created_by_mosaic=True,
                                    )
                                ],
                            )
                        break
                    result.status = PublishStepStatus.SUCCEEDED
                    await self._progress(pool, run, results, owned)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception("pool_apply_failed", pool_id=pool.id)
                failure = failure or str(error)
            if failure is None:
                await self._finish_success(
                    pool,
                    run,
                    results,
                    owned,
                    desired,
                    started,
                    cleanup,
                    leftovers,
                    access_snapshot=snapshot,
                )
                await self._release_run(pool, run)
            else:
                await self._governed_failure(
                    pool, client, writer, run, results, owned, started, failure, shape
                )
        except asyncio.CancelledError:
            await self._interrupted(pool, run, STALE_RUN_MESSAGE)
            raise
        except Exception as error:
            await self._interrupted(
                pool, run, f"Apply outcome could not be durably established: {error}"
            )
            raise
        finally:
            self._active.discard(pool.id)

    async def _delete_revoked_key(
        self,
        pool: ModelPool,
        client: ApimClient,
        writer: ApimWriter,
        name: str,
        owned: list[PublishedResource],
    ) -> list[PublishedResource]:
        """Delete a key every grant of which was revoked. MOSAIC deletes only keys it created."""

        if not self._owns(
            pool.model_copy(update={"resources": owned}), PublishedResourceKind.SUBSCRIPTION, name
        ):
            raise ConflictError(
                "MOSAIC deletes only the keys it created for this pool's grants.",
                details={"subscriptionName": name},
            )
        live = await client.get_subscription(name)
        if live is not None:
            self._validate_subscription_scope(pool, writer.resource, name, live)
            await writer.delete_subscription(name)
        return [
            item
            for item in owned
            if not (item.kind == PublishedResourceKind.SUBSCRIPTION and item.name == name)
        ]

    async def _write_governed_step(
        self,
        writer: ApimWriter,
        pool: ModelPool,
        policy: PublicationPolicy,
        step: PublishPlanStep,
        item: _Resource | None,
        snapshot: PoolAccessSnapshot,
    ) -> None:
        if step.kind == PublishedResourceKind.API_POLICY and step.stage == "prepare":
            await writer.put_api_policy(pool.api_name, DENY_ALL_POLICY)
            return
        if step.kind == PublishedResourceKind.SUBSCRIPTION:
            state = step.subscription_state or "suspended"
            if step.name == pool.subscription_name:
                await writer.put_subscription(
                    step.name,
                    display_name=f"{pool.display_name[:90]} (pool)",
                    product_name=pool.product_name,
                    state="suspended",
                )
                return
            await writer.put_api_subscription(
                step.name,
                display_name=_key_display_name(pool, snapshot.key_grants().get(step.name, [])),
                api_name=pool.api_name,
                state=state,
            )
            return
        if item is None:
            raise ConflictError(
                "MOSAIC lost track of this pool resource. Plan the pool again.",
                details={"kind": str(step.kind), "name": step.name},
            )
        if step.kind == PublishedResourceKind.API:
            payload = item.payload
            await writer.put_api(
                item.name,
                display_name=payload["displayName"],
                path=payload["path"],
                description=payload["description"],
                # Token-only calls are allowed only once the policy that checks tokens is in.
                subscription_required=(
                    step.stage != "activate" or not snapshot.settings.entra_enabled
                ),
                use_default_subscription_key_names=True,
            )
            return
        await self._write(writer, pool, policy, item)

    async def _establish_deny(
        self,
        pool: ModelPool,
        client: ApimClient,
        writer: ApimWriter,
        journal: RecoveryJournal | None = None,
    ) -> tuple[bool, list[str]]:
        """Make the pool's API refuse every call. True once it does, or when it has no API."""

        if not self._owns(pool, PublishedResourceKind.API, pool.api_name):
            return True, []
        if await client.get_api(pool.api_name) is None:
            return True, []
        errors: list[str] = []
        try:
            existed = await client.get_api_policy(pool.api_name) is not None
            await writer.put_api_policy(pool.api_name, DENY_ALL_POLICY)
            if journal is not None:
                await journal(PublishedResourceKind.API_POLICY, "policy", not existed, "policy")
            return True, []
        except asyncio.CancelledError:
            raise
        except Exception as error:
            errors.append(f"Denying every call to the pool's API failed: {error}")
        if self._owns(pool, PublishedResourceKind.POLICY_FRAGMENT, pool.fragment_name):
            # The API's policy, whichever MOSAIC wrote, includes the fragment.
            try:
                await writer.put_policy_fragment(
                    pool.fragment_name, DENY_ALL_FRAGMENT, description="MOSAIC fail-closed recovery"
                )
                if journal is not None:
                    await journal(
                        PublishedResourceKind.POLICY_FRAGMENT, pool.fragment_name, False, "policy"
                    )
                return True, errors
            except asyncio.CancelledError:
                raise
            except Exception as error:
                errors.append(f"Denying every call through the pool's fragment failed: {error}")
        return False, errors

    async def _suspend_owned_subscriptions(
        self,
        pool: ModelPool,
        client: ApimClient,
        writer: ApimWriter,
        journal: RecoveryJournal | None = None,
    ) -> list[str]:
        """Suspend every key the pool's record says MOSAIC created. Returns what failed."""

        errors: list[str] = []
        applied = pool.applied_access.key_grants() if pool.applied_access else {}
        for item in pool.created_resources():
            if item.kind != PublishedResourceKind.SUBSCRIPTION:
                continue
            try:
                live = await client.get_subscription(item.name)
                if live is None:
                    continue
                self._validate_subscription_scope(pool, writer.resource, item.name, live)
                if item.name == pool.subscription_name:
                    await writer.put_subscription(
                        item.name,
                        display_name=f"{pool.display_name[:90]} (pool)",
                        product_name=pool.product_name,
                        state="suspended",
                    )
                else:
                    await writer.put_api_subscription(
                        item.name,
                        display_name=_live_display_name(live)
                        or _key_display_name(pool, applied.get(item.name, [])),
                        api_name=pool.api_name,
                        state="suspended",
                    )
                if journal is not None:
                    await journal(PublishedResourceKind.SUBSCRIPTION, item.name, False, "prepare")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                errors.append(f"Suspending subscription {item.name} failed: {error}")
        return errors

    def _recovery_journal(
        self,
        pool: ModelPool,
        writer: ApimWriter,
        run: PublishRun,
        results: list[PublishStepResult],
        resources: list[PublishedResource],
    ) -> RecoveryJournal:
        """Record each recovery write as a step of the run, and what it made as the pool's."""

        async def record(
            kind: PublishedResourceKind, name: str, created: bool, stage: str
        ) -> None:
            resource_id = next(
                (
                    item.resource_id
                    for item in pool.resources
                    if item.kind == kind and item.name == name
                ),
                None,
            ) or writer.resource_id(_segment(pool, kind, name))
            results.append(
                PublishStepResult(
                    kind=kind,
                    name=name,
                    action=PublishAction.CREATE if created else PublishAction.UPDATE,
                    status=PublishStepStatus.SUCCEEDED,
                    resource_id=resource_id,
                    created_by_mosaic=created,
                    stage=stage,
                )
            )
            resources[:] = _merge_resources(
                resources,
                [
                    PublishedResource(
                        kind=kind, name=name, resource_id=resource_id, created_by_mosaic=created
                    )
                ],
            )
            await self._progress(pool, run, results, resources)

        return record

    async def _governed_failure(
        self,
        pool: ModelPool,
        client: ApimClient,
        writer: ApimWriter,
        run: PublishRun,
        results: list[PublishStepResult],
        resources: list[PublishedResource],
        started: datetime,
        failure: str,
        shape: str,
    ) -> None:
        """Fail closed, then put back no more access than the last applied snapshot allowed.

        First the pool refuses every call and every key MOSAIC made for it is suspended. Once
        that holds, the last applied snapshot's grants come back, less any this plan turned off,
        and only the keys they allow are active again. When the pool can't be shown to refuse
        calls, its access is unknown and the run keeps the lock until an administrator recovers
        it.
        """

        target = run.pool_access_snapshot
        if target is None:
            raise ConflictError("A governed apply's run must keep the snapshot it applied")
        await self._assert_lock(pool, run)
        errors = [failure]
        journal = self._recovery_journal(pool, writer, run, results, resources)
        denied, problems = await self._establish_deny(
            pool.model_copy(update={"resources": list(resources)}), client, writer, journal
        )
        errors.extend(problems)
        errors.extend(
            await self._suspend_owned_subscriptions(
                pool.model_copy(update={"resources": list(resources)}), client, writer, journal
            )
        )
        safe = denied_pool_access_snapshot(target)
        if denied:
            candidate = safe_pool_access_snapshot(pool, target)
            actual = pool.model_copy(update={"resources": list(resources)})
            # The restored policy includes the pool's fragment. Without one MOSAIC made, nothing
            # was ever applied that the deny took away, so the pool stays denied.
            if self._owns(actual, PublishedResourceKind.API, pool.api_name) and self._owns(
                actual, PublishedResourceKind.POLICY_FRAGMENT, pool.fragment_name
            ):
                try:
                    restored = _policy(pool, shape, candidate)
                    await ensure_blocked_list(self._blocked_list, client, writer, pool.tenant_id)
                    await writer.put_policy_fragment(
                        pool.fragment_name,
                        restored.fragment_xml,
                        description="MOSAIC last safe access snapshot",
                    )
                    await journal(
                        PublishedResourceKind.POLICY_FRAGMENT, pool.fragment_name, False, "policy"
                    )
                    await writer.put_api_policy(pool.api_name, restored.api_policy_xml)
                    await journal(PublishedResourceKind.API_POLICY, "policy", False, "policy")
                    await writer.put_api(
                        pool.api_name,
                        display_name=pool.display_name,
                        path=pool.api_path,
                        subscription_required=not candidate.settings.entra_enabled,
                        description="Published by MOSAIC; restricted recovery state.",
                        use_default_subscription_key_names=True,
                    )
                    await journal(PublishedResourceKind.API, pool.api_name, False, "activate")
                    if candidate.settings.keys_enabled:
                        for name, grants in sorted(candidate.key_grants().items()):
                            if not any(grant.enabled and grant.keys_allowed for grant in grants):
                                continue
                            if not self._owns(actual, PublishedResourceKind.SUBSCRIPTION, name):
                                continue
                            live = await client.get_subscription(name)
                            if live is None:
                                continue
                            self._validate_subscription_scope(pool, writer.resource, name, live)
                            await writer.put_api_subscription(
                                name,
                                display_name=_live_display_name(live)
                                or _key_display_name(pool, grants),
                                api_name=pool.api_name,
                                state="active",
                            )
                            await journal(
                                PublishedResourceKind.SUBSCRIPTION, name, False, "activate"
                            )
                    safe = candidate
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    errors.append(f"Restoring the last safe access snapshot failed: {error}")
                    actual = pool.model_copy(update={"resources": list(resources)})
                    denied, problems = await self._establish_deny(actual, client, writer, journal)
                    errors.extend(problems)
                    errors.extend(
                        await self._suspend_owned_subscriptions(actual, client, writer, journal)
                    )
        if denied:
            errors.append(
                "Access was restricted to the last safe snapshot. Keys were retained for a "
                "reviewed retry; suspension failures are listed above. No keys were rotated."
            )
        else:
            errors.append(
                "Runtime denial could not be confirmed. The durable lock is retained; stop the "
                "original writer and complete explicit recovery."
            )
        await self._record_state(
            pool,
            status=PublicationStatus.FAILED,
            resources=resources,
            run_id=run.id,
            error=failure,
            access_snapshot=safe if denied else None,
            access_state="failed" if denied else "unknown",
        )
        if denied:
            await self._project_bindings(pool, safe, run)
        await self._finish_run(
            run,
            PublishRunStatus.FAILED if denied else PublishRunStatus.INTERRUPTED,
            started,
            results=results,
            errors=errors,
            orphaned=[] if denied else [item for item in resources if item.created_by_mosaic],
        )
        if denied:
            await self._release_run(pool, run)

    async def _project_bindings(
        self, pool: ModelPool, snapshot: PoolAccessSnapshot | None, run: PublishRun
    ) -> None:
        """Point each grant the gateway now enforces at the key and counters it's enforced with.

        Usage and analytics read these bindings to charge a call to its grant and cost center. A
        grant the gateway no longer enforces loses the binding an apply gave it; one bound some
        other way is left alone.
        """

        if self._entitlements is None:
            return
        grants = {grant.entitlement_id: grant for grant in snapshot.grants} if snapshot else {}
        model_ids = {model.id for model in pool.models}
        if pool.applied_access is not None:
            model_ids.update(grant.pool_model_id for grant in pool.applied_access.grants)
        model_ids.update(grant.pool_model_id for grant in grants.values())
        actor = Actor(run.actor_object_id or "system:publishing", pool.tenant_id)
        for model_id in sorted(model_ids):
            records = await self._entitlements.list_entitlements(
                pool.tenant_id, resource_id=model_id
            )
            for entitlement in records:
                if (
                    not is_pool_model_entitlement(entitlement)
                    or entitlement.resource.scope_id != pool.id
                ):
                    continue
                grant = grants.get(entitlement.id)
                binding = entitlement.binding
                if (
                    grant is not None
                    and grant.enabled
                    and snapshot is not None
                    and (snapshot.settings.keys_enabled or snapshot.settings.entra_enabled)
                ):
                    binding = EntitlementBinding(
                        gateway_id=pool.gateway_id,
                        apim_subscription_name=grant.key_name,
                        counter_key_expression=pool_grant_counter_key_expression(pool, grant),
                        attribution_key=pool_grant_counter_identity(pool, grant),
                        attribution_per_member=grant.is_group_grant,
                        source=BindingSource.ORCHESTRATED,
                        bound_at=utc_now(),
                    )
                elif binding is not None and binding.source == BindingSource.ORCHESTRATED:
                    binding = None
                else:
                    continue
                await self._entitlements.save_entitlement(
                    entitlement.model_copy(update={"binding": binding, "runtime": None}),
                    self._audit(
                        actor,
                        "entitlement.runtimeProjected",
                        entitlement.id,
                        resource_type="entitlement",
                    ),
                )

    async def _finish_success(
        self,
        pool: ModelPool,
        run: PublishRun,
        results: list[PublishStepResult],
        tracked: list[PublishedResource],
        desired: list[_Resource],
        started: datetime,
        cleanup: list[str],
        leftovers: list[PublishedResource],
        *,
        access_snapshot: PoolAccessSnapshot | None = None,
    ) -> None:
        await self._assert_lock(pool, run)
        applied_at = utc_now()
        wanted = {(item.kind, item.name) for item in desired}
        written = {
            (result.kind, result.name)
            for result in results
            if result.status == PublishStepStatus.SUCCEEDED
            and result.action != PublishAction.DELETE
        }
        # What the pool no longer wants stays recorded only while MOSAIC still has to remove it.
        # The gateway's blocked list isn't the pool's: every governed policy on it reads it.
        resources = [
            item.model_copy(update={"applied_at": applied_at})
            if (item.kind, item.name) in written
            else item
            for item in tracked
            if ((item.kind, item.name) in wanted or item.created_by_mosaic)
            and not is_blocked_list(item.kind, item.name)
        ]
        note = (
            "The pool is published, but MOSAIC couldn't remove some resources it no longer "
            "uses. The next apply tries again: " + "; ".join(cleanup)
            if cleanup
            else None
        )
        await self._record_state(
            pool,
            status=PublicationStatus.PUBLISHED,
            resources=resources,
            run_id=run.id,
            error=note,
            applied=True,
            access_snapshot=access_snapshot,
            access_state="applied" if access_snapshot is not None else None,
            applied_model_ids=[route.model_id for route in _routes(pool)],
        )
        if access_snapshot is not None:
            await self._project_bindings(pool, access_snapshot, run)
        await self._finish_run(
            run,
            PublishRunStatus.SUCCEEDED,
            started,
            results=results,
            errors=[note] if note else [],
            orphaned=leftovers,
        )

    async def _rollback(
        self,
        pool: ModelPool,
        writer: ApimWriter,
        run: PublishRun,
        results: list[PublishStepResult],
        tracked: list[PublishedResource],
        started: datetime,
        failure: str,
    ) -> None:
        """Undo, in reverse, what this run brought into existence.

        A step that replaced something already there isn't reverted, because MOSAIC never kept
        its previous content. When anything was replaced, the member backends and backend pools
        this run created stay as well, since what replaced it may already route to them. They
        remain the pool's, so the next apply or an unpublish deals with them.
        """

        errors = [failure]
        orphaned: list[PublishedResource] = []
        replaced = sorted(
            f"{_noun(result.kind)} {result.name}"
            for result in results
            if result.status == PublishStepStatus.SUCCEEDED
            and result.action != PublishAction.DELETE
            and not result.created_by_mosaic
        )
        kept: list[str] = []
        for result in reversed(results):
            if (
                result.status != PublishStepStatus.SUCCEEDED
                or result.action == PublishAction.DELETE
            ):
                continue
            if not result.created_by_mosaic:
                result.status = PublishStepStatus.SKIPPED
                continue
            if replaced and result.kind in _MEMBER_KINDS:
                kept.append(f"{_noun(result.kind)} {result.name}")
                continue
            try:
                await self._remove(writer, pool, result.kind, result.name)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                result.status = PublishStepStatus.ROLLBACK_FAILED
                result.error = str(error)
                errors.append(f"rollback of {_noun(result.kind)} {result.name}: {error}")
                orphaned.append(
                    PublishedResource(
                        kind=result.kind,
                        name=result.name,
                        resource_id=result.resource_id,
                        created_by_mosaic=True,
                    )
                )
            else:
                result.status = PublishStepStatus.ROLLED_BACK
        rolled_back = {
            (result.kind, result.name)
            for result in results
            if result.status == PublishStepStatus.ROLLED_BACK
        }
        resources = [item for item in tracked if (item.kind, item.name) not in rolled_back]
        if replaced:
            errors.append(
                "MOSAIC replaced these existing resources before the failure and can't restore "
                "their previous contents: " + ", ".join(replaced)
            )
        if kept:
            errors.append(
                "MOSAIC kept these new resources, because what it replaced may already use "
                "them: " + ", ".join(sorted(kept))
            )
        await self._record_state(
            pool,
            status=PublicationStatus.FAILED if orphaned else PublicationStatus.ROLLED_BACK,
            resources=resources,
            run_id=run.id,
            error=failure,
        )
        await self._finish_run(
            run,
            PublishRunStatus.ROLLBACK_FAILED if orphaned else PublishRunStatus.ROLLED_BACK,
            started,
            results=results,
            errors=errors,
            orphaned=orphaned,
        )

    async def _finish_run(
        self,
        run: PublishRun,
        status: PublishRunStatus,
        started: datetime,
        *,
        results: list[PublishStepResult],
        errors: list[str],
        orphaned: list[PublishedResource],
    ) -> None:
        completed = utc_now()
        await self._repository.save_publish_run(
            run.model_copy(
                update={
                    "status": status,
                    "completed_at": completed,
                    "duration_ms": int((completed - started).total_seconds() * 1000),
                    "steps": results,
                    "rolled_back": status
                    in {PublishRunStatus.ROLLED_BACK, PublishRunStatus.ROLLBACK_FAILED},
                    "orphaned_resources": orphaned,
                    "errors": errors,
                    "updated_at": completed,
                }
            )
        )

    async def plan_unpublish(self, actor: Actor, pool_id: str) -> PublishPlan:
        """List what unpublishing removes, in the order it removes it, without removing anything.

        It changes nothing on the pool. Unpublish runs only such a plan, and only while
        :func:`unpublish_digest` still matches it.
        """

        async with publication_lock(self._repository, actor.tenant_id, pool_id):
            pool = await self.get_pool(actor, pool_id)
            if pool.status == PublicationStatus.APPLYING:
                raise ConflictError(_APPLYING, details={"id": pool_id})
            if pool.access_state == "unknown":
                raise ConflictError(_UNKNOWN_ACCESS, details={"id": pool_id})
            gateway = await self._load_gateway(actor, pool.gateway_id)
            self._require_writable(gateway)
            removals = sorted(
                pool.created_resources(),
                key=lambda item: _ORDER.get(item.kind, len(_ORDER)),
                reverse=True,
            )
            if not removals:
                raise ConflictError(
                    "MOSAIC hasn't created anything in API Management for this pool, so there's "
                    "nothing to remove.",
                    details={"id": pool_id},
                )
            governed = _is_governed(pool)
            if governed:
                # The API goes first: until it's gone, its deny policy is what answers callers.
                removals.sort(key=lambda item: item.kind != PublishedResourceKind.API)
            keys = pool.applied_access.key_grants() if pool.applied_access else {}
            warnings = []
            if pool.has_applied_api():
                warnings.append(
                    f"Unpublishing removes the {pool.display_name} API at /{pool.api_path} and "
                    "its subscription key, so every app calling the pool stops working."
                )
            if governed:
                warnings.append(
                    "Before it deletes anything, MOSAIC replaces this API's policy with one that "
                    "refuses every call, and suspends every key, so callers are cut off first."
                )
            plan = PublishPlan(
                id=new_id("publishplan"),
                tenant_id=actor.tenant_id,
                publication_id=pool.id,
                target="pool",
                operation="unpublish",
                gateway_id=gateway.id,
                digest=unpublish_digest(pool),
                steps=[
                    PublishPlanStep(
                        kind=item.kind,
                        name=item.name,
                        action=PublishAction.DELETE,
                        reason=f"Remove the pool's {_noun(item.kind)}.",
                        resource_id=item.resource_id,
                        existed=True,
                        entitlement_id=(
                            min(grant.entitlement_id for grant in keys[item.name])
                            if item.kind == PublishedResourceKind.SUBSCRIPTION
                            and keys.get(item.name)
                            else None
                        ),
                    )
                    for item in removals
                ],
                warnings=warnings,
                actor_object_id=actor.object_id,
                pool_access_snapshot=pool.applied_access if governed else None,
            )
            await self._repository.save_publish_plan(plan)
            return plan

    async def unpublish(self, actor: Actor, pool_id: str, plan_id: str | None = None) -> PublishRun:
        """Run a reviewed unpublish plan: exactly its steps, and only if it still matches."""

        if not plan_id:
            raise ConflictError(
                "Review what unpublishing removes before you unpublish. Plan the unpublish, check "
                "what it lists, then unpublish that plan.",
                details={"id": pool_id, "reason": "planRequired"},
            )
        owner = new_id("publishrun")
        await self._repository.acquire_publication_lock(actor.tenant_id, pool_id, owner)
        self._active.add(pool_id)
        try:
            return await self._unpublish_locked(actor, pool_id, plan_id, owner)
        except BaseException:
            self._active.discard(pool_id)
            await self._repository.release_publication_lock(actor.tenant_id, pool_id, owner)
            raise

    async def _unpublish_locked(
        self, actor: Actor, pool_id: str, plan_id: str, owner: str
    ) -> PublishRun:
        pool = await self.get_pool(actor, pool_id)
        gateway = await self._load_gateway(actor, pool.gateway_id)
        self._require_writable(gateway)
        plan = await self._repository.get_publish_plan(actor.tenant_id, plan_id)
        if (
            plan is None
            or plan.target != "pool"
            or plan.operation != "unpublish"
            or plan.publication_id != pool.id
        ):
            raise NotFoundError("Unpublish plan was not found", details={"id": plan_id})
        if unpublish_digest(pool) != plan.digest:
            raise ConflictError(
                "This pool changed after its unpublish plan was made, so the plan no longer says "
                "what unpublishing removes. Review a new plan before you unpublish.",
                details={"planId": plan.id, "reason": "stalePlan"},
            )
        run = self._claim(actor, pool, plan, owner)
        await self._repository.save_publish_run(run)
        await self._mark_applying(pool, run.id)
        self._spawn(self._run_unpublish(pool, gateway, plan, run))
        return run

    async def _run_unpublish(
        self, pool: ModelPool, gateway: Gateway, plan: PublishPlan, run: PublishRun
    ) -> None:
        started = utc_now()
        writer = self._writer_factory(ApimResourceId.parse(gateway.azure_resource_id))
        results: list[PublishStepResult] = []
        remaining = list(pool.resources)
        errors: list[str] = []
        governed = _is_governed(pool)
        applied = pool.applied_access
        denied_snapshot = (
            denied_pool_access_snapshot(applied).model_copy(update={"version": applied.version + 1})
            if governed and applied is not None
            else None
        )
        try:
            if governed:
                # Callers are cut off before anything is deleted, so a failure part way through
                # leaves the pool refusing calls rather than half there.
                await self._assert_lock(pool, run)
                client = self._client_factory(writer.resource)
                denied, problems = await self._establish_deny(pool, client, writer)
                problems.extend(await self._suspend_owned_subscriptions(pool, client, writer))
                if not denied:
                    raise ConflictError(
                        "Unpublish could not establish a fail-closed runtime policy: "
                        + "; ".join(problems)
                    )
                errors.extend(problems)
            for step in plan.steps:
                await self._assert_lock(pool, run)
                result = PublishStepResult(
                    kind=step.kind,
                    name=step.name,
                    action=PublishAction.DELETE,
                    resource_id=step.resource_id,
                )
                try:
                    await self._progress(pool, run, [*results, result], remaining)
                    await self._remove(writer, pool, step.kind, step.name)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    result.status = PublishStepStatus.FAILED
                    result.error = str(error)
                    errors.append(f"{_noun(step.kind)} {step.name}: {error}")
                else:
                    result.status = PublishStepStatus.SUCCEEDED
                    remaining = [
                        item
                        for item in remaining
                        if not (item.kind == step.kind and item.name == step.name)
                    ]
                results.append(result)
                await self._progress(pool, run, results, remaining)
                if governed and result.status == PublishStepStatus.FAILED:
                    # What's left still refuses every call. Stop, rather than delete around it.
                    break
        except asyncio.CancelledError:
            await self._interrupted(pool, run, STALE_RUN_MESSAGE)
            self._active.discard(pool.id)
            raise
        except Exception as error:
            await self._interrupted(pool, run, f"Unpublish outcome is unknown: {error}")
            self._active.discard(pool.id)
            raise

        try:
            succeeded = not any(result.status == PublishStepStatus.FAILED for result in results)
            access_state: Literal["pending", "applied", "failed"] | None = None
            if governed:
                # After a clean unpublish, what's applied is the denial, or nothing ever was.
                access_state = (
                    "failed" if not succeeded else "applied" if denied_snapshot else "pending"
                )
            await self._record_state(
                pool,
                status=PublicationStatus.DRAFT if succeeded else PublicationStatus.FAILED,
                # Once everything MOSAIC made is gone, nothing else is the pool's to manage.
                resources=[] if succeeded else remaining,
                run_id=run.id,
                error=None if succeeded else "; ".join(errors) or None,
                unpublished=succeeded,
                access_snapshot=denied_snapshot,
                access_state=access_state,
            )
            if governed:
                # Every call is refused now, so no grant is bound to anything the gateway runs.
                await self._project_bindings(pool, None, run)
            await self._finish_run(
                run,
                PublishRunStatus.SUCCEEDED if succeeded else PublishRunStatus.FAILED,
                started,
                results=results,
                errors=errors,
                orphaned=[]
                if succeeded
                else [item for item in remaining if item.created_by_mosaic],
            )
            await self._release_run(pool, run)
        except asyncio.CancelledError:
            await self._interrupted(pool, run, STALE_RUN_MESSAGE)
            raise
        except Exception as error:
            await self._interrupted(pool, run, f"Unpublish result was not recorded: {error}")
            raise
        finally:
            self._active.discard(pool.id)

    async def list_runs(self, actor: Actor, pool_id: str) -> list[PublishRun]:
        await self.get_pool(actor, pool_id)
        return [
            run
            for run in await self._repository.list_publish_runs(actor.tenant_id, pool_id)
            if run.target == "pool"
        ]

    async def get_run(self, actor: Actor, pool_id: str, run_id: str) -> PublishRun:
        run = await self._repository.get_publish_run(actor.tenant_id, run_id)
        if run is None or run.target != "pool" or run.publication_id != pool_id:
            raise NotFoundError("Pool run was not found", details={"id": run_id})
        return run

    async def recover_interrupted(
        self, actor: Actor, pool_id: str, *, run_id: str, confirm_quiesced: bool
    ) -> PublishRun:
        """Release the lock an interrupted run kept, once an administrator says it has stopped.

        Without ``confirm_quiesced`` this only describes the run. With it, what the run may have
        created is recorded as the pool's, so the next apply or an unpublish deals with it.
        """

        owner = await self._repository.get_publication_lock(actor.tenant_id, pool_id)
        if owner != run_id:
            raise ConflictError("Recovery must name the exact run holding this pool's lock")
        pool = await self._repository.get_model_pool(actor.tenant_id, pool_id)
        run = await self._repository.get_publish_run(actor.tenant_id, run_id)
        if run is None and run_id.startswith("mutation_"):
            diagnostic = PublishRun(
                id=run_id,
                tenant_id=actor.tenant_id,
                publication_id=pool_id,
                target="pool",
                gateway_id=pool.gateway_id if pool else "",
                plan_id="",
                plan_digest="",
                actor_object_id=actor.object_id,
                status=PublishRunStatus.INTERRUPTED,
                errors=[
                    "Retained mutation lock; process liveness is unknown. Confirm the original "
                    "process is stopped before releasing this lock."
                ],
            )
            if not confirm_quiesced:
                return diagnostic
            if local_mutation_active(run_id):
                raise ConflictError("This process still has an active change to this pool")
            if pool:
                await self._repository.save_model_pool(
                    pool.model_copy(
                        update={
                            "last_plan_id": None,
                            "last_plan_digest": None,
                            "updated_at": utc_now(),
                        }
                    ),
                    self._audit(actor, "modelPool.mutationRecovered", pool_id),
                )
            diagnostic.completed_at = utc_now()
            await self._repository.save_publish_run(diagnostic)
            await self._repository.release_publication_lock(actor.tenant_id, pool_id, run_id)
            return diagnostic
        if run is None or pool is None or run.target != "pool":
            raise ConflictError("The retained lock has no matching pool or run")
        if not confirm_quiesced:
            return run
        if pool_id in self._active:
            raise ConflictError("This process still has an active run for this pool")
        gateway = await self._load_gateway(actor, pool.gateway_id)
        client = self._client_factory(ApimResourceId.parse(gateway.azure_resource_id))
        resources = list(pool.resources)
        for step in run.steps:
            if not step.created_by_mosaic or step.action == PublishAction.DELETE:
                continue
            if await self._exists(client, pool, step.kind, step.name):
                resources = _merge_resources(
                    resources,
                    [
                        PublishedResource(
                            kind=step.kind,
                            name=step.name,
                            resource_id=step.resource_id,
                            created_by_mosaic=True,
                        )
                    ],
                )
        governed = _is_governed(pool) or run.pool_access_snapshot is not None
        safe: PoolAccessSnapshot | None = None
        recovery_steps = list(run.steps)
        if governed:
            # Nobody knows what the stopped run left in force, so recovery shuts the pool before
            # it lets go: every call refused, every key MOSAIC made suspended.
            self._require_writable(gateway)
            writer = self._writer_factory(ApimResourceId.parse(gateway.azure_resource_id))
            await self._assert_lock(pool, run)
            actual = pool.model_copy(update={"resources": resources})
            journal = self._recovery_journal(actual, writer, run, recovery_steps, resources)
            denied, errors = await self._establish_deny(actual, client, writer, journal)
            errors.extend(await self._suspend_owned_subscriptions(actual, client, writer, journal))
            if not denied or errors:
                await self._interrupted(
                    actual,
                    run,
                    "Explicit recovery could not confirm complete denial: " + "; ".join(errors),
                )
                return await self.get_run(actor, pool_id, run.id)
            target = run.pool_access_snapshot or pool.applied_access
            if target is not None:
                safe = denied_pool_access_snapshot(target).model_copy(
                    update={
                        "version": max(
                            target.version,
                            pool.applied_access.version if pool.applied_access else 0,
                        )
                        + 1
                    }
                )
        message = (
            "An administrator confirmed this run stopped, and runtime access is denied. Plan the "
            "pool again and review the changes before restoring any grants."
            if governed
            else "An administrator confirmed this run stopped. Plan the pool again and review the "
            "changes before applying or unpublishing it."
        )
        await self._record_state(
            pool,
            status=PublicationStatus.FAILED,
            resources=resources,
            run_id=run.id,
            error=message,
            access_snapshot=safe,
            access_state="failed" if governed else None,
        )
        if governed:
            await self._project_bindings(pool, safe, run)
        recovered = run.model_copy(
            update={
                "status": PublishRunStatus.INTERRUPTED,
                "steps": recovery_steps,
                "errors": [*run.errors, message],
                "completed_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
        await self._repository.save_publish_run(recovered)
        current = await self.get_pool(actor, pool_id)
        await self._repository.save_model_pool(
            current.model_copy(update={"last_plan_id": None, "last_plan_digest": None}),
            self._audit(actor, "modelPool.recovered", pool_id, {"runId": run.id}),
        )
        await self._release_run(pool, run)
        return recovered

    async def reap_stale_publish_runs(self, tenant_id: str) -> int:
        """Close runs left unfinished without a lock. A run that still holds one is recovered."""

        stale = await self._repository.list_unfinished_publish_runs(tenant_id)
        active = set(self._active)
        reaped = 0
        for run in stale:
            if run.target != "pool" or run.publication_id in active:
                continue
            if await self._repository.get_publication_lock(tenant_id, run.publication_id):
                continue
            completed = utc_now()
            await self._repository.save_publish_run(
                run.model_copy(
                    update={
                        "status": PublishRunStatus.FAILED,
                        "completed_at": completed,
                        "errors": [*run.errors, STALE_RUN_MESSAGE],
                        "updated_at": completed,
                    }
                )
            )
            pool = await self._repository.get_model_pool(tenant_id, run.publication_id)
            if pool and pool.status == PublicationStatus.APPLYING:
                await self._repository.record_model_pool_state(
                    pool.model_copy(
                        update={
                            "status": PublicationStatus.FAILED,
                            "last_error": STALE_RUN_MESSAGE,
                            # A run that lost its lock stops at its next step, so it never got
                            # far without one; and with no lock left to recover through,
                            # "unknown" would strand the pool. The next apply denies first.
                            "access_state": (
                                "failed" if pool.access_state == "applying" else pool.access_state
                            ),
                            "updated_at": completed,
                        }
                    )
                )
            reaped += 1
        return reaped

    async def wait_for_idle(self) -> None:
        """Drain in-flight runs. Used by tests and shutdown, not by request handlers."""

        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        if self._task_failures:
            error = self._task_failures.pop(0)
            self._task_failures.clear()
            raise error

    async def _write(
        self, writer: ApimWriter, pool: ModelPool, policy: PublicationPolicy, item: _Resource
    ) -> None:
        payload = item.payload
        match item.kind:
            case PublishedResourceKind.BACKEND:
                await writer.put_backend(
                    item.name,
                    url=payload["url"],
                    title=payload["title"],
                    circuit_breaker=payload.get("circuitBreaker"),
                )
            case PublishedResourceKind.BACKEND_POOL:
                await writer.put_backend_pool(
                    item.name,
                    title=payload["title"],
                    services=[
                        (service["backend"], service["priority"], service["weight"])
                        for service in payload["services"]
                    ],
                )
            case PublishedResourceKind.POLICY_FRAGMENT:
                await writer.put_policy_fragment(
                    item.name, policy.fragment_xml, description=payload["description"]
                )
            case PublishedResourceKind.API:
                await writer.put_api(
                    item.name,
                    display_name=payload["displayName"],
                    path=payload["path"],
                    subscription_required=True,
                    description=payload["description"],
                )
            case PublishedResourceKind.API_OPERATION:
                operation = item.operation
                if operation is None:
                    raise ConflictError(
                        "MOSAIC lost track of this pool operation. Plan the pool again.",
                        details={"name": item.name},
                    )
                await writer.put_api_operation(
                    pool.api_name,
                    operation.name,
                    display_name=operation.display_name,
                    method=operation.method,
                    url_template=operation.url_template,
                    description=operation.description,
                    template_parameters=payload.get("templateParameters"),
                )
            case PublishedResourceKind.API_POLICY:
                await writer.put_api_policy(pool.api_name, policy.api_policy_xml)
            case PublishedResourceKind.PRODUCT:
                await writer.put_product(
                    item.name,
                    display_name=payload["displayName"],
                    description=payload["description"],
                    subscription_required=True,
                )
            case PublishedResourceKind.PRODUCT_API:
                await writer.put_product_api(pool.product_name, pool.api_name)
            case PublishedResourceKind.SUBSCRIPTION:
                await writer.put_subscription(
                    item.name,
                    display_name=payload["displayName"],
                    product_name=payload["product"],
                )
            case PublishedResourceKind.NAMED_VALUE:
                raise ConflictError(
                    "Pools don't write named values yet.", details={"name": item.name}
                )

    async def _remove(
        self, writer: ApimWriter, pool: ModelPool, kind: PublishedResourceKind, name: str
    ) -> None:
        match kind:
            case PublishedResourceKind.NAMED_VALUE:
                await writer.delete_named_value(name)
            case PublishedResourceKind.POLICY_FRAGMENT:
                await writer.delete_policy_fragment(name)
            case PublishedResourceKind.BACKEND | PublishedResourceKind.BACKEND_POOL:
                await writer.delete_backend(name)
            case PublishedResourceKind.API:
                await writer.delete_api(name)
            case PublishedResourceKind.API_OPERATION:
                await writer.delete_api_operation(pool.api_name, name)
            case PublishedResourceKind.API_POLICY:
                await writer.delete_api_policy(pool.api_name)
            case PublishedResourceKind.PRODUCT:
                await writer.delete_product(name)
            case PublishedResourceKind.PRODUCT_API:
                await writer.delete_product_api(pool.product_name, pool.api_name)
            case PublishedResourceKind.SUBSCRIPTION:
                await writer.delete_subscription(name)
