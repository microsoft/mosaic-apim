"""Governed access to model pools: grants on pool models, and the keys they share (ADR 0024).

A grant names one pool model, with its pool as the resource's scope. Runtime state is derived from
the pool's applied snapshot, never from a stored annotation, exactly as a publication's grants are.
"""

from mosaic_api.domain import (
    BindingSource,
    Entitlement,
    EntitlementResourceKind,
    EntitlementRuntime,
    EntitlementSubjectKind,
    ModelAccessSettings,
    Principal,
    PublicationStatus,
)
from mosaic_api.model_pools import (
    ModelPool,
    ModelPoolVisibility,
    PoolAccessGrant,
    PoolAccessSnapshot,
    pool_key_name,
)
from mosaic_api.repositories import GatewayRepository
from mosaic_api.services.model_access import CostCenterIntent, entitlement_intent_digest


def is_pool_model_entitlement(entitlement: Entitlement) -> bool:
    return entitlement.resource.kind == EntitlementResourceKind.POOL_MODEL


async def entitlement_model_pool(
    repository: GatewayRepository, entitlement: Entitlement
) -> ModelPool | None:
    """The pool a pool model grant's scope names, whether or not it still has the model.

    A removed model's grant still needs its pool: the gateway may serve the model until the pool
    is applied again.
    """

    if not is_pool_model_entitlement(entitlement) or not entitlement.resource.scope_id:
        return None
    return await repository.get_model_pool(entitlement.tenant_id, entitlement.resource.scope_id)


def pool_model_listed(pool: ModelPool | None, pool_model_id: str) -> bool:
    """Whether an administrator made the pool model discoverable in the portal's catalog."""

    if pool is None or pool.visibility != ModelPoolVisibility.LISTED:
        return False
    model = pool.pool_model(pool_model_id)
    return model is not None and model.listed


def pool_model_offered(pool: ModelPool | None, pool_model_id: str) -> bool:
    """Whether end users may be offered the pool model, to request or to connect to.

    Only a governed pool's models are, and only while its gateway serves them. Before an
    administrator opts a pool into governed access, it's reachable only through its bootstrap
    subscription, as ADR 0024 says.
    """

    return (
        pool is not None
        and pool.governed_access is not None
        and pool.serves_model(pool_model_id)
    )


def applied_pool_grant(pool: ModelPool, entitlement_id: str) -> PoolAccessGrant | None:
    if pool.applied_access is None:
        return None
    return next(
        (
            grant
            for grant in pool.applied_access.grants
            if grant.entitlement_id == entitlement_id
        ),
        None,
    )


def entitlement_key_name(pool: ModelPool, entitlement: Entitlement) -> str | None:
    """The key a direct grant shares with the subject's other grants under its cost center."""

    if entitlement.subject.kind not in {
        EntitlementSubjectKind.USER,
        EntitlementSubjectKind.APPLICATION,
    }:
        return None
    return pool_key_name(
        pool.tenant_id, pool.id, entitlement.subject.id, entitlement.cost_center_id
    )


def pool_grant_needs_retention(pool: ModelPool, entitlement_id: str) -> bool:
    """Whether a grant must stay until an apply has taken its access away.

    A key is shared, so deleting a disabled grant never orphans it: the key stays the pool's,
    suspended unless another of its grants is enabled, until every grant it serves is revoked or
    the pool is unpublished.
    """

    grant = applied_pool_grant(pool, entitlement_id)
    return (
        (grant is not None and grant.enabled)
        or pool.status == PublicationStatus.APPLYING
        or pool.access_state in {"applying", "unknown"}
    )


