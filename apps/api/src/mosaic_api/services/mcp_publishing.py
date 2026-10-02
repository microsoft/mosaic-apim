"""Publish registered MCP servers into API Management with fail-closed access control."""

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog

from mosaic_api.budgets import BLOCKED_COST_CENTERS_NAMED_VALUE
from mosaic_api.domain import (
    ApimResourceId,
    AppliedCostCenterPool,
    AuditEvent,
    BindingSource,
    CapabilitySupport,
    EntitlementBinding,
    EntitlementSubjectKind,
    Gateway,
    GatewayTier,
    ManagementMode,
    McpAccessGrant,
    McpAccessSnapshot,
    McpAuthMode,
    McpEndpoint,
    McpEndpointStatus,
    McpModelCaller,
    McpModelCallerUpdate,
    McpPublication,
    McpPublicationCreate,
    McpPublicationUpdate,
    McpPublishingCapability,
    McpServer,
    McpServerKind,
    McpServerRoute,
    McpTransportType,
    Principal,
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
    apim_slug,
    gateway_tier,
    grant_precedence_key,
    mcp_metadata_api_path,
    mcp_publication_id,
    mcp_server_id,
    new_id,
    subject_kind_for,
    utc_now,
)
from mosaic_api.environments import (
    VerdictLevel,
    compatibility_fingerprint,
    permits,
    refuse_blocked_pairing,
)
from mosaic_api.errors import ConflictError, NotFoundError, ValidationError
from mosaic_api.integrations.apim import ApimClient
from mosaic_api.integrations.apim.writer import ApimWriter
from mosaic_api.integrations.mcp_access_policy import (
    McpPolicyDocuments,
    mcp_counter_key_expression,
    mcp_grant_counter_identity,
    render_mcp_policy,
)
from mosaic_api.observed import ObservedApi
from mosaic_api.repositories import (
    CostCenterRepository,
    DirectoryRepository,
    EntitlementRepository,
    EnvironmentRepository,
    GatewayRepository,
    McpEndpointRepository,
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
    local_mutation_active,
    publication_lock,
)
from mosaic_api.services.publishing import _KIND_NOUNS as _KIND_NOUNS
from mosaic_api.services.publishing import DENY_ALL_FRAGMENT, DENY_ALL_POLICY, STALE_RUN_MESSAGE
from mosaic_api.services.publishing import _merge_resources as _merge_resources

logger = structlog.get_logger()

ClientFactory = Callable[[ApimResourceId], ApimClient]
WriterFactory = Callable[[ApimResourceId], ApimWriter]
RecoveryJournal = Callable[[PublishedResourceKind, str, bool, str], Awaitable[None]]

MCP_CREATE_ORDER: tuple[PublishedResourceKind, ...] = (
    PublishedResourceKind.BACKEND,
    PublishedResourceKind.POLICY_FRAGMENT,
    PublishedResourceKind.API,
    PublishedResourceKind.API_POLICY,
    PublishedResourceKind.API,
    PublishedResourceKind.API_OPERATION,
    PublishedResourceKind.API_POLICY,
)


@dataclass(frozen=True)
class _Resource:
    kind: PublishedResourceKind
    name: str
    segment: str
    target_api: str | None = None


def _desired_resources(publication: McpPublication) -> list[_Resource]:
    return [
        _Resource(
            PublishedResourceKind.BACKEND,
            publication.backend_name,
            f"backends/{publication.backend_name}",
        ),
        _Resource(
            PublishedResourceKind.POLICY_FRAGMENT,
            publication.fragment_name,
            f"policyFragments/{publication.fragment_name}",
        ),
        _Resource(PublishedResourceKind.API, publication.api_name, f"apis/{publication.api_name}"),
        _Resource(
            PublishedResourceKind.API_POLICY,
            publication.api_name,
            f"apis/{publication.api_name}/policies/policy",
            target_api=publication.api_name,
        ),
        _Resource(
            PublishedResourceKind.API,
            publication.metadata_api_name,
            f"apis/{publication.metadata_api_name}",
        ),
        _Resource(
            PublishedResourceKind.API_OPERATION,
            "metadata",
            f"apis/{publication.metadata_api_name}/operations/metadata",
            target_api=publication.metadata_api_name,
        ),
        _Resource(
            PublishedResourceKind.API_POLICY,
            publication.metadata_api_name,
            f"apis/{publication.metadata_api_name}/policies/policy",
            target_api=publication.metadata_api_name,
        ),
    ]


def _resource_key(resource: PublishedResource | PublishStepResult) -> tuple[str, str]:
    return str(resource.kind), resource.name


def _disabled_snapshot(snapshot: McpAccessSnapshot) -> McpAccessSnapshot:
    return snapshot.model_copy(
        update={
            "grants": [
                grant.model_copy(update={"enabled": False}) for grant in snapshot.grants
            ]
        }
    )


def _policy_documents(
    publication: McpPublication,
    snapshot: McpAccessSnapshot,
    *,
    backend_auth: McpAuthMode,
    backend_audience: str | None,
) -> McpPolicyDocuments:
    return render_mcp_policy(
        publication,
        snapshot,
        backend_auth=backend_auth,
        backend_audience=backend_audience,
    )


