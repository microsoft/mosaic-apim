"""Approval settings, recovery records and leased evaluator state for local deployments."""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TypeVar

from mosaic_api.approvals import (
    AiApprovalOperationState,
    AiApprovalRecommendation,
    AiApprovalSettings,
    ApprovalEvaluatorState,
    ApprovalPolicy,
    ApprovalPolicyEvaluation,
    BulkApprovalRecord,
    BulkApprovalResult,
)
from mosaic_api.domain import AuditEvent, Entity, new_id, utc_now
from mosaic_api.errors import ConflictError
from mosaic_api.scheduling import SchedulerLease
from mosaic_api.services.notification_events import notification_record_id

T = TypeVar("T", bound=Entity)


def merge_bulk(
    current: BulkApprovalRecord | None, incoming: BulkApprovalRecord
) -> BulkApprovalRecord:
    if current is None:
        return incoming
    if current.input_fingerprint != incoming.input_fingerprint:
        raise ConflictError("This idempotency key was used for different approval inputs.")
    previous = {item.request_id: item for item in current.result.items}
    incoming.result = incoming.result.model_copy(
        update={
            "batch_id": current.result.batch_id,
            "items": [
                previous[item.request_id]
                if previous.get(item.request_id)
                and previous[item.request_id].status in {"approved", "alreadyApproved"}
                else item
                for item in incoming.result.items
            ],
        }
    )
    return incoming


