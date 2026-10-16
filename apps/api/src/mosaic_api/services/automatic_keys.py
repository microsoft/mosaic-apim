"""Durable initial Government key issuance, always under the owner's mutation lock."""

from typing import Any, Protocol

from mosaic_api.cost_centers import CostCenterBook
from mosaic_api.domain import (
    AuditEvent,
    Gateway,
    KeyState,
    ManagementMode,
    McpPublication,
    Publication,
    PublicationStatus,
    PublishedResource,
    PublishedResourceKind,
    model_access_subscription_name,
    new_id,
)
from mosaic_api.errors import ConflictError
from mosaic_api.integrations.apim import ApimClient
from mosaic_api.integrations.apim.key_runtime import (
    policy_hash,
    runtime_policy_hashes,
    verify_runtime,
)
from mosaic_api.integrations.apim.writer import ApimWriter
from mosaic_api.model_pools import ModelPool
from mosaic_api.repositories import DirectoryRepository, EntitlementRepository, GatewayRepository
from mosaic_api.services.model_access import (
    cost_center_intent,
    effective_enforcement,
    entitlement_intent_digest,
    grant_key_display_name,
)

KeyOwner = Publication | McpPublication | ModelPool


class InitialKeyManager(Protocol):
    async def get_subscription(self, name: str) -> dict[str, Any] | None: ...

    async def create(self, name: str, *, display_name: str, api_name: str) -> None: ...

    async def delete(self, name: str) -> None: ...

    async def verify_runtime(
        self,
        *,
        api_name: str,
        fragment_name: str,
        policy_hashes: dict[str, str],
        mcp: bool = False,
        metadata_name: str | None = None,
        legacy_metadata: bool = False,
    ) -> None: ...


class ApimInitialKeyManager:
    def __init__(self, client: ApimClient, writer: ApimWriter) -> None:
        self._client = client
        self._writer = writer

    async def get_subscription(self, name: str) -> dict[str, Any] | None:
        return await self._client.get_subscription(name)

    async def verify_runtime(
        self,
        *,
        api_name: str,
        fragment_name: str,
        policy_hashes: dict[str, str],
        mcp: bool = False,
        metadata_name: str | None = None,
        legacy_metadata: bool = False,
    ) -> None:
        await verify_runtime(
            self._client,
            api_name=api_name,
            fragment_name=fragment_name,
            policy_hashes=policy_hashes,
            mcp=mcp,
            metadata_name=metadata_name,
            legacy_metadata=legacy_metadata,
        )

    async def create(self, name: str, *, display_name: str, api_name: str) -> None:
        await self._writer.put_api_subscription(
            name, display_name=display_name, api_name=api_name, state="active"
        )

    async def delete(self, name: str) -> None:
        await self._writer.delete_subscription(name)


async def record_key_owner(repository: GatewayRepository, owner: KeyOwner) -> None:
    if isinstance(owner, ModelPool):
        await repository.record_model_pool_state(owner)
    elif isinstance(owner, McpPublication):
        await repository.record_mcp_publication_state(owner)
    else:
        await repository.record_publication_state(owner)


async def verify_key_runtime(
    client: ApimClient,
    owner: KeyOwner,
    *,
    repository: GatewayRepository,
    fragment_xml: str,
    api_policy_xml: str,
    metadata_policy_xml: str | None = None,
) -> None:
    """Confirm the applied API and policy, not merely that an ARM PUT was accepted."""

    hashes = await runtime_policy_hashes(
        client,
        api_name=owner.api_name,
        fragment_name=owner.fragment_name,
        **_runtime_kwargs(owner),
    )
    expected = {"fragment": policy_hash(fragment_xml), "api": policy_hash(api_policy_xml)}
    if metadata_policy_xml is not None:
        expected["metadata"] = policy_hash(metadata_policy_xml)
    if hashes != expected:
        raise ConflictError("APIM did not confirm the reviewed Government access policy.")
    if isinstance(owner, ModelPool):
        current: KeyOwner | None = await repository.get_model_pool(owner.tenant_id, owner.id)
    elif isinstance(owner, McpPublication):
        current = await repository.get_mcp_publication(owner.tenant_id, owner.id)
    else:
        current = await repository.get_publication(owner.tenant_id, owner.id)
    if current is None:
        raise ConflictError("The Government key owner disappeared while applying.")
    await record_key_owner(repository, current.model_copy(update={"key_policy_hashes": hashes}))


