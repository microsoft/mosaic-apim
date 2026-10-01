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
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog

from mosaic_api.budgets import BLOCKED_COST_CENTERS_NAMED_VALUE
from mosaic_api.domain import (
    AiBackendKind,
    ApimResourceId,
    AppliedCostCenterPool,
    AuditEvent,
    BindingSource,
    CapabilitySupport,
    DeclaredDeployment,
    EntitlementBinding,
    EntitlementSubjectKind,
    Gateway,
    KeyVaultSecretId,
    ManagementMode,
    ModelAccessGrant,
    ModelAccessSnapshot,
    ModelApi,
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
    TokenEnforcement,
    grant_precedence_key,
    model_access_subscription_name,
    model_api_id,
    new_id,
    publication_id,
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
from mosaic_api.integrations.apim.model_apis import (
    CURATED_SHAPE_VERSION,
    DeploymentFit,
    OperationSpec,
    assess_declared_deployment,
    assess_deployment,
    backend_origin,
    default_names,
    display_name_for,
    operations_for,
    suggested_names,
    token_limits_note,
)
from mosaic_api.integrations.apim.writer import DEFAULT_SUBSCRIPTION_KEY_NAMES, ApimWriter
from mosaic_api.integrations.backend_keys import backend_key_name
from mosaic_api.integrations.policy import PublicationPolicy, render_publication_policy
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
    denied_access_snapshot,
    effective_enforcement,
    entitlement_intent_digest,
    environment_guard,
    grant_key_display_name,
    local_mutation_active,
    publication_lock,
    safe_access_snapshot,
)

logger = structlog.get_logger()

ClientFactory = Callable[[ApimResourceId], ApimClient]
WriterFactory = Callable[[ApimResourceId], ApimWriter]
RecoveryJournal = Callable[[PublishedResourceKind, str, bool, str], Awaitable[None]]

STALE_RUN_MESSAGE = "The API restarted while this apply was running; its result is unknown."
DENY_ALL_POLICY = (
    "<policies><inbound><return-response><set-status code=\"403\" reason=\"Access unavailable\"/>"
    "</return-response></inbound><backend><base/></backend><outbound><base/></outbound>"
    "<on-error><base/></on-error></policies>"
)
DENY_ALL_FRAGMENT = (
    "<fragment><return-response><set-status code=\"403\" reason=\"Access unavailable\"/>"
    "</return-response></fragment>"
)

# Dependency order: every resource is created after the resources it names. The fragment's
# set-backend-service names the backend, and API Management validates a fragment asynchronously and
# fails the write when that backend does not exist yet, so the backend comes first. A key-
# authenticated publication's fragment also names its Key Vault-backed named value, which API
# Management refuses to delete while a policy still names it, so that comes first of all. The API
# policy's include-fragment names the fragment, and the subscription's scope names the product. A
# rollback walks this order backwards and unpublish is the same list reversed, so teardown removes
# each resource before the one it names. The plan follows it too; a test holds the two together.
CREATE_ORDER: tuple[PublishedResourceKind, ...] = (
    PublishedResourceKind.NAMED_VALUE,
    PublishedResourceKind.BACKEND,
    PublishedResourceKind.POLICY_FRAGMENT,
    PublishedResourceKind.API,
    PublishedResourceKind.API_OPERATION,
    PublishedResourceKind.API_POLICY,
    PublishedResourceKind.PRODUCT,
    PublishedResourceKind.PRODUCT_API,
    PublishedResourceKind.SUBSCRIPTION,
)


def _follows_create_order(steps: list[PublishPlanStep]) -> bool:
    """Whether a plan's steps run in CREATE_ORDER.

    Apply runs a plan's steps in the order they were stored. A plan saved before the order changed
    keeps the old one, and the digest covers intent rather than order, so this is checked apart.
    """

    ranks = [CREATE_ORDER.index(step.kind) for step in steps]
    return ranks == sorted(ranks)


@dataclass(frozen=True)
class _Resource:
    """One API Management resource a publication owns."""

    kind: PublishedResourceKind
    name: str
    segment: str
    operation: OperationSpec | None = None


@dataclass(frozen=True)
class _Backend:
    """Where a published API forwards to, and the key it sends when its endpoint takes one.

    ``key_secret`` is the versionless Key Vault secret identifier API Management resolves the key
    from. It is an identifier, never a key, and it goes nowhere but the named value MOSAIC writes.
    """

    origin: str
    key_secret: str | None = None


