"""Publishing model deployments into API Management.

This is the only module in MOSAIC that writes to API Management, and ADR 0010 records why the
read-only boundary ADR 0001 set was opened rather than worked around. Two things bound the damage
that capability can do, and both are enforced here:

* A gateway is written to only when an administrator moved it to ``manage`` mode *and* preflight
  confirmed write access. Neither substitutes for the other.
* Every write records whether it created the resource or found it already there, so a rollback
  deletes only what this run brought into existence. Ownership is never inferred from a name.

The loop is desired state -> observed state -> deterministic plan -> explicit apply -> audited
result. Saving a publication is never the operation that changes Azure.
"""

import asyncio
import hashlib
import json
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog

from mosaic_api.domain import (
    ApimResourceId,
    AuditEvent,
    Gateway,
    ManagementMode,
    ModelEndpoint,
    ModelProvider,
    Publication,
    PublicationCreate,
    PublicationStatus,
    PublicationUpdate,
    PublishableModel,
    PublishAction,
    PublishedResource,
    PublishedResourceKind,
    PublishPlan,
    PublishPlanStep,
    PublishRun,
    PublishRunStatus,
    PublishStepResult,
    PublishStepStatus,
    new_id,
    publication_id,
    utc_now,
)
from mosaic_api.errors import ConflictError, NotFoundError, ValidationError
from mosaic_api.integrations.apim import ApimClient
from mosaic_api.integrations.apim.model_apis import (
    CURATED_SHAPE_VERSION,
    OperationSpec,
    backend_url,
    curated_operations,
    default_names,
    display_name_for,
    suggested_names,
)
from mosaic_api.integrations.apim.writer import ApimWriter
from mosaic_api.integrations.policy import PublicationPolicy, render_publication_policy
from mosaic_api.observed import ObservedApi, ObservedModelDeployment
from mosaic_api.repositories import GatewayRepository, ModelEndpointRepository
from mosaic_api.services.directory import Actor

logger = structlog.get_logger()

ClientFactory = Callable[[ApimResourceId], ApimClient]
WriterFactory = Callable[[ApimResourceId], ApimWriter]

STALE_RUN_MESSAGE = "The API restarted while this apply was running; its result is unknown."

# Dependency order. A rollback walks it backwards, and unpublish is the same list reversed, so the
# ordering is stated once rather than duplicated in three places that could drift apart.
CREATE_ORDER: tuple[PublishedResourceKind, ...] = (
    PublishedResourceKind.POLICY_FRAGMENT,
    PublishedResourceKind.BACKEND,
    PublishedResourceKind.API,
    PublishedResourceKind.API_OPERATION,
    PublishedResourceKind.API_POLICY,
    PublishedResourceKind.PRODUCT,
    PublishedResourceKind.PRODUCT_API,
    PublishedResourceKind.SUBSCRIPTION,
)


@dataclass(frozen=True)
class _Resource:
    """One API Management resource a publication owns."""

    kind: PublishedResourceKind
    name: str
    segment: str
    operation: OperationSpec | None = None


def _desired_resources(publication: Publication) -> list[_Resource]:
    operations = curated_operations(publication.provider, publication.deployment_name)
    resources = [
        _Resource(
            PublishedResourceKind.POLICY_FRAGMENT,
            publication.fragment_name,
            f"policyFragments/{publication.fragment_name}",
        ),
        _Resource(
            PublishedResourceKind.BACKEND,
            publication.backend_name,
            f"backends/{publication.backend_name}",
        ),
        _Resource(
            PublishedResourceKind.API, publication.api_name, f"apis/{publication.api_name}"
        ),
    ]
    resources.extend(
        _Resource(
            PublishedResourceKind.API_OPERATION,
            operation.name,
            f"apis/{publication.api_name}/operations/{operation.name}",
            operation=operation,
        )
        for operation in operations
    )
    resources.append(
        _Resource(
            PublishedResourceKind.API_POLICY,
            "policy",
            f"apis/{publication.api_name}/policies/policy",
        )
    )
    resources.append(
        _Resource(
            PublishedResourceKind.PRODUCT,
            publication.product_name,
            f"products/{publication.product_name}",
        )
    )
    resources.append(
        _Resource(
            PublishedResourceKind.PRODUCT_API,
            publication.api_name,
            f"products/{publication.product_name}/apis/{publication.api_name}",
        )
    )
    if publication.subscription_required:
        resources.append(
            _Resource(
                PublishedResourceKind.SUBSCRIPTION,
                publication.subscription_name,
                f"subscriptions/{publication.subscription_name}",
            )
        )
    return resources