def _runtime_kwargs(owner: KeyOwner) -> dict[str, Any]:
    if not isinstance(owner, McpPublication):
        return {}
    return {
        "mcp": True,
        "metadata_name": owner.metadata_api_name,
        "legacy_metadata": any(
            item.name == owner.metadata_api_name
            and item.kind in {PublishedResourceKind.API, PublishedResourceKind.API_POLICY}
            for item in owner.resources
        ),
    }


async def validate_owner_runtime(manager: InitialKeyManager, owner: KeyOwner) -> None:
    await manager.verify_runtime(
        api_name=owner.api_name,
        fragment_name=owner.fragment_name,
        policy_hashes=owner.key_policy_hashes,
        **_runtime_kwargs(owner),
    )


def subscription_resource(gateway: Gateway, name: str) -> str:
    return f"{gateway.arm_resource().canonical}/subscriptions/{name}"


def owns_key(owner: KeyOwner, gateway: Gateway, name: str) -> bool:
    expected = subscription_resource(gateway, name).casefold()
    return any(
        item.kind == PublishedResourceKind.SUBSCRIPTION
        and item.name == name
        and item.resource_id.casefold() == expected
        and item.created_by_mosaic
        for item in owner.resources
    )


def require_key_scope(gateway: Gateway, api_name: str, live: dict[str, Any]) -> None:
    properties = live.get("properties") or {}
    scope = str(properties.get("scope", "")).rstrip("/").casefold()
    expected = f"/apis/{api_name}".casefold()
    if scope not in {expected, f"{gateway.arm_resource().canonical}{expected}".casefold()}:
        raise ConflictError("The subscription no longer belongs to this exact API.")