def decorate_pool_entitlement(
    entitlement: Entitlement,
    pool: ModelPool | None,
    principal: Principal | None,
    *,
    locked: bool = False,
    cost_center: CostCenterIntent | None = None,
) -> Entitlement:
    """Derive a pool model grant's runtime state from its pool's trusted state."""

    if (
        pool is None
        or (pool.governed_access is None and pool.applied_access is None)
        or entitlement.subject.kind == EntitlementSubjectKind.GROUP
        or not is_pool_model_entitlement(entitlement)
        or entitlement.resource.scope_id != pool.id
    ):
        return entitlement.model_copy(update={"runtime": None})

    snapshot = pool.applied_access
    grant = applied_pool_grant(pool, entitlement.id)
    methods = snapshot.settings if snapshot else None
    # A pool model with applied grants can't be removed, so a grant whose model left the pool is
    # one an apply already revoked, or one that was never applied. No apply will grant it now.
    model_removed = pool.pool_model(entitlement.resource.id) is None
    status: str
    if pool.access_state == "unknown":
        status = "unknown"
    elif locked or pool.access_state == "applying":
        status = "applying"
    elif pool.access_state == "failed":
        status = "failed"
    elif snapshot is None:
        status = "pending"
    elif pool.status == PublicationStatus.DRAFT and not pool.created_resources():
        status = "revoked"
    elif grant is not None and grant.enabled and (not entitlement.enabled or model_removed):
        status = "revocationPending"
    elif model_removed:
        status = "revoked"
    elif (
        grant is None
        or principal is None
        or grant.intent_digest != entitlement_intent_digest(entitlement, principal, cost_center)
        or snapshot.settings != pool.governed_access
    ):
        status = "pending"
    elif not grant.enabled or not (
        snapshot.settings.keys_enabled or snapshot.settings.entra_enabled
    ):
        status = "revoked"
    else:
        status = "applied"
    key_name = entitlement_key_name(pool, entitlement)
    runtime = EntitlementRuntime(
        publication_id=pool.id,
        status=status,
        applied_methods=methods,
        subscription_name=grant.key_name if grant else None,
        key_exists=key_name is not None and pool.owns_key(key_name),
        applied_at=pool.last_applied_at,
        error=pool.last_error,
    )
    binding = entitlement.binding
    if binding and binding.source == BindingSource.ORCHESTRATED and grant is None:
        binding = None
    return entitlement.model_copy(update={"runtime": runtime, "binding": binding})


def safe_pool_access_snapshot(pool: ModelPool, target: PoolAccessSnapshot) -> PoolAccessSnapshot:
    """Intersect old and reviewed intent: recovery may restrict access, never introduce it."""

    previous = pool.applied_access
    if previous is None or pool.access_state == "unknown":
        return denied_pool_access_snapshot(target)
    targets = {grant.entitlement_id: grant for grant in target.grants}
    metering_unchanged = previous.token_metering == target.token_metering
    grants = [
        grant.model_copy(
            update={
                "enabled": bool(
                    metering_unchanged
                    and grant.enabled
                    and (current := targets.get(grant.entitlement_id)) is not None
                    and current.enabled
                    and current.intent_digest == grant.intent_digest
                    and current.object_id == grant.object_id
                    and current.subject == grant.subject
                    and current.pool_model_id == grant.pool_model_id
                    and current.key_name == grant.key_name
                )
            }
        )
        for grant in previous.grants
    ]
    return PoolAccessSnapshot(
        version=target.version,
        settings=ModelAccessSettings(
            keys_enabled=previous.settings.keys_enabled and target.settings.keys_enabled,
            entra_enabled=(
                previous.settings.entra_enabled
                and target.settings.entra_enabled
                and previous.audience == target.audience
            ),
        ),
        audience=previous.audience,
        token_metering=previous.token_metering,
        grants=grants,
        # A grant whose cost center's quota changed has a changed intent, so it's off above.
        quotas=previous.quotas,
    )


def denied_pool_access_snapshot(snapshot: PoolAccessSnapshot) -> PoolAccessSnapshot:
    return snapshot.model_copy(
        update={
            "settings": ModelAccessSettings(keys_enabled=False, entra_enabled=False),
            "grants": [grant.model_copy(update={"enabled": False}) for grant in snapshot.grants],
        }
    )


__all__ = [
    "applied_pool_grant",
    "decorate_pool_entitlement",
    "denied_pool_access_snapshot",
    "entitlement_key_name",
    "entitlement_model_pool",
    "is_pool_model_entitlement",
    "pool_grant_needs_retention",
    "pool_model_listed",
    "pool_model_offered",
    "safe_pool_access_snapshot",
]
