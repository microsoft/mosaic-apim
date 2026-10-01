"""Budgets in ``desired-state``, and where each stands in ``usage-rollups``. See ADR 0023.

A budget and the email settings are administrator-authored desired state, each written with its
audit event and only over the version that was read. A budget's state and each gateway's
blocked-list state are operational, like a gateway's rollup state, so they sit beside it in
``usage-rollups``. They're saved only over the version that was read, so two writers can't lose
each other's change.
"""

from collections.abc import Callable
from typing import Any, TypeVar

from azure.core import MatchConditions
from azure.cosmos import exceptions
from azure.cosmos.aio import ContainerProxy, CosmosClient

from mosaic_api.budgets import (
    Budget,
    BudgetState,
    EmailSettings,
    GateState,
    budget_state_id,
    email_settings_id,
    gate_state_id,
)
from mosaic_api.domain import AuditEvent, utc_now
from mosaic_api.errors import ConflictError
from mosaic_api.repositories.cosmos import CosmosRepositoryBase

BUDGET_CHANGED = "The budget changed; reload it and try again"
EMAIL_CHANGED = "The email settings changed; reload them and try again"
# A budget's state has few writers, so a handful of conditional retries is plenty.
STATE_WRITE_ATTEMPTS = 5
StateT = TypeVar("StateT", BudgetState, GateState)


class CosmosBudgetRepository(CosmosRepositoryBase):
    def __init__(
        self,
        client: CosmosClient,
        database_name: str,
        desired_state_container: str,
        audit_events_container: str,
        usage_rollups_container: str,
        *,
        owns_client: bool = True,
    ) -> None:
        super().__init__(
            client,
            database_name,
            desired_state_container,
            audit_events_container,
            owns_client=owns_client,
        )
        database = client.get_database_client(database_name)
        self._states: ContainerProxy = database.get_container_client(usage_rollups_container)

    async def ready(self) -> bool:
        if not await super().ready():
            return False
        try:
            await self._states.read()
        except exceptions.CosmosHttpResponseError:
            return False
        return True

    # -- desired state ----------------------------------------------------------------------

    async def list_budgets(self, tenant_id: str) -> list[Budget]:
        items = await self._query(Budget, tenant_id, "budget")
        return sorted(items, key=lambda item: item.id)

    async def get_budget(self, tenant_id: str, budget_id: str) -> Budget | None:
        return await self._read(Budget, tenant_id, budget_id)

    async def save_budget(self, budget: Budget, audit_event: AuditEvent) -> Budget:
        await self._mutate(
            budget,
            None,
            audit_event,
            "replace" if budget.etag else "create",
            conflict_message=BUDGET_CHANGED,
        )
        return budget

    async def delete_budget(self, budget: Budget, audit_event: AuditEvent) -> None:
        if not budget.etag:
            raise ConflictError(BUDGET_CHANGED)
        await self._mutate(
            budget, budget.id, audit_event, "delete", conflict_message=BUDGET_CHANGED
        )

    async def get_email_settings(self, tenant_id: str) -> EmailSettings | None:
        return await self._read(EmailSettings, tenant_id, email_settings_id(tenant_id))

    async def save_email_settings(
        self, settings: EmailSettings, audit_event: AuditEvent
    ) -> EmailSettings:
        await self._mutate(
            settings,
            None,
            audit_event,
            "replace" if settings.etag else "create",
            conflict_message=EMAIL_CHANGED,
        )
        return settings

    # -- operational state ------------------------------------------------------------------

    @staticmethod
    def _state_model(model_type: type[StateT], document: dict[str, Any]) -> StateT:
        payload = {key: value for key, value in document.items() if not key.startswith("_")}
        # The version read, so a delete can be made only over it.
        return model_type.model_validate(payload).model_copy(update={"etag": document.get("_etag")})

    async def _list_states(
        self, model_type: type[StateT], tenant_id: str, entity: str
    ) -> list[StateT]:
        items = self._states.query_items(
            query="SELECT * FROM c WHERE c.tenantId = @tenantId AND c.entityType = @entityType",
            parameters=[
                {"name": "@tenantId", "value": tenant_id},
                {"name": "@entityType", "value": entity},
            ],
            partition_key=tenant_id,
        )
        return [self._state_model(model_type, item) async for item in items]

    async def _update_state(
        self,
        model_type: type[StateT],
        tenant_id: str,
        state_id: str,
        identity: dict[str, str],
        change: Callable[[StateT], StateT],
    ) -> StateT:
        for _ in range(STATE_WRITE_ATTEMPTS):
            try:
                item = await self._states.read_item(item=state_id, partition_key=tenant_id)
            except exceptions.CosmosResourceNotFoundError:
                item = None
            if item is not None and item.get("tenantId") != tenant_id:
                raise ConflictError("The budget state belongs to another tenant")
            current = (
                self._state_model(model_type, item)
                if item is not None
                else model_type.model_validate(
                    {"id": state_id, "tenant_id": tenant_id, **identity}
                )
            )
            stored = change(current).model_copy(
                update={"id": state_id, "tenant_id": tenant_id, **identity, "updated_at": utc_now()}
            )
            body = stored.model_dump(mode="json", by_alias=True, exclude={"etag"})
            try:
                if item is None:
                    saved = await self._states.create_item(body)
                else:
                    saved = await self._states.replace_item(
                        item=state_id,
                        body=body,
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
            # The version saved, so a later write or delete can be made only over it.
            return stored.model_copy(update={"etag": saved.get("_etag")})
        raise ConflictError("The budget's state kept changing. Try again in a moment.")

    async def list_budget_states(self, tenant_id: str) -> list[BudgetState]:
        return await self._list_states(BudgetState, tenant_id, "budgetState")

    async def get_budget_state(self, tenant_id: str, budget_id: str) -> BudgetState | None:
        try:
            item = await self._states.read_item(
                item=budget_state_id(tenant_id, budget_id), partition_key=tenant_id
            )
        except exceptions.CosmosResourceNotFoundError:
            return None
        state = self._state_model(BudgetState, item)
        return state if state.tenant_id == tenant_id else None

    async def update_budget_state(
        self,
        tenant_id: str,
        budget_id: str,
        change: Callable[[BudgetState], BudgetState],
    ) -> BudgetState:
        return await self._update_state(
            BudgetState,
            tenant_id,
            budget_state_id(tenant_id, budget_id),
            {"budget_id": budget_id},
            change,
        )

    async def delete_budget_state(
        self, tenant_id: str, budget_id: str, *, etag: str | None = None
    ) -> bool:
        conditions: dict[str, Any] = (
            {"etag": etag, "match_condition": MatchConditions.IfNotModified} if etag else {}
        )
        try:
            await self._states.delete_item(
                item=budget_state_id(tenant_id, budget_id), partition_key=tenant_id, **conditions
            )
        except (
            exceptions.CosmosResourceNotFoundError,
            exceptions.CosmosAccessConditionFailedError,
        ):
            # Gone already, or saved since by a budget set again under the same ID.
            return False
        return True

    async def list_gate_states(self, tenant_id: str) -> list[GateState]:
        return await self._list_states(GateState, tenant_id, "budgetGateState")

    async def update_gate_state(
        self,
        tenant_id: str,
        gateway_id: str,
        change: Callable[[GateState], GateState],
    ) -> GateState:
        return await self._update_state(
            GateState,
            tenant_id,
            gate_state_id(tenant_id, gateway_id),
            {"gateway_id": gateway_id},
            change,
        )
