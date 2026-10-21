"""Live Azure model discovery and explicitly reviewed, creation-only deployment runs."""

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import structlog

from mosaic_api.clouds import ConnectionValidator, validate_resource_connection
from mosaic_api.deployment_capacity import CapacityType, ProcessingScope, classify_sku
from mosaic_api.domain import (
    AuditEvent,
    CognitiveServicesResourceId,
    EndpointAuthMode,
    GatewaySyncStatus,
    ManagementMode,
    ModelEndpoint,
    ModelProvider,
    deterministic_id,
    new_id,
    utc_now,
)
from mosaic_api.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    UpstreamAuthorizationError,
    UpstreamConflictError,
    UpstreamError,
    UpstreamNotFoundError,
    ValidationError,
)
from mosaic_api.integrations.aoai.client import CognitiveServicesClient
from mosaic_api.integrations.aoai.deployment_access import deployment_access
from mosaic_api.integrations.aoai.deployment_catalogue import deployment_models, number
from mosaic_api.integrations.aoai.inventory import _int_or_none, _properties, _text
from mosaic_api.integrations.aoai.writer import ModelDeploymentWriter
from mosaic_api.integrations.apim.client import JsonObject
from mosaic_api.model_deployments import (
    DeploymentAccess,
    DeploymentCatalogue,
    DeploymentChecks,
    DeploymentRecoveryRequest,
    DeploymentRunStatus,
    DeploymentTarget,
    ModelDeploymentCreate,
    ModelDeploymentPlan,
    ModelDeploymentRun,
    deployment_lock_scope,
)
from mosaic_api.repositories import GatewayRepository, ModelEndpointRepository
from mosaic_api.services.directory import Actor
from mosaic_api.services.model_access import scope_lease
from mosaic_api.services.model_endpoints import ModelEndpointService

logger = structlog.get_logger()

_FAILED = {"failed", "canceled", "cancelled", "disabled"}
_SUPPORTED = {ModelProvider.AZURE_OPENAI, ModelProvider.AZURE_AI_FOUNDRY}
_CATALOGUE_ONLY = (
    "Azure quota and capacity checks do not reserve capacity. Provisioning can still be refused "
    "if Azure availability changes. Deploying does not publish through API Management."
)


def _expired(value: str | None) -> bool:
    if not value:
        return False
    try:
        expiry = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError("Azure's deprecation date could not be verified") from None
    return (expiry if expiry.tzinfo else expiry.replace(tzinfo=UTC)) <= utc_now()