def _resource_key(
    resource: PublishedResource | PublishStepResult,
) -> tuple[PublishedResourceKind, str]:
    return resource.kind, resource.name


def _merge_resources(
    existing: list[PublishedResource],
    updates: list[PublishedResource],
) -> list[PublishedResource]:
    merged = {_resource_key(item): item for item in existing}
    for item in updates:
        previous = merged.get(_resource_key(item))
        if previous is not None:
            item = item.model_copy(
                update={"created_by_mosaic": previous.created_by_mosaic or item.created_by_mosaic}
            )
        merged[_resource_key(item)] = item
    return list(merged.values())


def publication_digest(
    publication: Publication, policy: PublicationPolicy, origin: str
) -> str:
    """A digest over the *intent*, not the observation.

    Apply compares this against the plan's digest. Covering desired state alone is deliberate: it
    makes "the administrator edited the publication after approving a plan" a rejection, while
    leaving ordinary APIM churn to be handled by re-reading each resource during apply.
    """

    payload = json.dumps(
        {
            "gatewayId": publication.gateway_id,
            "modelEndpointId": publication.model_endpoint_id,
            "deploymentName": publication.deployment_name,
            "provider": str(publication.provider),
            "apiName": publication.api_name,
            "apiPath": publication.api_path,
            "backendName": publication.backend_name,
            "backendUrl": origin,
            "fragmentName": publication.fragment_name,
            "productName": publication.product_name,
            "subscriptionName": publication.subscription_name,
            "subscriptionRequired": publication.subscription_required,
            "shapeVersion": publication.shape_version,
            "policySha256": policy.content_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class PublishingService:
    def __init__(
        self,
        repository: GatewayRepository,
        *,
        endpoint_repository: ModelEndpointRepository,
        client_factory: ClientFactory,
        writer_factory: WriterFactory,
    ) -> None:
        self._repository = repository
        self._endpoints = endpoint_repository
        self._client_factory = client_factory
        self._writer_factory = writer_factory
        self._active: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()

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
        actor: Actor, action: str, resource_id: str, resource_type: str = "publication"
    ) -> AuditEvent:
        return AuditEvent(
            id=new_id("audit"),
            tenant_id=actor.tenant_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            actor_object_id=actor.object_id,
        )

    async def list_publications(
        self, actor: Actor, gateway_id: str | None = None
    ) -> list[Publication]:
        return await self._repository.list_publications(actor.tenant_id, gateway_id=gateway_id)

    async def get_publication(self, actor: Actor, target_id: str) -> Publication:
        publication = await self._repository.get_publication(actor.tenant_id, target_id)
        if not publication:
            raise NotFoundError("Publication was not found", details={"id": target_id})
        return publication

    async def _load_gateway(self, actor: Actor, gateway_id: str) -> Gateway:
        gateway = await self._repository.get_gateway(actor.tenant_id, gateway_id)
        if not gateway:
            raise NotFoundError("Gateway was not found", details={"id": gateway_id})
        return gateway

    async def _load_endpoint(self, actor: Actor, endpoint_id: str) -> ModelEndpoint:
        endpoint = await self._endpoints.get_endpoint(actor.tenant_id, endpoint_id)
        if not endpoint:
            raise NotFoundError("Model endpoint was not found", details={"id": endpoint_id})
        return endpoint

    @staticmethod
    def _require_writable(gateway: Gateway) -> None:
        if gateway.management_mode != ManagementMode.MANAGE:
            raise ConflictError(
                "This gateway is in observe mode. Switch it to managed before publishing to it.",
                details={"gatewayId": gateway.id, "managementMode": str(gateway.management_mode)},
            )
        if not gateway.access.can_write:
            raise ConflictError(
                "MOSAIC cannot write to this gateway. Grant the role shown on the gateway and "
                "re-run the access check.",
                details={
                    "gatewayId": gateway.id,
                    "missingActions": gateway.access.missing_actions,
                },
            )

    async def create(self, actor: Actor, request: PublicationCreate) -> Publication:
        gateway = await self._load_gateway(actor, request.gateway_id)
        endpoint = await self._load_endpoint(actor, request.model_endpoint_id)
        if endpoint.provider == ModelProvider.OPENAI_COMPATIBLE:
            raise ValidationError(
                "MOSAIC has no curated API shape for OpenAI-compatible endpoints, so it cannot "
                "publish from them yet.",
                details={"modelEndpointId": endpoint.id, "provider": str(endpoint.provider)},
            )
        await self._require_known_deployment(actor, endpoint, request.deployment_name)

        names = default_names(endpoint.name, request.deployment_name)
        target = publication_id(
            actor.tenant_id, gateway.id, endpoint.id, request.deployment_name
        )
        existing = await self._repository.get_publication(actor.tenant_id, target)
        if existing and existing.created_resources():
            raise ConflictError(
                "This model is already published through this gateway. Update it instead.",
                details={"id": existing.id, "status": str(existing.status)},
            )
        publication = Publication(
            id=target,
            tenant_id=actor.tenant_id,
            gateway_id=gateway.id,
            model_endpoint_id=endpoint.id,
            deployment_name=request.deployment_name,
            provider=endpoint.provider,
            display_name=(
                request.display_name or display_name_for(endpoint.name, request.deployment_name)
            ),
            api_name=request.api_name or names.api_name,
            api_path=request.api_path or names.api_path,
            backend_name=names.backend_name,
            fragment_name=names.fragment_name,
            product_name=request.product_name or names.product_name,
            subscription_name=names.subscription_name,
            subscription_required=request.subscription_required,
            enforcement=request.enforcement,
            shape_version=CURATED_SHAPE_VERSION,
            created_at=existing.created_at if existing else utc_now(),
        )
        return await self._repository.save_publication(
            publication, self._audit(actor, "publication.created", publication.id)
        )

    async def _require_known_deployment(
        self, actor: Actor, endpoint: ModelEndpoint, deployment_name: str
    ) -> None:
        deployments = await self._endpoints.list_observed_for_endpoint(
            ObservedModelDeployment,
            actor.tenant_id,
            endpoint.id,
            "observedModelDeployment",
        )
        if not deployments:
            raise ConflictError(
                "MOSAIC has not read the deployments on this endpoint yet. Sync it first.",
                details={"modelEndpointId": endpoint.id},
            )
        if not any(item.deployment_name == deployment_name for item in deployments):
            raise ValidationError(
                "MOSAIC has not observed that deployment on this endpoint.",
                details={
                    "modelEndpointId": endpoint.id,
                    "deploymentName": deployment_name,
                    "observed": sorted(item.deployment_name for item in deployments),
                },
            )

    async def update(
        self, actor: Actor, target_id: str, request: PublicationUpdate
    ) -> Publication:
        publication = await self.get_publication(actor, target_id)
        changes = request.model_dump(exclude_unset=True, by_alias=False)
        changes = {key: value for key, value in changes.items() if value is not None}
        if not changes:
            return publication
        updated = Publication.model_validate(
            {
                **publication.model_dump(by_alias=False),
                **changes,
                "etag": publication.etag,
                # Editing intent invalidates any approved plan. Clearing the reference is what makes
                # a later apply fail loudly rather than quietly applying superseded changes.
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
        return await self._repository.save_publication(
            updated, self._audit(actor, "publication.updated", updated.id)
        )

    async def delete(self, actor: Actor, target_id: str) -> None:
        publication = await self.get_publication(actor, target_id)
        if publication.created_resources():
            raise ConflictError(
                "This publication still owns resources in API Management. Unpublish it first so "
                "MOSAIC can remove them, rather than forgetting they exist.",
                details={
                    "id": publication.id,
                    "resources": [item.name for item in publication.created_resources()],
                },
            )
        await self._repository.delete_publication(
            publication, self._audit(actor, "publication.removed", publication.id)
        )

    async def publishable_models(self, actor: Actor, gateway_id: str) -> list[PublishableModel]:
        await self._load_gateway(actor, gateway_id)
        endpoints = await self._endpoints.list_endpoints(actor.tenant_id)
        publications = {
            item.model_endpoint_id + "|" + item.deployment_name: item
            for item in await self._repository.list_publications(
                actor.tenant_id, gateway_id=gateway_id
            )
        }
        candidates: list[PublishableModel] = []
        for endpoint in endpoints:
            if endpoint.provider == ModelProvider.OPENAI_COMPATIBLE:
                continue
            deployments = await self._endpoints.list_observed_for_endpoint(
                ObservedModelDeployment,
                actor.tenant_id,
                endpoint.id,
                "observedModelDeployment",
            )
            runtime = next(
                (item for item in endpoint.runtime_access if item.gateway_id == gateway_id), None
            )
            for deployment in deployments:
                existing = publications.get(f"{endpoint.id}|{deployment.deployment_name}")
                api_name, api_path = suggested_names(endpoint.name, deployment.deployment_name)
                candidates.append(
                    PublishableModel(
                        model_endpoint_id=endpoint.id,
                        endpoint_name=endpoint.name,
                        provider=endpoint.provider,
                        deployment_name=deployment.deployment_name,
                        model_name=deployment.model_name,
                        model_version=deployment.model_version,
                        publication_id=existing.id if existing else None,
                        publication_status=existing.status if existing else None,
                        suggested_api_name=api_name,
                        suggested_api_path=api_path,
                        runtime_access=runtime,
                    )
                )
        candidates.sort(key=lambda item: (item.endpoint_name.casefold(), item.deployment_name))
        return candidates

    async def plan(self, actor: Actor, target_id: str) -> PublishPlan:
        publication = await self.get_publication(actor, target_id)
        gateway = await self._load_gateway(actor, publication.gateway_id)
        self._require_writable(gateway)
        endpoint = await self._load_endpoint(actor, publication.model_endpoint_id)

        origin = backend_url(str(endpoint.endpoint))
        policy = render_publication_policy(publication)
        resource = ApimResourceId.parse(gateway.azure_resource_id)
        client = self._client_factory(resource)

        await self._reject_collisions(actor, publication, gateway, client)

        steps: list[PublishPlanStep] = []
        for item in _desired_resources(publication):
            existed = await self._exists(client, publication, item)
            steps.append(
                PublishPlanStep(
                    kind=item.kind,
                    name=item.name,
                    action=PublishAction.UPDATE if existed else PublishAction.CREATE,
                    reason=self._reason(item, existed=existed),
                    resource_id=f"{resource.canonical}/{item.segment}",
                    existed=existed,
                )
            )

        plan = PublishPlan(
            id=new_id("publishplan"),
            tenant_id=actor.tenant_id,
            publication_id=publication.id,
            gateway_id=gateway.id,
            digest=publication_digest(publication, policy, origin),
            steps=steps,
            facets=policy.facets,
            policy_content_sha256=policy.content_sha256,
            warnings=self._warnings(publication, gateway, endpoint),
            actor_object_id=actor.object_id,
        )
        await self._repository.save_publish_plan(plan)
        await self._repository.record_publication_state(
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

    @staticmethod
    def _reason(item: _Resource, *, existed: bool) -> str:
        subject = {
            PublishedResourceKind.POLICY_FRAGMENT: "the MOSAIC enforcement fragment",
            PublishedResourceKind.BACKEND: "the backend pointing at the model endpoint",
            PublishedResourceKind.API: "the API that fronts this model",
            PublishedResourceKind.API_OPERATION: f"the {item.name} operation",
            PublishedResourceKind.API_POLICY: "the API policy that includes the fragment",
            PublishedResourceKind.PRODUCT: "the product that carries this API",
            PublishedResourceKind.PRODUCT_API: "the link between the product and the API",
            PublishedResourceKind.SUBSCRIPTION: "the subscription callers authenticate with",
        }[item.kind]
        return f"{'Replace' if existed else 'Create'} {subject}."

    @staticmethod
    def _warnings(
        publication: Publication, gateway: Gateway, endpoint: ModelEndpoint
    ) -> list[str]:
        """Report what an administrator should know without blocking a legitimate publish.

        A gateway that cannot yet call the endpoint is a real problem, but it is fixed with a role
        assignment MOSAIC cannot make, and refusing to publish would not bring that assignment any
        closer. The published API would return 401 from the model rather than silently misbehave.
        """

        warnings: list[str] = []
        runtime = next(
            (item for item in endpoint.runtime_access if item.gateway_id == gateway.id), None
        )
        if runtime is None:
            warnings.append(
                "MOSAIC has not evaluated whether this gateway can call the model endpoint. "
                "Re-check the endpoint's runtime access to find out before relying on this API."
            )
        elif not runtime.can_invoke:
            warnings.append(
                runtime.message
                or (
                    "This gateway is not known to have the role it needs to call the model "
                    "endpoint, so published requests may be rejected by the model."
                )
            )
        if not publication.subscription_required:
            warnings.append(
                "This API does not require a subscription, so the token limit counts every "
                "caller together rather than per subscription."
            )
        if publication.shape_version != CURATED_SHAPE_VERSION:
            warnings.append(
                f"This publication was authored against operation shape "
                f"{publication.shape_version}; MOSAIC now ships {CURATED_SHAPE_VERSION}."
            )
        return warnings

    async def _reject_collisions(
        self, actor: Actor, publication: Publication, gateway: Gateway, client: ApimClient
    ) -> None:
        """Refuse to take over an API or path MOSAIC did not create.

        Replacing somebody else's API because its name matched is the single most destructive thing
        this feature could do, and it would look like success.
        """

        owned_apis = {
            item.name
            for item in publication.resources
            if item.kind == PublishedResourceKind.API
        }
        live_api = await client.get_api(publication.api_name)
        if live_api is not None and publication.api_name not in owned_apis:
            raise ConflictError(
                "An API with this name already exists in the gateway and MOSAIC did not create "
                "it. Choose a different name rather than replacing it.",
                details={"apiName": publication.api_name, "gatewayId": gateway.id},
            )
        observed = await self._repository.list_observed(
            ObservedApi, actor.tenant_id, gateway.id, "observedApi"
        )
        wanted = publication.api_path.strip("/").casefold()
        clash = next(
            (
                item
                for item in observed
                if item.path.strip("/").casefold() == wanted and item.name not in owned_apis
            ),
            None,
        )
        if clash is not None:
            raise ConflictError(
                "Another API in this gateway is already served at that path.",
                details={"apiPath": publication.api_path, "conflictingApi": clash.name},
            )

    async def _exists(
        self, client: ApimClient, publication: Publication, item: _Resource
    ) -> bool:
        match item.kind:
            case PublishedResourceKind.POLICY_FRAGMENT:
                return await client.get_policy_fragment_resource(item.name) is not None
            case PublishedResourceKind.BACKEND:
                return await client.get_backend(item.name) is not None
            case PublishedResourceKind.API:
                return await client.get_api(item.name) is not None
            case PublishedResourceKind.API_OPERATION:
                return (
                    await client.get_api_operation(publication.api_name, item.name) is not None
                )
            case PublishedResourceKind.API_POLICY:
                return await client.get_api_policy(publication.api_name) is not None
            case PublishedResourceKind.PRODUCT:
                return await client.get_product(item.name) is not None
            case PublishedResourceKind.PRODUCT_API:
                linked = await client.list_product_apis(publication.product_name)
                return any(entry.get("name") == publication.api_name for entry in linked)
            case PublishedResourceKind.SUBSCRIPTION:
                return await client.get_subscription(item.name) is not None

    async def apply(
        self, actor: Actor, target_id: str, plan_id: str | None = None
    ) -> PublishRun:
        publication = await self.get_publication(actor, target_id)
        gateway = await self._load_gateway(actor, publication.gateway_id)
        self._require_writable(gateway)
        endpoint = await self._load_endpoint(actor, publication.model_endpoint_id)

        resolved = plan_id or publication.last_plan_id
        if not resolved:
            raise ConflictError(
                "Plan this publication before applying it, so the changes are reviewed first.",
                details={"id": publication.id},
            )
        plan = await self._repository.get_publish_plan(actor.tenant_id, resolved)
        if plan is None or plan.publication_id != publication.id:
            raise NotFoundError("Publish plan was not found", details={"id": resolved})

        origin = backend_url(str(endpoint.endpoint))
        policy = render_publication_policy(publication)
        if publication_digest(publication, policy, origin) != plan.digest:
            raise ConflictError(
                "This publication changed after the plan was produced. Re-plan it and review the "
                "new changes before applying.",
                details={"planId": plan.id, "planDigest": plan.digest},
            )

        run = self._claim(actor, publication, plan)
        try:
            await self._repository.save_publish_run(run)
        except Exception:
            self._active.discard(publication.id)
            raise
        resource = ApimResourceId.parse(gateway.azure_resource_id)
        client = self._client_factory(resource)
        self._spawn(self._run_apply(publication, gateway, client, plan, policy, origin, run))
        return run

    async def unpublish(self, actor: Actor, target_id: str) -> PublishRun:
        publication = await self.get_publication(actor, target_id)
        gateway = await self._load_gateway(actor, publication.gateway_id)
        self._require_writable(gateway)
        owned = publication.created_resources()
        if not owned:
            raise ConflictError(
                "MOSAIC did not create anything in API Management for this publication, so there "
                "is nothing to remove.",
                details={"id": publication.id},
            )
        order = {kind: index for index, kind in enumerate(CREATE_ORDER)}
        removals = sorted(owned, key=lambda item: order[item.kind], reverse=True)
        plan = PublishPlan(
            id=new_id("publishplan"),
            tenant_id=actor.tenant_id,
            publication_id=publication.id,
            gateway_id=gateway.id,
            digest=publication.last_plan_digest or "",
            steps=[
                PublishPlanStep(
                    kind=item.kind,
                    name=item.name,
                    action=PublishAction.DELETE,
                    reason=f"Remove {item.kind} {item.name} that MOSAIC created.",
                    resource_id=item.resource_id,
                    existed=True,
                )
                for item in removals
            ],
            actor_object_id=actor.object_id,
        )
        await self._repository.save_publish_plan(plan)
        run = self._claim(actor, publication, plan)
        try:
            await self._repository.save_publish_run(run)
        except Exception:
            self._active.discard(publication.id)
            raise
        self._spawn(self._run_unpublish(publication, gateway, plan, run))
        return run

    def _claim(self, actor: Actor, publication: Publication, plan: PublishPlan) -> PublishRun:
        """Claim the publication synchronously, before any await.

        The same reasoning as ADR 0004's sync admission: the run has to be persisted before the
        task starts, and that await would let a second request slip between checking a lock and
        acquiring it.
        """

        if publication.id in self._active:
            raise ConflictError(
                "An apply is already running for this publication",
                details={"id": publication.id},
            )
        self._active.add(publication.id)
        return PublishRun(
            id=new_id("publishrun"),
            tenant_id=publication.tenant_id,
            publication_id=publication.id,
            gateway_id=publication.gateway_id,
            plan_id=plan.id,
            plan_digest=plan.digest,
            actor_object_id=actor.object_id,
        )

    def _spawn(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task: asyncio.Task[None] = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_apply(
        self,
        publication: Publication,
        gateway: Gateway,
        client: ApimClient,
        plan: PublishPlan,
        policy: PublicationPolicy,
        origin: str,
        run: PublishRun,
    ) -> None:
        started = utc_now()
        writer = self._writer_factory(ApimResourceId.parse(gateway.azure_resource_id))
        resources = {(item.kind, item.name): item for item in _desired_resources(publication)}
        results: list[PublishStepResult] = []
        failure: str | None = None
        try:
            await self._mark_applying(publication)
            for step in plan.steps:
                item = resources.get((step.kind, step.name))
                if item is None:
                    continue
                result = PublishStepResult(
                    kind=step.kind,
                    name=step.name,
                    action=step.action,
                    resource_id=step.resource_id,
                )
                try:
                    observed = await self._exists(client, publication, item)
                    result.created_by_mosaic = not observed
                    if not step.existed and observed:
                        raise ConflictError(
                            f"{step.kind} {step.name} appeared after the plan was produced. "
                            "MOSAIC will not overwrite something it did not create. Re-plan to "
                            "review it.",
                            details={"kind": str(step.kind), "name": step.name},
                        )
                    await self._write(writer, publication, policy, origin, item)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    result.status = PublishStepStatus.FAILED
                    result.error = str(error)
                    results.append(result)
                    failure = f"{step.kind} {step.name}: {error}"
                    break
                result.status = PublishStepStatus.SUCCEEDED
                results.append(result)
        except asyncio.CancelledError:
            self._active.discard(publication.id)
            raise
        except Exception as error:
            logger.exception("publish_apply_failed", publication_id=publication.id)
            failure = str(error)

        try:
            if failure is None:
                await self._finish_success(publication, run, results, started)
            else:
                await self._rollback(publication, writer, run, results, started, failure)
        except Exception:
            logger.exception("publish_result_not_recorded", publication_id=publication.id)
        finally:
            self._active.discard(publication.id)

    async def _run_unpublish(
        self, publication: Publication, gateway: Gateway, plan: PublishPlan, run: PublishRun
    ) -> None:
        started = utc_now()
        writer = self._writer_factory(ApimResourceId.parse(gateway.azure_resource_id))
        results: list[PublishStepResult] = []
        remaining: list[PublishedResource] = list(publication.resources)
        errors: list[str] = []
        try:
            for step in plan.steps:
                result = PublishStepResult(
                    kind=step.kind,
                    name=step.name,
                    action=PublishAction.DELETE,
                    resource_id=step.resource_id,
                )
                try:
                    await self._remove(writer, publication, step.kind, step.name)
                except asyncio.CancelledError:
                    raise
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
        except asyncio.CancelledError:
            self._active.discard(publication.id)
            raise

        try:
            succeeded = not errors
            await self._finish_run(
                run,
                PublishRunStatus.SUCCEEDED if succeeded else PublishRunStatus.FAILED,
                started,
                results=results,
                errors=errors,
                orphaned=[] if succeeded else remaining,
            )
            await self._record_state(
                publication,
                status=(
                    PublicationStatus.DRAFT if succeeded else PublicationStatus.FAILED
                ),
                resources=remaining,
                run_id=run.id,
                error="; ".join(errors) or None,
                applied=False,
            )
        except Exception:
            logger.exception("publish_result_not_recorded", publication_id=publication.id)
        finally:
            self._active.discard(publication.id)

    async def _rollback(
        self,
        publication: Publication,
        writer: ApimWriter,
        run: PublishRun,
        results: list[PublishStepResult],
        started: datetime,
        failure: str,
    ) -> None:
        """Undo, in reverse, only the resources this run brought into existence.

        A step that replaced a pre-existing resource is deliberately not reverted: MOSAIC never
        stored the previous content (ADR 0004 discards policy markup on purpose), so "restoring"
        it would mean inventing it. Those resources are named in the run's errors instead, because
        an operator needs to know exactly what was left changed.
        """

        errors = [failure]
        orphaned: list[PublishedResource] = []
        replaced: list[str] = []
        for result in reversed(results):
            if result.status != PublishStepStatus.SUCCEEDED:
                continue
            if not result.created_by_mosaic:
                result.status = PublishStepStatus.SKIPPED
                replaced.append(f"{result.kind} {result.name}")
                continue
            try:
                await self._remove(writer, publication, result.kind, result.name)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                result.status = PublishStepStatus.ROLLBACK_FAILED
                result.error = str(error)
                errors.append(f"rollback of {result.kind} {result.name}: {error}")
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
            _resource_key(result)
            for result in results
            if result.status == PublishStepStatus.ROLLED_BACK
        }
        resources = [
            item for item in publication.resources if _resource_key(item) not in rolled_back
        ]
        resources = _merge_resources(resources, orphaned)
        if replaced:
            errors.append(
                "MOSAIC replaced these existing resources before the failure and cannot restore "
                "their previous contents: " + ", ".join(sorted(replaced))
            )
        status = (
            PublishRunStatus.ROLLBACK_FAILED if orphaned else PublishRunStatus.ROLLED_BACK
        )
        await self._finish_run(
            run, status, started, results=results, errors=errors, orphaned=orphaned
        )
        await self._record_state(
            publication,
            status=(
                PublicationStatus.FAILED if orphaned else PublicationStatus.ROLLED_BACK
            ),
            resources=resources,
            run_id=run.id,
            error=failure,
            applied=False,
        )

    async def _finish_success(
        self,
        publication: Publication,
        run: PublishRun,
        results: list[PublishStepResult],
        started: datetime,
    ) -> None:
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
            ],
        )
        await self._finish_run(
            run, PublishRunStatus.SUCCEEDED, started, results=results, errors=[], orphaned=[]
        )
        await self._record_state(
            publication,
            status=PublicationStatus.PUBLISHED,
            resources=resources,
            run_id=run.id,
            error=None,
            applied=True,
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

    async def _mark_applying(self, publication: Publication) -> None:
        await self._record_state(
            publication,
            status=PublicationStatus.APPLYING,
            resources=publication.resources,
            run_id=None,
            error=None,
            applied=False,
        )

    async def _record_state(
        self,
        publication: Publication,
        *,
        status: PublicationStatus,
        resources: list[PublishedResource],
        run_id: str | None,
        error: str | None,
        applied: bool,
    ) -> None:
        """Re-read before writing, so an apply never resurrects a publication that was removed."""

        current = await self._repository.get_publication(publication.tenant_id, publication.id)
        if current is None:
            logger.info("publish_result_discarded", publication_id=publication.id)
            return
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
        await self._repository.record_publication_state(current.model_copy(update=updates))

    async def _write(
        self,
        writer: ApimWriter,
        publication: Publication,
        policy: PublicationPolicy,
        origin: str,
        item: _Resource,
    ) -> None:
        match item.kind:
            case PublishedResourceKind.POLICY_FRAGMENT:
                await writer.put_policy_fragment(
                    item.name,
                    policy.fragment_xml,
                    description=f"MOSAIC enforcement for {publication.display_name}",
                )
            case PublishedResourceKind.BACKEND:
                await writer.put_backend(
                    item.name, url=origin, title=f"MOSAIC backend for {publication.display_name}"
                )
            case PublishedResourceKind.API:
                await writer.put_api(
                    item.name,
                    display_name=publication.display_name,
                    path=publication.api_path,
                    subscription_required=publication.subscription_required,
                    description=(
                        f"Published by MOSAIC for the {publication.deployment_name} deployment."
                    ),
                )
            case PublishedResourceKind.API_OPERATION:
                operation = item.operation
                if operation is None:
                    return
                await writer.put_api_operation(
                    publication.api_name,
                    operation.name,
                    display_name=operation.display_name,
                    method=operation.method,
                    url_template=operation.url_template,
                    description=operation.description,
                )
            case PublishedResourceKind.API_POLICY:
                await writer.put_api_policy(publication.api_name, policy.api_policy_xml)
            case PublishedResourceKind.PRODUCT:
                await writer.put_product(
                    item.name,
                    display_name=publication.display_name,
                    description=(
                        f"Published by MOSAIC for the {publication.deployment_name} deployment."
                    ),
                    subscription_required=publication.subscription_required,
                )
            case PublishedResourceKind.PRODUCT_API:
                await writer.put_product_api(publication.product_name, publication.api_name)
            case PublishedResourceKind.SUBSCRIPTION:
                await writer.put_subscription(
                    item.name,
                    display_name=publication.display_name,
                    product_name=publication.product_name,
                )

    async def _remove(
        self,
        writer: ApimWriter,
        publication: Publication,
        kind: PublishedResourceKind,
        name: str,
    ) -> None:
        match kind:
            case PublishedResourceKind.POLICY_FRAGMENT:
                await writer.delete_policy_fragment(name)
            case PublishedResourceKind.BACKEND:
                await writer.delete_backend(name)
            case PublishedResourceKind.API:
                await writer.delete_api(name)
            case PublishedResourceKind.API_OPERATION:
                await writer.delete_api_operation(publication.api_name, name)
            case PublishedResourceKind.API_POLICY:
                await writer.delete_api_policy(publication.api_name)
            case PublishedResourceKind.PRODUCT:
                await writer.delete_product(name)
            case PublishedResourceKind.PRODUCT_API:
                await writer.delete_product_api(publication.product_name, publication.api_name)
            case PublishedResourceKind.SUBSCRIPTION:
                await writer.delete_subscription(name)

    async def get_run(self, actor: Actor, run_id: str) -> PublishRun:
        run = await self._repository.get_publish_run(actor.tenant_id, run_id)
        if not run:
            raise NotFoundError("Publish run was not found", details={"id": run_id})
        return run

    async def list_runs(self, actor: Actor, target_id: str) -> list[PublishRun]:
        await self.get_publication(actor, target_id)
        return await self._repository.list_publish_runs(actor.tenant_id, target_id)

    async def get_plan(self, actor: Actor, plan_id: str) -> PublishPlan:
        plan = await self._repository.get_publish_plan(actor.tenant_id, plan_id)
        if not plan:
            raise NotFoundError("Publish plan was not found", details={"id": plan_id})
        return plan

    async def wait_for_idle(self) -> None:
        """Drain in-flight applies. Used by tests and shutdown, not by request handlers."""

        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def reap_stale_publish_runs(self, tenant_id: str) -> int:
        """A run orphaned by a restart is unknown, not successful. Say so rather than leave it."""

        stale = await self._repository.list_unfinished_publish_runs(tenant_id)
        active = set(self._active)
        reaped = 0
        for run in stale:
            if run.publication_id in active:
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
            publication = await self._repository.get_publication(tenant_id, run.publication_id)
            if publication and publication.status == PublicationStatus.APPLYING:
                await self._repository.record_publication_state(
                    publication.model_copy(
                        update={
                            "status": PublicationStatus.FAILED,
                            "last_error": STALE_RUN_MESSAGE,
                            "updated_at": completed,
                        }
                    )
                )
            reaped += 1
        return reaped