async def reconcile_initial_keys(
    owner: KeyOwner,
    gateway: Gateway,
    *,
    repository: GatewayRepository,
    directory: DirectoryRepository,
    entitlements: EntitlementRepository,
    book: CostCenterBook,
    manager: InitialKeyManager,
    actor_object_id: str = "system:automatic-keys",
) -> KeyOwner:
    """Retry persisted issuance without reapplying policy, revealing keys, or rotating them.

    The caller holds the owner's publication lock. Ownership is journaled before the ARM write,
    so a process death after APIM creation can resume by reading the same subscription.
    """

    snapshot = owner.applied_access
    if (
        gateway.azure_cloud != "government"
        or gateway.management_mode != ManagementMode.MANAGE
        or owner.status != PublicationStatus.PUBLISHED
        or owner.access_state != "applied"
        or snapshot is None
        or snapshot.azure_cloud != "government"
        or not snapshot.settings.keys_enabled
        or snapshot.settings.entra_enabled
        or not owner.has_applied_api()
    ):
        return owner
    if isinstance(owner, (Publication, ModelPool)) and owner.governed_access != snapshot.settings:
        return owner
    for deleted_name, deletion_state in owner.key_lifecycle.items():
        if deletion_state != "deleted" or not owns_key(owner, gateway, deleted_name):
            continue
        try:
            live = await manager.get_subscription(deleted_name)
            if live is not None:
                require_key_scope(gateway, owner.api_name, live)
                await manager.delete(deleted_name)
            owner = owner.model_copy(
                update={
                    "resources": [
                        item
                        for item in owner.resources
                        if not (
                            item.kind == PublishedResourceKind.SUBSCRIPTION
                            and item.name == deleted_name
                        )
                    ]
                }
            )
            await record_key_owner(repository, owner)
        except Exception:
            # Persisted deletion intent wins over initial issuance even if cleanup must retry.
            continue
    handled: set[str] = set()
    runtime_valid: bool | None = None
    for grant in snapshot.grants:
        if grant.is_group_grant or not grant.enabled or not grant.keys_allowed or grant.revoked:
            continue
        name = grant.key_name if hasattr(grant, "key_name") else grant.subscription_name
        if not name or name in handled or owner.key_lifecycle.get(name) == "deleted":
            continue
        handled.add(name)
        entitlement = await entitlements.get_entitlement(owner.tenant_id, grant.entitlement_id)
        principal = await directory.get_principal(owner.tenant_id, grant.subject.id)
        if entitlement is None or principal is None:
            continue
        intent = cost_center_intent(entitlement, principal, book, key_capable=True)
        charged = book.get(entitlement.cost_center_id)
        expected_name = (
            name
            if isinstance(owner, ModelPool)
            else model_access_subscription_name(owner.tenant_id, owner.id, entitlement.id)
        )
        if (
            not entitlement.enabled
            or grant.subject != entitlement.subject
            or grant.object_id.casefold() != principal.object_id.casefold()
            or intent is None
            or not intent.keys_allowed
            or (charged is not None and charged.recheck_pending(entitlement.id, principal.id))
            or grant.intent_digest != entitlement_intent_digest(entitlement, principal, intent)
            or grant.enforcement != effective_enforcement(entitlement, intent)
            or name != expected_name
        ):
            continue
        previous = owner.key_lifecycle.get(name)
        try:
            if runtime_valid is None:
                runtime_valid = False
                await validate_owner_runtime(manager, owner)
                runtime_valid = True
            if not runtime_valid:
                raise ConflictError("The reviewed Government runtime could not be confirmed.")
            live = await manager.get_subscription(name)
            if live is not None and not owns_key(owner, gateway, name):
                raise ConflictError("An unowned subscription already uses this grant's name.")
            if live is not None:
                require_key_scope(gateway, owner.api_name, live)
            elif previous == "ready":
                owner = owner.model_copy(
                    update={
                        "key_lifecycle": {**owner.key_lifecycle, name: "deleted"},
                    }
                )
                await record_key_owner(repository, owner)
                continue
            if live is None:
                lifecycle = {**owner.key_lifecycle, name: "preparing"}
                resources = list(owner.resources)
                if not owns_key(owner, gateway, name):
                    resources.append(
                        PublishedResource(
                            kind=PublishedResourceKind.SUBSCRIPTION,
                            name=name,
                            resource_id=subscription_resource(gateway, name),
                            created_by_mosaic=True,
                        )
                    )
                owner = owner.model_copy(
                    update={
                        "key_lifecycle": lifecycle,
                        "resources": resources,
                    }
                )
                await record_key_owner(repository, owner)
                await entitlements.record_audit(
                    AuditEvent(
                        id=new_id("audit"),
                        tenant_id=owner.tenant_id,
                        action="credential.automatic.requested",
                        resource_type="entitlement",
                        resource_id=grant.entitlement_id,
                        actor_object_id=actor_object_id,
                        details={"subscriptionName": name, "ownerId": owner.id},
                    )
                )
                await manager.create(
                    name,
                    api_name=owner.api_name,
                    display_name=grant_key_display_name(grant.display_name, intent.code),
                )
                live = await manager.get_subscription(name)
            if live is None:
                raise ConflictError("APIM has not confirmed the subscription yet.")
            require_key_scope(gateway, owner.api_name, live)
            if (live.get("properties") or {}).get("state") != "active":
                raise ConflictError("APIM has not activated the subscription.")
            state: KeyState = "ready"
        except Exception:
            # ARM errors may contain internal resource names; the portal gets only fixed guidance.
            state = "failed"
        if state == owner.key_lifecycle.get(name):
            continue
        owner = owner.model_copy(
            update={
                "key_lifecycle": {**owner.key_lifecycle, name: state},
            }
        )
        await record_key_owner(repository, owner)
        if state != previous:
            await entitlements.record_audit(
                AuditEvent(
                    id=new_id("audit"),
                    tenant_id=owner.tenant_id,
                    action=f"credential.automatic.{state}",
                    resource_type="entitlement",
                    resource_id=grant.entitlement_id,
                    actor_object_id=actor_object_id,
                    details={"subscriptionName": name, "ownerId": owner.id},
                )
            )
    return owner
