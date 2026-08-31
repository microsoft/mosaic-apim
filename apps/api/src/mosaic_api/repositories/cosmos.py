from typing import Any, TypeVar

import structlog
from azure.core.credentials_async import AsyncTokenCredential
from azure.cosmos import exceptions
from azure.cosmos.aio import ContainerProxy, CosmosClient
from pydantic import BaseModel

from mosaic_api.domain import AuditEvent, Group, GroupMembership, Principal
from mosaic_api.errors import ConflictError

ModelT = TypeVar("ModelT", bound=BaseModel)
BatchOperation = tuple[str, tuple[Any, ...]] | tuple[str, tuple[Any, ...], dict[str, Any]]
logger = structlog.get_logger()


class CosmosDirectoryRepository:
    def __init__(
        self,
        endpoint: str,
        credential: AsyncTokenCredential,
        database_name: str,
        desired_state_container: str,
        audit_events_container: str,
    ) -> None:
        self._client = CosmosClient(endpoint, credential=credential)
        database = self._client.get_database_client(database_name)
        self._desired: ContainerProxy = database.get_container_client(desired_state_container)
        self._audit: ContainerProxy = database.get_container_client(audit_events_container)

    async def ready(self) -> bool:
        try:
            await self._desired.read()
            await self._audit.read()
            await self._flush_audit_outbox()
        except exceptions.CosmosHttpResponseError:
            return False
        return True

    async def close(self) -> None:
        await self._client.close()

    @staticmethod
    def _document(model: BaseModel) -> dict[str, Any]:
        return model.model_dump(mode="json", by_alias=True, exclude={"etag"})

    @staticmethod
    def _model(model_type: type[ModelT], document: dict[str, Any]) -> ModelT:
        payload = dict(document)
        etag = payload.get("_etag")
        for key in tuple(payload):
            if key.startswith("_"):
                payload.pop(key)
        payload["etag"] = etag
        return model_type.model_validate(payload)

    async def _read(self, model_type: type[ModelT], tenant_id: str, item_id: str) -> ModelT | None:
        try:
            item = await self._desired.read_item(item=item_id, partition_key=tenant_id)
        except exceptions.CosmosResourceNotFoundError:
            return None
        model = self._model(model_type, item)
        document_tenant_id = getattr(model, "tenant_id", None)
        return model if document_tenant_id == tenant_id else None

    async def _query(
        self,
        model_type: type[ModelT],
        tenant_id: str,
        entity_type: str,
        extra: str = "",
        parameters: list[dict[str, Any]] | None = None,
    ) -> list[ModelT]:
        query = "SELECT * FROM c WHERE c.tenantId = @tenantId AND c.entityType = @entityType"
        query += extra
        query_parameters = [
            {"name": "@tenantId", "value": tenant_id},
            {"name": "@entityType", "value": entity_type},
            *(parameters or []),
        ]
        items = self._desired.query_items(
            query=query,
            parameters=query_parameters,
            partition_key=tenant_id,
        )
        return [self._model(model_type, item) async for item in items]

    async def _project_audit_event(self, event: AuditEvent) -> None:
        try:
            await self._audit.create_item(self._document(event))
        except exceptions.CosmosResourceExistsError:
            pass
        try:
            await self._desired.delete_item(item=event.id, partition_key=event.tenant_id)
        except exceptions.CosmosResourceNotFoundError:
            pass

    async def _flush_audit_outbox(self) -> None:
        items = self._desired.query_items(
            query=(
                "SELECT * FROM c WHERE c.entityType = @entityType AND STARTSWITH(c.id, @prefix)"
            ),
            parameters=[
                {"name": "@entityType", "value": "auditEvent"},
                {"name": "@prefix", "value": "audit_"},
            ],
        )
        async for item in items:
            await self._project_audit_event(self._model(AuditEvent, item))

    def _replace_operation(self, model: BaseModel) -> BatchOperation:
        model_id = getattr(model, "id", None)
        if not isinstance(model_id, str):
            raise ValueError("A replace mutation requires a model ID")
        operation: BatchOperation = ("replace", (model_id, self._document(model)))
        etag = getattr(model, "etag", None)
        if isinstance(etag, str):
            operation = (*operation, {"if_match_etag": etag})
        return operation

    @staticmethod
    def _delete_operation(item_id: str, model: BaseModel | None) -> BatchOperation:
        operation: BatchOperation = ("delete", (item_id,))
        etag = getattr(model, "etag", None)
        if isinstance(etag, str):
            operation = (*operation, {"if_match_etag": etag})
        return operation

    async def _execute_batch(
        self,
        batch_operations: list[BatchOperation],
        audit_event: AuditEvent,
        conflict_message: str | None,
    ) -> None:
        try:
            await self._desired.execute_item_batch(
                batch_operations=batch_operations,
                partition_key=audit_event.tenant_id,
            )
        except exceptions.CosmosBatchOperationError as exc:
            response = exc.operation_responses[exc.error_index]
            if response.get("statusCode") in {404, 409, 412} and conflict_message:
                raise ConflictError(conflict_message) from exc
            raise

        try:
            await self._project_audit_event(audit_event)
        except exceptions.CosmosHttpResponseError:
            logger.exception(
                "audit_projection_deferred",
                audit_event_id=audit_event.id,
                tenant_id=audit_event.tenant_id,
            )

    async def _mutate(
        self,
        model: ModelT | None,
        item_id: str | None,
        audit_event: AuditEvent,
        operation: str,
        conflict_message: str | None = None,
    ) -> ModelT | None:
        audit_document = self._document(audit_event)
        if operation == "delete":
            if item_id is None:
                raise ValueError("A delete mutation requires an item ID")
            batch_operations: list[BatchOperation] = [
                self._delete_operation(item_id, model),
                ("create", (audit_document,)),
            ]
        else:
            if model is None:
                raise ValueError("A create or upsert mutation requires a model")
            document = self._document(model)
            if operation == "replace":
                entity_operation = self._replace_operation(model)
            else:
                entity_operation = (operation, (document,))
            batch_operations = [entity_operation, ("create", (audit_document,))]
        await self._execute_batch(batch_operations, audit_event, conflict_message)
        return model

    async def list_principals(self, tenant_id: str) -> list[Principal]:
        items = await self._query(Principal, tenant_id, "principal")
        return sorted(items, key=lambda item: (item.label or "", item.object_id))

    async def get_principal(self, tenant_id: str, principal_id: str) -> Principal | None:
        return await self._read(Principal, tenant_id, principal_id)

    async def find_principal_by_object_id(self, tenant_id: str, object_id: str) -> Principal | None:
        items = await self._query(
            Principal,
            tenant_id,
            "principal",
            " AND c.objectId = @objectId",
            [{"name": "@objectId", "value": object_id}],
        )
        return items[0] if items else None

    async def save_principal(self, principal: Principal, audit_event: AuditEvent) -> Principal:
        await self._mutate(
            principal,
            None,
            audit_event,
            "replace",
            conflict_message="The principal changed; reload it and try again",
        )
        return principal

    async def create_principal(self, principal: Principal, audit_event: AuditEvent) -> Principal:
        await self._mutate(
            principal,
            None,
            audit_event,
            "create",
            conflict_message="A principal with this Entra object ID already exists",
        )
        return principal

    async def delete_principal(self, principal: Principal, audit_event: AuditEvent) -> None:
        await self._mutate(
            principal,
            principal.id,
            audit_event,
            "delete",
            conflict_message="The principal changed; reload it and try again",
        )

    async def list_groups(self, tenant_id: str) -> list[Group]:
        items = await self._query(Group, tenant_id, "group")
        return sorted(items, key=lambda item: item.name.casefold())

    async def get_group(self, tenant_id: str, group_id: str) -> Group | None:
        return await self._read(Group, tenant_id, group_id)

    async def find_group_by_name(self, tenant_id: str, name: str) -> Group | None:
        items = await self._query(
            Group,
            tenant_id,
            "group",
            " AND LOWER(c.name) = @name",
            [{"name": "@name", "value": name.casefold()}],
        )
        return items[0] if items else None

    async def save_group(self, group: Group, audit_event: AuditEvent) -> Group:
        await self._mutate(
            group,
            None,
            audit_event,
            "replace",
            conflict_message="The group changed; reload it and try again",
        )
        return group

    async def create_group(self, group: Group, audit_event: AuditEvent) -> Group:
        await self._mutate(
            group,
            None,
            audit_event,
            "create",
            conflict_message="A group with this name already exists",
        )
        return group

    async def delete_group(self, group: Group, audit_event: AuditEvent) -> None:
        await self._mutate(
            group,
            group.id,
            audit_event,
            "delete",
            conflict_message="The group changed; reload it and try again",
        )

    async def list_memberships(
        self, tenant_id: str, *, group_id: str | None = None, principal_id: str | None = None
    ) -> list[GroupMembership]:
        extra = ""
        parameters: list[dict[str, Any]] = []
        if group_id:
            extra += " AND c.groupId = @groupId"
            parameters.append({"name": "@groupId", "value": group_id})
        if principal_id:
            extra += " AND c.principalId = @principalId"
            parameters.append({"name": "@principalId", "value": principal_id})
        return await self._query(GroupMembership, tenant_id, "groupMembership", extra, parameters)

    async def get_membership(
        self, tenant_id: str, group_id: str, principal_id: str
    ) -> GroupMembership | None:
        items = await self.list_memberships(tenant_id, group_id=group_id, principal_id=principal_id)
        return items[0] if items else None

    async def create_membership(
        self,
        membership: GroupMembership,
        group: Group,
        principal: Principal,
        audit_event: AuditEvent,
    ) -> GroupMembership:
        await self._execute_batch(
            [
                self._replace_operation(group),
                self._replace_operation(principal),
                ("create", (self._document(membership),)),
                ("create", (self._document(audit_event),)),
            ],
            audit_event,
            conflict_message=(
                "The membership already exists or a referenced group or principal changed"
            ),
        )
        return membership

    async def delete_membership(self, membership: GroupMembership, audit_event: AuditEvent) -> None:
        await self._mutate(
            membership,
            membership.id,
            audit_event,
            "delete",
            conflict_message="The membership changed; reload it and try again",
        )