def mcp_publication_digest(
    publication: McpPublication,
    endpoint: McpEndpoint,
    policy: McpPolicyDocuments,
    snapshot: McpAccessSnapshot,
    environment_fingerprint: str | None = None,
) -> str:
    payload = json.dumps(
        {
            "gatewayId": publication.gateway_id,
            "mcpEndpointId": publication.mcp_endpoint_id,
            "displayName": publication.display_name,
            "apiName": publication.api_name,
            "apiPath": publication.api_path,
            "backendName": publication.backend_name,
            "backendUrl": str(endpoint.endpoint),
            "backendAuth": str(endpoint.auth_mode),
            "backendAudience": endpoint.resource_audience,
            "fragmentName": publication.fragment_name,
            "metadataApiName": publication.metadata_api_name,
            "mcpServerId": publication.mcp_server_id,
            "policySha256": policy.content_sha256,
            "accessSnapshot": snapshot.model_dump(mode="json"),
            "environmentFingerprint": environment_fingerprint,
            "previousAccessVersion": (
                publication.applied_access.version if publication.applied_access else None
            ),
            "previousAccessSnapshot": (
                publication.applied_access.model_dump(mode="json")
                if publication.applied_access
                else None
            ),
            "ownedResources": sorted(
                (str(item.kind), item.name, item.resource_id, item.created_by_mosaic)
                for item in publication.resources
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def mcp_unpublish_digest(publication: McpPublication) -> str:
    """A digest over what unpublishing removes and whose access it ends.

    Unpublish runs a reviewed plan only while this still matches: a resource recorded since the
    review, or access applied since, makes it stale. It covers the names the deletes and the
    fail-closed guard read besides the plan's own steps.
    """

    payload = json.dumps(
        {
            "operation": "unpublish",
            "publicationId": publication.id,
            "gatewayId": publication.gateway_id,
            "apiName": publication.api_name,
            "metadataApiName": publication.metadata_api_name,
            "fragmentName": publication.fragment_name,
            "backendName": publication.backend_name,
            "appliedAccess": (
                publication.applied_access.model_dump(mode="json")
                if publication.applied_access
                else None
            ),
            "ownedResources": sorted(
                (str(item.kind), item.name, item.resource_id, item.created_by_mosaic)
                for item in publication.resources
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _resource_label(publication: McpPublication, item: PublishedResource) -> str:
    if item.kind == PublishedResourceKind.API_POLICY:
        return f"the policy on API {item.name}"
    if item.kind == PublishedResourceKind.API_OPERATION:
        return f"operation {item.name} of API {publication.metadata_api_name}"
    return f"{_KIND_NOUNS[item.kind]} {item.name}"


def _removal_reason(publication: McpPublication, item: PublishedResource) -> str:
    if item.kind == PublishedResourceKind.API:
        return (
            "Delete the MCP API. The gateway stops serving the server."
            if item.name == publication.api_name
            else "Delete the API that tells MCP clients where to sign in."
        )
    if item.kind == PublishedResourceKind.API_POLICY:
        return (
            "Delete the MCP API's policy."
            if item.name == publication.api_name
            else "Delete the sign-in metadata API's policy."
        )
    return {
        PublishedResourceKind.API_OPERATION: "Delete the operation that serves sign-in metadata.",
        PublishedResourceKind.POLICY_FRAGMENT: "Delete the MOSAIC enforcement fragment.",
        PublishedResourceKind.BACKEND: "Delete the backend that points at the MCP server.",
    }.get(item.kind, f"Delete {_resource_label(publication, item)}.")


class McpPublishingService:
    def __init__(
        self,
        repository: GatewayRepository,
        *,
        mcp_endpoint_repository: McpEndpointRepository,
        entitlement_repository: EntitlementRepository,
        directory_repository: DirectoryRepository,
        client_factory: ClientFactory,
        writer_factory: WriterFactory,
        runtime_client_id: str | None,
        security_group_claims: bool = True,
        environment_repository: EnvironmentRepository | None = None,
        cost_center_repository: CostCenterRepository | None = None,
        blocked_list: BlockedListGate | None = None,
    ) -> None:
        self._repository = repository
        self._cost_centers = cost_center_repository
        self._blocked_list = blocked_list
        self._endpoints = mcp_endpoint_repository
        self._entitlements = entitlement_repository
        self._directory = directory_repository
        self._client_factory = client_factory
        self._writer_factory = writer_factory
        self._runtime_client_id = runtime_client_id
        self._security_group_claims = security_group_claims
        self._environments = environment_repository
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
        actor: Actor, action: str, resource_id: str, resource_type: str = "mcpPublication"
    ) -> AuditEvent:
        return AuditEvent(
            id=new_id("audit"),
            tenant_id=actor.tenant_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            actor_object_id=actor.object_id,
        )

    async def capability(self, actor: Actor, gateway_id: str) -> McpPublishingCapability:
        gateway = await self._load_gateway(actor, gateway_id)
        reasons: list[str] = []
        warnings: list[str] = []
        if gateway.management_mode != ManagementMode.MANAGE:
            reasons.append("Gateway must be in managed mode.")
        if not gateway.access.can_write:
            reasons.append("MOSAIC cannot write to this gateway.")
        tier = gateway_tier(gateway.capabilities.sku_name)
        if tier == GatewayTier.CONSUMPTION:
            reasons.append("Consumption tier does not support MCP servers.")
        elif tier == GatewayTier.UNKNOWN:
            warnings.append("Gateway tier is unknown; verify MCP support before relying on it.")
        if gateway.capabilities.mcp_servers == CapabilitySupport.UNAVAILABLE:
            reasons.append("This gateway does not support MCP servers.")
        elif gateway.capabilities.mcp_servers == CapabilitySupport.UNKNOWN:
            warnings.append("MCP server support is unknown; synchronize the gateway first.")
        if gateway.capabilities.gateway_url is None:
            reasons.append("Gateway URL is unknown; synchronize this gateway first.")
        if not self._runtime_client_id:
            reasons.append("The MCP runtime client ID is not configured.")
        if not _is_guid(actor.tenant_id):
            reasons.append("The tenant ID must be a GUID to render MCP access policy.")
        warnings.append(
            "API Management diagnostics must log 0 bytes of response bodies for MCP APIs, or "
            "streaming breaks."
        )
        return McpPublishingCapability(
            gateway_id=gateway.id,
            supported=not reasons,
            reasons=reasons,
            warnings=warnings,
        )

    async def list_publications(
        self, actor: Actor, gateway_id: str | None
    ) -> list[McpPublication]:
        return await self._repository.list_mcp_publications(
            actor.tenant_id, gateway_id=gateway_id
        )

    async def get_publication(self, actor: Actor, publication_id: str) -> McpPublication:
        publication = await self._repository.get_mcp_publication(actor.tenant_id, publication_id)
        if publication is None:
            raise NotFoundError("MCP publication was not found", details={"id": publication_id})
        return publication

    async def get_lock_owner(self, actor: Actor, publication_id: str) -> str | None:
        return await self._repository.get_publication_lock(actor.tenant_id, publication_id)

    async def create(self, actor: Actor, request: McpPublicationCreate) -> McpPublication:
        target = mcp_publication_id(actor.tenant_id, request.gateway_id, request.mcp_endpoint_id)
        async with environment_guard(
            self._repository,
            actor.tenant_id,
            environments=True,
            gateway_ids=[request.gateway_id],
            publication_ids=[target],
        ):
            return await self._create(actor, request)

    async def _create(self, actor: Actor, request: McpPublicationCreate) -> McpPublication:
        gateway = await self._load_gateway(actor, request.gateway_id)
        capability = await self.capability(actor, gateway.id)
        if not capability.supported:
            raise ConflictError(capability.reasons[0], details={"reasons": capability.reasons})
        endpoint = await self._load_endpoint(actor, request.mcp_endpoint_id)
        self._validate_endpoint(endpoint)
        catalog = await load_environment_catalog(self._environments, actor.tenant_id)
        verdict = permits(catalog, gateway.environment, endpoint.environment)
        refuse_blocked_pairing(verdict)

        slug = apim_slug(endpoint.name) or "server"
        api_name = request.api_name or f"mosaic-mcp-{slug}"
        if request.api_name is not None and len(api_name) > 76:
            raise ValidationError("API names over 76 characters leave no room for the metadata API")
        api_path = request.api_path or f"mosaic/mcp/{slug}"
        publication = McpPublication(
            id=mcp_publication_id(actor.tenant_id, gateway.id, endpoint.id),
            tenant_id=actor.tenant_id,
            gateway_id=gateway.id,
            mcp_endpoint_id=endpoint.id,
            display_name=request.display_name or endpoint.name,
            api_name=api_name,
            api_path=api_path,
            backend_name=api_name,
            fragment_name=api_name,
            metadata_api_name=f"{api_name}-prm",
            mcp_server_id=mcp_server_id(actor.tenant_id, gateway.id, api_name),
        )
        existing = await self._repository.get_mcp_publication(actor.tenant_id, publication.id)
        if existing:
            raise ConflictError(
                "This MCP server already has a publication on this gateway. Edit it, or delete it "
                "and publish again.",
                details={"id": existing.id, "status": str(existing.status)},
            )
        await self._reject_desired_collisions(actor, publication, gateway)
        saved = await self._repository.save_mcp_publication(
            publication, self._audit(actor, "mcpPublication.created", publication.id)
        )
        await self._materialize_mcp_server(actor, saved)
        return saved

    async def update(
        self, actor: Actor, publication_id: str, request: McpPublicationUpdate
    ) -> McpPublication:
        async with publication_lock(self._repository, actor.tenant_id, publication_id):
            publication = await self.get_publication(actor, publication_id)
            if publication.access_state == "unknown":
                raise ConflictError(
                    "Recover this publication's interrupted apply before editing it"
                )
            if request.display_name is None:
                return publication
            updated = publication.model_copy(
                update={
                    "display_name": request.display_name,
                    "last_plan_id": None,
                    "last_plan_digest": None,
                    "status": (
                        PublicationStatus.DRAFT
                        if publication.status == PublicationStatus.PLANNED
                        else publication.status
                    ),
                    "updated_at": utc_now(),
                }
            )
            saved = await self._repository.save_mcp_publication(
                updated, self._audit(actor, "mcpPublication.updated", updated.id)
            )
            await self._materialize_mcp_server(actor, saved)
            return saved

    async def set_model_caller(
        self, actor: Actor, publication_id: str, request: McpModelCallerUpdate
    ) -> McpPublication:
        """Name the application this server's tools call governed models as. See ADR 0025.

        It's recorded for usage, never for access: the application's own model grants still decide
        what its calls may do. Like any change to the publication, it applies with the next plan.
        """

        async with publication_lock(self._repository, actor.tenant_id, publication_id):
            publication = await self._editable(actor, publication_id)
            principal = await self._directory.get_principal(actor.tenant_id, request.principal_id)
            if principal is None:
                raise NotFoundError(
                    "No principal has that ID", details={"principalId": request.principal_id}
                )
            if not _can_call_models(principal):
                raise ValidationError(
                    "An MCP server calls models as an application. Choose a service principal, "
                    "a managed identity or an agent identity with a GUID object ID.",
                    details={"principalId": principal.id},
                )
            return await self._save_model_caller(actor, publication, principal.id)

    async def clear_model_caller(self, actor: Actor, publication_id: str) -> McpPublication:
        """Stop attributing the server's model calls to its callers, from its next apply."""

        async with publication_lock(self._repository, actor.tenant_id, publication_id):
            publication = await self._editable(actor, publication_id)
            return await self._save_model_caller(actor, publication, None)

    async def _editable(self, actor: Actor, publication_id: str) -> McpPublication:
        publication = await self.get_publication(actor, publication_id)
        if publication.access_state == "unknown":
            raise ConflictError("Recover this publication's interrupted apply before editing it")
        return publication

    async def _save_model_caller(
        self, actor: Actor, publication: McpPublication, principal_id: str | None
    ) -> McpPublication:
        if publication.model_caller_id == principal_id:
            return publication
        updated = publication.model_copy(
            update={
                "model_caller_id": principal_id,
                "last_plan_id": None,
                "last_plan_digest": None,
                "status": (
                    PublicationStatus.DRAFT
                    if publication.status == PublicationStatus.PLANNED
                    else publication.status
                ),
                "updated_at": utc_now(),
            }
        )
        event = self._audit(actor, "mcpPublication.modelCallerChanged", publication.id)
        details = {"previous": publication.model_caller_id, "modelCaller": principal_id}
        return await self._repository.save_mcp_publication(
            updated, event.model_copy(update={"details": details})
        )

    async def delete(self, actor: Actor, publication_id: str) -> None:
        async with publication_lock(self._repository, actor.tenant_id, publication_id):
            publication = await self.get_publication(actor, publication_id)
            if publication.may_own_gateway_state():
                raise ConflictError(
                    "This MCP publication still owns gateway state. Unpublish it first.",
                    details={"id": publication.id},
                )
            entitlements = await self._entitlements.list_entitlements(
                actor.tenant_id, resource_id=publication.mcp_server_id
            )
            if entitlements:
                raise ConflictError(
                    "Remove grants for this MCP server before deleting its publication.",
                    details={"entitlements": [item.id for item in entitlements]},
                )
            server = await self._repository.get_mcp_server(
                actor.tenant_id, publication.mcp_server_id
            )
            if server is not None:
                await self._repository.delete_mcp_server(
                    server,
                    self._audit(actor, "mcpServer.removed", server.id, "mcpServer"),
                )
            await self._repository.delete_mcp_publication(
                publication, self._audit(actor, "mcpPublication.removed", publication.id)
            )

    async def plan(self, actor: Actor, publication_id: str) -> PublishPlan:
        async with publication_lock(self._repository, actor.tenant_id, publication_id):
            return await self._plan(actor, publication_id)

    async def _plan(self, actor: Actor, publication_id: str) -> PublishPlan:
        publication = await self.get_publication(actor, publication_id)
        if publication.access_state == "unknown":
            raise ConflictError("Recover this publication's interrupted apply before replanning")
        gateway = await self._load_gateway(actor, publication.gateway_id)
        self._require_writable(gateway)
        endpoint = await self._load_endpoint(actor, publication.mcp_endpoint_id)
        self._validate_endpoint(endpoint)
        catalog = await load_environment_catalog(self._environments, actor.tenant_id)
        verdict = permits(catalog, gateway.environment, endpoint.environment)
        refuse_blocked_pairing(verdict)
        environment_fingerprint = compatibility_fingerprint(
            catalog, gateway.environment, endpoint.environment
        )
        await self._reject_live_collisions(actor, publication, gateway)

        snapshot, access_warnings = await self._access_snapshot(publication, endpoint)
        policy = _policy_documents(
            publication,
            snapshot,
            backend_auth=endpoint.auth_mode,
            backend_audience=endpoint.resource_audience,
        )
        resource = ApimResourceId.parse(gateway.azure_resource_id)
        client = self._client_factory(resource)
        steps = await self._steps(publication, endpoint, client, resource)
        warnings = [*self._warnings(publication, gateway, endpoint, snapshot)]
        if verdict.level == VerdictLevel.WARNING and verdict.reason not in warnings:
            warnings.append(verdict.reason)
        warnings.extend(item for item in access_warnings if item not in warnings)
        plan = PublishPlan(
            id=new_id("publishplan"),
            tenant_id=actor.tenant_id,
            publication_id=publication.id,
            target="mcp",
            gateway_id=gateway.id,
            digest=mcp_publication_digest(
                publication,
                endpoint,
                policy,
                snapshot,
                environment_fingerprint=environment_fingerprint,
            ),
            steps=steps,
            facets=policy.facets,
            policy_content_sha256=policy.content_sha256,
            warnings=warnings,
            actor_object_id=actor.object_id,
            mcp_access_snapshot=snapshot,
            previous_access_version=(
                publication.applied_access.version if publication.applied_access else None
            ),
        )
        await self._repository.save_publish_plan(plan)
        await self._repository.record_mcp_publication_state(
            publication.model_copy(
                update={
                    "status": (
                        publication.status
                        if publication.status == PublicationStatus.PUBLISHED
                        else PublicationStatus.PLANNED
                    ),
                    "last_plan_id": plan.id,
                    "last_plan_digest": plan.digest,
                    "updated_at": utc_now(),
                }
            )
        )
        return plan

    async def get_plan(self, actor: Actor, plan_id: str) -> PublishPlan:
        plan = await self._repository.get_publish_plan(actor.tenant_id, plan_id)
        if plan is None or plan.target != "mcp":
            raise NotFoundError("MCP publish plan was not found", details={"id": plan_id})
        return plan

    async def apply(
        self, actor: Actor, publication_id: str, plan_id: str | None
    ) -> PublishRun:
        owner = new_id("publishrun")
        await self._repository.acquire_publication_lock(actor.tenant_id, publication_id, owner)
        self._active.add(publication_id)
        try:
            return await self._apply_locked(actor, publication_id, plan_id, owner)
        except BaseException:
            self._active.discard(publication_id)
            await self._repository.release_publication_lock(
                actor.tenant_id, publication_id, owner
            )
            raise

    async def _apply_locked(
        self, actor: Actor, publication_id: str, plan_id: str | None, owner: str
    ) -> PublishRun:
        publication = await self.get_publication(actor, publication_id)
        if publication.access_state == "unknown":
            raise ConflictError("Recover this publication's interrupted apply before applying")
        gateway = await self._load_gateway(actor, publication.gateway_id)
        self._require_writable(gateway)
        endpoint = await self._load_endpoint(actor, publication.mcp_endpoint_id)
        resolved = plan_id or publication.last_plan_id
        if not resolved:
            raise ConflictError("Plan this MCP publication before applying it.")
        plan = await self.get_plan(actor, resolved)
        if plan.operation != "publish" or any(
            step.action == PublishAction.DELETE for step in plan.steps
        ):
            # Plans saved before unpublishing was planned carry no operation, only delete steps.
            raise ConflictError(
                "That plan unpublishes this MCP server, and apply runs only publish plans. Plan "
                "the publication to publish it.",
                details={"planId": plan.id, "operation": "unpublish"},
            )
        if plan.publication_id != publication.id or plan.id != publication.last_plan_id:
            raise ConflictError("This is not the latest plan for this MCP publication; plan again.")
        catalog = await load_environment_catalog(self._environments, actor.tenant_id)
        verdict = permits(catalog, gateway.environment, endpoint.environment)
        refuse_blocked_pairing(verdict)
        environment_fingerprint = compatibility_fingerprint(
            catalog, gateway.environment, endpoint.environment
        )
        snapshot, _ = await self._access_snapshot(publication, endpoint)
        policy = _policy_documents(
            publication,
            snapshot,
            backend_auth=endpoint.auth_mode,
            backend_audience=endpoint.resource_audience,
        )
        if (
            mcp_publication_digest(
                publication,
                endpoint,
                policy,
                snapshot,
                environment_fingerprint=environment_fingerprint,
            )
            != plan.digest
        ):
            raise ConflictError(
                "This MCP publication changed after the plan was produced. Re-plan it and "
                "review the new changes before applying."
            )
        run = self._claim(actor, publication, plan, owner)
        await self._repository.save_publish_run(run)
        await self._mark_applying(publication, run.id)
        resource = ApimResourceId.parse(gateway.azure_resource_id)
        self._spawn(self._run_apply(publication, gateway, endpoint, plan, policy, run, resource))
        return run

    async def plan_unpublish(self, actor: Actor, publication_id: str) -> PublishPlan:
        """Plan what unpublishing removes and whose access it ends, without removing anything.

        Like model unpublishing: the plan lists the resources MOSAIC created, in deletion order,
        and carries the access last applied. It changes nothing on the publication, and unpublish
        runs it only while :func:`mcp_unpublish_digest` still matches.
        """

        async with publication_lock(self._repository, actor.tenant_id, publication_id):
            publication = await self.get_publication(actor, publication_id)
            gateway = await self._load_gateway(actor, publication.gateway_id)
            self._require_writable(gateway)
            removals = self._unpublish_removals(publication)
            if not removals:
                raise ConflictError(
                    "MOSAIC did not create gateway resources for this MCP publication, so there "
                    "is nothing to remove.",
                    details={"id": publication.id},
                )
            plan = PublishPlan(
                id=new_id("publishplan"),
                tenant_id=actor.tenant_id,
                publication_id=publication.id,
                target="mcp",
                operation="unpublish",
                gateway_id=gateway.id,
                digest=mcp_unpublish_digest(publication),
                steps=[
                    PublishPlanStep(
                        kind=item.kind,
                        name=item.name,
                        action=PublishAction.DELETE,
                        reason=_removal_reason(publication, item),
                        resource_id=item.resource_id,
                        existed=True,
                    )
                    for item in removals
                ],
                warnings=[
                    "Before it deletes anything, MOSAIC replaces the MCP API's policy with one "
                    "that refuses every call, so callers are cut off first.",
                    *(
                        f"MOSAIC didn't create {_resource_label(publication, item)}, so it stays "
                        "in API Management."
                        for item in publication.resources
                        if not item.created_by_mosaic
                    ),
                ],
                actor_object_id=actor.object_id,
                mcp_access_snapshot=publication.applied_access,
            )
            await self._repository.save_publish_plan(plan)
            return plan

    async def unpublish(
        self, actor: Actor, publication_id: str, plan_id: str | None = None
    ) -> PublishRun:
        """Run a reviewed unpublish plan: exactly its steps, and only if it still matches."""

        if not plan_id:
            raise ConflictError(
                "Review what unpublishing removes before you unpublish. Plan the unpublish, check "
                "the resources and grants it lists, then unpublish that plan.",
                details={"id": publication_id, "reason": "planRequired"},
            )
        owner = new_id("publishrun")
        await self._repository.acquire_publication_lock(actor.tenant_id, publication_id, owner)
        self._active.add(publication_id)
        try:
            publication = await self.get_publication(actor, publication_id)
            gateway = await self._load_gateway(actor, publication.gateway_id)
            self._require_writable(gateway)
            plan = await self._repository.get_publish_plan(actor.tenant_id, plan_id)
            if (
                plan is None
                or plan.target != "mcp"
                or plan.operation != "unpublish"
                or plan.publication_id != publication.id
            ):
                raise NotFoundError("MCP unpublish plan was not found", details={"id": plan_id})
            if mcp_unpublish_digest(publication) != plan.digest:
                raise ConflictError(
                    "This MCP publication changed after its unpublish plan was made, so the plan "
                    "no longer says what unpublishing removes or who loses access. Review a new "
                    "plan before you unpublish.",
                    details={"planId": plan.id, "reason": "stalePlan"},
                )
            run = self._claim(actor, publication, plan, owner)
            await self._repository.save_publish_run(run)
            await self._mark_applying(publication, run.id)
            self._spawn(self._run_unpublish(publication, gateway, plan, run))
            return run
        except BaseException:
            self._active.discard(publication_id)
            await self._repository.release_publication_lock(
                actor.tenant_id, publication_id, owner
            )
            raise

    async def list_runs(self, actor: Actor, publication_id: str) -> list[PublishRun]:
        await self.get_publication(actor, publication_id)
        return [
            run
            for run in await self._repository.list_publish_runs(actor.tenant_id, publication_id)
            if run.target == "mcp"
        ]

    async def get_run(
        self, actor: Actor, publication_id: str, run_id: str
    ) -> PublishRun:
        run = await self._repository.get_publish_run(actor.tenant_id, run_id)
        if run is None or run.publication_id != publication_id or run.target != "mcp":
            raise NotFoundError("MCP publish run was not found", details={"id": run_id})
        return run

    async def recover_interrupted(
        self, actor: Actor, publication_id: str, *, run_id: str, confirm_quiesced: bool
    ) -> PublishRun:
        owner = await self._repository.get_publication_lock(actor.tenant_id, publication_id)
        if owner != run_id:
            raise ConflictError("Recovery must name the exact run holding this publication's lock")
        publication = await self._repository.get_mcp_publication(actor.tenant_id, publication_id)
        run = await self._repository.get_publish_run(actor.tenant_id, run_id)
        if run is None and run_id.startswith("mutation_"):
            diagnostic = PublishRun(
                id=run_id,
                tenant_id=actor.tenant_id,
                publication_id=publication_id,
                target="mcp",
                gateway_id=publication.gateway_id if publication else "",
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
                raise ConflictError("This process still has an active desired-state mutation")
            if publication:
                await self._repository.save_mcp_publication(
                    publication.model_copy(
                        update={
                            "last_plan_id": None,
                            "last_plan_digest": None,
                            "updated_at": utc_now(),
                        }
                    ),
                    self._audit(actor, "mcpPublication.mutationRecovered", publication_id),
                )
            diagnostic.completed_at = utc_now()
            await self._repository.save_publish_run(diagnostic)
            await self._repository.release_publication_lock(actor.tenant_id, publication_id, run_id)
            return diagnostic
        if run is None or publication is None or run.target != "mcp":
            raise ConflictError("The retained lock has no matching MCP publication/run evidence")
        if not confirm_quiesced:
            return run
        if publication_id in self._active:
            raise ConflictError("This process still has an active writer.")
        gateway = await self._load_gateway(actor, publication.gateway_id)
        self._require_writable(gateway)
        client = self._client_factory(ApimResourceId.parse(gateway.azure_resource_id))
        writer = self._writer_factory(ApimResourceId.parse(gateway.azure_resource_id))
        resources = list(publication.resources)
        for step in run.steps:
            if not step.created_by_mosaic or step.action == PublishAction.DELETE:
                continue
            item = self._resource_for_step(publication, step)
            if await self._exists(client, publication, item):
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
        actual = publication.model_copy(update={"resources": resources})
        journal = self._recovery_journal(actual, writer, run, list(run.steps), resources)
        denied, errors = await self._establish_deny(actual, client, writer, journal)
        if not denied:
            await self._interrupted(
                actual,
                run,
                "Explicit recovery could not confirm complete denial: " + "; ".join(errors),
            )
            return await self.get_run(actor, publication_id, run.id)
        snapshot = run.mcp_access_snapshot or publication.applied_access
        safe = (
            _disabled_snapshot(snapshot).model_copy(
                update={
                    "version": max(
                        snapshot.version,
                        publication.applied_access.version if publication.applied_access else 0,
                    )
                    + 1
                }
            )
            if snapshot
            else None
        )
        message = (
            "Operator-confirmed quiescence: MCP runtime access is denied. Re-plan and review "
            "before restoring any grants."
        )
        await self._record_state(
            publication,
            status=PublicationStatus.FAILED,
            resources=resources,
            run_id=run.id,
            error=message,
            applied=False,
            access_snapshot=safe,
            access_state="failed",
        )
        recovered = run.model_copy(
            update={
                "status": PublishRunStatus.INTERRUPTED,
                "errors": [*run.errors, message],
                "completed_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
        await self._repository.save_publish_run(recovered)
        current = await self.get_publication(actor, publication_id)
        await self._repository.save_mcp_publication(
            current, self._audit(actor, "mcpPublication.recovered", publication_id)
        )
        await self._release_run(publication, run)
        return recovered

    async def reap_stale_publish_runs(self, tenant_id: str) -> int:
        stale = await self._repository.list_unfinished_publish_runs(tenant_id)
        active = set(self._active)
        reaped = 0
        for run in stale:
            if run.target != "mcp" or run.publication_id in active:
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
            publication = await self._repository.get_mcp_publication(tenant_id, run.publication_id)
            if publication and publication.status == PublicationStatus.APPLYING:
                await self._repository.record_mcp_publication_state(
                    publication.model_copy(
                        update={
                            "status": PublicationStatus.FAILED,
                            "last_error": STALE_RUN_MESSAGE,
                            "access_state": "unknown",
                            "updated_at": completed,
                        }
                    )
                )
            reaped += 1
        return reaped

    async def wait_for_idle(self) -> None:
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        if self._task_failures:
            error = self._task_failures.pop(0)
            self._task_failures.clear()
            raise error

    async def _load_gateway(self, actor: Actor, gateway_id: str) -> Gateway:
        gateway = await self._repository.get_gateway(actor.tenant_id, gateway_id)
        if gateway is None:
            raise NotFoundError("Gateway was not found", details={"id": gateway_id})
        return gateway

    async def _load_endpoint(self, actor: Actor, endpoint_id: str) -> McpEndpoint:
        endpoint = await self._endpoints.get_endpoint(actor.tenant_id, endpoint_id)
        if endpoint is None:
            raise NotFoundError("MCP endpoint was not found", details={"id": endpoint_id})
        return endpoint

    @staticmethod
    def _require_writable(gateway: Gateway) -> None:
        if gateway.management_mode != ManagementMode.MANAGE:
            raise ConflictError("This gateway is in observe mode. Switch it to managed first.")
        if not gateway.access.can_write:
            raise ConflictError("MOSAIC cannot write to this gateway.")

    @staticmethod
    def _validate_endpoint(endpoint: McpEndpoint) -> None:
        if endpoint.auth_mode == McpAuthMode.API_KEY:
            raise ValidationError(
                "MOSAIC can't forward an API key to an MCP server yet; register it with managed "
                "identity or no authentication."
            )
        if endpoint.auth_mode == McpAuthMode.MANAGED_IDENTITY and not endpoint.resource_audience:
            raise ValidationError("Managed identity MCP endpoints need a resource audience.")
        if endpoint.capabilities.transport_type == McpTransportType.SSE:
            raise ValidationError("MOSAIC can publish only streamable MCP servers.")
        if endpoint.status == McpEndpointStatus.UNSUPPORTED_TRANSPORT:
            raise ValidationError("This MCP server uses an unsupported transport.")

    async def _materialize_mcp_server(self, actor: Actor, publication: McpPublication) -> McpServer:
        existing = await self._repository.get_mcp_server(actor.tenant_id, publication.mcp_server_id)
        if existing and existing.publication_id not in {None, publication.id}:
            raise ConflictError("This MCP server already belongs to a different publication")
        endpoint = await self._load_endpoint(actor, publication.mcp_endpoint_id)
        changes: dict[str, Any] = {
            "publication_id": publication.id,
            "display_name": publication.display_name,
            "path": publication.api_path,
            "protocols": ["https"],
            "kind": McpServerKind.PASSTHROUGH,
            "transport_type": McpTransportType.STREAMABLE,
            "endpoints": [McpServerRoute(name="message", uri_template="/mcp")],
            "tool_count": endpoint.inventory.tools,
            "subscription_required": False,
            "selection": "manual",
            "imported_by": actor.object_id,
            "updated_at": utc_now(),
        }
        if existing:
            record = existing.model_copy(update=changes)
        else:
            record = McpServer(
                id=publication.mcp_server_id,
                tenant_id=publication.tenant_id,
                gateway_id=publication.gateway_id,
                api_name=publication.api_name,
                service_url=None,
                product_names=[],
                **changes,
            )
        return await self._repository.save_mcp_server(
            record, self._audit(actor, "mcpServer.publicationLinked", record.id, "mcpServer")
        )

    async def _reject_desired_collisions(
        self, actor: Actor, publication: McpPublication, gateway: Gateway
    ) -> None:
        names = {
            publication.api_name,
            publication.backend_name,
            publication.fragment_name,
            publication.metadata_api_name,
        }
        for model_publication in await self._repository.list_publications(
            actor.tenant_id, gateway_id=gateway.id
        ):
            if (
                model_publication.api_name in names
                or model_publication.backend_name in names
                or model_publication.fragment_name in names
            ):
                raise ConflictError("A model publication already uses one of these APIM names.")
            if (
                model_publication.api_path.strip("/").casefold()
                == publication.api_path.strip("/").casefold()
            ):
                raise ConflictError("A model publication already uses this API path.")
        for mcp_publication in await self._repository.list_mcp_publications(
            actor.tenant_id, gateway_id=gateway.id
        ):
            if mcp_publication.id == publication.id:
                continue
            if (
                mcp_publication.api_name in names
                or mcp_publication.backend_name in names
                or mcp_publication.fragment_name in names
                or mcp_publication.metadata_api_name in names
            ):
                raise ConflictError("An MCP publication already uses one of these APIM names.")
            if (
                mcp_publication.api_path.strip("/").casefold()
                == publication.api_path.strip("/").casefold()
            ):
                raise ConflictError("An MCP publication already uses this API path.")
        existing_server = await self._repository.get_mcp_server(
            actor.tenant_id, publication.mcp_server_id
        )
        if existing_server and existing_server.publication_id != publication.id:
            raise ConflictError("An MCP server record with this deterministic ID already exists.")
        observed = await self._repository.list_observed(
            ObservedApi, actor.tenant_id, gateway.id, "observedApi"
        )
        paths = {
            publication.api_path.strip("/").casefold(),
            mcp_metadata_api_path(publication.api_path).strip("/").casefold(),
        }
        clash = next(
            (item for item in observed if item.path.strip("/").casefold() in paths),
            None,
        )
        if clash:
            raise ConflictError(
                "Another API in this gateway is already served at a planned MCP path.",
                details={"conflictingApi": clash.name},
            )

    async def _reject_live_collisions(
        self, actor: Actor, publication: McpPublication, gateway: Gateway
    ) -> None:
        resource = ApimResourceId.parse(gateway.azure_resource_id)
        client = self._client_factory(resource)
        for item in _desired_resources(publication):
            if await self._exists(client, publication, item) and not self._owns(
                publication, item.kind, item.name
            ):
                raise ConflictError(
                    "MOSAIC will not replace API Management resources it does not own.",
                    details={"kind": str(item.kind), "name": item.name},
                )
        owned_apis = {
            item.name
            for item in publication.resources
            if item.kind == PublishedResourceKind.API and item.created_by_mosaic
        }
        observed = await self._repository.list_observed(
            ObservedApi, actor.tenant_id, gateway.id, "observedApi"
        )
        paths = {
            publication.api_path.strip("/").casefold(),
            mcp_metadata_api_path(publication.api_path).strip("/").casefold(),
        }
        clash = next(
            (
                item
                for item in observed
                if item.path.strip("/").casefold() in paths and item.name not in owned_apis
            ),
            None,
        )
        if clash:
            raise ConflictError(
                "Another API in this gateway is already served at a planned MCP path.",
                details={"conflictingApi": clash.name},
            )

    async def _access_snapshot(
        self, publication: McpPublication, endpoint: McpEndpoint
    ) -> tuple[McpAccessSnapshot, list[str]]:
        if not self._runtime_client_id:
            raise ConflictError("The MCP runtime client ID is not configured.")
        warnings = [
            "This is a publication-wide batch: every direct and security-group grant listed in "
            "this snapshot will be applied together. MOSAIC groups remain desired-state only."
        ]
        grants: list[McpAccessGrant] = []
        pools: dict[str, AppliedCostCenterPool] = {}
        entitlements = await self._entitlements.list_entitlements(
            publication.tenant_id, resource_id=publication.mcp_server_id
        )
        book = await load_book(self._cost_centers, publication.tenant_id)
        saw_security_group_grant = False
        for entitlement in entitlements:
            if entitlement.resource.kind != "mcpServer":
                continue
            if entitlement.subject.kind == EntitlementSubjectKind.GROUP:
                warnings.append(
                    f"Grant {entitlement.id} names a MOSAIC group and will not be enforced."
                )
                continue
            if entitlement.enforcement is not None and entitlement.enforcement.tokens is not None:
                warnings.append(
                    f"Grant {entitlement.id} sets token limits; MCP servers are limited by calls, "
                    "so it is excluded from runtime access."
                )
                continue
            principal = await self._directory.get_principal(
                publication.tenant_id, entitlement.subject.id
            )
            if principal is None or entitlement.subject.kind != subject_kind_for(principal.kind):
                warnings.append(
                    f"Grant {entitlement.id} has a missing or mismatched principal; it is excluded "
                    "from runtime access."
                )
                continue
            intent = cost_center_intent(entitlement, principal, book)
            if intent is None:
                warnings.append(
                    f"Grant {entitlement.id}'s cost center no longer exists; it is excluded from "
                    "runtime access."
                )
                continue
            charged = book.get(entitlement.cost_center_id)
            if charged is not None and charged.recheck_pending(
                entitlement.id, entitlement.subject.id
            ):
                # Its subject may have lost the right to charge the cost center, and MOSAIC
                # hasn't finished checking: it stays out rather than outlive that right.
                warnings.append(
                    f"Grant {entitlement.id} is waiting for MOSAIC to check that its subject "
                    f"may still charge {charged.name} ({charged.code}); it is excluded from "
                    "runtime access until then. Open the cost center and choose Check grants "
                    "again."
                )
                continue
            is_security_group = entitlement.subject.kind == EntitlementSubjectKind.SECURITY_GROUP
            saw_security_group_grant = saw_security_group_grant or is_security_group
            grants.append(
                McpAccessGrant(
                    entitlement_id=entitlement.id,
                    subject=entitlement.subject,
                    object_id=(
                        principal.object_id.casefold()
                        if is_security_group
                        else principal.object_id
                    ),
                    display_name=principal.label or principal.object_id,
                    enabled=entitlement.enabled,
                    enforcement=effective_enforcement(entitlement, intent),
                    intent_digest=entitlement_intent_digest(entitlement, principal, intent),
                    cost_center_id=intent.cost_center_id,
                    cost_center_code=intent.code,
                    default_cost_center=intent.default_for_subject,
                    granted_at=entitlement.created_at,
                )
            )
            if intent.pool is not None and intent.pool.monthly_calls is not None:
                pools[intent.cost_center_id] = AppliedCostCenterPool(
                    cost_center_id=intent.cost_center_id,
                    cost_center_code=intent.code,
                    monthly_calls=intent.pool.monthly_calls,
                )
        if saw_security_group_grant and not self._security_group_claims:
            warnings.append(
                "Group claims aren't configured for this deployment, so the gateway can't match "
                "grants to security groups."
            )
        group_grants = [
            grant
            for grant in grants
            if grant.enabled and grant.subject.kind == EntitlementSubjectKind.SECURITY_GROUP
        ]
        if len(group_grants) >= 2:
            ordered = sorted(
                group_grants,
                key=lambda grant: grant_precedence_key(grant.enforcement, grant.entitlement_id),
            )
            warnings.append(
                "Members of more than one of these groups get only the most generous grant: "
                f"{', '.join(grant.display_name for grant in ordered)}. "
                f"{ordered[0].display_name} wins."
            )
        included = {grant.entitlement_id for grant in grants}
        if publication.applied_access:
            grants.extend(
                grant.model_copy(update={"enabled": False})
                for grant in publication.applied_access.grants
                if grant.entitlement_id not in included
            )
        grants.sort(key=lambda grant: grant.entitlement_id)
        model_caller = await self._model_caller(publication, warnings)
        return (
            McpAccessSnapshot(
                version=publication.applied_access.version + 1 if publication.applied_access else 1,
                audience=self._runtime_client_id,
                grants=grants,
                pools=sorted(pools.values(), key=lambda pool: pool.cost_center_id),
                model_caller=model_caller,
            ),
            warnings,
        )

    async def _model_caller(
        self, publication: McpPublication, warnings: list[str]
    ) -> McpModelCaller | None:
        """The application the server calls models as, as the policy compiles it. See ADR 0025.

        One MOSAIC can no longer name as an application compiles to nothing, with a warning:
        attribution is never a reason to hold back the server's own access.
        """

        if publication.model_caller_id is None:
            return None
        principal = await self._directory.get_principal(
            publication.tenant_id, publication.model_caller_id
        )
        if principal is None or not _can_call_models(principal):
            warnings.append(
                "The application this MCP server calls models as is no longer one MOSAIC can "
                "name, so its model calls won't be attributed to the people it serves. Choose it "
                "again, or clear it."
            )
            return None
        caller = McpModelCaller(
            principal_id=principal.id,
            object_id=principal.object_id.lower(),
            display_name=principal.label or principal.object_id,
        )
        if not await self._has_model_grant_here(publication, principal):
            warnings.append(
                f"{caller.display_name} has no enabled direct grant on a model this gateway "
                "publishes. Unless a security group gives it access, its model calls will be "
                "refused. MOSAIC attributes only calls made through this gateway."
            )
        return caller

    async def _has_model_grant_here(
        self, publication: McpPublication, principal: Principal
    ) -> bool:
        granted = {
            entitlement.resource.id
            for entitlement in await self._entitlements.list_entitlements(
                publication.tenant_id, subject_id=principal.id
            )
            if entitlement.enabled and entitlement.resource.kind == "modelApi"
        }
        if not granted:
            return False
        return any(
            model.id in granted
            for model in await self._repository.list_model_apis(
                publication.tenant_id, gateway_id=publication.gateway_id
            )
        )

    @staticmethod
    def _warnings(
        publication: McpPublication,
        gateway: Gateway,
        endpoint: McpEndpoint,
        snapshot: McpAccessSnapshot,
    ) -> list[str]:
        warnings = [
            "API Management must log 0 bytes of response bodies for this API, or streaming breaks.",
            f"VS Code and other interactive clients need consent for api://{snapshot.audience}/Mcp.Invoke.",
        ]
        if endpoint.status != McpEndpointStatus.CONNECTED:
            warnings.append("This MCP endpoint is not currently connected.")
        if endpoint.auth_mode == McpAuthMode.MANAGED_IDENTITY:
            warnings.append(
                f"The gateway's managed identity needs access to {endpoint.resource_audience}."
            )
        if gateway.capabilities.gateway_url is None:
            warnings.append(
                "The gateway URL is unknown, so clients cannot be shown a final MCP URL."
            )
        return warnings

    async def _steps(
        self,
        publication: McpPublication,
        endpoint: McpEndpoint,
        client: ApimClient,
        resource: ApimResourceId,
    ) -> list[PublishPlanStep]:
        base: dict[tuple[PublishedResourceKind, str], PublishPlanStep] = {}
        for item in _desired_resources(publication):
            exists = await self._exists(client, publication, item)
            if exists and not self._owns(publication, item.kind, item.name):
                raise ConflictError(
                    "MOSAIC does not own an existing MCP publication resource.",
                    details={"kind": str(item.kind), "name": item.name},
                )
            base[(item.kind, item.name)] = PublishPlanStep(
                kind=item.kind,
                name=item.name,
                action=PublishAction.UPDATE if exists else PublishAction.CREATE,
                reason=self._reason(publication, item, existed=exists),
                resource_id=f"{resource.canonical}/{item.segment}",
                existed=exists,
                stage="prepare",
            )
        # First of all: every governed policy reads the gateway's blocked list. See ADR 0023.
        steps: list[PublishPlanStep] = [
            BlockedListGate.plan_step(
                resource,
                exists=await client.get_named_value(BLOCKED_COST_CENTERS_NAMED_VALUE) is not None,
            )
        ]
        mcp_api = base[(PublishedResourceKind.API, publication.api_name)]
        mcp_policy = base[(PublishedResourceKind.API_POLICY, publication.api_name)]
        if mcp_api.existed and await self._needs_backend_deny(client, publication, endpoint):
            steps.append(
                mcp_policy.model_copy(
                    update={
                        "stage": "prepare",
                        "reason": "Deny calls before changing the MCP backend.",
                    }
                )
            )
        steps.append(base[(PublishedResourceKind.BACKEND, publication.backend_name)])
        steps.append(
            base[(PublishedResourceKind.POLICY_FRAGMENT, publication.fragment_name)].model_copy(
                update={"stage": "policy"}
            )
        )
        if not mcp_api.existed:
            steps.append(
                mcp_api.model_copy(
                    update={
                        "stage": "prepare",
                        "reason": (
                            "Create the MCP API requiring a subscription until its policy is "
                            "installed."
                        ),
                    }
                )
            )
        steps.append(
            mcp_policy.model_copy(
                update={"stage": "policy", "reason": "Install the reviewed MCP access policy."}
            )
        )
        steps.append(base[(PublishedResourceKind.API, publication.metadata_api_name)])
        steps.append(base[(PublishedResourceKind.API_OPERATION, "metadata")])
        steps.append(
            base[(PublishedResourceKind.API_POLICY, publication.metadata_api_name)].model_copy(
                update={"stage": "policy"}
            )
        )
        steps.append(
            mcp_api.model_copy(
                update={
                    "stage": "activate",
                    "reason": (
                        "Allow token-authenticated MCP calls after installing the reviewed policy."
                    ),
                }
            )
        )
        return steps

    @staticmethod
    def _reason(publication: McpPublication, item: _Resource, *, existed: bool) -> str:
        subject = {
            PublishedResourceKind.BACKEND: "the backend pointing at the registered MCP server",
            PublishedResourceKind.POLICY_FRAGMENT: "the MOSAIC MCP enforcement fragment",
            PublishedResourceKind.API: (
                "the MCP API"
                if item.name == publication.api_name
                else "the protected resource metadata API"
            ),
            PublishedResourceKind.API_OPERATION: "the protected resource metadata operation",
            PublishedResourceKind.API_POLICY: (
                "the MCP API policy"
                if item.name == publication.api_name
                else "the protected resource metadata policy"
            ),
            PublishedResourceKind.PRODUCT: "unused MCP product",
            PublishedResourceKind.PRODUCT_API: "unused MCP product link",
            PublishedResourceKind.SUBSCRIPTION: "unused MCP subscription",
        }[item.kind]
        return f"{'Replace' if existed else 'Create'} {subject}."

    async def _needs_backend_deny(
        self, client: ApimClient, publication: McpPublication, endpoint: McpEndpoint
    ) -> bool:
        api = await client.get_mcp_api(publication.api_name)
        backend = await client.get_backend(publication.backend_name)
        if api is None:
            return False
        if backend is None:
            return True
        properties = backend.get("properties") if isinstance(backend, dict) else None
        return not isinstance(properties, dict) or properties.get("url") != str(endpoint.endpoint)

    @staticmethod
    def _owns(publication: McpPublication, kind: PublishedResourceKind, name: str) -> bool:
        return any(
            item.kind == kind and item.name == name and item.created_by_mosaic
            for item in publication.resources
        )

    async def _exists(
        self, client: ApimClient, publication: McpPublication, item: _Resource
    ) -> bool:
        match item.kind:
            case PublishedResourceKind.POLICY_FRAGMENT:
                return await client.get_policy_fragment_resource(item.name) is not None
            case PublishedResourceKind.BACKEND:
                return await client.get_backend(item.name) is not None
            case PublishedResourceKind.API:
                return await client.get_mcp_api(item.name) is not None
            case PublishedResourceKind.API_OPERATION:
                api = item.target_api or publication.metadata_api_name
                return await client.get_api_operation(api, item.name) is not None
            case PublishedResourceKind.API_POLICY:
                api = item.target_api or item.name
                if api == publication.api_name:
                    return await client.get_mcp_api_policy(api) is not None
                return await client.get_api_policy(api) is not None
            case _:
                return False

    def _claim(
        self, actor: Actor, publication: McpPublication, plan: PublishPlan, owner: str
    ) -> PublishRun:
        return PublishRun(
            id=owner,
            tenant_id=publication.tenant_id,
            publication_id=publication.id,
            target="mcp",
            gateway_id=publication.gateway_id,
            plan_id=plan.id,
            plan_digest=plan.digest,
            actor_object_id=actor.object_id,
            mcp_access_snapshot=plan.mcp_access_snapshot,
        )

    def _spawn(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task: asyncio.Task[None] = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._task_finished)

    def _task_finished(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            self._task_failures.append(error)

    async def _assert_lock(self, publication: McpPublication, run: PublishRun) -> None:
        if (
            await self._repository.get_publication_lock(publication.tenant_id, publication.id)
            != run.id
        ):
            raise ConflictError("The MCP publication write lock is no longer owned by this run")

    async def _release_run(self, publication: McpPublication, run: PublishRun) -> None:
        await self._repository.release_publication_lock(
            publication.tenant_id, publication.id, run.id
        )

    async def _progress(
        self,
        publication: McpPublication,
        run: PublishRun,
        results: list[PublishStepResult],
        resources: list[PublishedResource],
    ) -> None:
        await self._assert_lock(publication, run)
        await self._repository.save_publish_run(run.model_copy(update={"steps": list(results)}))
        await self._record_state(
            publication,
            status=PublicationStatus.APPLYING,
            resources=resources,
            run_id=run.id,
            error=None,
            applied=False,
            access_state="applying",
        )

    async def _interrupted(
        self, publication: McpPublication, run: PublishRun, message: str
    ) -> None:
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
            current_publication = await self._repository.get_mcp_publication(
                publication.tenant_id, publication.id
            )
            if current_publication and current_publication.last_run_id == run.id:
                await self._repository.record_mcp_publication_state(
                    current_publication.model_copy(
                        update={
                            "access_state": "unknown",
                            "status": PublicationStatus.FAILED,
                            "last_error": message,
                            "last_run_id": run.id,
                            "updated_at": utc_now(),
                        }
                    )
                )
        except Exception:
            logger.exception("mcp_publish_interruption_not_recorded", publication_id=publication.id)

    async def _run_apply(
        self,
        publication: McpPublication,
        gateway: Gateway,
        endpoint: McpEndpoint,
        plan: PublishPlan,
        policy: McpPolicyDocuments,
        run: PublishRun,
        resource: ApimResourceId,
    ) -> None:
        started = utc_now()
        client = self._client_factory(resource)
        writer = self._writer_factory(resource)
        results: list[PublishStepResult] = []
        owned = list(publication.resources)
        failure: str | None = None
        try:
            try:
                for step in plan.steps:
                    await self._assert_lock(publication, run)
                    result = PublishStepResult(
                        kind=step.kind,
                        name=step.name,
                        action=step.action,
                        resource_id=step.resource_id,
                        stage=step.stage,
                    )
                    results.append(result)
                    if is_blocked_list(step.kind, step.name):
                        # Before any policy that reads it. MOSAIC keeps it for the gateway, so the
                        # publication never owns it, and no recovery or unpublish deletes it.
                        try:
                            await ensure_blocked_list(
                                self._blocked_list, client, writer, publication.tenant_id
                            )
                        except Exception as error:
                            result.status = PublishStepStatus.FAILED
                            result.error = str(error)
                            failure = f"{step.kind} {step.name}: {error}"
                            break
                        result.status = PublishStepStatus.SUCCEEDED
                        await self._progress(publication, run, results, owned)
                        continue
                    item = self._resource_for_step(publication, step)
                    write_started = False
                    try:
                        exists = await self._exists(client, publication, item)
                        current = publication.model_copy(update={"resources": owned})
                        if exists and not self._owns(current, step.kind, step.name):
                            raise ConflictError(
                                f"{step.kind} {step.name} appeared without MOSAIC ownership."
                            )
                        result.created_by_mosaic = not exists
                        await self._progress(publication, run, results, owned)
                        write_started = True
                        await self._write_step(writer, publication, endpoint, policy, step)
                        result.status = PublishStepStatus.SUCCEEDED
                        owned = _merge_resources(
                            owned,
                            [
                                PublishedResource(
                                    kind=result.kind,
                                    name=result.name,
                                    resource_id=result.resource_id,
                                    created_by_mosaic=result.created_by_mosaic,
                                )
                            ],
                        )
                        await self._progress(publication, run, results, owned)
                    except Exception as error:
                        result.status = PublishStepStatus.FAILED
                        result.error = str(error)
                        failure = f"{step.kind} {step.name}: {error}"
                        if write_started and result.created_by_mosaic:
                            try:
                                if await self._exists(client, publication, item):
                                    owned = _merge_resources(
                                        owned,
                                        [
                                            PublishedResource(
                                                kind=result.kind,
                                                name=result.name,
                                                resource_id=result.resource_id,
                                                created_by_mosaic=True,
                                            )
                                        ],
                                    )
                            except Exception:
                                pass
                        break
                if failure is None:
                    await self._finish_success(
                        publication, run, results, started, access_snapshot=plan.mcp_access_snapshot
                    )
            except Exception as error:
                failure = str(error)
            if failure is not None:
                await self._failure(
                    publication, client, writer, run, results, owned, started, failure
                )
            else:
                await self._release_run(publication, run)
        except asyncio.CancelledError:
            await self._interrupted(publication, run, STALE_RUN_MESSAGE)
            raise
        except Exception as error:
            await self._interrupted(
                publication, run, f"Apply outcome could not be durably established: {error}"
            )
            raise
        finally:
            self._active.discard(publication.id)

    async def _write_step(
        self,
        writer: ApimWriter,
        publication: McpPublication,
        endpoint: McpEndpoint,
        policy: McpPolicyDocuments,
        step: PublishPlanStep,
    ) -> None:
        if step.kind == PublishedResourceKind.API_POLICY and step.stage == "prepare":
            await writer.put_mcp_api_policy(publication.api_name, DENY_ALL_POLICY)
        elif step.kind == PublishedResourceKind.BACKEND:
            await writer.put_backend(
                publication.backend_name,
                url=str(endpoint.endpoint),
                title=f"MOSAIC backend for {publication.display_name}",
            )
        elif step.kind == PublishedResourceKind.POLICY_FRAGMENT:
            await writer.put_policy_fragment(
                publication.fragment_name,
                policy.fragment_xml,
                description=f"MOSAIC MCP enforcement for {publication.display_name}",
            )
        elif step.kind == PublishedResourceKind.API and step.name == publication.api_name:
            await writer.put_mcp_api(
                publication.api_name,
                display_name=publication.display_name,
                path=publication.api_path,
                backend_name=publication.backend_name,
                subscription_required=step.stage != "activate",
                description=f"Published by MOSAIC for {publication.display_name}.",
            )
        elif step.kind == PublishedResourceKind.API_POLICY and step.name == publication.api_name:
            await writer.put_mcp_api_policy(publication.api_name, policy.api_policy_xml)
        elif step.kind == PublishedResourceKind.API and step.name == publication.metadata_api_name:
            await writer.put_api(
                publication.metadata_api_name,
                display_name=f"{publication.display_name} protected resource metadata",
                path=mcp_metadata_api_path(publication.api_path),
                subscription_required=False,
                description=f"Protected resource metadata for {publication.display_name}.",
            )
        elif step.kind == PublishedResourceKind.API_OPERATION:
            await writer.put_api_operation(
                publication.metadata_api_name,
                "metadata",
                display_name="Protected resource metadata",
                method="GET",
                url_template="/mcp",
                description="Return OAuth protected resource metadata for this MCP server.",
            )
        elif (
            step.kind == PublishedResourceKind.API_POLICY
            and step.name == publication.metadata_api_name
        ):
            await writer.put_api_policy(publication.metadata_api_name, policy.metadata_policy_xml)

    async def _establish_deny(
        self,
        publication: McpPublication,
        client: ApimClient,
        writer: ApimWriter,
        journal: RecoveryJournal | None = None,
    ) -> tuple[bool, list[str]]:
        errors: list[str] = []
        if not self._owns(publication, PublishedResourceKind.API, publication.api_name):
            return True, errors
        try:
            if await client.get_mcp_api(publication.api_name) is None:
                return True, errors
            existed = await client.get_mcp_api_policy(publication.api_name) is not None
            await writer.put_mcp_api_policy(publication.api_name, DENY_ALL_POLICY)
            if journal:
                await journal(
                    PublishedResourceKind.API_POLICY, publication.api_name, not existed, "policy"
                )
            return True, errors
        except Exception as error:
            errors.append(f"Installing the deny-all MCP API policy failed: {error}")
        if self._owns(
            publication, PublishedResourceKind.POLICY_FRAGMENT, publication.fragment_name
        ):
            try:
                await writer.put_policy_fragment(
                    publication.fragment_name,
                    DENY_ALL_FRAGMENT,
                    description="MOSAIC MCP fail-closed recovery",
                )
                if journal:
                    await journal(
                        PublishedResourceKind.POLICY_FRAGMENT,
                        publication.fragment_name,
                        False,
                        "policy",
                    )
                return True, errors
            except Exception as error:
                errors.append(f"Installing the deny-all MCP fragment failed: {error}")
        return False, errors

    async def _failure(
        self,
        publication: McpPublication,
        client: ApimClient,
        writer: ApimWriter,
        run: PublishRun,
        results: list[PublishStepResult],
        resources: list[PublishedResource],
        started: datetime,
        failure: str,
    ) -> None:
        target = run.mcp_access_snapshot
        if target is None:
            raise RuntimeError("An MCP run must record its reviewed access snapshot")
        await self._assert_lock(publication, run)
        actual = publication.model_copy(update={"resources": resources})
        journal = self._recovery_journal(publication, writer, run, results, resources)
        denied, recovery_errors = await self._establish_deny(actual, client, writer, journal)
        safe = _disabled_snapshot(target)
        await self._record_state(
            publication,
            status=PublicationStatus.FAILED,
            resources=resources,
            run_id=run.id,
            error=failure,
            applied=False,
            access_snapshot=safe if denied else None,
            access_state="failed" if denied else "unknown",
        )
        errors = [
            failure,
            *recovery_errors,
            (
                "Runtime access is denied. No grants were restored automatically; re-plan and "
                "apply after fixing the failure."
                if denied
                else "Runtime denial could not be confirmed. The durable lock is retained; stop "
                "the original writer and complete explicit recovery."
            ),
        ]
        await self._finish_run(
            run,
            PublishRunStatus.FAILED if denied else PublishRunStatus.INTERRUPTED,
            started,
            results=results,
            errors=errors,
            orphaned=resources if not denied else [],
        )
        if denied:
            await self._release_run(publication, run)

    async def _run_unpublish(
        self, publication: McpPublication, gateway: Gateway, plan: PublishPlan, run: PublishRun
    ) -> None:
        started = utc_now()
        resource = ApimResourceId.parse(gateway.azure_resource_id)
        client = self._client_factory(resource)
        writer = self._writer_factory(resource)
        results: list[PublishStepResult] = []
        remaining = list(publication.resources)
        errors: list[str] = []
        denied_snapshot = (
            _disabled_snapshot(publication.applied_access).model_copy(
                update={"version": publication.applied_access.version + 1}
            )
            if publication.applied_access
            else None
        )
        try:
            await self._assert_lock(publication, run)
            denied, guard_errors = await self._establish_deny(publication, client, writer)
            errors.extend(guard_errors)
            if not denied:
                raise ConflictError("Unpublish could not establish a fail-closed runtime policy")
            for step in plan.steps:
                await self._assert_lock(publication, run)
                result = PublishStepResult(
                    kind=step.kind,
                    name=step.name,
                    action=PublishAction.DELETE,
                    resource_id=step.resource_id,
                )
                try:
                    await self._progress(publication, run, [*results, result], remaining)
                    await self._remove(writer, publication, step.kind, step.name)
                except Exception as error:
                    result.status = PublishStepStatus.FAILED
                    result.error = str(error)
                    errors.append(f"{step.kind} {step.name}: {error}")
                else:
                    result.status = PublishStepStatus.SUCCEEDED
                    remaining = [
                        item
                        for item in remaining
                        if not (item.kind == step.kind and item.name == step.name)
                    ]
                results.append(result)
                await self._progress(publication, run, results, remaining)
                if result.status == PublishStepStatus.FAILED:
                    break
        except asyncio.CancelledError:
            await self._interrupted(publication, run, STALE_RUN_MESSAGE)
            self._active.discard(publication.id)
            raise
        except Exception as error:
            await self._interrupted(publication, run, f"Unpublish outcome is unknown: {error}")
            self._active.discard(publication.id)
            raise
        try:
            succeeded = not errors
            await self._record_state(
                publication,
                status=PublicationStatus.DRAFT if succeeded else PublicationStatus.FAILED,
                resources=remaining,
                run_id=run.id,
                error="; ".join(errors) or None,
                applied=False,
                unpublished=succeeded,
                access_snapshot=denied_snapshot,
                access_state="applied" if succeeded else "failed",
            )
            if succeeded:
                await self._project_bindings(publication, None, run)
            await self._finish_run(
                run,
                PublishRunStatus.SUCCEEDED if succeeded else PublishRunStatus.FAILED,
                started,
                results=results,
                errors=errors,
                orphaned=[] if succeeded else remaining,
            )
            await self._release_run(publication, run)
        except asyncio.CancelledError:
            await self._interrupted(publication, run, STALE_RUN_MESSAGE)
            raise
        except Exception as error:
            await self._interrupted(publication, run, f"Unpublish result was not recorded: {error}")
            raise
        finally:
            self._active.discard(publication.id)

    def _recovery_journal(
        self,
        publication: McpPublication,
        writer: ApimWriter,
        run: PublishRun,
        results: list[PublishStepResult],
        resources: list[PublishedResource],
    ) -> RecoveryJournal:
        async def record(kind: PublishedResourceKind, name: str, created: bool, stage: str) -> None:
            item = next(
                (
                    desired
                    for desired in _desired_resources(publication)
                    if desired.kind == kind and desired.name == name
                ),
                _Resource(kind, name, f"apis/{publication.api_name}/policies/policy"),
            )
            resource_id = writer.resource_id(item.segment)
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
                        kind=kind,
                        name=name,
                        resource_id=resource_id,
                        created_by_mosaic=created,
                    )
                ],
            )
            await self._progress(publication, run, results, resources)

        return record

    async def _finish_success(
        self,
        publication: McpPublication,
        run: PublishRun,
        results: list[PublishStepResult],
        started: datetime,
        *,
        access_snapshot: McpAccessSnapshot | None,
    ) -> None:
        await self._assert_lock(publication, run)
        applied_at = utc_now()
        resources = _merge_resources(
            publication.resources,
            [
                PublishedResource(
                    kind=result.kind,
                    name=result.name,
                    resource_id=result.resource_id,
                    created_by_mosaic=result.created_by_mosaic,
                    applied_at=applied_at,
                )
                for result in results
                if result.status == PublishStepStatus.SUCCEEDED
                # The gateway's blocked list is the gateway's, not this publication's.
                and not is_blocked_list(result.kind, result.name)
            ],
        )
        await self._record_state(
            publication,
            status=PublicationStatus.PUBLISHED,
            resources=resources,
            run_id=run.id,
            error=None,
            applied=True,
            access_snapshot=access_snapshot,
            access_state="applied",
        )
        await self._project_bindings(publication, access_snapshot, run)
        await self._finish_run(
            run, PublishRunStatus.SUCCEEDED, started, results=results, errors=[], orphaned=[]
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
                    "rolled_back": False,
                    "orphaned_resources": orphaned,
                    "errors": errors,
                    "updated_at": completed,
                }
            )
        )

    async def _project_bindings(
        self,
        publication: McpPublication,
        snapshot: McpAccessSnapshot | None,
        run: PublishRun,
    ) -> None:
        """Record each grant's gateway binding once the publication's state is durable.

        Bindings only link usage to grants; the gateway already enforces the reviewed access. A
        storage error here is logged rather than failing the run, which would put a correctly
        published server behind the deny-all fragment. The next apply writes the bindings again.
        """
        grants = {grant.entitlement_id: grant for grant in snapshot.grants} if snapshot else {}
        try:
            records = await self._entitlements.list_entitlements(
                publication.tenant_id, resource_id=publication.mcp_server_id
            )
        except Exception:
            logger.exception(
                "mcp_binding_projection_failed", publication_id=publication.id, run_id=run.id
            )
            return
        actor = Actor(run.actor_object_id or "system:mcp-publishing", publication.tenant_id)
        for entitlement in records:
            grant = grants.get(entitlement.id)
            binding = entitlement.binding
            if grant and grant.enabled and snapshot:
                binding = EntitlementBinding(
                    gateway_id=publication.gateway_id,
                    apim_subscription_name=None,
                    counter_key_expression=mcp_counter_key_expression(publication, grant),
                    source=BindingSource.ORCHESTRATED,
                    bound_at=utc_now(),
                    attribution_key=mcp_grant_counter_identity(publication, grant),
                    attribution_per_member=grant.is_group_grant,
                )
            elif binding and binding.source == BindingSource.ORCHESTRATED:
                binding = None
            else:
                continue
            try:
                await self._entitlements.save_entitlement(
                    entitlement.model_copy(update={"binding": binding, "runtime": None}),
                    self._audit(
                        actor, "entitlement.runtimeProjected", entitlement.id, "entitlement"
                    ),
                )
            except Exception:
                logger.exception(
                    "mcp_binding_projection_failed",
                    publication_id=publication.id,
                    run_id=run.id,
                    entitlement_id=entitlement.id,
                )

    async def _mark_applying(self, publication: McpPublication, run_id: str) -> None:
        await self._record_state(
            publication,
            status=PublicationStatus.APPLYING,
            resources=publication.resources,
            run_id=run_id,
            error=None,
            applied=False,
            access_state="applying",
        )

    async def _record_state(
        self,
        publication: McpPublication,
        *,
        status: PublicationStatus,
        resources: list[PublishedResource],
        run_id: str | None,
        error: str | None,
        applied: bool,
        unpublished: bool = False,
        access_snapshot: McpAccessSnapshot | None = None,
        access_state: str | None = None,
    ) -> None:
        current = await self._repository.get_mcp_publication(publication.tenant_id, publication.id)
        if current is None:
            raise ConflictError("The MCP publication disappeared while applying")
        updates: dict[str, object] = {
            "status": status,
            "resources": resources,
            "last_error": error,
            "updated_at": utc_now(),
        }
        if run_id is not None:
            updates["last_run_id"] = run_id
        if applied:
            updates["last_applied_at"] = utc_now()
            updates["unpublished_at"] = None
        if unpublished:
            updates["unpublished_at"] = utc_now()
        if access_snapshot is not None:
            updates["applied_access"] = access_snapshot
        if access_state is not None:
            updates["access_state"] = access_state
        await self._repository.record_mcp_publication_state(current.model_copy(update=updates))

    async def _remove(
        self,
        writer: ApimWriter,
        publication: McpPublication,
        kind: PublishedResourceKind,
        name: str,
    ) -> None:
        match kind:
            case PublishedResourceKind.API if name == publication.api_name:
                await writer.delete_mcp_api(name)
            case PublishedResourceKind.API if name == publication.metadata_api_name:
                await writer.delete_api(name)
            case PublishedResourceKind.API_OPERATION:
                await writer.delete_api_operation(publication.metadata_api_name, name)
            case PublishedResourceKind.API_POLICY if name == publication.metadata_api_name:
                await writer.delete_api_policy(publication.metadata_api_name)
            case PublishedResourceKind.API_POLICY:
                await writer.delete_mcp_api_policy(publication.api_name)
            case PublishedResourceKind.POLICY_FRAGMENT:
                await writer.delete_policy_fragment(name)
            case PublishedResourceKind.BACKEND:
                await writer.delete_backend(name)
            case _:
                return

    def _unpublish_removals(self, publication: McpPublication) -> list[PublishedResource]:
        order = [
            (PublishedResourceKind.API, publication.api_name),
            (PublishedResourceKind.API, publication.metadata_api_name),
            (PublishedResourceKind.API_OPERATION, "metadata"),
            (PublishedResourceKind.API_POLICY, publication.metadata_api_name),
            (PublishedResourceKind.POLICY_FRAGMENT, publication.fragment_name),
            (PublishedResourceKind.BACKEND, publication.backend_name),
        ]
        rank = {key: index for index, key in enumerate(order)}
        return sorted(
            publication.created_resources(),
            key=lambda item: rank.get((item.kind, item.name), 99),
        )

    def _resource_for_step(
        self, publication: McpPublication, step: PublishPlanStep | PublishStepResult
    ) -> _Resource:
        return next(
            (
                item
                for item in _desired_resources(publication)
                if item.kind == step.kind and item.name == step.name
            ),
            _Resource(step.kind, step.name, step.resource_id.rsplit("/", 1)[-1]),
        )


def _is_guid(value: str) -> bool:
    parts = value.split("-")
    lengths = [8, 4, 4, 4, 12]
    return len(parts) == 5 and all(
        len(part) == length and all(char in "0123456789abcdefABCDEF" for char in part)
        for part, length in zip(parts, lengths, strict=True)
    )


def _can_call_models(principal: Principal) -> bool:
    """Whether an MCP server can call models as this principal: an application, by object ID."""

    return subject_kind_for(principal.kind) == EntitlementSubjectKind.APPLICATION and _is_guid(
        principal.object_id
    )
