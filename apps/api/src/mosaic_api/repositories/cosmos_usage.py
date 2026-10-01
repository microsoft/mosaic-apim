"""Rolled-up gateway telemetry in the ``usage-rollups`` Cosmos container. See ADR 0019.

Standalone rather than a :class:`CosmosRepositoryBase`: these items are derived from Log Analytics
and from MOSAIC's own bindings, and writing them isn't audited, so they need neither the
desired-state container nor the audit outbox. Every query names its tenant partition.
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Any, TypeVar

from azure.core import MatchConditions
from azure.cosmos import exceptions
from azure.cosmos.aio import ContainerProxy, CosmosClient
from pydantic import BaseModel

from mosaic_api.domain import utc_now
from mosaic_api.errors import ConflictError
from mosaic_api.usage_telemetry import (
    AttributionRecord,
    SummaryDimension,
    SummaryPeriod,
    UsageFact,
    UsageRollupState,
    UsageSummary,
    usage_rollup_state_id,
)

ModelT = TypeVar("ModelT", bound=BaseModel)
# Enough parallel writes to replace a busy gateway's day quickly without tripping throttling on a
# modestly provisioned container.
WRITE_CONCURRENCY = 8
# A gateway's state has few writers, so a handful of conditional retries is plenty.
STATE_WRITE_ATTEMPTS = 5


class CosmosUsageRollupRepository:
    def __init__(
        self,
        client: CosmosClient,
        database_name: str,
        container_name: str,
        *,
        owns_client: bool = False,
    ) -> None:
        self._client = client
        self._owns_client = owns_client
        database = client.get_database_client(database_name)
        self._container: ContainerProxy = database.get_container_client(container_name)

    async def ready(self) -> bool:
        try:
            await self._container.read()
        except exceptions.CosmosHttpResponseError:
            return False
        return True

    async def close(self) -> None:
        if self._owns_client:
            await self._client.close()

    @staticmethod
    def _document(model: BaseModel) -> dict[str, Any]:
        return model.model_dump(mode="json", by_alias=True, exclude={"etag"})

    @staticmethod
    def _model(model_type: type[ModelT], document: dict[str, Any]) -> ModelT:
        payload = {key: value for key, value in document.items() if not key.startswith("_")}
        return model_type.model_validate(payload)

    async def _query(
        self,
        model_type: type[ModelT],
        tenant_id: str,
        entity_type: str,
        extra: str = "",
        parameters: Iterable[dict[str, Any]] = (),
    ) -> list[ModelT]:
        items = self._container.query_items(
            query=(
                "SELECT * FROM c WHERE c.tenantId = @tenantId AND c.entityType = @entityType"
                + extra
            ),
            parameters=[
                {"name": "@tenantId", "value": tenant_id},
                {"name": "@entityType", "value": entity_type},
                *parameters,
            ],
            partition_key=tenant_id,
        )
        return [self._model(model_type, item) async for item in items]

    @staticmethod
    async def _each[ItemT](
        items: Iterable[ItemT], action: Callable[[ItemT], Awaitable[None]]
    ) -> None:
        semaphore = asyncio.Semaphore(WRITE_CONCURRENCY)

        async def run(item: ItemT) -> None:
            async with semaphore:
                await action(item)

        await asyncio.gather(*(run(item) for item in items))

    async def _upsert(self, model: BaseModel) -> None:
        await self._container.upsert_item(self._document(model))

    async def get_rollup_state(self, tenant_id: str, gateway_id: str) -> UsageRollupState | None:
        try:
            item = await self._container.read_item(
                item=usage_rollup_state_id(tenant_id, gateway_id), partition_key=tenant_id
            )
        except exceptions.CosmosResourceNotFoundError:
            return None
        state = self._model(UsageRollupState, item)
        return state if state.tenant_id == tenant_id else None

    async def list_rollup_states(self, tenant_id: str) -> list[UsageRollupState]:
        return await self._query(UsageRollupState, tenant_id, "usageRollupState")

    async def update_rollup_state(
        self,
        tenant_id: str,
        gateway_id: str,
        change: Callable[[UsageRollupState], UsageRollupState],
    ) -> UsageRollupState:
        state_id = usage_rollup_state_id(tenant_id, gateway_id)
        for _ in range(STATE_WRITE_ATTEMPTS):
            try:
                item = await self._container.read_item(item=state_id, partition_key=tenant_id)
            except exceptions.CosmosResourceNotFoundError:
                item = None
            if item is not None and item.get("tenantId") != tenant_id:
                raise ConflictError("The gateway's rollup state belongs to another tenant")
            current = (
                self._model(UsageRollupState, item)
                if item is not None
                else UsageRollupState(id=state_id, tenant_id=tenant_id, gateway_id=gateway_id)
            )
            stored = change(current).model_copy(
                update={
                    "id": state_id,
                    "tenant_id": tenant_id,
                    "gateway_id": gateway_id,
                    "updated_at": utc_now(),
                }
            )
            try:
                if item is None:
                    await self._container.create_item(self._document(stored))
                else:
                    await self._container.replace_item(
                        item=state_id,
                        body=self._document(stored),
                        etag=item["_etag"],
                        match_condition=MatchConditions.IfNotModified,
                    )
            except (
                exceptions.CosmosAccessConditionFailedError,
                exceptions.CosmosResourceExistsError,
                exceptions.CosmosResourceNotFoundError,
            ):
                # Someone else saved it since it was read; apply the change to what they saved.
                continue
            return stored
        raise ConflictError("The gateway's rollup state kept changing. Try again in a moment.")

    async def list_attribution_records(self, tenant_id: str) -> list[AttributionRecord]:
        return await self._query(AttributionRecord, tenant_id, "attributionRecord")

    async def save_attribution_records(self, records: Sequence[AttributionRecord]) -> None:
        await self._each(records, self._upsert)

    async def list_facts(
        self,
        tenant_id: str,
        *,
        start_day: str,
        end_day: str,
        link_keys: Sequence[str] | None = None,
        gateway_ids: Sequence[str] | None = None,
    ) -> list[UsageFact]:
        extra = " AND c.day >= @startDay AND c.day <= @endDay"
        parameters: list[dict[str, Any]] = [
            {"name": "@startDay", "value": start_day},
            {"name": "@endDay", "value": end_day},
        ]
        if link_keys is not None:
            if not link_keys:
                return []
            extra += " AND ARRAY_CONTAINS(@linkKeys, c.linkKey)"
            parameters.append({"name": "@linkKeys", "value": sorted(set(link_keys))})
        if gateway_ids is not None:
            if not gateway_ids:
                return []
            extra += " AND ARRAY_CONTAINS(@gatewayIds, c.gatewayId)"
            parameters.append({"name": "@gatewayIds", "value": sorted(set(gateway_ids))})
        return await self._query(UsageFact, tenant_id, "usageFact", extra, parameters)

    async def list_summaries(
        self,
        tenant_id: str,
        *,
        period: SummaryPeriod,
        start: str,
        end: str,
        dimensions: Sequence[SummaryDimension],
        gateway_ids: Sequence[str] | None = None,
    ) -> list[UsageSummary]:
        if not dimensions:
            return []
        extra = (
            " AND c.period = @period AND c.periodStart >= @start AND c.periodStart <= @end"
            " AND ARRAY_CONTAINS(@dimensions, c.dimension)"
        )
        parameters: list[dict[str, Any]] = [
            {"name": "@period", "value": period},
            {"name": "@start", "value": start},
            {"name": "@end", "value": end},
            {"name": "@dimensions", "value": sorted(set(dimensions))},
        ]
        if gateway_ids is not None:
            if not gateway_ids:
                return []
            extra += " AND ARRAY_CONTAINS(@gatewayIds, c.gatewayId)"
            parameters.append({"name": "@gatewayIds", "value": sorted(set(gateway_ids))})
        return await self._query(UsageSummary, tenant_id, "usageSummary", extra, parameters)

    async def list_bucket_hashes(
        self, tenant_id: str, gateway_id: str, bucket: str
    ) -> dict[str, str]:
        items = self._container.query_items(
            query=(
                "SELECT c.id, c.contentHash FROM c WHERE c.tenantId = @tenantId "
                "AND c.gatewayId = @gatewayId AND c.bucket = @bucket "
                "AND (c.entityType = 'usageFact' OR c.entityType = 'usageSummary')"
            ),
            parameters=[
                {"name": "@tenantId", "value": tenant_id},
                {"name": "@gatewayId", "value": gateway_id},
                {"name": "@bucket", "value": bucket},
            ],
            partition_key=tenant_id,
        )
        return {str(item["id"]): str(item.get("contentHash") or "") async for item in items}

    async def upsert_rollups(self, items: Sequence[UsageFact | UsageSummary]) -> None:
        await self._each(items, self._upsert)

    async def delete_rollups(self, tenant_id: str, item_ids: Sequence[str]) -> None:
        async def delete(item_id: str) -> None:
            try:
                await self._container.delete_item(item=item_id, partition_key=tenant_id)
            except exceptions.CosmosResourceNotFoundError:
                pass

        await self._each(item_ids, delete)
