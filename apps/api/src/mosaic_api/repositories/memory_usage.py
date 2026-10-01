from collections.abc import Callable, Sequence

from pydantic import BaseModel

from mosaic_api.domain import utc_now
from mosaic_api.usage_telemetry import (
    AttributionRecord,
    SummaryDimension,
    SummaryPeriod,
    UsageFact,
    UsageRollupState,
    UsageSummary,
    usage_rollup_state_id,
)


def _copy[ModelT: BaseModel](model: ModelT) -> ModelT:
    return model.model_copy(deep=True)


class InMemoryUsageRollupRepository:
    """Rolled-up telemetry for local development and tests. Every read returns a copy."""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], UsageRollupState] = {}
        self._records: dict[tuple[str, str], AttributionRecord] = {}
        self._facts: dict[tuple[str, str], UsageFact] = {}
        self._summaries: dict[tuple[str, str], UsageSummary] = {}
        self.upserted = 0
        self.deleted = 0

    async def ready(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    async def get_rollup_state(self, tenant_id: str, gateway_id: str) -> UsageRollupState | None:
        state = self._states.get((tenant_id, usage_rollup_state_id(tenant_id, gateway_id)))
        return None if state is None else _copy(state)

    async def list_rollup_states(self, tenant_id: str) -> list[UsageRollupState]:
        return [
            _copy(state)
            for (tenant, _), state in sorted(self._states.items())
            if tenant == tenant_id
        ]

    async def update_rollup_state(
        self,
        tenant_id: str,
        gateway_id: str,
        change: Callable[[UsageRollupState], UsageRollupState],
    ) -> UsageRollupState:
        # Nothing awaits between the read and the write, so no other writer can come between them.
        state_id = usage_rollup_state_id(tenant_id, gateway_id)
        current = self._states.get((tenant_id, state_id))
        changed = change(
            _copy(current)
            if current is not None
            else UsageRollupState(id=state_id, tenant_id=tenant_id, gateway_id=gateway_id)
        )
        stored = changed.model_copy(
            update={
                "id": state_id,
                "tenant_id": tenant_id,
                "gateway_id": gateway_id,
                "updated_at": utc_now(),
            },
            deep=True,
        )
        self._states[(tenant_id, state_id)] = stored
        return _copy(stored)

    async def list_attribution_records(self, tenant_id: str) -> list[AttributionRecord]:
        return [
            _copy(record)
            for (tenant, _), record in sorted(self._records.items())
            if tenant == tenant_id
        ]

    async def save_attribution_records(self, records: Sequence[AttributionRecord]) -> None:
        for record in records:
            self._records[(record.tenant_id, record.id)] = _copy(record)

    async def list_facts(
        self,
        tenant_id: str,
        *,
        start_day: str,
        end_day: str,
        link_keys: Sequence[str] | None = None,
        gateway_ids: Sequence[str] | None = None,
    ) -> list[UsageFact]:
        keys = None if link_keys is None else set(link_keys)
        gateways = None if gateway_ids is None else set(gateway_ids)
        return [
            _copy(fact)
            for (tenant, _), fact in sorted(self._facts.items())
            if tenant == tenant_id
            and start_day <= fact.day <= end_day
            and (keys is None or fact.link_key in keys)
            and (gateways is None or fact.gateway_id in gateways)
        ]

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
        wanted = set(dimensions)
        gateways = None if gateway_ids is None else set(gateway_ids)
        return [
            _copy(summary)
            for (tenant, _), summary in sorted(self._summaries.items())
            if tenant == tenant_id
            and summary.period == period
            and start <= summary.period_start <= end
            and summary.dimension in wanted
            and (gateways is None or summary.gateway_id in gateways)
        ]

    async def list_bucket_hashes(
        self, tenant_id: str, gateway_id: str, bucket: str
    ) -> dict[str, str]:
        hashes: dict[str, str] = {}
        items: list[tuple[tuple[str, str], UsageFact | UsageSummary]] = [
            *self._facts.items(),
            *self._summaries.items(),
        ]
        for (tenant, item_id), item in items:
            if tenant == tenant_id and item.gateway_id == gateway_id and item.bucket == bucket:
                hashes[item_id] = item.content_hash
        return hashes

    async def upsert_rollups(self, items: Sequence[UsageFact | UsageSummary]) -> None:
        for item in items:
            stored = item.model_copy(update={"updated_at": utc_now()}, deep=True)
            if isinstance(stored, UsageFact):
                self._facts[(stored.tenant_id, stored.id)] = stored
            else:
                self._summaries[(stored.tenant_id, stored.id)] = stored
            self.upserted += 1

    async def delete_rollups(self, tenant_id: str, item_ids: Sequence[str]) -> None:
        for item_id in item_ids:
            removed_fact = self._facts.pop((tenant_id, item_id), None)
            removed_summary = self._summaries.pop((tenant_id, item_id), None)
            if removed_fact is not None or removed_summary is not None:
                self.deleted += 1
