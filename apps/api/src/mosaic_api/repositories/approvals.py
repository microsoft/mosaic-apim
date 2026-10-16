"""Focused approval persistence extensions; incumbent EntitlementRepository stays compatible."""

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Protocol

from mosaic_api.approvals import (
    AiApprovalOperationState,
    AiApprovalRecommendation,
    AiApprovalSettings,
    ApprovalDecisionProvenance,
    ApprovalEvaluatorState,
    ApprovalPolicy,
    ApprovalPolicyEvaluation,
    BulkApprovalResult,
)
from mosaic_api.domain import AccessRequest, AuditEvent, Entitlement
from mosaic_api.notifications import NotificationEvent
from mosaic_api.scheduling import SchedulerLease


class AccessRequestEventRepository(Protocol):
    """Same desired-state tenant partition as requests/grants/audit.

    Each method commits all source writes and events together or none. A failed request/grant
    ETag precondition raises ConflictError without publishing any event. Never implement this
    extension as a post-mutation callback to a separate notification repository.
    """

    async def create_access_request_with_events(
        self,
        request: AccessRequest,
        audit_event: AuditEvent,
        events: Sequence[NotificationEvent],
    ) -> AccessRequest: ...

    async def save_access_request_with_events(
        self,
        request: AccessRequest,
        audit_event: AuditEvent,
        events: Sequence[NotificationEvent],
    ) -> AccessRequest: ...

    async def approve_access_request_with_events(
        self,
        request: AccessRequest,
        entitlement: Entitlement,
        audit_events: Sequence[AuditEvent],
        events: Sequence[NotificationEvent],
        *,
        provenance: ApprovalDecisionProvenance | None = None,
        policy_guard: ApprovalPolicy | None = None,
    ) -> AccessRequest: ...


class ApprovalRepository(Protocol):
    """Desired settings use create-if-absent / ETag-conditional replace with audit.

    Revisions increase on every settings edit, invalidating old evaluations. Operational updates
    re-read and reapply their pure change function on ETag conflicts, as BudgetRepository does.
    """

    async def ready(self) -> bool: ...

    async def close(self) -> None: ...

    async def claim_evaluator_lease(
        self, tenant_id: str, owner: str, *, now: datetime, lease_seconds: int
    ) -> SchedulerLease | None: ...

    async def release_evaluator_lease(self, lease: SchedulerLease) -> None: ...

    async def check_evaluator_lease(self, lease: SchedulerLease, *, now: datetime) -> bool: ...

    async def list_approval_policies(self, tenant_id: str) -> list[ApprovalPolicy]: ...

    async def get_approval_policy(
        self, tenant_id: str, policy_id: str
    ) -> ApprovalPolicy | None: ...

    async def save_approval_policy(
        self, policy: ApprovalPolicy, audit_event: AuditEvent
    ) -> ApprovalPolicy: ...

    async def delete_approval_policy(
        self, policy: ApprovalPolicy, audit_event: AuditEvent
    ) -> None: ...

    async def save_policy_evaluation(self, evaluation: ApprovalPolicyEvaluation) -> None: ...

    async def list_policy_evaluations(
        self, tenant_id: str, request_id: str
    ) -> list[ApprovalPolicyEvaluation]: ...

    async def get_ai_approval_settings(self, tenant_id: str) -> AiApprovalSettings | None: ...

    async def get_ai_operation_state(self, tenant_id: str) -> AiApprovalOperationState | None: ...

    async def update_ai_operation_state(
        self, tenant_id: str, change: Callable[[AiApprovalOperationState], AiApprovalOperationState]
    ) -> AiApprovalOperationState: ...

    async def save_ai_approval_settings(
        self, settings: AiApprovalSettings, audit_event: AuditEvent
    ) -> AiApprovalSettings: ...

    async def save_ai_recommendation(self, recommendation: AiApprovalRecommendation) -> None: ...

    async def list_ai_recommendations(
        self, tenant_id: str, request_id: str
    ) -> list[AiApprovalRecommendation]: ...

    async def get_evaluator_state(self, tenant_id: str) -> ApprovalEvaluatorState | None: ...

    async def update_evaluator_state(
        self,
        tenant_id: str,
        change: Callable[[ApprovalEvaluatorState], ApprovalEvaluatorState],
    ) -> ApprovalEvaluatorState: ...

    async def get_bulk_result(
        self, tenant_id: str, actor_object_id: str, idempotency_key: str
    ) -> BulkApprovalResult | None: ...

    async def save_bulk_result(
        self,
        tenant_id: str,
        actor_object_id: str,
        result: BulkApprovalResult,
        *,
        input_fingerprint: str,
    ) -> BulkApprovalResult:
        """CAS-merge per-request outcomes; a reused key with a different fingerprint conflicts.

        This is recovery/correlation metadata, not a transaction across independent approvals.
        """
        ...
