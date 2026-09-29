"""Shared, secret-free model access state and publication mutation guards."""

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mosaic_api.domain import (
    BindingSource,
    Entitlement,
    EntitlementRuntime,
    ModelAccessGrant,
    ModelAccessSettings,
    ModelAccessSnapshot,
    Principal,
    Publication,
    PublicationStatus,
    PublishedResourceKind,
    model_access_subscription_name,
    new_id,
)
from mosaic_api.repositories import GatewayRepository

_LOCAL_MUTATIONS: set[str] = set()


def local_mutation_active(owner_id: str) -> bool:
    return owner_id in _LOCAL_MUTATIONS


def gateway_mutation_scope(gateway_id: str) -> str:
    return f"gateway:{gateway_id}"


def entitlement_intent_digest(entitlement: Entitlement, principal: Principal | None) -> str:
    """Hash authorization inputs, not annotations, bindings, timestamps, or quota counters."""

    payload = {
        "tenantId": entitlement.tenant_id,
        "entitlementId": entitlement.id,
        "subject": entitlement.subject.model_dump(mode="json"),
        "resource": entitlement.resource.model_dump(mode="json"),
        "enabled": entitlement.enabled,
        "enforcement": (
            entitlement.enforcement.model_dump(mode="json") if entitlement.enforcement else None
        ),
        "principal": (
            {"id": principal.id, "objectId": principal.object_id, "kind": str(principal.kind)}
            if principal
            else None
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@asynccontextmanager
async def publication_lock(
    repository: GatewayRepository, tenant_id: str, publication_id: str
) -> AsyncIterator[str]:
    """Serialize mutations or credential reads; yield the owner for checks inside the lock."""

    owner = new_id("mutation")
    await repository.acquire_publication_lock(tenant_id, publication_id, owner)
    _LOCAL_MUTATIONS.add(owner)
    try:
        yield owner
    finally:
        try:
            await repository.release_publication_lock(tenant_id, publication_id, owner)
        finally:
            _LOCAL_MUTATIONS.discard(owner)


async def entitlement_publication(
    repository: GatewayRepository, entitlement: Entitlement
) -> Publication | None:
    if entitlement.resource.kind != "modelApi":
        return None
    model = await repository.get_model_api(entitlement.tenant_id, entitlement.resource.id)
    if model is None or model.publication_id is None:
        return None
    publication = await repository.get_publication(entitlement.tenant_id, model.publication_id)
    if publication is None or publication.model_api_id != model.id:
        return None
    if publication.gateway_id != model.gateway_id or publication.api_name != model.api_name:
        return None
    return publication


def applied_grant(publication: Publication, entitlement_id: str) -> ModelAccessGrant | None:
    if publication.applied_access is None:
        return None
    return next(
        (
            grant
            for grant in publication.applied_access.grants
            if grant.entitlement_id == entitlement_id
        ),
        None,
    )


def owns_grant_subscription(publication: Publication, entitlement_id: str) -> bool:
    expected = model_access_subscription_name(publication.tenant_id, publication.id, entitlement_id)
    return any(
        item.kind == PublishedResourceKind.SUBSCRIPTION
        and item.name == expected
        and item.created_by_mosaic
        for item in publication.resources
    )


def managed_grant_needs_retention(publication: Publication, entitlement_id: str) -> bool:
    grant = applied_grant(publication, entitlement_id)
    return (
        owns_grant_subscription(publication, entitlement_id)
        or (grant is not None and grant.enabled)
        or publication.status == PublicationStatus.APPLYING
        or publication.access_state in {"applying", "unknown"}
    )


def decorate_entitlement(
    entitlement: Entitlement,
    publication: Publication | None,
    principal: Principal | None,
    *,
    locked: bool = False,
) -> Entitlement:
    """Derive runtime state from trusted publication state; never trust a stored annotation."""

    if (
        publication is None
        or (publication.governed_access is None and publication.applied_access is None)
        or entitlement.subject.kind == "group"
        or entitlement.resource.kind != "modelApi"
        or publication.model_api_id != entitlement.resource.id
    ):
        return entitlement.model_copy(update={"runtime": None})

    snapshot = publication.applied_access
    grant = applied_grant(publication, entitlement.id)
    methods = snapshot.settings if snapshot else None
    status: str
    if publication.access_state == "unknown":
        status = "unknown"
    elif locked or publication.access_state == "applying":
        status = "applying"
    elif publication.access_state == "failed":
        status = "failed"
    elif snapshot is None:
        status = "pending"
    elif publication.status == PublicationStatus.DRAFT and not publication.created_resources():
        status = "revoked"
    elif not entitlement.enabled and grant is not None and grant.enabled:
        status = "revocationPending"
    elif (
        grant is None
        or principal is None
        or grant.intent_digest != entitlement_intent_digest(entitlement, principal)
        or snapshot.settings != publication.governed_access
        or snapshot.publication_enforcement != publication.enforcement
    ):
        status = "pending"
    elif not grant.enabled or not (
        snapshot.settings.keys_enabled or snapshot.settings.entra_enabled
    ):
        status = "revoked"
    else:
        status = "applied"
    runtime = EntitlementRuntime(
        publication_id=publication.id,
        status=status,
        applied_methods=methods,
        subscription_name=grant.subscription_name if grant else None,
        applied_at=publication.last_applied_at,
        error=publication.last_error,
    )
    binding = entitlement.binding
    if binding and binding.source == BindingSource.ORCHESTRATED and grant is None:
        binding = None
    return entitlement.model_copy(update={"runtime": runtime, "binding": binding})


def portal_runtime(runtime: EntitlementRuntime | None) -> EntitlementRuntime | None:
    """Runtime state as the end-user routes return it: everything except API Management's error.

    ``error`` is the publication's last apply error as API Management reported it. It names
    MOSAIC's internal resources, such as the policy fragment, and quotes APIM's validation text.
    It is also stored once per publication, so every grantee on the model would see the same text,
    and that text can describe other people's grants. ``status`` already tells the grantee that the
    apply failed; administrators read the error on their own routes.
    """

    if runtime is None or runtime.error is None:
        return runtime
    return runtime.model_copy(update={"error": None})


def portal_entitlement(entitlement: Entitlement) -> Entitlement:
    """An entitlement as the end-user routes return it; see :func:`portal_runtime`."""

    runtime = portal_runtime(entitlement.runtime)
    if runtime is entitlement.runtime:
        return entitlement
    return entitlement.model_copy(update={"runtime": runtime})


def safe_access_snapshot(
    publication: Publication, target: ModelAccessSnapshot
) -> ModelAccessSnapshot:
    """Intersect old and reviewed intent: recovery may restrict access, never introduce it."""

    previous = publication.applied_access
    if previous is None or publication.access_state == "unknown":
        return denied_access_snapshot(target)
    targets = {grant.entitlement_id: grant for grant in target.grants}
    limits_unchanged = previous.publication_enforcement == target.publication_enforcement
    grants = [
        grant.model_copy(
            update={
                "enabled": bool(
                    limits_unchanged
                    and grant.enabled
                    and (current := targets.get(grant.entitlement_id)) is not None
                    and current.enabled
                    and current.intent_digest == grant.intent_digest
                    and current.object_id == grant.object_id
                    and current.subject == grant.subject
                )
            }
        )
        for grant in previous.grants
    ]
    return ModelAccessSnapshot(
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
        publication_enforcement=previous.publication_enforcement,
        grants=grants,
    )


def denied_access_snapshot(snapshot: ModelAccessSnapshot) -> ModelAccessSnapshot:
    return snapshot.model_copy(
        update={
            "settings": ModelAccessSettings(keys_enabled=False, entra_enabled=False),
            "grants": [grant.model_copy(update={"enabled": False}) for grant in snapshot.grants],
        }
    )
