from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

import structlog
from pydantic import SecretStr

from mosaic_api.domain import (
    ApimResourceId,
    AuditEvent,
    ConnectionOperation,
    Entitlement,
    Gateway,
    KeyRevealResult,
    ModelAccessGrant,
    ModelConnection,
    Principal,
    Publication,
    PublicationStatus,
    PublishedResourceKind,
    model_access_subscription_name,
    new_id,
)
from mosaic_api.errors import ConflictError, DomainError, NotFoundError
from mosaic_api.integrations.access_policy import governed_operations
from mosaic_api.integrations.apim.model_apis import operations_for
from mosaic_api.repositories import (
    DirectoryRepository,
    EntitlementRepository,
    GatewayRepository,
)
from mosaic_api.services.directory import Actor
from mosaic_api.services.entitlements import EntitlementService
from mosaic_api.services.model_access import entitlement_intent_digest

logger = structlog.get_logger()


class CredentialReader(Protocol):
    async def read_key(
        self, subscription_name: str, api_name: str, slot: Literal["primary", "secondary"]
    ) -> SecretStr: ...


@dataclass(frozen=True)
class _AccessContext:
    entitlement: Entitlement
    principal: Principal
    publication: Publication
    gateway: Gateway


class PortalAccessService:
    def __init__(
        self,
        entitlements: EntitlementService,
        *,
        repository: EntitlementRepository,
        directory_repository: DirectoryRepository,
        gateway_repository: GatewayRepository,
        credential_factory: Callable[[ApimResourceId], CredentialReader],
        model_runtime_client_id: str | None = None,
    ) -> None:
        self._entitlements = entitlements
        self._repository = repository
        self._directory = directory_repository
        self._gateways = gateway_repository
        self._credential_factory = credential_factory
        self._runtime_client_id = model_runtime_client_id

    async def list_for_caller(self, actor: Actor) -> list[Entitlement]:
        principal = await self._directory.find_principal_by_object_id(
            actor.tenant_id, actor.object_id
        )
        if principal is None:
            matches = [
                candidate
                for candidate in await self._directory.list_principals(actor.tenant_id)
                if candidate.object_id.casefold() == actor.object_id.casefold()
            ]
            if len(matches) > 1:
                raise ConflictError(
                    "Multiple principals map to this identity; resolve the duplicates"
                )
            if not matches:
                return []
            principal = matches[0]
        expected_kind = "user" if principal.kind == "user" else "application"
        return [
            entitlement
            for entitlement in await self._entitlements.list_entitlements(
                actor, subject_id=principal.id
            )
            if entitlement.subject.kind == expected_kind
        ]

    async def _context(
        self, actor: Actor, entitlement_id: str, *, administrator: bool
    ) -> _AccessContext:
        entitlement = await self._entitlements.get_entitlement(actor, entitlement_id)
        principal = await self._directory.get_principal(
            actor.tenant_id, entitlement.subject.id
        )
        if (
            principal is None
            or entitlement.subject.kind == "group"
            or (
                not administrator
                and principal.object_id.casefold() != actor.object_id.casefold()
            )
        ):
            raise NotFoundError("Entitlement was not found")
        expected_kind = "user" if principal.kind == "user" else "application"
        if entitlement.subject.kind != expected_kind:
            raise ConflictError("The grant no longer matches its principal's identity kind")
        if entitlement.resource.kind != "modelApi":
            raise ConflictError("Connection details are available for published model grants only")
        model_api = await self._gateways.get_model_api(
            actor.tenant_id, entitlement.resource.id
        )
        if model_api is None or model_api.publication_id is None:
            raise ConflictError("This model API is not linked to a MOSAIC publication")
        publication = await self._gateways.get_publication(
            actor.tenant_id, model_api.publication_id
        )
        if (
            publication is None
            or publication.model_api_id != model_api.id
            or publication.gateway_id != model_api.gateway_id
            or publication.api_name != model_api.api_name
        ):
            raise ConflictError("The publication binding changed; re-plan model access")
        gateway = await self._gateways.get_gateway(actor.tenant_id, publication.gateway_id)
        if gateway is None:
            raise ConflictError("The publication's gateway is no longer registered")
        return _AccessContext(entitlement, principal, publication, gateway)

    async def connection(
        self, actor: Actor, entitlement_id: str, *, administrator: bool = False
    ) -> ModelConnection:
        context = await self._context(actor, entitlement_id, administrator=administrator)
        publication = context.publication
        url = context.gateway.capabilities.gateway_url
        if url is None:
            raise ConflictError("Synchronize the gateway to discover its model API endpoint")
        snapshot = publication.applied_access
        audience = snapshot.audience if snapshot else self._runtime_client_id
        scope_suffix = (
            "Models.Invoke" if context.entitlement.subject.kind == "user" else ".default"
        )
        operations = (
            governed_operations(publication)
            if publication.governed_access is not None or snapshot is not None
            else operations_for(publication)
        )
        return ModelConnection(
            entitlement_id=entitlement_id,
            publication_id=publication.id,
            gateway_id=publication.gateway_id,
            endpoint=f"{str(url).rstrip('/')}/{publication.api_path.strip('/')}",
            deployment_name=publication.deployment_name,
            tenant_id=actor.tenant_id,
            runtime=context.entitlement.runtime,
            applied_methods=snapshot.settings if snapshot else None,
            entra_audience=audience,
            entra_scope=f"api://{audience}/{scope_suffix}" if audience else None,
            api_shape=publication.api_shape,
            operations=[
                ConnectionOperation(
                    name=operation.name, method=operation.method, path=operation.url_template
                )
                for operation in operations
            ],
            publication_limits=(
                snapshot.publication_enforcement if snapshot else publication.enforcement
            ),
            grant_limits=context.entitlement.enforcement,
        )

    @staticmethod
    def _applied_grant(context: _AccessContext) -> ModelAccessGrant:
        entitlement = context.entitlement
        publication = context.publication
        snapshot = publication.applied_access
        if (
            not entitlement.enabled
            or publication.status != PublicationStatus.PUBLISHED
            or publication.access_state != "applied"
            or publication.governed_access is None
            or not publication.governed_access.keys_enabled
            or snapshot is None
            or not snapshot.settings.keys_enabled
            or snapshot.settings != publication.governed_access
            or snapshot.publication_enforcement != publication.enforcement
        ):
            raise ConflictError(
                "Key access requires an enabled grant successfully applied to API Management"
            )
        matches = [
            grant for grant in snapshot.grants if grant.entitlement_id == entitlement.id
        ]
        if len(matches) != 1:
            raise ConflictError("This grant has not been applied to API Management")
        grant = matches[0]
        expected_name = model_access_subscription_name(
            publication.tenant_id, publication.id, entitlement.id
        )
        if (
            not grant.enabled
            or grant.subject != entitlement.subject
            or grant.object_id.casefold() != context.principal.object_id.casefold()
            or grant.enforcement != entitlement.enforcement
            or grant.intent_digest != entitlement_intent_digest(entitlement, context.principal)
            or grant.subscription_name != expected_name
        ):
            raise ConflictError("Apply the pending changes to this grant before revealing its key")
        resource = ApimResourceId.parse(context.gateway.azure_resource_id)
        expected_id = f"{resource.canonical}/subscriptions/{expected_name}"
        if not any(
            item.kind == PublishedResourceKind.SUBSCRIPTION
            and item.name == expected_name
            and item.resource_id.casefold() == expected_id.casefold()
            and item.created_by_mosaic
            for item in publication.resources
        ):
            raise ConflictError("MOSAIC has no trusted ownership record for this subscription")
        return grant

    async def _key_context(
        self, actor: Actor, entitlement_id: str, *, administrator: bool
    ) -> tuple[_AccessContext, ModelAccessGrant]:
        context = await self._context(actor, entitlement_id, administrator=administrator)
        if await self._gateways.get_publication_lock(
            actor.tenant_id, context.publication.id
        ) is not None:
            raise ConflictError(
                "A publication operation is in progress or needs recovery; key retrieval is blocked"
            )
        return context, self._applied_grant(context)

    async def _audit(
        self,
        actor: Actor,
        entitlement_id: str,
        slot: Literal["primary", "secondary"],
        outcome: str,
        *,
        subscription_name: str | None = None,
        error_code: str | None = None,
    ) -> None:
        await self._repository.record_audit(
            AuditEvent(
                id=new_id("audit"),
                tenant_id=actor.tenant_id,
                action=f"credential.reveal.{outcome}",
                resource_type="entitlement",
                resource_id=entitlement_id,
                actor_object_id=actor.object_id,
                details={
                    "slot": slot,
                    "subscriptionName": subscription_name,
                    "errorCode": error_code,
                },
            )
        )

    async def reveal_key(
        self,
        actor: Actor,
        entitlement_id: str,
        slot: Literal["primary", "secondary"],
        *,
        administrator: bool = False,
    ) -> KeyRevealResult:
        await self._audit(actor, entitlement_id, slot, "requested")
        subscription_name: str | None = None
        try:
            context, grant = await self._key_context(
                actor, entitlement_id, administrator=administrator
            )
            subscription_name = grant.subscription_name
            reader = self._credential_factory(
                ApimResourceId.parse(context.gateway.azure_resource_id)
            )
            key = await reader.read_key(subscription_name, context.publication.api_name, slot)

            # A revoke or identity/configuration change can arrive during the ARM round trip.
            latest, latest_grant = await self._key_context(
                actor, entitlement_id, administrator=administrator
            )
            if (
                latest.publication.applied_access != context.publication.applied_access
                or latest_grant != grant
            ):
                raise ConflictError("Model access changed during key retrieval; reload and retry")
            await self._audit(
                actor, entitlement_id, slot, "succeeded", subscription_name=subscription_name
            )
        except DomainError as error:
            await self._audit(
                actor,
                entitlement_id,
                slot,
                "denied" if error.status_code < 500 else "failed",
                subscription_name=subscription_name,
                error_code=error.code,
            )
            logger.warning(
                "credential_reveal_failed",
                tenant_id=actor.tenant_id,
                entitlement_id=entitlement_id,
                error_code=error.code,
            )
            raise
        return KeyRevealResult(
            entitlement_id=entitlement_id,
            subscription_name=subscription_name,
            slot=slot,
            key=key.get_secret_value(),
        )
