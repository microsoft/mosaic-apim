import structlog
from azure.core import MatchConditions
from azure.cosmos import exceptions

from mosaic_api.domain import (
    AuditEvent,
    CredentialReference,
    ModelEndpoint,
    ModelEndpointSyncRun,
)
from mosaic_api.errors import ConflictError
from mosaic_api.model_deployments import ModelDeploymentPlan, ModelDeploymentRun
from mosaic_api.model_lifecycle import DiscoveryRow, LifecycleRecord, SavedProposalContext
from mosaic_api.repositories.cosmos import BatchOperation
from mosaic_api.repositories.cosmos_endpoint_state import CosmosEndpointStateBase
from mosaic_api.repositories.observation_writes import MODEL_ENDPOINT_AUTHORED_FIELDS

logger = structlog.get_logger()

SYNC_RUN_ENTITY_TYPE = "modelEndpointSyncRun"


class CosmosModelEndpointRepository(CosmosEndpointStateBase):
    """Registered endpoints are desired state; the models MOSAIC read are observed state.

    Observed model documents share the ``observed-state`` container with gateway inventory but are
    keyed on ``endpointId``, so the two sweeps never touch each other's documents. That
    endpoint-scoped half is inherited from
    :class:`~mosaic_api.repositories.cosmos_endpoint_state.CosmosEndpointStateBase`, which MCP
    endpoints share.
    """

    async def ready(self) -> bool:
        if not await super().ready():
            return False
        try:
            await self._flush_audit_outbox(source=self._sync)
        except exceptions.CosmosHttpResponseError:
            return False
        return True

    async def list_endpoints(self, tenant_id: str) -> list[ModelEndpoint]:
        items = await self._query(ModelEndpoint, tenant_id, "modelEndpoint")
        return sorted(items, key=lambda item: item.name.casefold())

    async def get_endpoint(self, tenant_id: str, endpoint_id: str) -> ModelEndpoint | None:
        return await self._read(ModelEndpoint, tenant_id, endpoint_id)

    async def find_endpoint_by_resource_id(
        self, tenant_id: str, azure_resource_id: str, *, azure_cloud: str = "commercial"
    ) -> ModelEndpoint | None:
        items = await self._query(
            ModelEndpoint,
            tenant_id,
            "modelEndpoint",
            " AND LOWER(c.azureResourceId) = @resourceId",
            [{"name": "@resourceId", "value": azure_resource_id.casefold()}],
        )
        return next((item for item in items if item.azure_cloud == azure_cloud), None)

    async def find_endpoint_by_url(
        self, tenant_id: str, endpoint: str, *, azure_cloud: str = "commercial"
    ) -> ModelEndpoint | None:
        target = endpoint.casefold().rstrip("/")
        items = await self._query(ModelEndpoint, tenant_id, "modelEndpoint")
        return next(
            (
                item for item in items
                if item.azure_cloud == azure_cloud
                and str(item.endpoint).casefold().rstrip("/") == target
            ),
            None,
        )

    async def create_endpoint(
        self, endpoint: ModelEndpoint, audit_event: AuditEvent
    ) -> ModelEndpoint:
        await self._mutate(
            endpoint,
            None,
            audit_event,
            "create",
            conflict_message="This model endpoint is already registered",
        )
        return endpoint

    async def save_endpoint(
        self, endpoint: ModelEndpoint, audit_event: AuditEvent
    ) -> ModelEndpoint:
        await self._mutate(
            endpoint,
            None,
            audit_event,
            "replace",
            conflict_message="The endpoint changed; reload it and try again",
        )
        return endpoint

    async def record_endpoint_state(self, endpoint: ModelEndpoint) -> ModelEndpoint | None:
        return await self._record_observation(
            ModelEndpoint,
            endpoint,
            MODEL_ENDPOINT_AUTHORED_FIELDS,
            conflict_message="The model endpoint changed while MOSAIC was recording observations",
        )

    async def delete_endpoint(self, endpoint: ModelEndpoint, audit_event: AuditEvent) -> None:
        await self.delete_observed_for_endpoint(endpoint.tenant_id, endpoint.id)
        await self._delete_runs_for_endpoint(
            ModelEndpointSyncRun, SYNC_RUN_ENTITY_TYPE, endpoint.tenant_id, endpoint.id
        )
        await self._mutate(
            endpoint,
            endpoint.id,
            audit_event,
            "delete",
            conflict_message="The endpoint changed; reload it and try again",
        )

    async def get_credential(
        self, tenant_id: str, credential_id: str
    ) -> CredentialReference | None:
        return await self._read(CredentialReference, tenant_id, credential_id)

    async def save_credential(
        self, credential: CredentialReference, audit_event: AuditEvent
    ) -> CredentialReference:
        await self._desired.upsert_item(self._document(credential))
        await self._audit.upsert_item(self._document(audit_event))
        return credential

    async def save_endpoint_sync_run(self, run: ModelEndpointSyncRun) -> ModelEndpointSyncRun:
        await self._save_run(run)
        return run

    async def get_endpoint_sync_run(
        self, tenant_id: str, run_id: str
    ) -> ModelEndpointSyncRun | None:
        return await self._read_run(ModelEndpointSyncRun, tenant_id, run_id)

    async def list_endpoint_sync_runs(
        self, tenant_id: str, endpoint_id: str, *, limit: int = 20
    ) -> list[ModelEndpointSyncRun]:
        return await self._runs_for_endpoint(
            ModelEndpointSyncRun, SYNC_RUN_ENTITY_TYPE, tenant_id, endpoint_id, limit
        )

    async def list_unfinished_endpoint_sync_runs(
        self, tenant_id: str
    ) -> list[ModelEndpointSyncRun]:
        return await self._unfinished_runs(ModelEndpointSyncRun, SYNC_RUN_ENTITY_TYPE, tenant_id)

    async def save_model_deployment_plan(self, plan: ModelDeploymentPlan) -> None:
        await self._sync.create_item(self._document(plan))

    async def get_model_deployment_plan(
        self, tenant_id: str, plan_id: str
    ) -> ModelDeploymentPlan | None:
        return await self._read_run(ModelDeploymentPlan, tenant_id, plan_id)

    async def create_model_deployment_run(
        self, run: ModelDeploymentRun, audit_event: AuditEvent
    ) -> ModelDeploymentRun:
        await self._audit.create_item(self._document(audit_event))
        try:
            item = await self._sync.create_item(self._document(run))
        except exceptions.CosmosResourceExistsError:
            raise ConflictError("This deployment plan already has a run") from None
        return self._model(ModelDeploymentRun, item)

    async def save_model_deployment_run(
        self, run: ModelDeploymentRun, audit_event: AuditEvent | None = None
    ) -> ModelDeploymentRun:
        if run.etag is None:
            raise ConflictError("Reload the deployment run before continuing")
        if audit_event is not None:
            responses = await self._execute_batch(
                [
                    self._replace_operation(run),
                    ("create", (self._document(audit_event),)),
                ],
                audit_event,
                "The deployment run changed; reload it before continuing",
                source=self._sync,
            )
            return self._model(ModelDeploymentRun, responses[0]["resourceBody"])
        try:
            item = await self._sync.replace_item(
                item=run.id, body=self._document(run),
                etag=run.etag, match_condition=MatchConditions.IfNotModified,
            )
        except (
            exceptions.CosmosAccessConditionFailedError,
            exceptions.CosmosResourceNotFoundError,
        ):
            raise ConflictError("The deployment run changed; reload it before continuing") from None
        return self._model(ModelDeploymentRun, item)

    async def get_model_deployment_run(
        self, tenant_id: str, run_id: str
    ) -> ModelDeploymentRun | None:
        return await self._read_run(ModelDeploymentRun, tenant_id, run_id)

    async def list_model_deployment_runs(
        self, tenant_id: str, endpoint_id: str, *, limit: int = 20
    ) -> list[ModelDeploymentRun]:
        return await self._runs_for_endpoint(
            ModelDeploymentRun, "modelDeploymentRun", tenant_id, endpoint_id, limit
        )

    async def get_lifecycle[R: LifecycleRecord](
        self, record_type: type[R], tenant_id: str, record_id: str
    ) -> R | None:
        return await self._read_run(record_type, tenant_id, record_id)

    async def list_discovery_rows(self, tenant_id: str, run_id: str) -> list[DiscoveryRow]:
        return await self._query_runs(
            DiscoveryRow, "modelDiscoveryRow", tenant_id, " AND c.runId = @runId",
            [{"name": "@runId", "value": run_id}],
        )

    async def list_proposal_contexts(
        self, tenant_id: str, endpoint_id: str
    ) -> list[SavedProposalContext]:
        return await self._query_runs(
            SavedProposalContext, "modelProposalContext", tenant_id, " AND c.endpointId = @id",
            [{"name": "@id", "value": endpoint_id}],
        )

    async def save_lifecycle(
        self, records: list[LifecycleRecord], audit: AuditEvent | None = None
    ) -> None:
        operations: list[BatchOperation] = [
            self._replace_operation(record) if record.etag is not None
            else ("create", (self._document(record),))
            for record in records
        ]
        message = "Model discovery changed; reload the saved evidence before continuing"
        if audit is not None:
            operations.append(("create", (self._document(audit),)))
            await self._execute_batch(operations, audit, message, source=self._sync)
            return
        try:
            await self._sync.execute_item_batch(
                batch_operations=operations, partition_key=records[0].tenant_id
            )
        except exceptions.CosmosBatchOperationError as exc:
            if exc.operation_responses[exc.error_index].get("statusCode") in {404, 409, 412}:
                raise ConflictError(message) from exc
            raise
