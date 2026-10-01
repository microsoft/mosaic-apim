from collections.abc import Callable
from uuid import uuid4

from mosaic_api.budgets import (
    Budget,
    BudgetState,
    EmailSettings,
    GateState,
    budget_state_id,
    gate_state_id,
)
from mosaic_api.domain import AuditEvent, utc_now
from mosaic_api.errors import ConflictError

BUDGET_CHANGED = "The budget changed; reload it and try again"
EMAIL_CHANGED = "The email settings changed; reload them and try again"


class InMemoryBudgetRepository:
    """Budgets and their state for local runs and tests. Every read returns a copy.

    Nothing awaits between a state's read and its write, so no other writer can come between them.
    """

    def __init__(self) -> None:
        self.budgets: dict[tuple[str, str], Budget] = {}
        self.email: dict[str, EmailSettings] = {}
        self.states: dict[tuple[str, str], BudgetState] = {}
        self.gates: dict[tuple[str, str], GateState] = {}
        self.audit_events: dict[str, AuditEvent] = {}

    async def ready(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    async def record_audit(self, event: AuditEvent) -> None:
        self.audit_events[event.id] = event

    async def list_budgets(self, tenant_id: str) -> list[Budget]:
        return sorted(
            (
                item.model_copy(deep=True)
                for (tenant, _), item in self.budgets.items()
                if tenant == tenant_id
            ),
            key=lambda item: item.id,
        )

    async def get_budget(self, tenant_id: str, budget_id: str) -> Budget | None:
        item = self.budgets.get((tenant_id, budget_id))
        return None if item is None else item.model_copy(deep=True)

    async def save_budget(self, budget: Budget, audit_event: AuditEvent) -> Budget:
        key = (budget.tenant_id, budget.id)
        current = self.budgets.get(key)
        if (current is None) != (budget.etag is None) or (
            current is not None and current.etag != budget.etag
        ):
            raise ConflictError(BUDGET_CHANGED)
        stored = budget.model_copy(update={"etag": uuid4().hex}, deep=True)
        self.budgets[key] = stored
        self.audit_events[audit_event.id] = audit_event
        return stored.model_copy(deep=True)

    async def delete_budget(self, budget: Budget, audit_event: AuditEvent) -> None:
        key = (budget.tenant_id, budget.id)
        current = self.budgets.get(key)
        if current is None or current.etag != budget.etag:
            raise ConflictError(BUDGET_CHANGED)
        del self.budgets[key]
        self.audit_events[audit_event.id] = audit_event

    async def get_email_settings(self, tenant_id: str) -> EmailSettings | None:
        item = self.email.get(tenant_id)
        return None if item is None else item.model_copy(deep=True)

    async def save_email_settings(
        self, settings: EmailSettings, audit_event: AuditEvent
    ) -> EmailSettings:
        current = self.email.get(settings.tenant_id)
        if (current is None) != (settings.etag is None) or (
            current is not None and current.etag != settings.etag
        ):
            raise ConflictError(EMAIL_CHANGED)
        stored = settings.model_copy(update={"etag": uuid4().hex}, deep=True)
        self.email[settings.tenant_id] = stored
        self.audit_events[audit_event.id] = audit_event
        return stored.model_copy(deep=True)

    async def list_budget_states(self, tenant_id: str) -> list[BudgetState]:
        return [
            state.model_copy(deep=True)
            for (tenant, _), state in sorted(self.states.items())
            if tenant == tenant_id
        ]

    async def get_budget_state(self, tenant_id: str, budget_id: str) -> BudgetState | None:
        state = self.states.get((tenant_id, budget_state_id(tenant_id, budget_id)))
        return None if state is None else state.model_copy(deep=True)

    async def update_budget_state(
        self,
        tenant_id: str,
        budget_id: str,
        change: Callable[[BudgetState], BudgetState],
    ) -> BudgetState:
        state_id = budget_state_id(tenant_id, budget_id)
        current = self.states.get((tenant_id, state_id))
        changed = change(
            current.model_copy(deep=True)
            if current is not None
            else BudgetState(id=state_id, tenant_id=tenant_id, budget_id=budget_id)
        )
        stored = changed.model_copy(
            update={
                "id": state_id,
                "tenant_id": tenant_id,
                "budget_id": budget_id,
                "updated_at": utc_now(),
            },
            deep=True,
        )
        self.states[(tenant_id, state_id)] = stored
        return stored.model_copy(deep=True)

    async def delete_budget_state(self, tenant_id: str, budget_id: str) -> None:
        self.states.pop((tenant_id, budget_state_id(tenant_id, budget_id)), None)

    async def list_gate_states(self, tenant_id: str) -> list[GateState]:
        return [
            state.model_copy(deep=True)
            for (tenant, _), state in sorted(self.gates.items())
            if tenant == tenant_id
        ]

    async def update_gate_state(
        self,
        tenant_id: str,
        gateway_id: str,
        change: Callable[[GateState], GateState],
    ) -> GateState:
        state_id = gate_state_id(tenant_id, gateway_id)
        current = self.gates.get((tenant_id, state_id))
        changed = change(
            current.model_copy(deep=True)
            if current is not None
            else GateState(id=state_id, tenant_id=tenant_id, gateway_id=gateway_id)
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
        self.gates[(tenant_id, state_id)] = stored
        return stored.model_copy(deep=True)
