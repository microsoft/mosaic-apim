"""Entitlement and access-request orchestration.

Cosmos is the source of truth for who may use what and under which limits. This service never
reads API Management to answer that question; it reads observed state only to *infer* the APIM
product or subscription that realizes a grant, because gateway telemetry is keyed on the
subscription and a grant with no binding cannot be joined to a usage row.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import structlog
from pydantic import ValidationError as SchemaValidationError

from mosaic_api.domain import (
    AccessRequest,
    AccessRequestApproval,
    AccessRequestCreate,
    AccessRequestState,
    AuditEvent,
    BindingSource,
    Entitlement,
    EntitlementBinding,
    EntitlementCreate,
    EntitlementResource,
    EntitlementSubject,
    EntitlementSubjectKind,
    EntitlementUpdate,
    GrantPath,
    Principal,
    PrincipalCreate,
    PrincipalKind,
    ResolvedEntitlement,
    deterministic_id,
    entitlement_id,
    new_id,
    utc_now,
)
from mosaic_api.errors import ConflictError, NotFoundError, ValidationError
from mosaic_api.observed import (
    ObservedApimUser,
    ObservedModelDeployment,
    ObservedProduct,
    ObservedSubscription,
)
from mosaic_api.repositories import (
    DirectoryRepository,
    EntitlementRepository,
    GatewayRepository,
    ModelEndpointRepository,
)
from mosaic_api.services.directory import Actor, DirectoryService
from mosaic_api.services.model_access import (
    decorate_entitlement,
    entitlement_publication,
    managed_grant_needs_retention,
    publication_lock,
)

logger = structlog.get_logger()


@dataclass(frozen=True)
class ResourceDescriptor:
    """A governed resource resolved to something a human can read."""

    kind: str
    id: str
    display_name: str
    gateway_id: str | None = None
    product_names: tuple[str, ...] = ()


def _subscription_owner(subscription: ObservedSubscription) -> str | None:
    """The APIM user name that owns a subscription.

    API Management reports ``ownerId`` as a resource path ending in ``/users/{name}``, while an
    ``ObservedApimUser`` is keyed on the bare name, so the two are only comparable after the path
    is reduced.
    """

    if subscription.owner_label:
        return subscription.owner_label
    if subscription.owner_id:
        return subscription.owner_id.rsplit("/", 1)[-1]
    return None


def _resource_key(resource: EntitlementResource) -> tuple[str, str, str]:
    return (str(resource.kind), resource.id, resource.scope_id or "")


def _subject_for(principal: Principal) -> EntitlementSubject:
    kind = (
        EntitlementSubjectKind.USER
        if principal.kind == PrincipalKind.USER
        else EntitlementSubjectKind.APPLICATION
    )
    return EntitlementSubject(kind=kind, id=principal.id)


def _approved_with_grant(access_request: AccessRequest) -> bool:
    return (
        access_request.state == AccessRequestState.APPROVED
        and access_request.granted_entitlement_id is not None
    )


def _existing_grant_conflict(existing: Entitlement) -> ConflictError:
    # Refused rather than linked. The administrator confirmed limits for a new grant, and linking
    # would silently discard them. Linking could also report a disabled grant as access.
    return ConflictError(
        "The requester already has a direct grant for this resource. Deny this request, or "
        "change the existing grant instead.",
        details={"entitlementId": existing.id, "enabled": existing.enabled},
    )


class EntitlementService:
    def __init__(
        self,
        repository: EntitlementRepository,
        *,
        directory_repository: DirectoryRepository,
        gateway_repository: GatewayRepository,
        endpoint_repository: ModelEndpointRepository,
    ) -> None:
        self._repository = repository
        self._directory = directory_repository
        self._gateways = gateway_repository
        self._endpoints = endpoint_repository

    @staticmethod
    def _audit(
        actor: Actor,
        action: str,
        resource_type: str,
        resource_id: str,
        details: dict[str, Any] | None = None,
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

    # ------------------------------------------------------------------ validation

    async def _validate_subject(self, actor: Actor, subject: EntitlementSubject) -> None:
        if subject.kind == "group":
            if not await self._directory.get_group(actor.tenant_id, subject.id):
                raise ValidationError(
                    "The group named by this entitlement does not exist",
                    details={"subjectId": subject.id},
                )
            return
        principal = await self._directory.get_principal(actor.tenant_id, subject.id)
        if not principal:
            raise ValidationError(
                "The principal named by this entitlement does not exist",
                details={"subjectId": subject.id},
            )
        expected = "application" if principal.kind != "user" else "user"
        if subject.kind != expected:
            raise ValidationError(
                f"This principal is a {principal.kind}, so the entitlement subject must be "
                f"{expected!r}",
                details={"subjectId": subject.id, "principalKind": str(principal.kind)},
            )

    async def _describe_resource(
        self, actor: Actor, resource: EntitlementResource
    ) -> ResourceDescriptor:
        """Resolve a resource reference, failing when it names something MOSAIC does not govern."""

        if resource.kind == "modelApi":
            model_api = await self._gateways.get_model_api(actor.tenant_id, resource.id)
            if model_api:
                return ResourceDescriptor(
                    kind="modelApi",
                    id=model_api.id,
                    display_name=model_api.display_name,
                    gateway_id=model_api.gateway_id,
                    product_names=tuple(model_api.product_names),
                )
        elif resource.kind == "mcpServer":
            mcp_server = await self._gateways.get_mcp_server(actor.tenant_id, resource.id)
            if mcp_server:
                return ResourceDescriptor(
                    kind="mcpServer",
                    id=mcp_server.id,
                    display_name=mcp_server.display_name,
                    gateway_id=mcp_server.gateway_id,
                    product_names=tuple(mcp_server.product_names),
                )
        elif resource.kind == "product":
            scope_id = resource.scope_id or ""
            products = await self._gateways.list_observed(
                ObservedProduct, actor.tenant_id, scope_id, "observedProduct"
            )
            product = next((item for item in products if item.id == resource.id), None)
            if product:
                return ResourceDescriptor(
                    kind="product",
                    id=product.id,
                    display_name=product.display_name,
                    gateway_id=scope_id,
                    product_names=(product.name,),
                )
        elif resource.kind == "modelDeployment":
            scope_id = resource.scope_id or ""
            deployments = await self._endpoints.list_observed_for_endpoint(
                ObservedModelDeployment, actor.tenant_id, scope_id, "observedModelDeployment"
            )
            deployment = next((item for item in deployments if item.id == resource.id), None)
            if deployment:
                return ResourceDescriptor(
                    kind="modelDeployment",
                    id=deployment.id,
                    display_name=deployment.deployment_name,
                )

        raise ValidationError(
            "MOSAIC does not govern the resource named by this entitlement",
            details={"resourceKind": str(resource.kind), "resourceId": resource.id},
        )

    async def resource_display_names(
        self, actor: Actor, resources: Sequence[EntitlementResource]
    ) -> list[str | None]:
        """Name many resources at once, in the order given, for a list a person will read.

        Each name comes from the record :meth:`_describe_resource` reads for the same reference,
        so it is the name the catalog and the administrator console show. Reads are batched: one
        per desired-state kind, and one per gateway or model endpoint for observed resources,
        however many references there are. A resource that no longer exists, or whose name is
        blank, is ``None`` rather than an error, because a grant or request can outlive the
        resource it names.

        Only the resources passed in are named, so a caller that passes its own grants and
        requests learns nothing about anything else.
        """

        keys = [_resource_key(resource) for resource in resources]
        kinds = {kind for kind, _, _ in keys}
        # Desired-state records carry their own gateway, so a scope never distinguishes them.
        desired: dict[tuple[str, str], str] = {}
        if "modelApi" in kinds:
            for model_api in await self._gateways.list_model_apis(actor.tenant_id):
                desired[("modelApi", model_api.id)] = model_api.display_name
        if "mcpServer" in kinds:
            for mcp_server in await self._gateways.list_mcp_servers(actor.tenant_id):
                desired[("mcpServer", mcp_server.id)] = mcp_server.display_name

        observed: dict[tuple[str, str, str], str] = {}
        for scope_id in {scope for kind, _, scope in keys if kind == "product" and scope}:
            for product in await self._gateways.list_observed(
                ObservedProduct, actor.tenant_id, scope_id, "observedProduct"
            ):
                observed[("product", product.id, scope_id)] = product.display_name
        for scope_id in {scope for kind, _, scope in keys if kind == "modelDeployment" and scope}:
            for deployment in await self._endpoints.list_observed_for_endpoint(
                ObservedModelDeployment, actor.tenant_id, scope_id, "observedModelDeployment"
            ):
                observed[("modelDeployment", deployment.id, scope_id)] = deployment.deployment_name

        names: list[str | None] = []
        for kind, resource_id, scope_id in keys:
            if kind in {"modelApi", "mcpServer"}:
                name = desired.get((kind, resource_id))
            else:
                name = observed.get((kind, resource_id, scope_id))
            names.append(name if name and name.strip() else None)
        return names

    # ------------------------------------------------------------------ binding

    async def infer_binding(
        self, actor: Actor, descriptor: ResourceDescriptor, subject: EntitlementSubject
    ) -> EntitlementBinding | None:
        """Match a grant to the APIM subscription its usage will be logged against.

        Inference is best effort and deliberately conservative: it only claims a subscription when
        exactly one candidate matches, because attributing a user's consumption to the wrong
        subscription is worse than reporting that MOSAIC could not determine it.
        """

        if not descriptor.gateway_id or subject.kind == "group":
            return None
        principal = await self._directory.get_principal(actor.tenant_id, subject.id)
        if not principal:
            return None

        apim_user = await self._find_apim_user(actor, descriptor.gateway_id, principal)
        if not apim_user:
            return None

        subscriptions = await self._gateways.list_observed(
            ObservedSubscription, actor.tenant_id, descriptor.gateway_id, "observedSubscription"
        )
        owned = [
            item for item in subscriptions if _subscription_owner(item) == apim_user.name
        ]
        candidates = [
            item
            for item in owned
            if item.scope_kind == "allApis"
            or (item.scope_kind == "product" and item.scope_name in descriptor.product_names)
        ]
        if len(candidates) != 1:
            return None
        match = candidates[0]
        return EntitlementBinding(
            gateway_id=descriptor.gateway_id,
            apim_product_name=match.scope_name if match.scope_kind == "product" else None,
            apim_subscription_name=match.name,
            source=BindingSource.INFERRED,
            bound_at=utc_now(),
        )

    async def _find_apim_user(
        self, actor: Actor, gateway_id: str, principal: Principal
    ) -> ObservedApimUser | None:
        users = await self._gateways.list_observed(
            ObservedApimUser, actor.tenant_id, gateway_id, "observedApimUser"
        )
        return next(
            (user for user in users if user.entra_object_id == principal.object_id),
            None,
        )

    # ------------------------------------------------------------------ CRUD

    async def _decorate(self, entitlement: Entitlement) -> Entitlement:
        publication = await entitlement_publication(self._gateways, entitlement)
        principal = (
            await self._directory.get_principal(entitlement.tenant_id, entitlement.subject.id)
            if entitlement.subject.kind != "group"
            else None
        )
        locked = bool(
            publication
            and await self._gateways.get_publication_lock(
                entitlement.tenant_id, publication.id
            )
        )
        return decorate_entitlement(entitlement, publication, principal, locked=locked)

    @asynccontextmanager
    async def _mutation(self, entitlement: Entitlement) -> AsyncIterator[None]:
        publication = await entitlement_publication(self._gateways, entitlement)
        if publication is None:
            yield
        else:
            async with publication_lock(self._gateways, entitlement.tenant_id, publication.id):
                yield

    async def list_entitlements(
        self,
        actor: Actor,
        *,
        subject_id: str | None = None,
        resource_id: str | None = None,
    ) -> list[Entitlement]:
        records = await self._repository.list_entitlements(
            actor.tenant_id, subject_id=subject_id, resource_id=resource_id
        )
        return [await self._decorate(record) for record in records]

    async def get_entitlement(self, actor: Actor, entitlement_ref: str) -> Entitlement:
        entitlement = await self._repository.get_entitlement(actor.tenant_id, entitlement_ref)
        if not entitlement:
            raise NotFoundError("Entitlement was not found", details={"id": entitlement_ref})
        return await self._decorate(entitlement)

    async def _prepare_entitlement(
        self,
        actor: Actor,
        request: EntitlementCreate,
        descriptor: ResourceDescriptor | None = None,
    ) -> Entitlement:
        """Validate a grant and build its record without writing it.

        Shared by direct creation and by access-request approval, so an approved request's grant
        passes exactly the same subject, resource, and binding checks as one created directly.
        """

        if request.binding and request.binding.source == BindingSource.ORCHESTRATED:
            raise ValidationError("Orchestrated bindings are server-managed")
        await self._validate_subject(actor, request.subject)
        if descriptor is None:
            descriptor = await self._describe_resource(actor, request.resource)
        binding = request.binding
        if binding is None:
            binding = await self.infer_binding(actor, descriptor, request.subject)
        return Entitlement(
            id=entitlement_id(actor.tenant_id, request.subject, request.resource),
            tenant_id=actor.tenant_id,
            subject=request.subject,
            resource=request.resource,
            enabled=request.enabled,
            enforcement=request.enforcement,
            binding=binding,
            notes=request.notes,
        )

    async def create_entitlement(self, actor: Actor, request: EntitlementCreate) -> Entitlement:
        record = await self._prepare_entitlement(actor, request)
        async with self._mutation(record):
            await self._validate_subject(actor, request.subject)
            saved = await self._repository.create_entitlement(
                record,
                self._audit(actor, "entitlement.created", "entitlement", record.id),
            )
        logger.info(
            "entitlement_created",
            entitlement_id=record.id,
            subject_kind=str(request.subject.kind),
            resource_kind=str(request.resource.kind),
            bound=record.binding is not None,
            tenant_id=actor.tenant_id,
        )
        return await self._decorate(saved)

    async def update_entitlement(
        self, actor: Actor, entitlement_ref: str, request: EntitlementUpdate
    ) -> Entitlement:
        entitlement = await self.get_entitlement(actor, entitlement_ref)
        async with self._mutation(entitlement):
            saved = await self._update_entitlement(actor, entitlement_ref, request)
        return await self._decorate(saved)

    async def _update_entitlement(
        self, actor: Actor, entitlement_ref: str, request: EntitlementUpdate
    ) -> Entitlement:
        entitlement = await self.get_entitlement(actor, entitlement_ref)
        changes = request.model_dump(exclude_unset=True)
        publication = await entitlement_publication(self._gateways, entitlement)
        if "binding" in changes and (
            (entitlement.binding and entitlement.binding.source == BindingSource.ORCHESTRATED)
            or (
                publication is not None
                and managed_grant_needs_retention(publication, entitlement.id)
            )
            or (request.binding and request.binding.source == BindingSource.ORCHESTRATED)
        ):
            raise ConflictError(
                "This grant's runtime binding is server-managed and cannot be cleared or replaced"
            )
        # ``enabled`` is not nullable on the stored record, so an explicit null means "leave it
        # alone" rather than a validation crash. ``enforcement``, ``binding``, and ``notes`` are
        # nullable and keep their clear-on-null behaviour.
        if changes.get("enabled") is None:
            changes.pop("enabled", None)
        updated = Entitlement.model_validate(
            {
                **entitlement.model_dump(by_alias=False),
                **changes,
                "etag": entitlement.etag,
                "updated_at": utc_now(),
                "runtime": None,
            }
        )
        return await self._repository.save_entitlement(
            updated,
            self._audit(actor, "entitlement.updated", "entitlement", updated.id),
        )

    async def delete_entitlement(self, actor: Actor, entitlement_ref: str) -> None:
        entitlement = await self.get_entitlement(actor, entitlement_ref)
        async with self._mutation(entitlement):
            entitlement = await self.get_entitlement(actor, entitlement_ref)
            publication = await entitlement_publication(self._gateways, entitlement)
            if publication and managed_grant_needs_retention(publication, entitlement.id):
                raise ConflictError(
                    "This grant still has managed or uncertain runtime access. Disable and apply "
                    "it to revoke access; retain the disabled grant until its publication is "
                    "unpublished so its subscription is not orphaned.",
                    details={"publicationId": publication.id},
                )
            await self._repository.delete_entitlement(
                entitlement,
                self._audit(actor, "entitlement.deleted", "entitlement", entitlement_ref),
            )

    # ------------------------------------------------------------------ resolution

    async def resolve_for_object_id(
        self, actor: Actor, object_id: str
    ) -> list[ResolvedEntitlement]:
        """Effective access for an Entra object ID.

        A principal MOSAIC has never seen has no entitlements, which is an empty list rather than
        an error: the portal renders that as "nothing has been granted to you yet".
        """

        principal = await self._directory.find_principal_by_object_id(actor.tenant_id, object_id)
        if not principal:
            return []
        return await self.resolve_for_principal(actor, principal.id)

    async def resolve_for_principal(
        self, actor: Actor, principal_id: str
    ) -> list[ResolvedEntitlement]:
        # Keyed on the resource, not the entitlement: a direct grant and a group grant over the
        # same resource are different entitlements, and returning both would leave a consumer
        # choosing arbitrarily between two contradictory limits.
        resolved: dict[tuple[str, str, str], ResolvedEntitlement] = {}
        for entitlement in await self._repository.list_entitlements(
            actor.tenant_id, subject_id=principal_id
        ):
            if entitlement.subject.kind != "group" and entitlement.enabled:
                resolved[_resource_key(entitlement.resource)] = ResolvedEntitlement(
                    entitlement=await self._decorate(entitlement), via=GrantPath.DIRECT
                )

        memberships = await self._directory.list_memberships(
            actor.tenant_id, principal_id=principal_id
        )
        for membership in memberships:
            group = await self._directory.get_group(actor.tenant_id, membership.group_id)
            for entitlement in await self._repository.list_entitlements(
                actor.tenant_id, subject_id=membership.group_id
            ):
                if entitlement.subject.kind != "group" or not entitlement.enabled:
                    continue
                # A direct grant is the more specific statement about this person, so it wins over
                # anything a group contributes for the same resource.
                if _resource_key(entitlement.resource) in resolved:
                    continue
                resolved[_resource_key(entitlement.resource)] = ResolvedEntitlement(
                    entitlement=entitlement,
                    via=GrantPath.GROUP,
                    via_group_id=membership.group_id,
                    via_group_name=group.name if group else None,
                )
        return sorted(resolved.values(), key=lambda item: item.entitlement.id)

    # ------------------------------------------------------------------ access requests

    async def list_access_requests(
        self,
        actor: Actor,
        *,
        requester_object_id: str | None = None,
        state: str | None = None,
    ) -> list[AccessRequest]:
        return await self._repository.list_access_requests(
            actor.tenant_id, requester_object_id=requester_object_id, state=state
        )

    async def get_access_request(self, actor: Actor, request_id: str) -> AccessRequest:
        access_request = await self._repository.get_access_request(actor.tenant_id, request_id)
        if not access_request:
            raise NotFoundError("Access request was not found", details={"id": request_id})
        return access_request

    async def create_access_request(
        self, actor: Actor, request: AccessRequestCreate
    ) -> AccessRequest:
        descriptor = await self._describe_resource(actor, request.resource)
        principal = await self._directory.find_principal_by_object_id(
            actor.tenant_id, actor.object_id
        )
        open_requests = [
            item
            for item in await self._repository.list_access_requests(
                actor.tenant_id, requester_object_id=actor.object_id, state="pending"
            )
            if item.resource.id == request.resource.id
        ]
        if open_requests:
            raise ConflictError(
                "You already have an open request for this resource",
                details={"id": open_requests[0].id},
            )
        record = AccessRequest(
            id=deterministic_id(
                "accessRequest",
                actor.tenant_id,
                actor.object_id,
                descriptor.id,
                utc_now().isoformat(),
            ),
            tenant_id=actor.tenant_id,
            requester_object_id=actor.object_id,
            requester_principal_id=principal.id if principal else None,
            resource=request.resource,
            justification=request.justification,
        )
        return await self._repository.create_access_request(
            record,
            self._audit(actor, "accessRequest.created", "accessRequest", record.id),
        )

    async def decide_access_request(
        self,
        actor: Actor,
        request_id: str,
        *,
        state: AccessRequestState,
        note: str | None = None,
    ) -> AccessRequest:
        """Close a pending request without granting anything: deny or withdraw it.

        Approval goes through :meth:`approve_access_request` instead. That is the only path that
        writes the grant together with the decision, so an approved request cannot exist
        without its grant.
        """

        if state not in {AccessRequestState.DENIED, AccessRequestState.WITHDRAWN}:
            raise ValueError("Only a denial or withdrawal closes a request without a grant")
        access_request = await self.get_access_request(actor, request_id)
        if access_request.state != AccessRequestState.PENDING:
            raise ConflictError(
                f"This request was already {access_request.state}",
                details={"id": request_id, "state": str(access_request.state)},
            )
        updated = AccessRequest.model_validate(
            {
                **access_request.model_dump(by_alias=False),
                "state": state,
                "decided_by_object_id": actor.object_id,
                "decided_at": utc_now(),
                "decision_note": note,
                "etag": access_request.etag,
                "updated_at": utc_now(),
            }
        )
        return await self._repository.save_access_request(
            updated,
            self._audit(actor, f"accessRequest.{state}", "accessRequest", request_id),
        )

    async def approve_access_request(
        self, actor: Actor, request_id: str, approval: AccessRequestApproval
    ) -> AccessRequest:
        """Approve a pending request by creating the requester's grant and linking it.

        The grant is desired state. API Management is unchanged until its model plan is reviewed
        and applied. The grant and the decision are written in one atomic repository call, so
        neither exists without the other. A requester MOSAIC has never registered becomes a
        ``user`` principal first. That registration is idempotent and grants nothing on its
        own, so it is safe to leave in place if the approval then fails.

        Approving an already approved request returns it unchanged. That makes a retry after an
        ambiguous failure safe, and the first decision's limits stand.
        """

        access_request = await self.get_access_request(actor, request_id)
        if _approved_with_grant(access_request):
            return access_request
        self._require_pending(access_request)
        descriptor = await self._describe_resource(actor, access_request.resource)

        principal = await self._requester_principal(actor, access_request.requester_object_id)
        if principal is not None:
            existing = await self._repository.get_entitlement(
                actor.tenant_id,
                entitlement_id(actor.tenant_id, _subject_for(principal), access_request.resource),
            )
            if existing is not None:
                raise _existing_grant_conflict(existing)
        principal_created = principal is None
        if principal is None:
            principal = await self._register_requester(actor, access_request.requester_object_id)

        subject = _subject_for(principal)
        entitlement = await self._prepare_entitlement(
            actor,
            EntitlementCreate(
                subject=subject,
                resource=access_request.resource,
                enforcement=approval.enforcement,
            ),
            descriptor,
        )
        decided_at = utc_now()
        approved = AccessRequest.model_validate(
            {
                **access_request.model_dump(by_alias=False),
                "state": AccessRequestState.APPROVED,
                "requester_principal_id": principal.id,
                "decided_by_object_id": actor.object_id,
                "decided_at": decided_at,
                "decision_note": approval.note,
                "granted_entitlement_id": entitlement.id,
                "etag": access_request.etag,
                "updated_at": decided_at,
            }
        )
        audit_events = [
            self._audit(
                actor,
                "entitlement.created",
                "entitlement",
                entitlement.id,
                {"accessRequestId": request_id},
            ),
            self._audit(
                actor,
                "accessRequest.approved",
                "accessRequest",
                request_id,
                {
                    "grantedEntitlementId": entitlement.id,
                    "principalId": principal.id,
                    "principalCreated": principal_created,
                },
            ),
        ]
        try:
            async with self._mutation(entitlement):
                await self._validate_subject(actor, subject)
                saved = await self._repository.approve_access_request(
                    approved, entitlement, audit_events
                )
        except ConflictError:
            # Nothing was written. Work out why, so a concurrent approval converges on its
            # result instead of failing, and any other conflict is reported precisely.
            current = await self.get_access_request(actor, request_id)
            if _approved_with_grant(current):
                return current
            self._require_pending(current)
            existing = await self._repository.get_entitlement(actor.tenant_id, entitlement.id)
            if existing is not None:
                raise _existing_grant_conflict(existing) from None
            raise
        logger.info(
            "access_request_approved",
            access_request_id=request_id,
            entitlement_id=entitlement.id,
            principal_id=principal.id,
            principal_created=principal_created,
            resource_kind=str(access_request.resource.kind),
            limited=approval.enforcement is not None,
            tenant_id=actor.tenant_id,
        )
        return saved

    @staticmethod
    def _require_pending(access_request: AccessRequest) -> None:
        if access_request.state == AccessRequestState.PENDING:
            return
        if access_request.state == AccessRequestState.APPROVED:
            raise ConflictError(
                "This request was approved before approval created grants, so no grant is linked "
                "to it. Grant access from Entitlements, or ask the requester to submit a new "
                "request.",
                details={"id": access_request.id, "state": str(access_request.state)},
            )
        raise ConflictError(
            f"This request was already {access_request.state}",
            details={"id": access_request.id, "state": str(access_request.state)},
        )

    async def _requester_principal(self, actor: Actor, object_id: str) -> Principal | None:
        principal = await self._directory.find_principal_by_object_id(actor.tenant_id, object_id)
        if principal is None:
            # Principal IDs derive from the case-folded object ID. That also finds a principal
            # an administrator registered with different letter case, which would otherwise
            # collide with the one registration would create.
            principal = await self._directory.get_principal(
                actor.tenant_id, deterministic_id("principal", actor.tenant_id, object_id)
            )
        return principal

    async def _register_requester(self, actor: Actor, object_id: str) -> Principal:
        try:
            request = PrincipalCreate(object_id=object_id, kind=PrincipalKind.USER)
        except SchemaValidationError as exc:
            raise ValidationError(
                "The requester's Entra object ID cannot be registered as a principal",
                details={"objectId": object_id},
            ) from exc
        try:
            return await DirectoryService(self._directory).create_principal(actor, request)
        except ConflictError:
            # A concurrent approval for the same person registered them first.
            principal = await self._requester_principal(actor, object_id)
            if principal is None:
                raise
            return principal

    async def withdraw_access_request(self, actor: Actor, request_id: str) -> AccessRequest:
        access_request = await self.get_access_request(actor, request_id)
        if access_request.requester_object_id != actor.object_id:
            raise NotFoundError("Access request was not found", details={"id": request_id})
        return await self.decide_access_request(
            actor, request_id, state=AccessRequestState.WITHDRAWN
        )