class InMemoryApprovalRepository:
    def __init__(self) -> None:
        self.policies: dict[tuple[str, str], ApprovalPolicy] = {}
        self.evaluations: dict[tuple[str, str], ApprovalPolicyEvaluation] = {}
        self.settings: dict[tuple[str, str], AiApprovalSettings] = {}
        self.ai_states: dict[tuple[str, str], AiApprovalOperationState] = {}
        self.recommendations: dict[tuple[str, str], AiApprovalRecommendation] = {}
        self.states: dict[tuple[str, str], ApprovalEvaluatorState] = {}
        self.batches: dict[tuple[str, str], BulkApprovalRecord] = {}
        self.leases: dict[tuple[str, str], SchedulerLease] = {}
        self.audit_events: dict[str, AuditEvent] = {}

    async def ready(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    def _save(self, store: dict[tuple[str, str], T], value: T, *, cas: bool = False) -> T:
        key = (value.tenant_id, value.id)
        previous = store.get(key)
        if cas and (
            (previous is None and value.etag)
            or (previous is not None and previous.etag != value.etag)
        ):
            raise ConflictError("The approval record changed; reload it and try again.")
        saved = value.model_copy(update={"etag": new_id("etag")}, deep=True)
        store[key] = saved
        return saved.model_copy(deep=True)

    async def list_approval_policies(self, tenant_id: str) -> list[ApprovalPolicy]:
        return [
            value.model_copy(deep=True)
            for (tenant, _), value in self.policies.items()
            if tenant == tenant_id
        ]

    async def get_approval_policy(self, tenant_id: str, policy_id: str) -> ApprovalPolicy | None:
        value = self.policies.get((tenant_id, policy_id))
        return value.model_copy(deep=True) if value else None

    async def save_approval_policy(
        self, policy: ApprovalPolicy, audit_event: AuditEvent
    ) -> ApprovalPolicy:
        saved = self._save(self.policies, policy, cas=True)
        self.audit_events[audit_event.id] = audit_event
        return saved

    async def delete_approval_policy(self, policy: ApprovalPolicy, audit_event: AuditEvent) -> None:
        current = self.policies.get((policy.tenant_id, policy.id))
        if current is None or current.etag != policy.etag:
            raise ConflictError("The approval policy changed; reload it.")
        del self.policies[(policy.tenant_id, policy.id)]
        self.audit_events[audit_event.id] = audit_event

    async def save_policy_evaluation(self, evaluation: ApprovalPolicyEvaluation) -> None:
        self._save(self.evaluations, evaluation)

    async def list_policy_evaluations(
        self, tenant_id: str, request_id: str
    ) -> list[ApprovalPolicyEvaluation]:
        return [
            value.model_copy(deep=True)
            for (tenant, _), value in self.evaluations.items()
            if tenant == tenant_id and value.request_id == request_id
        ]

    async def get_ai_approval_settings(self, tenant_id: str) -> AiApprovalSettings | None:
        value = self.settings.get(
            (tenant_id, notification_record_id("aiApprovalSettings", tenant_id))
        )
        return value.model_copy(deep=True) if value else None

    async def save_ai_approval_settings(
        self, settings: AiApprovalSettings, audit_event: AuditEvent
    ) -> AiApprovalSettings:
        saved = self._save(self.settings, settings, cas=True)
        self.audit_events[audit_event.id] = audit_event
        return saved

    async def save_ai_recommendation(self, recommendation: AiApprovalRecommendation) -> None:
        self._save(self.recommendations, recommendation)

    async def get_ai_operation_state(self, tenant_id: str) -> AiApprovalOperationState | None:
        value = self.ai_states.get(
            (tenant_id, notification_record_id("aiApprovalOperationState", tenant_id))
        )
        return value.model_copy(deep=True) if value else None

    async def update_ai_operation_state(
        self, tenant_id: str, change: Callable[[AiApprovalOperationState], AiApprovalOperationState]
    ) -> AiApprovalOperationState:
        state = await self.get_ai_operation_state(tenant_id) or AiApprovalOperationState(
            id=notification_record_id("aiApprovalOperationState", tenant_id), tenant_id=tenant_id
        )
        return self._save(
            self.ai_states,
            change(state).model_copy(
                update={"id": state.id, "tenant_id": tenant_id, "updated_at": utc_now()}
            ),
        )

    async def list_ai_recommendations(
        self, tenant_id: str, request_id: str
    ) -> list[AiApprovalRecommendation]:
        return [
            value.model_copy(deep=True)
            for (tenant, _), value in self.recommendations.items()
            if tenant == tenant_id and value.request_id == request_id
        ]

    async def get_evaluator_state(self, tenant_id: str) -> ApprovalEvaluatorState | None:
        value = self.states.get(
            (tenant_id, notification_record_id("approvalEvaluatorState", tenant_id))
        )
        return value.model_copy(deep=True) if value else None

    async def update_evaluator_state(
        self, tenant_id: str, change: Callable[[ApprovalEvaluatorState], ApprovalEvaluatorState]
    ) -> ApprovalEvaluatorState:
        state = await self.get_evaluator_state(tenant_id) or ApprovalEvaluatorState(
            id=notification_record_id("approvalEvaluatorState", tenant_id), tenant_id=tenant_id
        )
        return self._save(
            self.states,
            change(state).model_copy(
                update={"id": state.id, "tenant_id": tenant_id, "updated_at": utc_now()}
            ),
        )

    async def get_bulk_result(
        self, tenant_id: str, actor_object_id: str, idempotency_key: str
    ) -> BulkApprovalResult | None:
        value = self.batches.get(
            (
                tenant_id,
                notification_record_id("bulkApproval", tenant_id, actor_object_id, idempotency_key),
            )
        )
        return value.result.model_copy(deep=True) if value else None

    async def save_bulk_result(
        self,
        tenant_id: str,
        actor_object_id: str,
        result: BulkApprovalResult,
        *,
        input_fingerprint: str,
    ) -> BulkApprovalResult:
        record_id = notification_record_id(
            "bulkApproval", tenant_id, actor_object_id, result.idempotency_key
        )
        record = BulkApprovalRecord(
            id=record_id,
            tenant_id=tenant_id,
            actor_object_id=actor_object_id,
            input_fingerprint=input_fingerprint,
            result=result.model_copy(deep=True),
        )
        return self._save(
            self.batches, merge_bulk(self.batches.get((tenant_id, record_id)), record)
        ).result

    async def claim_evaluator_lease(
        self, tenant_id: str, owner: str, *, now: datetime, lease_seconds: int
    ) -> SchedulerLease | None:
        lease_id = notification_record_id("approvalEvaluatorLease", tenant_id)
        current = self.leases.get((tenant_id, lease_id))
        if current and current.expires_at > now:
            return None
        return self._save(
            self.leases,
            SchedulerLease(
                id=lease_id,
                tenant_id=tenant_id,
                scope="approvalPolicies",
                owner=owner,
                token=new_id("lease"),
                expires_at=now + timedelta(seconds=lease_seconds),
            ),
        )

    async def check_evaluator_lease(self, lease: SchedulerLease, *, now: datetime) -> bool:
        current = self.leases.get((lease.tenant_id, lease.id))
        return bool(current and current.token == lease.token and current.expires_at > now)

    async def release_evaluator_lease(self, lease: SchedulerLease) -> None:
        current = self.leases.get((lease.tenant_id, lease.id))
        if current and current.token == lease.token:
            del self.leases[(lease.tenant_id, lease.id)]