def _desired_resources(publication: Publication) -> list[_Resource]:
    operations = operations_for(publication)
    resources: list[_Resource] = []
    if publication.backend_key_name is not None:
        resources.append(
            _Resource(
                PublishedResourceKind.NAMED_VALUE,
                publication.backend_key_name,
                f"namedValues/{publication.backend_key_name}",
            )
        )
    resources.extend(
        [
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
            _Resource(
                PublishedResourceKind.API, publication.api_name, f"apis/{publication.api_name}"
            ),
        ]
    )
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
    if publication.subscription_required and publication.governed_access is None:
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
    publication: Publication,
    policy: PublicationPolicy,
    origin: str,
    snapshot: ModelAccessSnapshot | None = None,
    environment_fingerprint: str | None = None,
    key_secret: str | None = None,
) -> str:
    """A digest over the *intent*, not the observation.

    Apply compares this against the plan's digest. Covering desired state alone is deliberate: it
    makes "the administrator edited the publication after approving a plan" a rejection, while
    leaving ordinary APIM churn to be handled by re-reading each resource during apply.

    A key-authenticated publication's digest also covers the Key Vault secret its named value
    points at, so pointing the endpoint at another secret needs a new review. The field is added
    only for such publications, so every other publication's digest is what it always was.
    """

    intent: dict[str, Any] = {
        "gatewayId": publication.gateway_id,
        "modelEndpointId": publication.model_endpoint_id,
        "deploymentName": publication.deployment_name,
        "provider": str(publication.provider),
        "displayName": publication.display_name,
        "apiName": publication.api_name,
        "apiPath": publication.api_path,
        "backendName": publication.backend_name,
        "backendUrl": origin,
        "fragmentName": publication.fragment_name,
        "productName": publication.product_name,
        "subscriptionName": publication.subscription_name,
        "subscriptionRequired": publication.subscription_required,
        "subscriptionKeyParameterNames": DEFAULT_SUBSCRIPTION_KEY_NAMES if snapshot else None,
        "shapeVersion": publication.shape_version,
        "policySha256": policy.content_sha256,
        "modelApiId": publication.model_api_id,
        "governedAccess": (
            publication.governed_access.model_dump(mode="json")
            if publication.governed_access
            else None
        ),
        "accessSnapshot": snapshot.model_dump(mode="json") if snapshot else None,
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
            (
                str(item.kind),
                item.name,
                item.resource_id,
                item.created_by_mosaic,
            )
            for item in publication.resources
        ),
    }
    if publication.backend_key_name is not None:
        intent["backendKey"] = {"namedValue": publication.backend_key_name, "secret": key_secret}
    payload = json.dumps(intent, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _is_governed(publication: Publication) -> bool:
    """Whether unpublishing denies calls and suspends subscriptions before it deletes anything."""

    return publication.governed_access is not None or publication.applied_access is not None


def unpublish_digest(publication: Publication) -> str:
    """A digest over what unpublishing removes and whose access it ends.

    Unpublish runs a reviewed plan only while this still matches, so an administrator never
    confirms one teardown and gets another. A resource recorded since the review, or access applied
    since, changes it. It covers what the run reads besides the plan's own steps: the names the
    deletes and the fail-closed guard use, and whether that guard runs.
    """

    payload = json.dumps(
        {
            "operation": "unpublish",
            "publicationId": publication.id,
            "gatewayId": publication.gateway_id,
            "apiName": publication.api_name,
            "productName": publication.product_name,
            "subscriptionName": publication.subscription_name,
            "fragmentName": publication.fragment_name,
            "governed": _is_governed(publication),
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


_KIND_NOUNS: dict[PublishedResourceKind, str] = {
    PublishedResourceKind.NAMED_VALUE: "named value",
    PublishedResourceKind.BACKEND: "backend",
    PublishedResourceKind.POLICY_FRAGMENT: "policy fragment",
    PublishedResourceKind.API: "API",
    PublishedResourceKind.API_OPERATION: "operation",
    PublishedResourceKind.API_POLICY: "API policy",
    PublishedResourceKind.PRODUCT: "product",
    PublishedResourceKind.PRODUCT_API: "product link",
    PublishedResourceKind.SUBSCRIPTION: "subscription",
}


def _resource_label(publication: Publication, item: PublishedResource) -> str:
    if item.kind == PublishedResourceKind.API_POLICY:
        return "the API's policy"
    if item.kind == PublishedResourceKind.PRODUCT_API:
        return f"the link from product {publication.product_name} to the API"
    return f"{_KIND_NOUNS[item.kind]} {item.name}"


_REMOVAL_REASONS: dict[PublishedResourceKind, str] = {
    PublishedResourceKind.API: (
        "Delete the API that fronts this model. The gateway stops serving it."
    ),
    PublishedResourceKind.API_POLICY: "Delete the API's policy.",
    PublishedResourceKind.POLICY_FRAGMENT: "Delete the MOSAIC enforcement fragment.",
    PublishedResourceKind.BACKEND: "Delete the backend that points at the model endpoint.",
    PublishedResourceKind.PRODUCT: "Delete the product, and every subscription to it.",
    PublishedResourceKind.PRODUCT_API: "Delete the link between the product and the API.",
    PublishedResourceKind.NAMED_VALUE: (
        "Delete the named value API Management reads the model endpoint's API key through. The "
        "key itself stays in Key Vault."
    ),
}


def _removal_reason(
    publication: Publication, item: PublishedResource, grant: ModelAccessGrant | None
) -> str:
    if item.kind == PublishedResourceKind.API_OPERATION:
        return f"Delete the {item.name} operation."
    if item.kind != PublishedResourceKind.SUBSCRIPTION:
        return _REMOVAL_REASONS[item.kind]
    if grant is not None:
        return f"Delete {grant.display_name}'s subscription. Its keys stop working."
    if item.name == publication.subscription_name:
        return "Delete the subscription MOSAIC created for the product. Its keys stop working."
    return "Delete a subscription MOSAIC created. Its keys stop working."


class PublishingService:
    def __init__(
        self,
        repository: GatewayRepository,
        *,
        endpoint_repository: ModelEndpointRepository,
        client_factory: ClientFactory,
        writer_factory: WriterFactory,
        directory_repository: DirectoryRepository | None = None,
        entitlement_repository: EntitlementRepository | None = None,
        model_runtime_client_id: str | None = None,
        security_group_claims: bool = True,
        environment_repository: EnvironmentRepository | None = None,
        cost_center_repository: CostCenterRepository | None = None,
        blocked_list: BlockedListGate | None = None,
    ) -> None:
        self._repository = repository
        self._cost_centers = cost_center_repository
        # What a new blocked list holds. Without it, a list MOSAIC creates blocks nothing until
        # the budget check writes it.
        self._blocked_list = blocked_list
        self._endpoints = endpoint_repository
        self._client_factory = client_factory
        self._writer_factory = writer_factory
        self._directory = directory_repository
        self._entitlements = entitlement_repository
        self._runtime_audience = model_runtime_client_id
        self._security_group_claims = security_group_claims
        # None only in tests that don't exercise environments; the built-in seeds apply then.
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

    async def get_lock_owner(self, actor: Actor, target_id: str) -> str | None:
        """Diagnostic owner lookup, including mutation locks with no persisted publish run.

        A lookup alone cannot serialize authorization. Credential reads should hold
        ``model_access.publication_lock`` and reload eligibility inside that critical section.
        """

        return await self._repository.get_publication_lock(actor.tenant_id, target_id)

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
        target = publication_id(
            actor.tenant_id, request.gateway_id, request.model_endpoint_id, request.deployment_name
        )
        async with environment_guard(
            self._repository,
            actor.tenant_id,
            environments=True,
            gateway_ids=[request.gateway_id],
            endpoint_ids=[request.model_endpoint_id],
            publication_ids=[target],
        ):
            return await self._create(actor, request)

    async def _create(self, actor: Actor, request: PublicationCreate) -> Publication:
        gateway = await self._load_gateway(actor, request.gateway_id)
        endpoint = await self._load_endpoint(actor, request.model_endpoint_id)
        catalog = await load_environment_catalog(self._environments, actor.tenant_id)
        verdict = permits(catalog, gateway.environment, endpoint.environment)
        refuse_blocked_pairing(verdict)
        if endpoint.provider == ModelProvider.OPENAI_COMPATIBLE:
            raise ValidationError(
                "MOSAIC has no curated API shape for OpenAI-compatible endpoints, so it cannot "
                "publish from them yet.",
                details={"modelEndpointId": endpoint.id, "provider": str(endpoint.provider)},
            )
        deployment = await self._require_known_deployment(
            actor, endpoint, request.deployment_name
        )
        fit = self._fit(endpoint, deployment, gateway)
        if not fit.publishable:
            raise ValidationError(
                fit.unpublishable_reason or "MOSAIC can't publish this deployment yet.",
                details={
                    "deploymentName": request.deployment_name,
                    "capability": str(fit.capability),
                },
            )
        self._require_enforcement_fit(request.enforcement, fit.token_limits_note)
        if endpoint.uses_backend_key():
            self._require_key_identity(gateway)

        names = default_names(endpoint.name, request.deployment_name)
        target = publication_id(
            actor.tenant_id, gateway.id, endpoint.id, request.deployment_name
        )
        existing = await self._repository.get_publication(actor.tenant_id, target)
        if existing and (
            existing.created_resources()
            or existing.applied_access is not None
            or existing.access_state in {"applying", "unknown"}
        ):
            raise ConflictError(
                "This model already has a publication on this gateway. Re-plan that publication "
                "instead of publishing it again.",
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
            backend_key_name=(
                backend_key_name(names.backend_name) if endpoint.uses_backend_key() else None
            ),
            subscription_required=(
                not request.governed_access.entra_enabled
                if request.governed_access
                else request.subscription_required
            ),
            enforcement=request.enforcement,
            governed_access=request.governed_access,
            shape_version=CURATED_SHAPE_VERSION,
            api_shape=fit.api_shape,
            created_at=existing.created_at if existing else utc_now(),
        )
        return await self._repository.save_publication(
            publication, self._audit(actor, "publication.created", publication.id)
        )

    @staticmethod
    def _fit(
        endpoint: ModelEndpoint,
        deployment: ObservedModelDeployment | DeclaredDeployment,
        gateway: Gateway,
    ) -> DeploymentFit:
        if isinstance(deployment, DeclaredDeployment):
            return assess_declared_deployment(
                endpoint.provider,
                deployment.api_shape,
                endpoint=str(endpoint.endpoint),
                gateway_sku=gateway.capabilities.sku_name,
            )
        return assess_deployment(
            endpoint.provider,
            model_name=deployment.model_name,
            model_format=deployment.model_format,
            capabilities=deployment.capabilities,
            endpoint=str(endpoint.endpoint),
            gateway_sku=gateway.capabilities.sku_name,
        )

    @staticmethod
    def _require_key_identity(gateway: Gateway) -> None:
        """A key-authenticated publication needs a gateway identity to read its key with.

        A gateway MOSAIC hasn't read yet isn't refused: that is "not known", not "has none", and
        the readiness check and the named value step both still stand between it and a live API.
        """

        if gateway.capabilities.identity_observed and not gateway.capabilities.principal_id:
            raise ValidationError(
                f"API Management reads this endpoint's API key from Key Vault with its managed "
                f"identity, and {gateway.name} has none. Turn on its system-assigned managed "
                "identity and re-run its access check first.",
                details={"gatewayId": gateway.id},
            )

    @staticmethod
    def _require_enforcement_fit(
        enforcement: TokenEnforcement | None, unsupported_note: str | None
    ) -> None:
        """Token enforcement is required exactly when the gateway can apply it.

        Silently dropping limits an administrator asked for would be worse than refusing them, and
        a publication that could be metered but isn't would be ungoverned by accident.
        """

        if unsupported_note is not None and enforcement is not None:
            raise ValidationError(unsupported_note)
        if unsupported_note is None and enforcement is None:
            raise ValidationError("Token enforcement is required for this publication.")

    async def _require_known_deployment(
        self, actor: Actor, endpoint: ModelEndpoint, deployment_name: str
    ) -> ObservedModelDeployment | DeclaredDeployment:
        if endpoint.uses_backend_key():
            declared = endpoint.declared(deployment_name)
            if declared is None:
                raise ValidationError(
                    "Declare this deployment on the endpoint before publishing it. MOSAIC can't "
                    "list the deployments of an endpoint it reaches with an API key.",
                    details={
                        "modelEndpointId": endpoint.id,
                        "deploymentName": deployment_name,
                        "declared": sorted(
                            item.deployment_name for item in endpoint.declared_deployments
                        ),
                    },
                )
            return declared
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
        match = next(
            (item for item in deployments if item.deployment_name == deployment_name), None
        )
        if match is None:
            raise ValidationError(
                "MOSAIC has not observed that deployment on this endpoint.",
                details={
                    "modelEndpointId": endpoint.id,
                    "deploymentName": deployment_name,
                    "observed": sorted(item.deployment_name for item in deployments),
                },
            )
        return match

    async def update(
        self, actor: Actor, target_id: str, request: PublicationUpdate
    ) -> Publication:
        async with publication_lock(self._repository, actor.tenant_id, target_id):
            return await self._update(actor, target_id, request)

    async def _update(
        self, actor: Actor, target_id: str, request: PublicationUpdate
    ) -> Publication:
        publication = await self.get_publication(actor, target_id)
        if publication.access_state == "unknown":
            raise ConflictError("Recover this publication's interrupted apply before editing it")
        if (
            "governed_access" in request.model_fields_set
            and request.governed_access is None
            and (publication.governed_access is not None or publication.applied_access is not None)
        ):
            raise ValidationError(
                "Governed access cannot be cleared. Disable keys and Entra independently instead."
            )
        changes = request.model_dump(exclude_unset=True, by_alias=False)
        changes = {key: value for key, value in changes.items() if value is not None}
        if not changes:
            return publication
        if request.enforcement is not None:
            gateway = await self._load_gateway(actor, publication.gateway_id)
            note = token_limits_note(publication.api_shape, gateway.capabilities.sku_name)
            if note is not None:
                raise ValidationError(note)
        settings = request.governed_access or publication.governed_access
        if settings:
            changes["subscription_required"] = not settings.entra_enabled
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
        async with publication_lock(self._repository, actor.tenant_id, target_id):
            await self._delete(actor, target_id)

    async def _delete(self, actor: Actor, target_id: str) -> None:
        publication = await self.get_publication(actor, target_id)
        if publication.may_own_gateway_state():
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

    async def link_model_api(self, actor: Actor, publication_id: str) -> ModelApi:
        """Explicitly adopt a successfully published API, never an arbitrary imported API."""

        async with publication_lock(self._repository, actor.tenant_id, publication_id):
            publication = await self.get_publication(actor, publication_id)
            gateway = await self._load_gateway(actor, publication.gateway_id)
            if (
                publication.status != PublicationStatus.PUBLISHED
                or publication.access_state in {"applying", "unknown"}
                or not self._owns(publication, PublishedResourceKind.API, publication.api_name)
            ):
                raise ConflictError("Only an owned, successfully published API can be linked")
            client = self._client_factory(ApimResourceId.parse(gateway.azure_resource_id))
            if await client.get_api(publication.api_name) is None:
                raise ConflictError("The publication's owned API no longer exists in the gateway")
            return await self._materialize_model_api(actor, publication)

    async def _materialize_model_api(self, actor: Actor, publication: Publication) -> ModelApi:
        if not self._owns(publication, PublishedResourceKind.API, publication.api_name):
            raise ConflictError("MOSAIC cannot materialize an API it does not own")
        record_id = model_api_id(actor.tenant_id, publication.gateway_id, publication.api_name)
        existing = await self._repository.get_model_api(actor.tenant_id, record_id)
        if existing and existing.publication_id not in {None, publication.id}:
            raise ConflictError("This model API already belongs to a different publication")
        changes: dict[str, Any] = {
            "publication_id": publication.id,
            "display_name": publication.display_name,
            "path": publication.api_path,
            "protocols": ["https"],
            "subscription_required": publication.subscription_required,
            "operation_count": len(operations_for(publication)),
            "product_names": sorted(
                set(existing.product_names if existing else []) | {publication.product_name}
            ),
            "updated_at": utc_now(),
        }
        if existing:
            record = existing.model_copy(update=changes)
        else:
            record = ModelApi(
                id=record_id,
                tenant_id=actor.tenant_id,
                gateway_id=publication.gateway_id,
                api_name=publication.api_name,
                ai_kind=(
                    AiBackendKind.AZURE_OPENAI
                    if publication.provider == ModelProvider.AZURE_OPENAI
                    else AiBackendKind.AZURE_AI_FOUNDRY
                ),
                ai_signals=["Published and owned by MOSAIC"],
                imported_by=actor.object_id,
                **changes,
            )
        saved = await self._repository.save_model_api(
            record, self._audit(actor, "modelApi.publicationLinked", record.id, "modelApi")
        )
        current = await self.get_publication(actor, publication.id)
        await self._repository.record_publication_state(
            current.model_copy(update={"model_api_id": record.id, "updated_at": utc_now()})
        )
        return saved

    async def publishable_models(self, actor: Actor, gateway_id: str) -> list[PublishableModel]:
        gateway = await self._load_gateway(actor, gateway_id)
        endpoints = await self._endpoints.list_endpoints(actor.tenant_id)
        publications = {
            item.model_endpoint_id + "|" + item.deployment_name: item
            for item in await self._repository.list_publications(
                actor.tenant_id, gateway_id=gateway_id
            )
        }
        catalog = await load_environment_catalog(self._environments, actor.tenant_id)
        candidates: list[PublishableModel] = []
        for endpoint in endpoints:
            if endpoint.provider == ModelProvider.OPENAI_COMPATIBLE:
                continue
            deployments: list[ObservedModelDeployment | DeclaredDeployment]
            if endpoint.uses_backend_key():
                deployments = list(endpoint.declared_deployments)
            else:
                deployments = list(
                    await self._endpoints.list_observed_for_endpoint(
                        ObservedModelDeployment,
                        actor.tenant_id,
                        endpoint.id,
                        "observedModelDeployment",
                    )
                )
            runtime = next(
                (item for item in endpoint.runtime_access if item.gateway_id == gateway_id), None
            )
            for deployment in deployments:
                existing = publications.get(f"{endpoint.id}|{deployment.deployment_name}")
                api_name, api_path = suggested_names(endpoint.name, deployment.deployment_name)
                fit = self._fit(endpoint, deployment, gateway)
                verdict = permits(catalog, gateway.environment, endpoint.environment)
                observed = deployment if isinstance(deployment, ObservedModelDeployment) else None
                candidates.append(
                    PublishableModel(
                        model_endpoint_id=endpoint.id,
                        endpoint_name=endpoint.name,
                        provider=endpoint.provider,
                        deployment_name=deployment.deployment_name,
                        model_name=deployment.model_name,
                        model_version=deployment.model_version,
                        model_format=observed.model_format if observed else None,
                        model_publisher=observed.model_publisher if observed else None,
                        capability=fit.capability,
                        api_shape=fit.api_shape,
                        publishable=fit.publishable,
                        unpublishable_reason=fit.unpublishable_reason,
                        token_limits_supported=fit.token_limits_supported,
                        token_limits_note=fit.token_limits_note,
                        publication_id=existing.id if existing else None,
                        publication_status=existing.status if existing else None,
                        suggested_api_name=api_name,
                        suggested_api_path=api_path,
                        runtime_access=runtime,
                        environment_verdict=verdict,
                        declared=observed is None,
                    )
                )
        candidates.sort(key=lambda item: (item.endpoint_name.casefold(), item.deployment_name))
        return candidates

    async def plan(self, actor: Actor, target_id: str) -> PublishPlan:
        async with publication_lock(self._repository, actor.tenant_id, target_id):
            return await self._plan(actor, target_id)

    async def _plan(self, actor: Actor, target_id: str) -> PublishPlan:
        publication = await self.get_publication(actor, target_id)
        if publication.access_state == "unknown":
            raise ConflictError("Recover this publication's interrupted apply before replanning")
        gateway = await self._load_gateway(actor, publication.gateway_id)
        self._require_writable(gateway)
        endpoint = await self._load_endpoint(actor, publication.model_endpoint_id)
        catalog = await load_environment_catalog(self._environments, actor.tenant_id)
        verdict = permits(catalog, gateway.environment, endpoint.environment)
        refuse_blocked_pairing(verdict)
        environment_fingerprint = compatibility_fingerprint(
            catalog, gateway.environment, endpoint.environment
        )

        self._require_enforcement_fit(
            publication.enforcement,
            token_limits_note(publication.api_shape, gateway.capabilities.sku_name),
        )
        backend = await self._backend(publication, endpoint, gateway)
        origin = backend.origin
        snapshot, access_warnings = await self._access_snapshot(publication, gateway)
        policy = self._policy(publication, snapshot)
        resource = ApimResourceId.parse(gateway.azure_resource_id)
        client = self._client_factory(resource)

        await self._reject_collisions(actor, publication, gateway, client)

        if snapshot:
            steps = await self._governed_steps(publication, snapshot, client, resource)
            live = await client.get_api(publication.api_name)
            key_names = (
                (live.get("properties") or {}).get("subscriptionKeyParameterNames")
                if live
                else None
            )
            if key_names and (
                not isinstance(key_names, dict)
                or any(
                    key_names.get(kind, default) != default
                    for kind, default in DEFAULT_SUBSCRIPTION_KEY_NAMES.items()
                )
            ):
                access_warnings.append(
                    "This API's customized subscription key names will be reset to "
                    "Ocp-Apim-Subscription-Key (header) and subscription-key (query). "
                    "Callers must use these governed defaults."
                )
        else:
            steps = []
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
            steps=steps,
            facets=policy.facets,
            policy_content_sha256=policy.content_sha256,
            digest=publication_digest(
                publication,
                policy,
                origin,
                snapshot,
                environment_fingerprint=environment_fingerprint,
                key_secret=backend.key_secret,
            ),
            warnings=[
                *self._warnings(publication, gateway, endpoint),
                *([verdict.reason] if verdict.level == VerdictLevel.WARNING else []),
                *access_warnings,
            ],
            actor_object_id=actor.object_id,
            access_snapshot=snapshot,
            previous_access_version=(
                publication.applied_access.version if publication.applied_access else None
            ),
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
    def _policy(
        publication: Publication, snapshot: ModelAccessSnapshot | None
    ) -> PublicationPolicy:
        if snapshot is None:
            return render_publication_policy(publication)
        from mosaic_api.integrations.access_policy import render_governed_policy

        return render_governed_policy(publication, snapshot)

    async def _backend(
        self, publication: Publication, endpoint: ModelEndpoint, gateway: Gateway
    ) -> _Backend:
        """Where the published API forwards to, and for a key endpoint, which secret it sends.

        The secret is read from the endpoint's credential reference each time, so pointing the
        endpoint at another secret is picked up by the next plan, and approving it is a review of
        its own because the digest covers it.
        """

        origin = backend_origin(publication.api_shape, str(endpoint.endpoint))
        if publication.backend_key_name is None:
            if endpoint.uses_backend_key():
                raise ConflictError(
                    "This publication authenticates with the gateway's managed identity, but its "
                    "endpoint is reached with an API key. Remove the publication and publish the "
                    "model again.",
                    details={"publicationId": publication.id, "modelEndpointId": endpoint.id},
                )
            return _Backend(origin)
        self._require_key_identity(gateway)
        credential = (
            await self._endpoints.get_credential(
                endpoint.tenant_id, endpoint.credential_reference_id
            )
            if endpoint.uses_backend_key() and endpoint.credential_reference_id
            else None
        )
        if credential is None:
            raise ConflictError(
                "MOSAIC can't find the Key Vault secret this endpoint's API key is in. Set the "
                "endpoint's Key Vault secret URI, then plan again.",
                details={"publicationId": publication.id, "modelEndpointId": endpoint.id},
            )
        try:
            secret = KeyVaultSecretId.parse(str(credential.secret_uri))
        except ValueError:
            raise ConflictError(
                "The endpoint's Key Vault secret URI isn't a Key Vault secret identifier. Set it "
                "again, then plan again.",
                details={"publicationId": publication.id, "modelEndpointId": endpoint.id},
            ) from None
        return _Backend(origin, secret.versionless)

    async def _access_snapshot(
        self, publication: Publication, gateway: Gateway
    ) -> tuple[ModelAccessSnapshot | None, list[str]]:
        settings = publication.governed_access
        if settings is None:
            if publication.applied_access is not None:
                raise ValidationError(
                    "An applied governed publication cannot return to legacy mode"
                )
            return None, []
        if self._directory is None or self._entitlements is None:
            raise ValidationError("Governed access repositories are not configured")
        if gateway.capabilities.ai_gateway_policies == CapabilitySupport.UNAVAILABLE:
            raise ValidationError("This gateway does not support the required AI gateway policies")
        if publication.model_api_id is None and publication.created_resources():
            raise ConflictError(
                "Link this existing publication to its model API before planning governed access",
                details={"publicationId": publication.id},
            )
        warnings = [
            "This is a publication-wide batch: every direct grant and authentication-method change "
            "listed in this snapshot will be applied together. Group and other-resource grants "
            "remain desired-state only.",
            "Existing generic, product, all-API and all-access keys will not authorize this API. "
            "The legacy bootstrap subscription is suspended when present.",
            "Governed key authentication uses Ocp-Apim-Subscription-Key (header) or "
            "subscription-key (query); each apply explicitly enforces these parameter names.",
        ]
        grants: list[ModelAccessGrant] = []
        pools: dict[str, AppliedCostCenterPool] = {}
        if publication.model_api_id is not None:
            model = await self._repository.get_model_api(
                publication.tenant_id, publication.model_api_id
            )
            if (
                model is None
                or model.publication_id != publication.id
                or model.gateway_id != publication.gateway_id
                or model.api_name != publication.api_name
                or model.id
                != model_api_id(
                    publication.tenant_id, publication.gateway_id, publication.api_name
                )
            ):
                raise ConflictError("The publication has no trustworthy model API association")
            entitlements = await self._entitlements.list_entitlements(
                publication.tenant_id, resource_id=model.id
            )
            book = await load_book(self._cost_centers, publication.tenant_id)
            saw_security_group_grant = False
            for entitlement in entitlements:
                if entitlement.resource.kind != "modelApi" or entitlement.subject.kind == "group":
                    warnings.append(
                        f"Grant {entitlement.id} is not a supported direct model grant and will "
                        "not be enforced by this apply."
                    )
                    continue
                is_security_group = (
                    entitlement.subject.kind == EntitlementSubjectKind.SECURITY_GROUP
                )
                saw_security_group_grant = saw_security_group_grant or is_security_group
                principal = await self._directory.get_principal(
                    publication.tenant_id, entitlement.subject.id
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
                    # Its subject may have lost the right to charge the cost center, and MOSAIC
                    # hasn't finished checking: it stays out rather than outlive that right.
                    warnings.append(
                        f"Grant {entitlement.id} is waiting for MOSAIC to check that its subject "
                        f"may still charge {charged.name} ({charged.code}); it is excluded from "
                        "runtime access until then. Open the cost center and choose Check grants "
                        "again."
                    )
                    continue
                enforcement = effective_enforcement(entitlement, intent)
                if (
                    publication.enforcement is None
                    and enforcement is not None
                    and enforcement.tokens is not None
                ):
                    # Granting access without the token limits an administrator set would widen
                    # it, so the grant stays out until its limits fit what the gateway can apply.
                    warnings.append(
                        f"Grant {entitlement.id} sets token limits, itself or through its cost "
                        "center's per-person limits, which this gateway can't apply to this "
                        "publication; it is excluded from runtime access. Use call limits "
                        "instead."
                    )
                    continue
                if (
                    publication.enforcement is None
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
                        "this model to apply them."
                    )
                    if warning not in warnings:
                        warnings.append(warning)
                    continue
                grants.append(
                    ModelAccessGrant(
                        entitlement_id=entitlement.id,
                        subject=entitlement.subject,
                        object_id=(
                            principal.object_id.casefold()
                            if is_security_group
                            else principal.object_id
                        ),
                        display_name=principal.label or principal.object_id,
                        subscription_name=(
                            None
                            if is_security_group
                            else model_access_subscription_name(
                                publication.tenant_id, publication.id, entitlement.id
                            )
                        ),
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
                    pools[intent.cost_center_id] = AppliedCostCenterPool(
                        cost_center_id=intent.cost_center_id,
                        cost_center_code=intent.code,
                        monthly_tokens=intent.pool.monthly_tokens,
                        monthly_calls=intent.pool.monthly_calls,
                    )
            if saw_security_group_grant and not self._security_group_claims:
                warnings.append(
                    "Group claims aren't configured for this deployment, so the gateway can't "
                    "match grants to security groups."
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
        return (
            ModelAccessSnapshot(
                version=publication.applied_access.version + 1 if publication.applied_access else 1,
                settings=settings,
                audience=self._runtime_audience,
                publication_enforcement=publication.enforcement,
                grants=grants,
                pools=sorted(pools.values(), key=lambda pool: pool.cost_center_id),
            ),
            warnings,
        )

    @staticmethod
    def _owns(publication: Publication, kind: PublishedResourceKind, name: str) -> bool:
        return any(
            item.kind == kind and item.name == name and item.created_by_mosaic
            for item in publication.resources
        )

    async def _governed_steps(
        self,
        publication: Publication,
        snapshot: ModelAccessSnapshot,
        client: ApimClient,
        resource: ApimResourceId,
    ) -> list[PublishPlanStep]:
        steps: list[PublishPlanStep] = []
        base: dict[tuple[PublishedResourceKind, str], PublishPlanStep] = {}
        # First of all: every governed policy reads the gateway's blocked list. See ADR 0023.
        steps.append(
            BlockedListGate.plan_step(
                resource,
                exists=await client.get_named_value(BLOCKED_COST_CENTERS_NAMED_VALUE) is not None,
            )
        )
        for item in _desired_resources(publication):
            exists = await self._exists(client, publication, item)
            if exists and not self._owns(publication, item.kind, item.name):
                raise ConflictError(
                    f"MOSAIC does not own {item.kind} {item.name}; governed access will not "
                    "replace customer resources or policy.",
                    details={"kind": str(item.kind), "name": item.name},
                )
            base[(item.kind, item.name)] = PublishPlanStep(
                kind=item.kind,
                name=item.name,
                action=PublishAction.UPDATE if exists else PublishAction.CREATE,
                reason=self._reason(item, existed=exists),
                resource_id=f"{resource.canonical}/{item.segment}",
                existed=exists,
                stage="prepare",
            )

        api = base[(PublishedResourceKind.API, publication.api_name)]
        api_policy = base[(PublishedResourceKind.API_POLICY, "policy")]
        if api.existed:
            steps.append(
                api_policy.model_copy(
                    update={
                        "stage": "prepare",
                        "reason": "Temporarily deny all calls while this model-wide batch applies.",
                    }
                )
            )
        if publication.backend_key_name is not None:
            # Before anything that names it: the fragment's key header refers to it.
            steps.append(base[(PublishedResourceKind.NAMED_VALUE, publication.backend_key_name)])
        steps.append(base[(PublishedResourceKind.BACKEND, publication.backend_name)])
        steps.append(
            api.model_copy(
                update={
                    "reason": "Require a subscription until the governed policy is installed, "
                    "and reset key parameter names to Ocp-Apim-Subscription-Key/subscription-key."
                }
            )
        )

        grants = {
            grant.subscription_name: grant
            for grant in snapshot.grants
            if grant.subscription_name is not None
        }
        subscription_names = set(grants)
        subscription_names.update(
            item.name
            for item in publication.resources
            if item.kind == PublishedResourceKind.SUBSCRIPTION and item.created_by_mosaic
        )
        subscriptions: list[PublishPlanStep] = []
        deletions: list[PublishPlanStep] = []
        for name in sorted(subscription_names):
            live = await client.get_subscription(name)
            if live is not None and not self._owns(
                publication, PublishedResourceKind.SUBSCRIPTION, name
            ):
                raise ConflictError(
                    "The planned subscription already exists without MOSAIC ownership",
                    details={"subscriptionName": name},
                )
            if live is not None:
                self._validate_subscription_scope(publication, resource, name, live)
            grant = grants.get(name)
            # Keys are created on request, by a grant's holder or an administrator, and never by
            # an apply. A grant without one keeps none; the policy already knows its name, so a
            # key created later works without another apply.
            if live is None:
                continue
            if grant is not None and grant.revoked:
                deletions.append(
                    PublishPlanStep(
                        kind=PublishedResourceKind.SUBSCRIPTION,
                        name=name,
                        action=PublishAction.DELETE,
                        reason=(
                            f"Delete the key of grant {grant.entitlement_id}, revoked when its "
                            "subject left its cost center."
                        ),
                        resource_id=f"{resource.canonical}/subscriptions/{name}",
                        existed=True,
                        entitlement_id=grant.entitlement_id,
                        stage="prepare",
                    )
                )
                continue
            step = PublishPlanStep(
                kind=PublishedResourceKind.SUBSCRIPTION,
                name=name,
                action=PublishAction.UPDATE,
                reason=(
                    f"Suspend the key of grant {grant.entitlement_id} while access is applied."
                    if grant
                    else "Retire this publication's legacy or obsolete subscription."
                ),
                resource_id=f"{resource.canonical}/subscriptions/{name}",
                existed=True,
                entitlement_id=grant.entitlement_id if grant else None,
                subscription_state="suspended",
                stage="prepare",
            )
            subscriptions.append(step)
        steps.extend(deletions)
        steps.extend(subscriptions)
        for item in _desired_resources(publication):
            if item.kind in {
                PublishedResourceKind.NAMED_VALUE,
                PublishedResourceKind.API,
                PublishedResourceKind.BACKEND,
                PublishedResourceKind.API_POLICY,
            }:
                continue
            step = base[(item.kind, item.name)]
            if item.kind == PublishedResourceKind.POLICY_FRAGMENT:
                step = step.model_copy(update={"stage": "policy"})
            steps.append(step)
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
        for step in subscriptions:
            grant = grants.get(step.name)
            if (
                grant
                and grant.enabled
                and grant.keys_allowed
                and snapshot.settings.keys_enabled
            ):
                steps.append(
                    step.model_copy(
                        update={
                            "stage": "activate",
                            "subscription_state": "active",
                            "reason": f"Activate key access for grant {grant.entitlement_id}.",
                        }
                    )
                )
        return steps

    @staticmethod
    def _validate_subscription_scope(
        publication: Publication, resource: ApimResourceId, name: str, live: dict[str, Any]
    ) -> None:
        properties = live.get("properties") or {}
        relative = (
            f"/products/{publication.product_name}"
            if name == publication.subscription_name
            else f"/apis/{publication.api_name}"
        )
        expected = {relative.casefold(), f"{resource.canonical}{relative}".casefold()}
        if str(properties.get("scope", "")).rstrip("/").casefold() not in expected:
            raise ConflictError(
                "The managed subscription's live scope changed. Resolve it before applying.",
                details={"subscriptionName": name},
            )

    @staticmethod
    def _reason(item: _Resource, *, existed: bool) -> str:
        subject = {
            PublishedResourceKind.NAMED_VALUE: (
                "the named value API Management reads the model endpoint's API key through, from "
                "Key Vault"
            ),
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
        if (
            not publication.subscription_required
            and publication.governed_access is None
            and publication.enforcement is not None
        ):
            warnings.append(
                "This API does not require a subscription, so the token limit counts every "
                "caller together rather than per subscription."
            )
        note = token_limits_note(publication.api_shape, gateway.capabilities.sku_name)
        if note is not None and publication.enforcement is None:
            warnings.append(note)
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
            if item.kind == PublishedResourceKind.API and item.created_by_mosaic
        }
        live_api = await client.get_api(publication.api_name)
        if live_api is not None and publication.api_name not in owned_apis:
            raise ConflictError(
                "An API with this name already exists in the gateway and MOSAIC did not create "
                "it. Choose a different name rather than replacing it.",
                details={"apiName": publication.api_name, "gatewayId": gateway.id},
            )
        for item in _desired_resources(publication):
            if item.kind not in {
                PublishedResourceKind.NAMED_VALUE,
                PublishedResourceKind.POLICY_FRAGMENT,
                PublishedResourceKind.API_POLICY,
            }:
                continue
            if (
                await self._exists(client, publication, item)
                and not self._owns(publication, item.kind, item.name)
            ):
                raise ConflictError(
                    "MOSAIC will not replace customer resources or policy it does not own",
                    details={"kind": str(item.kind), "name": item.name},
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
            case PublishedResourceKind.NAMED_VALUE:
                return await client.get_named_value(item.name) is not None
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
        owner = new_id("publishrun")
        await self._repository.acquire_publication_lock(actor.tenant_id, target_id, owner)
        self._active.add(target_id)
        try:
            return await self._apply_locked(actor, target_id, plan_id, owner)
        except BaseException:
            self._active.discard(target_id)
            await self._repository.release_publication_lock(actor.tenant_id, target_id, owner)
            raise

    async def _apply_locked(
        self, actor: Actor, target_id: str, plan_id: str | None, owner: str
    ) -> PublishRun:
        publication = await self.get_publication(actor, target_id)
        if publication.access_state == "unknown":
            raise ConflictError("Recover this publication's interrupted apply before applying")
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
        if plan is None or plan.target != "model" or plan.publication_id != publication.id:
            raise NotFoundError("Publish plan was not found", details={"id": resolved})
        if plan.operation != "publish" or any(
            step.action == PublishAction.DELETE
            # A publish plan deletes only a revoked grant's key, before it applies anything else.
            and not (step.kind == PublishedResourceKind.SUBSCRIPTION and step.stage == "prepare")
            for step in plan.steps
        ):
            # Plans saved before unpublishing was planned carry no operation, only delete steps.
            raise ConflictError(
                "That plan unpublishes this model, and apply runs only publish plans. Re-plan "
                "the publication to publish it.",
                details={"planId": plan.id, "operation": "unpublish"},
            )

        catalog = await load_environment_catalog(self._environments, actor.tenant_id)
        verdict = permits(catalog, gateway.environment, endpoint.environment)
        refuse_blocked_pairing(verdict)
        environment_fingerprint = compatibility_fingerprint(
            catalog, gateway.environment, endpoint.environment
        )
        backend = await self._backend(publication, endpoint, gateway)
        snapshot, _ = await self._access_snapshot(publication, gateway)
        policy = self._policy(publication, snapshot)
        if (
            publication_digest(
                publication,
                policy,
                backend.origin,
                snapshot,
                environment_fingerprint=environment_fingerprint,
                key_secret=backend.key_secret,
            )
            != plan.digest
        ):
            raise ConflictError(
                "This publication changed after the plan was produced. Re-plan it and review the "
                "new changes before applying.",
                details={"planId": plan.id, "planDigest": plan.digest},
            )
        if not snapshot and not _follows_create_order(plan.steps):
            raise ConflictError(
                "This plan does not create resources in the order MOSAIC now uses, so API "
                "Management could reject a step that names a resource not created yet. Re-plan "
                "this publication and review the new order before applying.",
                details={"planId": plan.id},
            )

        run = self._claim(actor, publication, plan, owner)
        await self._repository.save_publish_run(run)
        await self._mark_applying(publication, run.id)
        resource = ApimResourceId.parse(gateway.azure_resource_id)
        client = self._client_factory(resource)
        if snapshot:
            self._spawn(
                self._run_governed_apply(publication, gateway, client, plan, policy, backend, run)
            )
        else:
            self._spawn(self._run_apply(publication, gateway, client, plan, policy, backend, run))
        return run

    async def plan_unpublish(self, actor: Actor, target_id: str) -> PublishPlan:
        """Plan what unpublishing removes and whose access it ends, without removing anything.

        The plan lists every resource MOSAIC created, in the order unpublish deletes them, and
        carries the access the gateway applied last, so an administrator sees who loses access
        before confirming. It changes nothing on the publication. Unpublish runs only such a plan,
        and only while :func:`unpublish_digest` still matches it.
        """

        async with publication_lock(self._repository, actor.tenant_id, target_id):
            publication = await self.get_publication(actor, target_id)
            gateway = await self._load_gateway(actor, publication.gateway_id)
            self._require_writable(gateway)
            removals = self._unpublish_removals(publication)
            plan = PublishPlan(
                id=new_id("publishplan"),
                tenant_id=actor.tenant_id,
                publication_id=publication.id,
                operation="unpublish",
                gateway_id=gateway.id,
                digest=unpublish_digest(publication),
                steps=[self._removal_step(publication, item) for item in removals],
                warnings=self._unpublish_warnings(publication),
                actor_object_id=actor.object_id,
                access_snapshot=publication.applied_access,
            )
            await self._repository.save_publish_plan(plan)
            return plan

    async def unpublish(
        self, actor: Actor, target_id: str, plan_id: str | None = None
    ) -> PublishRun:
        """Run a reviewed unpublish plan: exactly its steps, and only if it still matches."""

        if not plan_id:
            raise ConflictError(
                "Review what unpublishing removes before you unpublish. Plan the unpublish, check "
                "the resources and grants it lists, then unpublish that plan.",
                details={"id": target_id, "reason": "planRequired"},
            )
        owner = new_id("publishrun")
        await self._repository.acquire_publication_lock(actor.tenant_id, target_id, owner)
        self._active.add(target_id)
        try:
            return await self._unpublish_locked(actor, target_id, plan_id, owner)
        except BaseException:
            self._active.discard(target_id)
            await self._repository.release_publication_lock(actor.tenant_id, target_id, owner)
            raise

    async def _unpublish_locked(
        self, actor: Actor, target_id: str, plan_id: str, owner: str
    ) -> PublishRun:
        publication = await self.get_publication(actor, target_id)
        gateway = await self._load_gateway(actor, publication.gateway_id)
        self._require_writable(gateway)
        plan = await self._repository.get_publish_plan(actor.tenant_id, plan_id)
        if (
            plan is None
            or plan.target != "model"
            or plan.operation != "unpublish"
            or plan.publication_id != publication.id
        ):
            raise NotFoundError("Unpublish plan was not found", details={"id": plan_id})
        if unpublish_digest(publication) != plan.digest:
            raise ConflictError(
                "This publication changed after its unpublish plan was made, so the plan no "
                "longer says what unpublishing removes or who loses access. Review a new plan "
                "before you unpublish.",
                details={"planId": plan.id, "reason": "stalePlan"},
            )
        run = self._claim(actor, publication, plan, owner)
        await self._repository.save_publish_run(run)
        await self._mark_applying(publication, run.id)
        self._spawn(self._run_unpublish(publication, gateway, plan, run))
        return run

    @staticmethod
    def _unpublish_removals(publication: Publication) -> list[PublishedResource]:
        owned = publication.created_resources()
        if not owned:
            raise ConflictError(
                "MOSAIC did not create anything in API Management for this publication, so there "
                "is nothing to remove.",
                details={"id": publication.id},
            )
        order = {kind: index for index, kind in enumerate(CREATE_ORDER)}
        removals = sorted(owned, key=lambda item: order[item.kind], reverse=True)
        if _is_governed(publication):
            # Keep the deny policy attached until the API itself is gone. Removing a policy first
            # can expose the backend under inherited APIM policies while cleanup is still running.
            removals.sort(key=lambda item: item.kind != PublishedResourceKind.API)
        return removals

    @staticmethod
    def _removal_step(publication: Publication, item: PublishedResource) -> PublishPlanStep:
        grant = next(
            (
                grant
                for grant in (
                    publication.applied_access.grants if publication.applied_access else []
                )
                if item.kind == PublishedResourceKind.SUBSCRIPTION
                and grant.subscription_name == item.name
            ),
            None,
        )
        return PublishPlanStep(
            kind=item.kind,
            name=item.name,
            action=PublishAction.DELETE,
            reason=_removal_reason(publication, item, grant),
            resource_id=item.resource_id,
            existed=True,
            entitlement_id=grant.entitlement_id if grant else None,
        )

    @staticmethod
    def _unpublish_warnings(publication: Publication) -> list[str]:
        warnings: list[str] = []
        if _is_governed(publication):
            warnings.append(
                "Before it deletes anything, MOSAIC replaces this API's policy with one that "
                "refuses every call, and suspends every grant's subscription, so callers are cut "
                "off first."
            )
        if any(
            item.kind == PublishedResourceKind.PRODUCT and item.name == publication.product_name
            for item in publication.created_resources()
        ):
            warnings.append(
                f"Deleting product {publication.product_name} also deletes every subscription to "
                "it, including any that API Management or another administrator added."
            )
        warnings.extend(
            f"MOSAIC didn't create {_resource_label(publication, item)}, so it stays in API "
            "Management."
            for item in publication.resources
            if not item.created_by_mosaic
        )
        return warnings

    def _claim(
        self, actor: Actor, publication: Publication, plan: PublishPlan, owner: str
    ) -> PublishRun:
        return PublishRun(
            id=owner,
            tenant_id=publication.tenant_id,
            publication_id=publication.id,
            gateway_id=publication.gateway_id,
            plan_id=plan.id,
            plan_digest=plan.digest,
            actor_object_id=actor.object_id,
            access_snapshot=plan.access_snapshot,
        )

    def _spawn(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task: asyncio.Task[None] = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._task_finished)

    def _task_finished(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            self._task_failures.append(error)

    async def _assert_lock(self, publication: Publication, run: PublishRun) -> None:
        if (
            await self._repository.get_publication_lock(publication.tenant_id, publication.id)
            != run.id
        ):
            raise ConflictError("The publication write lock is no longer owned by this run")

    async def _release_run(self, publication: Publication, run: PublishRun) -> None:
        await self._repository.release_publication_lock(
            publication.tenant_id, publication.id, run.id
        )

    async def _progress(
        self,
        publication: Publication,
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
        )

    async def _interrupted(
        self, publication: Publication, run: PublishRun, message: str
    ) -> None:
        # Best effort diagnostics are not a successful apply. The non-expiring lock remains even
        # if storage is unavailable, so a different instance cannot apply or release credentials.
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
            current_publication = await self._repository.get_publication(
                publication.tenant_id, publication.id
            )
            if current_publication and current_publication.last_run_id == run.id:
                await self._repository.record_publication_state(
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
            logger.exception("publish_interruption_not_recorded", publication_id=publication.id)

    async def _run_governed_apply(
        self,
        publication: Publication,
        gateway: Gateway,
        client: ApimClient,
        plan: PublishPlan,
        policy: PublicationPolicy,
        backend: _Backend,
        run: PublishRun,
    ) -> None:
        started = utc_now()
        writer = self._writer_factory(ApimResourceId.parse(gateway.azure_resource_id))
        results: list[PublishStepResult] = []
        owned = list(publication.resources)
        failure: str | None = None
        items = {(item.kind, item.name): item for item in _desired_resources(publication)}
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
                    item = items.get((step.kind, step.name)) or _Resource(
                        step.kind, step.name, f"subscriptions/{step.name}"
                    )
                    write_started = False
                    try:
                        if is_blocked_list(step.kind, step.name):
                            # Before any policy that reads it. MOSAIC keeps it for the gateway, so
                            # the publication never owns it, and no rollback deletes it.
                            await ensure_blocked_list(
                                self._blocked_list, client, writer, publication.tenant_id
                            )
                            result.status = PublishStepStatus.SUCCEEDED
                            await self._progress(publication, run, results, owned)
                            continue
                        if step.action == PublishAction.DELETE:
                            owned = await self._delete_revoked_key(
                                publication, client, writer, step, owned
                            )
                            result.status = PublishStepStatus.SUCCEEDED
                            await self._progress(publication, run, results, owned)
                            continue
                        exists = await self._exists(client, publication, item)
                        current = publication.model_copy(update={"resources": owned})
                        if exists and not self._owns(current, step.kind, step.name):
                            raise ConflictError(
                                f"{step.kind} {step.name} appeared without MOSAIC ownership; "
                                "the reviewed plan cannot overwrite it."
                            )
                        if step.kind == PublishedResourceKind.SUBSCRIPTION and exists:
                            live = await client.get_subscription(step.name)
                            if live is not None:
                                self._validate_subscription_scope(
                                    publication, writer.resource, step.name, live
                                )
                        result.created_by_mosaic = not exists
                        await self._progress(publication, run, results, owned)
                        write_started = True
                        await self._write_governed_step(
                            writer, publication, policy, backend, step, item, plan.access_snapshot
                        )
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
                            # A failed ARM poll/response is not proof that the create did nothing.
                            # Retain confirmed creation for denial and subsequent reconciliation.
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
                        break
                if failure is None:
                    await self._finish_success(
                        publication, run, results, started, access_snapshot=plan.access_snapshot
                    )
            except Exception as error:
                failure = str(error)
            if failure is not None:
                await self._governed_failure(
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

    async def _delete_revoked_key(
        self,
        publication: Publication,
        client: ApimClient,
        writer: ApimWriter,
        step: PublishPlanStep,
        owned: list[PublishedResource],
    ) -> list[PublishedResource]:
        """Delete a revoked grant's key, which only this publication may own, and forget it."""

        current = publication.model_copy(update={"resources": owned})
        if step.kind != PublishedResourceKind.SUBSCRIPTION or not self._owns(
            current, PublishedResourceKind.SUBSCRIPTION, step.name
        ):
            raise ConflictError(
                "MOSAIC deletes only the keys it created for this publication's grants.",
                details={"subscriptionName": step.name},
            )
        live = await client.get_subscription(step.name)
        if live is not None:
            self._validate_subscription_scope(publication, writer.resource, step.name, live)
            await writer.delete_subscription(step.name)
        return [
            item
            for item in owned
            if not (item.kind == PublishedResourceKind.SUBSCRIPTION and item.name == step.name)
        ]

    async def _write_governed_step(
        self,
        writer: ApimWriter,
        publication: Publication,
        policy: PublicationPolicy,
        backend: _Backend,
        step: PublishPlanStep,
        item: _Resource,
        snapshot: ModelAccessSnapshot | None,
    ) -> None:
        if step.kind == PublishedResourceKind.API_POLICY and step.stage == "prepare":
            await writer.put_api_policy(publication.api_name, DENY_ALL_POLICY)
        elif step.kind == PublishedResourceKind.SUBSCRIPTION:
            grant = next(
                (
                    grant
                    for grant in snapshot.grants
                    if grant.subscription_name is not None and grant.subscription_name == step.name
                ),
                None,
            ) if snapshot else None
            if step.name == publication.subscription_name:
                await writer.put_subscription(
                    step.name,
                    display_name=publication.display_name,
                    product_name=publication.product_name,
                    state="suspended",
                )
            else:
                await writer.put_api_subscription(
                    step.name,
                    display_name=(
                        grant_key_display_name(grant.display_name, grant.cost_center_code)
                        if grant
                        else publication.display_name
                    ),
                    api_name=publication.api_name,
                    state=step.subscription_state or "suspended",
                )
        else:
            effective = publication
            if step.kind == PublishedResourceKind.API and step.stage == "prepare":
                effective = publication.model_copy(update={"subscription_required": True})
            await self._write(writer, effective, policy, backend, item)

    async def _establish_deny(
        self,
        publication: Publication,
        client: ApimClient,
        writer: ApimWriter,
        journal: RecoveryJournal | None = None,
    ) -> tuple[bool, list[str]]:
        """Deny before restoring typed policy. Never remove a guard as part of rollback."""

        errors: list[str] = []
        if not self._owns(publication, PublishedResourceKind.API, publication.api_name):
            return True, errors  # No owned API has been written, so no target grant is live.
        try:
            if await client.get_api(publication.api_name) is None:
                return True, errors
            existed = await client.get_api_policy(publication.api_name) is not None
            await writer.put_api_policy(publication.api_name, DENY_ALL_POLICY)
            if journal:
                await journal(PublishedResourceKind.API_POLICY, "policy", not existed, "policy")
            return True, errors
        except Exception as error:
            errors.append(f"Installing the deny-all API policy failed: {error}")
        if self._owns(
            publication, PublishedResourceKind.POLICY_FRAGMENT, publication.fragment_name
        ):
            try:
                await writer.put_policy_fragment(
                    publication.fragment_name,
                    DENY_ALL_FRAGMENT,
                    description="MOSAIC fail-closed recovery",
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
                errors.append(f"Installing the deny-all fragment failed: {error}")
        return False, errors

    async def _suspend_owned_subscriptions(
        self,
        publication: Publication,
        client: ApimClient,
        writer: ApimWriter,
        journal: RecoveryJournal | None = None,
    ) -> list[str]:
        errors: list[str] = []
        for resource in publication.created_resources():
            if resource.kind != PublishedResourceKind.SUBSCRIPTION:
                continue
            try:
                live = await client.get_subscription(resource.name)
                if live is None:
                    continue
                self._validate_subscription_scope(publication, writer.resource, resource.name, live)
                if resource.name == publication.subscription_name:
                    await writer.put_subscription(
                        resource.name,
                        display_name=publication.display_name,
                        product_name=publication.product_name,
                        state="suspended",
                    )
                else:
                    # A grant's key keeps its name, which ends with its cost center's code.
                    grant = next(
                        (
                            item
                            for item in (
                                publication.applied_access.grants
                                if publication.applied_access
                                else []
                            )
                            if item.subscription_name == resource.name
                        ),
                        None,
                    )
                    await writer.put_api_subscription(
                        resource.name,
                        display_name=(
                            grant_key_display_name(grant.display_name, grant.cost_center_code)
                            if grant
                            else publication.display_name
                        ),
                        api_name=publication.api_name,
                        state="suspended",
                    )
                if journal:
                    await journal(
                        PublishedResourceKind.SUBSCRIPTION, resource.name, False, "prepare"
                    )
            except Exception as error:
                errors.append(f"Suspending subscription {resource.name} failed: {error}")
        return errors

    def _recovery_journal(
        self,
        publication: Publication,
        writer: ApimWriter,
        run: PublishRun,
        results: list[PublishStepResult],
        resources: list[PublishedResource],
    ) -> RecoveryJournal:
        async def record(
            kind: PublishedResourceKind, name: str, created: bool, stage: str
        ) -> None:
            item = next(
                (
                    item
                    for item in _desired_resources(publication)
                    if item.kind == kind and item.name == name
                ),
                _Resource(kind, name, f"subscriptions/{name}"),
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

    async def _governed_failure(
        self,
        publication: Publication,
        client: ApimClient,
        writer: ApimWriter,
        run: PublishRun,
        results: list[PublishStepResult],
        resources: list[PublishedResource],
        started: datetime,
        failure: str,
    ) -> None:
        target = run.access_snapshot
        if target is None:
            raise RuntimeError("A governed run must record its reviewed access snapshot")
        await self._assert_lock(publication, run)
        actual = publication.model_copy(update={"resources": resources})
        journal = self._recovery_journal(publication, writer, run, results, resources)
        denied, recovery_errors = await self._establish_deny(actual, client, writer, journal)
        recovery_errors.extend(
            await self._suspend_owned_subscriptions(actual, client, writer, journal)
        )
        safe = denied_access_snapshot(target)
        if denied:
            # If persisting progress fails here, the API stays denied and the lock is retained.
            await self._progress(publication, run, results, resources)
            candidate = safe_access_snapshot(publication, target)
            if self._owns(actual, PublishedResourceKind.API, publication.api_name):
                try:
                    await self._assert_lock(publication, run)
                    restored = self._policy(publication, candidate)
                    # The restored policy reads the gateway's blocked list, so it must exist.
                    await ensure_blocked_list(
                        self._blocked_list, client, writer, publication.tenant_id
                    )
                    await writer.put_policy_fragment(
                        publication.fragment_name,
                        restored.fragment_xml,
                        description="MOSAIC last safe access snapshot",
                    )
                    await journal(
                        PublishedResourceKind.POLICY_FRAGMENT,
                        publication.fragment_name,
                        not self._owns(
                            actual, PublishedResourceKind.POLICY_FRAGMENT, publication.fragment_name
                        ),
                        "policy",
                    )
                    await writer.put_api_policy(publication.api_name, restored.api_policy_xml)
                    await journal(PublishedResourceKind.API_POLICY, "policy", False, "policy")
                    await writer.put_api(
                        publication.api_name,
                        display_name=publication.display_name,
                        path=publication.api_path,
                        subscription_required=not candidate.settings.entra_enabled,
                        description="Published by MOSAIC; restricted recovery state.",
                        use_default_subscription_key_names=True,
                    )
                    await journal(
                        PublishedResourceKind.API, publication.api_name, False, "activate"
                    )
                    for grant in candidate.grants:
                        if (
                            not grant.enabled
                            or not grant.keys_allowed
                            or not candidate.settings.keys_enabled
                            or grant.subscription_name is None
                        ):
                            continue
                        # Keys are created on request, so most grants own none. Recovery only
                        # reactivates keys the publication owns and that still exist; it never
                        # creates one.
                        if not self._owns(
                            actual, PublishedResourceKind.SUBSCRIPTION, grant.subscription_name
                        ):
                            continue
                        live = await client.get_subscription(grant.subscription_name)
                        if live is None:
                            continue
                        self._validate_subscription_scope(
                            publication, writer.resource, grant.subscription_name, live
                        )
                        await writer.put_api_subscription(
                            grant.subscription_name,
                            display_name=grant_key_display_name(
                                grant.display_name, grant.cost_center_code
                            ),
                            api_name=publication.api_name,
                            state="active",
                        )
                        await journal(
                            PublishedResourceKind.SUBSCRIPTION,
                            grant.subscription_name,
                            False,
                            "activate",
                        )
                    safe = candidate
                except Exception as error:
                    recovery_errors.append(
                        f"Restoring the last safe access snapshot failed: {error}"
                    )
                    denied, guard_errors = await self._establish_deny(
                        actual, client, writer, journal
                    )
                    recovery_errors.extend(guard_errors)
                    recovery_errors.extend(
                        await self._suspend_owned_subscriptions(actual, client, writer, journal)
                    )
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
                "Access was restricted to the last safe snapshot. Subscriptions were retained "
                "for a reviewed retry; suspension failures are listed above. No keys were rotated."
                if denied
                else "Runtime denial could not be confirmed. The durable lock is retained; "
                "stop the original writer and complete explicit recovery."
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

    async def _run_apply(
        self,
        publication: Publication,
        gateway: Gateway,
        client: ApimClient,
        plan: PublishPlan,
        policy: PublicationPolicy,
        backend: _Backend,
        run: PublishRun,
    ) -> None:
        started = utc_now()
        writer = self._writer_factory(ApimResourceId.parse(gateway.azure_resource_id))
        resources = {(item.kind, item.name): item for item in _desired_resources(publication)}
        tracked = list(publication.resources)
        results: list[PublishStepResult] = []
        failure: str | None = None
        try:
            for step in plan.steps:
                await self._assert_lock(publication, run)
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
                    await self._progress(publication, run, [*results, result], tracked)
                    await self._write(writer, publication, policy, backend, item)
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
                await self._progress(publication, run, results, tracked)
        except asyncio.CancelledError:
            await self._interrupted(publication, run, STALE_RUN_MESSAGE)
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
            await self._release_run(publication, run)
        except asyncio.CancelledError:
            await self._interrupted(publication, run, STALE_RUN_MESSAGE)
            raise
        except Exception as error:
            await self._interrupted(publication, run, f"Publish result was not recorded: {error}")
            raise
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
        governed = _is_governed(publication)
        denied_snapshot = (
            denied_access_snapshot(publication.applied_access).model_copy(
                update={"version": publication.applied_access.version + 1}
            )
            if publication.applied_access
            else None
        )
        try:
            await self._assert_lock(publication, run)
            if governed:
                client = self._client_factory(writer.resource)
                denied, guard_errors = await self._establish_deny(publication, client, writer)
                errors.extend(guard_errors)
                errors.extend(
                    await self._suspend_owned_subscriptions(publication, client, writer)
                )
                if not denied:
                    raise ConflictError(
                        "Unpublish could not establish a fail-closed runtime policy"
                    )
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
                await self._progress(publication, run, results, remaining)
                if governed and result.status == PublishStepStatus.FAILED:
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
                status=(
                    PublicationStatus.DRAFT if succeeded else PublicationStatus.FAILED
                ),
                resources=remaining,
                run_id=run.id,
                error="; ".join(errors) or None,
                applied=False,
                unpublished=succeeded,
                access_snapshot=denied_snapshot,
                access_state=("applied" if succeeded else "failed") if governed else None,
            )
            if succeeded and governed:
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
        await self._finish_run(
            run, status, started, results=results, errors=errors, orphaned=orphaned
        )

    async def _finish_success(
        self,
        publication: Publication,
        run: PublishRun,
        results: list[PublishStepResult],
        started: datetime,
        *,
        access_snapshot: ModelAccessSnapshot | None = None,
    ) -> None:
        await self._assert_lock(publication, run)
        applied_at = utc_now()
        # A revoked grant's key this run deleted is no longer the publication's.
        deleted = {
            (result.kind, result.name)
            for result in results
            if result.status == PublishStepStatus.SUCCEEDED
            and result.action == PublishAction.DELETE
        }
        resources = _merge_resources(
            [item for item in publication.resources if (item.kind, item.name) not in deleted],
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
                and result.action != PublishAction.DELETE
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
            access_state="applied" if access_snapshot else None,
        )
        current = await self._repository.get_publication(publication.tenant_id, publication.id)
        if current is None:
            raise ConflictError("The publication disappeared while applying")
        await self._materialize_model_api(
            Actor(run.actor_object_id or "system:publishing", publication.tenant_id), current
        )
        if access_snapshot:
            await self._project_bindings(current, access_snapshot, run)
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
                    "rolled_back": status
                    in {PublishRunStatus.ROLLED_BACK, PublishRunStatus.ROLLBACK_FAILED},
                    "orphaned_resources": orphaned,
                    "errors": errors,
                    "updated_at": completed,
                }
            )
        )

    async def _project_bindings(
        self,
        publication: Publication,
        snapshot: ModelAccessSnapshot | None,
        run: PublishRun,
    ) -> None:
        if self._entitlements is None:
            return
        from mosaic_api.integrations.access_policy import (
            governed_counter_key_expression,
            grant_counter_identity,
        )

        grants = {grant.entitlement_id: grant for grant in snapshot.grants} if snapshot else {}
        records = await self._entitlements.list_entitlements(
            publication.tenant_id,
            resource_id=publication.model_api_id
            or model_api_id(
                publication.tenant_id, publication.gateway_id, publication.api_name
            ),
        )
        actor = Actor(run.actor_object_id or "system:publishing", publication.tenant_id)
        for entitlement in records:
            grant = grants.get(entitlement.id)
            binding = entitlement.binding
            if grant and grant.enabled and snapshot and (
                snapshot.settings.keys_enabled or snapshot.settings.entra_enabled
            ):
                binding = EntitlementBinding(
                    gateway_id=publication.gateway_id,
                    apim_subscription_name=grant.subscription_name,
                    counter_key_expression=governed_counter_key_expression(publication, grant),
                    attribution_key=grant_counter_identity(publication, grant),
                    attribution_per_member=grant.is_group_grant,
                    source=BindingSource.ORCHESTRATED,
                    bound_at=utc_now(),
                )
            elif binding and binding.source == BindingSource.ORCHESTRATED:
                binding = None
            else:
                continue
            await self._entitlements.save_entitlement(
                entitlement.model_copy(update={"binding": binding, "runtime": None}),
                self._audit(actor, "entitlement.runtimeProjected", entitlement.id, "entitlement"),
            )

    async def _mark_applying(self, publication: Publication, run_id: str) -> None:
        await self._record_state(
            publication,
            status=PublicationStatus.APPLYING,
            resources=publication.resources,
            run_id=run_id,
            error=None,
            applied=False,
            access_state="applying" if publication.governed_access else None,
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
        unpublished: bool = False,
        access_snapshot: ModelAccessSnapshot | None = None,
        access_state: str | None = None,
    ) -> None:
        """Re-read before writing, so an apply never resurrects a publication that was removed."""

        current = await self._repository.get_publication(publication.tenant_id, publication.id)
        if current is None:
            raise ConflictError("The publication disappeared while applying")
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
        await self._repository.record_publication_state(current.model_copy(update=updates))

    async def _write(
        self,
        writer: ApimWriter,
        publication: Publication,
        policy: PublicationPolicy,
        backend: _Backend,
        item: _Resource,
    ) -> None:
        match item.kind:
            case PublishedResourceKind.NAMED_VALUE:
                if backend.key_secret is None:
                    raise ConflictError(
                        "MOSAIC doesn't know which Key Vault secret holds this endpoint's key. "
                        "Plan the publication again.",
                        details={"publicationId": publication.id},
                    )
                await writer.put_named_value(item.name, secret_identifier=backend.key_secret)
            case PublishedResourceKind.POLICY_FRAGMENT:
                await writer.put_policy_fragment(
                    item.name,
                    policy.fragment_xml,
                    description=f"MOSAIC enforcement for {publication.display_name}",
                )
            case PublishedResourceKind.BACKEND:
                await writer.put_backend(
                    item.name,
                    url=backend.origin,
                    title=f"MOSAIC backend for {publication.display_name}",
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
                    use_default_subscription_key_names=publication.governed_access is not None,
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
                    subscription_required=(
                        True if publication.governed_access else publication.subscription_required
                    ),
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
            case PublishedResourceKind.NAMED_VALUE:
                await writer.delete_named_value(name)
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
        if not run or run.target != "model":
            raise NotFoundError("Publish run was not found", details={"id": run_id})
        return run

    async def list_runs(self, actor: Actor, target_id: str) -> list[PublishRun]:
        await self.get_publication(actor, target_id)
        return [
            run
            for run in await self._repository.list_publish_runs(actor.tenant_id, target_id)
            if run.target == "model"
        ]

    async def get_plan(self, actor: Actor, plan_id: str) -> PublishPlan:
        plan = await self._repository.get_publish_plan(actor.tenant_id, plan_id)
        if not plan or plan.target != "model":
            raise NotFoundError("Publish plan was not found", details={"id": plan_id})
        return plan

    async def wait_for_idle(self) -> None:
        """Drain in-flight applies. Used by tests and shutdown, not by request handlers."""

        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        if self._task_failures:
            error = self._task_failures.pop(0)
            self._task_failures.clear()
            raise error

    async def reap_stale_publish_runs(self, tenant_id: str) -> int:
        """Only diagnose pre-lock legacy runs; never reap another instance's durable owner."""

        stale = await self._repository.list_unfinished_publish_runs(tenant_id)
        active = set(self._active)
        reaped = 0
        for run in stale:
            if run.target != "model":
                continue
            if run.publication_id in active:
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
            publication = await self._repository.get_publication(tenant_id, run.publication_id)
            if publication and publication.status == PublicationStatus.APPLYING:
                await self._repository.record_publication_state(
                    publication.model_copy(
                        update={
                            "status": PublicationStatus.FAILED,
                            "last_error": STALE_RUN_MESSAGE,
                            "access_state": (
                                "unknown"
                                if publication.governed_access
                                else publication.access_state
                            ),
                            "updated_at": completed,
                        }
                    )
                )
            reaped += 1
        return reaped

    async def recover_interrupted(
        self,
        actor: Actor,
        target_id: str,
        *,
        run_id: str,
        confirm_quiesced: bool = False,
    ) -> PublishRun:
        """Inspect a retained lock, or explicitly deny access after an operator quiesces ARM.

        ``confirm_quiesced`` attests that the original worker has been stopped and *all* ARM
        operations it submitted have reached a terminal state. A mere timeout/restart is not
        evidence: APIM cannot fence an already-submitted asynchronous operation. There is no
        automated lock theft, lease expiry, or startup recovery.
        """

        owner = await self._repository.get_publication_lock(actor.tenant_id, target_id)
        if owner != run_id:
            raise ConflictError("Recovery must name the exact run holding this publication's lock")
        publication = await self._repository.get_publication(actor.tenant_id, target_id)
        run = await self._repository.get_publish_run(actor.tenant_id, run_id)
        if run is None and run_id.startswith("mutation_"):
            diagnostic = PublishRun(
                id=run_id,
                tenant_id=actor.tenant_id,
                publication_id=target_id,
                gateway_id=publication.gateway_id if publication else "",
                plan_id="",
                plan_digest="",
                actor_object_id=actor.object_id,
                status=PublishRunStatus.INTERRUPTED,
                errors=[
                    "Retained mutation or credential-read lock; process liveness is unknown. "
                    "No ARM apply was admitted. Confirm the original process is stopped before "
                    "releasing this lock."
                ],
            )
            if not confirm_quiesced:
                return diagnostic
            if local_mutation_active(run_id):
                raise ConflictError("This process still has an active desired-state mutation")
            if publication:
                await self._repository.save_publication(
                    publication.model_copy(
                        update={
                            "last_plan_id": None,
                            "last_plan_digest": None,
                            "updated_at": utc_now(),
                        }
                    ),
                    self._audit(actor, "publication.mutationRecovered", target_id),
                )
            diagnostic.completed_at = utc_now()
            await self._repository.save_publish_run(diagnostic)
            await self._repository.release_publication_lock(actor.tenant_id, target_id, run_id)
            return diagnostic
        if (
            run is None
            or run.target != "model"
            or publication is None
            or run.publication_id != target_id
        ):
            raise ConflictError(
                "The retained lock has no matching publication/run recovery evidence"
            )
        if not confirm_quiesced:
            return run
        if target_id in self._active:
            raise ConflictError(
                "This process still has an active writer. Stop it before attempting recovery."
            )
        gateway = await self._load_gateway(actor, publication.gateway_id)
        self._require_writable(gateway)
        client = self._client_factory(ApimResourceId.parse(gateway.azure_resource_id))
        writer = self._writer_factory(ApimResourceId.parse(gateway.azure_resource_id))
        await self._assert_lock(publication, run)
        # Pending steps identify writes the old process may have completed before it lost storage.
        # Recovery retains that evidence rather than dropping an unrecorded resource on the floor.
        resources = list(publication.resources)
        for step in run.steps:
            if not step.created_by_mosaic or step.action == PublishAction.DELETE:
                continue
            item = _Resource(step.kind, step.name, "")
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
        governed = publication.governed_access is not None or run.access_snapshot is not None
        errors: list[str] = []
        safe: ModelAccessSnapshot | None = None
        recovery_steps = list(run.steps)
        if governed:
            journal = self._recovery_journal(
                actual, writer, run, recovery_steps, resources
            )
            denied, errors = await self._establish_deny(actual, client, writer, journal)
            errors.extend(await self._suspend_owned_subscriptions(actual, client, writer, journal))
            if not denied or errors:
                await self._interrupted(
                    actual,
                    run,
                    "Explicit recovery could not confirm complete denial: " + "; ".join(errors),
                )
                return await self.get_run(actor, run.id)
            snapshot = run.access_snapshot or publication.applied_access
            if snapshot:
                safe = denied_access_snapshot(snapshot).model_copy(
                    update={
                        "version": max(
                            snapshot.version,
                            publication.applied_access.version if publication.applied_access else 0,
                        ) + 1
                    }
                )
        message = (
            "Operator-confirmed quiescence: runtime access is denied. Re-plan and review before "
            "restoring any grants."
            if governed
            else "Operator-confirmed quiescence: ownership retained. Re-plan before retrying."
        )
        await self._record_state(
            publication,
            status=PublicationStatus.FAILED,
            resources=resources,
            run_id=run.id,
            error=message,
            applied=False,
            access_snapshot=safe,
            access_state="failed" if governed else "pending",
        )
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
        current = await self.get_publication(actor, target_id)
        await self._repository.save_publication(
            current, self._audit(actor, "publication.recovered", target_id)
        )
        await self._release_run(publication, run)
        return recovered