def _digest(target: DeploymentTarget, configuration: ModelDeploymentCreate) -> str:
    payload = {
        "target": target.model_dump(mode="json"),
        "configuration": configuration.model_dump(mode="json"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class ModelDeploymentService:
    def __init__(
        self,
        repository: ModelEndpointRepository,
        *,
        gateway_repository: GatewayRepository,
        endpoint_service: ModelEndpointService,
        client_factory: Callable[[CognitiveServicesResourceId], CognitiveServicesClient],
        writer_factory: Callable[[CognitiveServicesResourceId], ModelDeploymentWriter],
        connection_validator: ConnectionValidator | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_attempts: int = 360,
    ) -> None:
        self._repository = repository
        self._gateways = gateway_repository
        self._endpoints = endpoint_service
        self._client_factory = client_factory
        self._writer_factory = writer_factory
        self._connection_validator = connection_validator
        self._sleep = sleep
        self._poll_attempts = poll_attempts
        self._tasks: set[asyncio.Task[None]] = set()
        self._active: set[str] = set()

    async def aclose(self) -> None:
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _endpoint(self, actor: Actor, endpoint_id: str) -> ModelEndpoint:
        endpoint = await self._endpoints.get_endpoint(actor, endpoint_id)
        await validate_resource_connection(
            self._connection_validator,
            actor.tenant_id,
            endpoint.azure_connection_id,
            endpoint.azure_cloud,
        )
        return endpoint

    @staticmethod
    def _supported(endpoint: ModelEndpoint) -> bool:
        return bool(
            endpoint.azure_resource_id
            and endpoint.auth_mode == EndpointAuthMode.MANAGED_IDENTITY
            and endpoint.provider in _SUPPORTED
        )

    async def access(self, actor: Actor, endpoint_id: str) -> DeploymentAccess:
        endpoint = await self._endpoint(actor, endpoint_id)
        if not self._supported(endpoint):
            return DeploymentAccess(
                evaluation="unsupported",
                message=(
                    "Deployment needs an Azure endpoint registered by ARM resource ID and an "
                    "authorized Azure management connection. An inference API key cannot deploy "
                    "models. Other providers are not supported."
                ),
            )
        return await deployment_access(self._client_factory(endpoint.arm_resource()))

    @staticmethod
    async def _optional_read(
        label: str, work: Awaitable[list[JsonObject]]
    ) -> tuple[list[JsonObject], str | None]:
        try:
            return await work, None
        except DomainError as error:
            logger.warning("deployment_options_unavailable", section=label, reason=error.message)
            return [], f"Could not read {label}: {error.message}"

    async def catalogue(self, actor: Actor, endpoint_id: str) -> DeploymentCatalogue:
        endpoint = await self._endpoint(actor, endpoint_id)
        if not self._supported(endpoint):
            return DeploymentCatalogue(
                endpoint_id=endpoint_id, access=await self.access(actor, endpoint_id)
            )
        client = self._client_factory(endpoint.arm_resource())
        account = await client.get_account()
        if account is None:
            raise NotFoundError("The Azure AI account could not be read")
        location = _text(account.get("location"))
        if not location:
            raise UpstreamError("Azure did not report this account's region; deployment is blocked")
        resource = endpoint.arm_resource()
        target = DeploymentTarget(
            endpoint_id=endpoint.id,
            endpoint_name=endpoint.name,
            account_scope=resource.account_scope,
            azure_cloud=endpoint.azure_cloud,
            azure_connection_id=endpoint.azure_connection_id,
            subscription_id=resource.subscription_id,
            resource_group=resource.resource_group,
            account_name=resource.account_name,
            location=location,
        )
        access, models, quota_read, policy_read = await asyncio.gather(
            deployment_access(client),
            client.list_models(strict=True),
            self._optional_read("subscription quota", client.list_usages(location)),
            self._optional_read("content-filter policies", client.list_rai_policies()),
        )
        usages, quota_error = quota_read
        policies, policy_error = policy_read
        blocking_run = await self._locked_run(endpoint)
        return DeploymentCatalogue(
            endpoint_id=endpoint.id,
            blocking_run_id=blocking_run.id if blocking_run else None,
            target=target,
            access=access,
            models=deployment_models(models, usages),
            rai_policies=sorted(
                {name for policy in policies if (name := _text(policy.get("name")))}
            ),
            quota_error=quota_error,
            policy_error=policy_error,
        )

    async def _validate(
        self, actor: Actor, endpoint: ModelEndpoint, configuration: ModelDeploymentCreate
    ) -> tuple[DeploymentTarget, DeploymentChecks]:
        if endpoint.management_mode != ManagementMode.MANAGE:
            raise ConflictError("Switch this endpoint to Manage deployments before deploying")
        catalogue = await self.catalogue(actor, endpoint.id)
        if not catalogue.access.can_deploy or catalogue.target is None:
            raise ConflictError(catalogue.access.message)
        model = next(
            (item for item in catalogue.models if item.reference() == configuration.model), None
        )
        if model is None:
            raise ValidationError("This model and exact version are not in the account's catalogue")
        if (model.lifecycle_status or "").casefold() == "deprecated" or _expired(
            model.deprecation_inference
        ):
            raise ValidationError("This model version is deprecated and cannot be newly deployed")
        sku = next((item for item in model.skus if item.name == configuration.sku_name), None)
        if sku is None or _expired(sku.deprecation_date):
            raise ValidationError(
                "This deployment type is unavailable for the selected model version"
            )
        capacity = configuration.capacity
        bounds = sku.capacity
        if (
            (bounds.minimum is not None and capacity < bounds.minimum)
            or (bounds.maximum is not None and capacity > bounds.maximum)
            or (bounds.allowed_values and capacity not in bounds.allowed_values)
            or (
                bounds.step is not None
                and bounds.step > 0
                and (capacity - (bounds.minimum or 0)) % bounds.step != 0
            )
        ):
            raise ValidationError("Capacity does not meet Azure's advertised limits or increments")
        if sku.quota is None or catalogue.quota_error:
            raise ConflictError(
                catalogue.quota_error
                or "Azure did not provide a verifiable quota mapping for this model and SKU. "
                "Deployment is blocked; check subscription quota access and the Azure catalogue."
            )
        if sku.quota.blocked or capacity > sku.quota.available:
            raise ConflictError(
                "The subscription does not have enough usable quota for this capacity"
            )
        if configuration.rai_policy_name is not None and (
            catalogue.policy_error or configuration.rai_policy_name not in catalogue.rai_policies
        ):
            raise ValidationError("The selected content-filter policy could not be verified")
        client = self._client_factory(endpoint.arm_resource())
        if await client.get_deployment(configuration.deployment_name) is not None:
            raise ConflictError(
                "A deployment with this name already exists. Choose a new name; MOSAIC does not "
                "replace or resize existing Azure deployments."
            )
        warnings = [_CATALOGUE_ONLY]
        available_capacity: float | None = None
        kind, processing = classify_sku(configuration.sku_name)
        if kind == CapacityType.PROVISIONED or "provisioned" in configuration.sku_name.casefold():
            capacities = await client.list_model_capacities(
                model.model_format, model.model_name, model.model_version
            )
            matches: list[float] = []
            for item in capacities:
                properties = _properties(item)
                capacity_model = properties.get("model")
                if (
                    str(item.get("location", "")).casefold() == catalogue.target.location.casefold()
                    and properties.get("skuName") == sku.name
                    and isinstance(capacity_model, dict)
                    and all(
                        capacity_model.get(key) == value
                        for key, value in {
                            "format": model.model_format,
                            "name": model.model_name,
                            "version": model.model_version,
                        }.items()
                    )
                ):
                    value = number(properties.get("availableCapacity"))
                    if value is not None:
                        matches.append(value)
            if len(matches) != 1:
                raise ConflictError(
                    "Azure did not confirm physical capacity for this provisioned deployment. "
                    "Check capacity availability in the account's region before retrying."
                )
            available_capacity = matches[0]
            if capacity > available_capacity:
                raise ConflictError("Azure does not currently report enough provisioned capacity")
            warnings.append(
                "Provisioned capacity is billable while allocated, even when idle. "
                "This workflow does not buy a reservation or a discounted commitment."
            )
        if kind == CapacityType.BATCH or "batch" in sku.name.casefold():
            warnings.append(
                "Batch deployments serve asynchronous batch jobs, not synchronous model APIs or "
                "MOSAIC pools. Deploying one does not make it eligible for synchronous publishing."
            )
        if processing == ProcessingScope.GLOBAL:
            warnings.append("Global processing can serve requests outside the account's region.")
        elif processing == ProcessingScope.DATA_ZONE:
            warnings.append("Data-zone processing is not restricted to the account's own region.")
        elif processing == ProcessingScope.UNKNOWN:
            warnings.append(
                "MOSAIC cannot classify this Azure SKU's processing scope. Verify its residency "
                "and billing terms before deploying."
            )
        if catalogue.policy_error and configuration.rai_policy_name is None:
            warnings.append(
                f"{catalogue.policy_error} No policy override will be sent; "
                "Azure's default applies."
            )
        if (model.lifecycle_status or "").casefold() in {"preview", "deprecating"}:
            warnings.append(f"Azure labels this model version {model.lifecycle_status}.")
        warnings.append("No reliable price estimate is included. Verify the Azure offer's charges.")
        return catalogue.target, DeploymentChecks(
            quota=sku.quota, available_capacity=available_capacity, warnings=warnings
        )

    async def plan(
        self, actor: Actor, endpoint_id: str, configuration: ModelDeploymentCreate
    ) -> ModelDeploymentPlan:
        endpoint = await self._endpoint(actor, endpoint_id)
        if await self._gateways.get_publication_lock(
            actor.tenant_id, deployment_lock_scope(endpoint)
        ):
            raise ConflictError(
                "An Azure deployment is active or unconfirmed. Reconcile its deployment history "
                "before reviewing another create."
            )
        target, checks = await self._validate(actor, endpoint, configuration)
        plan = ModelDeploymentPlan(
            id=new_id("deploymentplan"),
            tenant_id=actor.tenant_id,
            endpoint_id=endpoint.id,
            actor_object_id=actor.object_id,
            target=target,
            configuration=configuration,
            checks=checks,
            input_digest=_digest(target, configuration),
        )
        await self._repository.save_model_deployment_plan(plan)
        return plan

    async def apply(self, actor: Actor, endpoint_id: str, plan_id: str) -> ModelDeploymentRun:
        endpoint = await self._endpoint(actor, endpoint_id)
        if not plan_id.startswith("deploymentplan_"):
            raise NotFoundError("Deployment plan was not found")
        plan = await self._repository.get_model_deployment_plan(actor.tenant_id, plan_id)
        if plan is None or plan.endpoint_id != endpoint_id:
            raise NotFoundError("Deployment plan was not found")
        run_id = deterministic_id("deploymentrun", actor.tenant_id, plan.id)
        existing = await self._repository.get_model_deployment_run(actor.tenant_id, run_id)
        if existing is not None:
            return existing
        target, _checks = await self._validate(actor, endpoint, plan.configuration)
        if _digest(target, plan.configuration) != plan.input_digest:
            raise ConflictError("The deployment target changed; create and review a new plan")
        try:
            run = await self._repository.create_model_deployment_run(
                ModelDeploymentRun(
                    id=run_id,
                    tenant_id=actor.tenant_id,
                    endpoint_id=endpoint_id,
                    plan_id=plan.id,
                    target=target,
                    configuration=plan.configuration,
                    actor_object_id=actor.object_id,
                ),
                self._audit(actor, "modelDeployment.requested", run_id, endpoint_id),
            )
        except ConflictError:
            existing = await self._repository.get_model_deployment_run(actor.tenant_id, run_id)
            if existing is not None:
                return existing
            raise
        scope = deployment_lock_scope(endpoint)
        try:
            # Persist the run before its non-expiring lock: a crash can never strand a lock
            # whose owner has no recoverable operation. This lease covers only record writes.
            async with scope_lease(self._gateways, actor.tenant_id, scope):
                current = await self._endpoints.get_endpoint(actor, endpoint_id)
                if (
                    current.management_mode != ManagementMode.MANAGE
                    or current.name != target.endpoint_name
                ):
                    raise ConflictError("The endpoint changed; create and review a new plan")
                await self._gateways.acquire_publication_lock(actor.tenant_id, scope, run_id)
                endpoint = await self._repository.save_endpoint(
                    current.model_copy(update={"updated_at": utc_now()}),
                    self._audit(actor, "modelDeployment.locked", run_id, endpoint_id),
                )
        except DomainError as error:
            await self._finish(actor, endpoint, run, succeeded=False, error=error.message)
            raise
        self._active.add(run.id)
        task = asyncio.create_task(self._execute(actor, endpoint, run, plan.input_digest))
        self._tasks.add(task)
        task.add_done_callback(self._task_finished)
        return run

    async def _execute(
        self, actor: Actor, endpoint: ModelEndpoint, run: ModelDeploymentRun, digest: str
    ) -> None:
        try:
            current = await self._endpoint(actor, endpoint.id)
            target, _checks = await self._validate(actor, current, run.configuration)
            if _digest(target, run.configuration) != digest:
                raise ConflictError("The deployment target changed; review a new plan")
            run = await self._repository.save_model_deployment_run(
                run.model_copy(update={"write_attempted": True, "updated_at": utc_now()})
            )
            await self._writer_factory(current.arm_resource()).create(run.configuration)
            resource = await self._poll(current, run.configuration.deployment_name)
            await self._complete(actor, current, run, resource)
        except asyncio.CancelledError:
            await self._interrupt(
                run, "The API stopped before this deployment outcome was confirmed."
            )
            raise
        except DomainError as error:
            logger.warning("model_deployment_failed", run_id=run.id, reason=error.message)
            status_code = error.details.get("statusCode")
            rejected = (
                error.details.get("method") == "PUT"
                and (
                    isinstance(
                        error,
                        UpstreamAuthorizationError | UpstreamNotFoundError | UpstreamConflictError,
                    )
                    or (
                        isinstance(status_code, int)
                        and 400 <= status_code < 500
                        and status_code != 408
                    )
                )
            ) or str(error.details.get("status", "")).casefold() in _FAILED
            if run.write_attempted and not rejected:
                await self._interrupt(run, error.message)
            else:
                await self._finish(actor, endpoint, run, succeeded=False, error=error.message)
        except Exception:
            logger.exception("model_deployment_unexpected_failure", run_id=run.id)
            await self._interrupt(
                run,
                "The deployment outcome could not be recorded. Reconcile with Azure; "
                "the API log has the details.",
            )
        finally:
            self._active.discard(run.id)

    def _task_finished(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("model_deployment_recovery_record_failed", error=str(error))

    async def _poll(self, endpoint: ModelEndpoint, name: str) -> JsonObject:
        client = self._client_factory(endpoint.arm_resource())
        for _ in range(self._poll_attempts):
            resource = await client.get_deployment(name)
            state = _text(_properties(resource or {}).get("provisioningState"))
            if state and (state.casefold() == "succeeded" or state.casefold() in _FAILED):
                return resource or {}
            await self._sleep(5)
        raise UpstreamError(
            "Azure has not confirmed a terminal provisioning state. The deployment may still be "
            "running; reconcile it rather than submitting another create."
        )

    @staticmethod
    def _matches(configuration: ModelDeploymentCreate, resource: JsonObject) -> bool:
        properties = _properties(resource)
        model = properties.get("model")
        sku = resource.get("sku")
        return bool(
            isinstance(model, dict)
            and isinstance(sku, dict)
            and all(
                model.get(key) == value
                for key, value in configuration.model.azure_payload().items()
            )
            and str(sku.get("name", "")).casefold() == configuration.sku_name.casefold()
            and _int_or_none(sku.get("capacity")) == configuration.capacity
            and (
                configuration.rai_policy_name is None
                or properties.get("raiPolicyName") == configuration.rai_policy_name
            )
            and (
                configuration.version_upgrade_option is None
                or properties.get("versionUpgradeOption") == configuration.version_upgrade_option
            )
        )

    async def _complete(
        self,
        actor: Actor,
        endpoint: ModelEndpoint,
        run: ModelDeploymentRun,
        resource: JsonObject,
        *,
        confirmed_recovery: bool = False,
    ) -> ModelDeploymentRun:
        state = _text(_properties(resource).get("provisioningState"))
        if not self._matches(run.configuration, resource):
            message = (
                "Azure's deployment does not match the reviewed configuration. Inspect it in "
                "Azure; MOSAIC will not overwrite or delete it."
            )
            if confirmed_recovery:
                return await self._finish(
                    actor,
                    endpoint,
                    run,
                    succeeded=False,
                    state=state,
                    error=f"{message} An administrator confirmed the original worker stopped "
                    "and Azure reports a terminal deployment. No create was retried.",
                )
            return await self._interrupt(
                run,
                message,
            )
        return await self._finish(
            actor,
            endpoint,
            run,
            succeeded=(state or "").casefold() == "succeeded",
            state=state,
            error=None
            if (state or "").casefold() == "succeeded"
            else f"Azure reports provisioning state {state}. Inspect the deployment in Azure.",
        )

    async def _finish(
        self,
        actor: Actor,
        endpoint: ModelEndpoint,
        run: ModelDeploymentRun,
        *,
        succeeded: bool,
        state: str | None = None,
        error: str | None = None,
    ) -> ModelDeploymentRun:
        saved = await self._repository.save_model_deployment_run(
            run.model_copy(
                update={
                    "status": DeploymentRunStatus.SUCCEEDED
                    if succeeded
                    else DeploymentRunStatus.FAILED,
                    "completed_at": utc_now(),
                    "updated_at": utc_now(),
                    "provisioning_state": state,
                    "error": error,
                    "refresh_status": "running" if succeeded else "notStarted",
                }
            ),
            self._audit(
                actor,
                "modelDeployment.succeeded" if succeeded else "modelDeployment.failed",
                run.id,
                endpoint.id,
            ),
        )
        if succeeded:
            saved = await self._refresh(actor, saved)
        await self._release_run_lock(endpoint, run)
        return saved

    async def _release_run_lock(self, endpoint: ModelEndpoint, run: ModelDeploymentRun) -> None:
        scope = deployment_lock_scope(endpoint)
        if await self._gateways.get_publication_lock(run.tenant_id, scope) == run.id:
            await self._gateways.release_publication_lock(run.tenant_id, scope, run.id)

    async def _locked_run(self, endpoint: ModelEndpoint) -> ModelDeploymentRun | None:
        owner = await self._gateways.get_publication_lock(
            endpoint.tenant_id, deployment_lock_scope(endpoint)
        )
        if owner is None:
            return None
        run = await self._repository.get_model_deployment_run(endpoint.tenant_id, owner)
        return run if run and run.endpoint_id == endpoint.id else None

    async def _refresh(self, actor: Actor, run: ModelDeploymentRun) -> ModelDeploymentRun:
        error: str | None = None
        try:
            endpoint = await self._endpoints.get_endpoint(actor, run.endpoint_id)
            if not endpoint.access.can_read:
                await self._endpoints.preflight(actor, run.endpoint_id)
            sync = await self._endpoints.sync_now(actor, run.endpoint_id)
            if sync.status != GatewaySyncStatus.SUCCEEDED:
                error = "; ".join(sync.errors) or "The inventory synchronization was incomplete."
        except DomainError as failure:
            logger.warning(
                "deployment_inventory_refresh_failed", run_id=run.id, reason=failure.message
            )
            error = failure.message
        except Exception:
            logger.exception("deployment_inventory_refresh_failed", run_id=run.id)
            error = "Inventory refresh failed unexpectedly; the API log has the details."
        return await self._repository.save_model_deployment_run(
            run.model_copy(
                update={
                    "refresh_status": "failed" if error else "succeeded",
                    "refresh_error": error,
                    "updated_at": utc_now(),
                }
            )
        )

    async def _interrupt(self, run: ModelDeploymentRun, message: str) -> ModelDeploymentRun:
        current = await self._repository.get_model_deployment_run(run.tenant_id, run.id)
        if current is None:
            raise NotFoundError("The deployment run could not be read for recovery")
        if current.status in {DeploymentRunStatus.SUCCEEDED, DeploymentRunStatus.FAILED}:
            return current
        return await self._repository.save_model_deployment_run(
            current.model_copy(
                update={
                    "status": DeploymentRunStatus.INTERRUPTED,
                    "error": message,
                    "updated_at": utc_now(),
                }
            )
        )

    async def get_run(self, actor: Actor, endpoint_id: str, run_id: str) -> ModelDeploymentRun:
        await self._endpoint(actor, endpoint_id)
        if not run_id.startswith("deploymentrun_"):
            raise NotFoundError("Deployment run was not found")
        run = await self._repository.get_model_deployment_run(actor.tenant_id, run_id)
        if run is None or run.endpoint_id != endpoint_id:
            raise NotFoundError("Deployment run was not found")
        return run

    async def list_runs(self, actor: Actor, endpoint_id: str) -> list[ModelDeploymentRun]:
        endpoint = await self._endpoint(actor, endpoint_id)
        runs = await self._repository.list_model_deployment_runs(actor.tenant_id, endpoint_id)
        blocking = await self._locked_run(endpoint)
        return [blocking, *[run for run in runs if run.id != blocking.id]] if blocking else runs

    async def reconcile(
        self, actor: Actor, endpoint_id: str, run_id: str, request: DeploymentRecoveryRequest
    ) -> ModelDeploymentRun:
        run = await self.get_run(actor, endpoint_id, run_id)
        if run.id in self._active:
            raise ConflictError(
                "This API worker is still executing the deployment; wait for its result"
            )
        endpoint = await self._endpoint(actor, endpoint_id)
        if run.status in {DeploymentRunStatus.SUCCEEDED, DeploymentRunStatus.FAILED}:
            if run.status == DeploymentRunStatus.SUCCEEDED and run.refresh_status in {
                "running",
                "notStarted",
            }:
                if run.refresh_status == "running" and not request.confirm_worker_stopped:
                    return run.model_copy(
                        update={
                            "refresh_error": "An inventory-refresh worker may still be running. "
                            "Verify that it stopped before confirming recovery.",
                        }
                    )
                run = await self._repository.save_model_deployment_run(
                    run.model_copy(
                        update={
                            "refresh_status": "failed",
                            "refresh_error": "Inventory refresh did not record completion. "
                            "Retry inventory refresh; the Azure deployment is confirmed.",
                            "updated_at": utc_now(),
                        }
                    )
                )
            await self._release_run_lock(endpoint, run)
            return run
        resource = await self._client_factory(endpoint.arm_resource()).get_deployment(
            run.configuration.deployment_name
        )
        state = _text(_properties(resource or {}).get("provisioningState"))
        if state and (state.casefold() == "succeeded" or state.casefold() in _FAILED):
            # A terminal resource cannot fence an old worker paused before its PUT.
            if not request.confirm_worker_stopped:
                return run.model_copy(
                    update={
                        "error": f"Azure reports {state}. Verify that the original deployment "
                        "worker has stopped, then confirm recovery before MOSAIC finalizes "
                        "this operation and releases its account lock. No create was retried.",
                    }
                )
            return await self._complete(
                actor, endpoint, run, resource or {}, confirmed_recovery=True
            )
        if (
            resource is None
            and request.confirm_worker_stopped
            and request.confirm_no_pending_operation
        ):
            return await self._finish(
                actor,
                endpoint,
                run,
                succeeded=False,
                error="An administrator confirmed the original worker and Azure operation "
                "stopped. Azure reports no deployment with this name. No create was retried.",
            )
        return run.model_copy(
            update={
                "error": "Azure has not confirmed a matching terminal deployment. If it is absent, "
                "stop the original API worker and verify no Azure operation is pending before "
                "explicitly confirming recovery. MOSAIC has not retried or deleted anything.",
            }
        )

    async def retry_refresh(
        self, actor: Actor, endpoint_id: str, run_id: str
    ) -> ModelDeploymentRun:
        run = await self.get_run(actor, endpoint_id, run_id)
        if run.status != DeploymentRunStatus.SUCCEEDED:
            raise ConflictError("Only a confirmed successful deployment can refresh its inventory")
        if run.id in self._active:
            raise ConflictError("This API worker is still refreshing the deployment inventory")
        if run.refresh_status == "running":
            raise ConflictError(
                "An inventory-refresh worker may still be running. Reconcile it after verifying "
                "that the original worker stopped."
            )
        self._active.add(run.id)
        try:
            claimed = await self._repository.save_model_deployment_run(
                run.model_copy(
                    update={
                        "refresh_status": "running",
                        "refresh_error": None,
                        "updated_at": utc_now(),
                    }
                )
            )
            return await self._refresh(actor, claimed)
        finally:
            self._active.discard(run.id)

    @staticmethod
    def _audit(actor: Actor, action: str, run_id: str, endpoint_id: str) -> AuditEvent:
        return AuditEvent(
            id=new_id("audit"),
            tenant_id=actor.tenant_id,
            action=action,
            resource_type="modelDeployment",
            resource_id=run_id,
            actor_object_id=actor.object_id,
            details={"endpointId": endpoint_id},
        )
