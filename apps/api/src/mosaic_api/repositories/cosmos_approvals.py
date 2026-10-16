"""Approval records use tenant-partition CAS; policies/settings include their audit outbox."""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TypeVar

from azure.core import MatchConditions
from azure.cosmos import exceptions

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
from mosaic_api.repositories.cosmos import CosmosRepositoryBase
from mosaic_api.repositories.memory_approvals import merge_bulk
from mosaic_api.scheduling import SchedulerLease
from mosaic_api.services.notification_events import notification_record_id

T = TypeVar("T", bound=Entity)


class CosmosApprovalRepository(CosmosRepositoryBase):
    async def _cas(self, value: T) -> T:
        try:
            if value.etag:
                saved = await self._desired.replace_item(
                    item=value.id,
                    body=self._document(value),
                    etag=value.etag,
                    match_condition=MatchConditions.IfNotModified,
                )
            else:
                saved = await self._desired.create_item(self._document(value))
        except (
            exceptions.CosmosResourceExistsError,
            exceptions.CosmosAccessConditionFailedError,
            exceptions.CosmosResourceNotFoundError,
        ) as error:
            raise ConflictError("The approval record changed; reload it and try again.") from error
        return self._model(type(value), saved)

    async def list_approval_policies(self, tenant_id: str) -> list[ApprovalPolicy]:
        return await self._query(ApprovalPolicy, tenant_id, "approvalPolicy")

    async def get_approval_policy(self, tenant_id: str, policy_id: str) -> ApprovalPolicy | None:
        return await self._read(ApprovalPolicy, tenant_id, policy_id)

    async def save_approval_policy(
        self, policy: ApprovalPolicy, audit_event: AuditEvent
    ) -> ApprovalPolicy:
        await self._mutate(
            policy,
            None,
            audit_event,
            "replace" if policy.etag else "create",
            conflict_message="The approval policy changed; reload it.",
        )
        return await self._read(ApprovalPolicy, policy.tenant_id, policy.id) or policy

    async def delete_approval_policy(self, policy: ApprovalPolicy, audit_event: AuditEvent) -> None:
        if not policy.etag:
            raise ConflictError("Deleting an approval policy requires its current ETag.")
        await self._mutate(
            policy,
            policy.id,
            audit_event,
            "delete",
            conflict_message="The approval policy changed; reload it.",
        )

    async def save_policy_evaluation(self, evaluation: ApprovalPolicyEvaluation) -> None:
        await self._desired.upsert_item(self._document(evaluation))

    async def list_policy_evaluations(
        self, tenant_id: str, request_id: str
    ) -> list[ApprovalPolicyEvaluation]:
        return await self._query(
            ApprovalPolicyEvaluation,
            tenant_id,
            "approvalPolicyEvaluation",
            " AND c.requestId = @requestId",
            [{"name": "@requestId", "value": request_id}],
        )

    async def get_ai_approval_settings(self, tenant_id: str) -> AiApprovalSettings | None:
        return await self._read(
            AiApprovalSettings, tenant_id, notification_record_id("aiApprovalSettings", tenant_id)
        )

    async def save_ai_approval_settings(
        self, settings: AiApprovalSettings, audit_event: AuditEvent
    ) -> AiApprovalSettings:
        await self._mutate(
            settings,
            None,
            audit_event,
            "replace" if settings.etag else "create",
            conflict_message="AI assistance settings changed; reload them.",
        )
        return await self.get_ai_approval_settings(settings.tenant_id) or settings

    async def save_ai_recommendation(self, recommendation: AiApprovalRecommendation) -> None:
        await self._desired.upsert_item(self._document(recommendation))

    async def get_ai_operation_state(self, tenant_id: str) -> AiApprovalOperationState | None:
        return await self._read(
            AiApprovalOperationState,
            tenant_id,
            notification_record_id("aiApprovalOperationState", tenant_id),
        )

    async def update_ai_operation_state(
        self, tenant_id: str, change: Callable[[AiApprovalOperationState], AiApprovalOperationState]
    ) -> AiApprovalOperationState:
        for _ in range(5):
            state = await self.get_ai_operation_state(tenant_id) or AiApprovalOperationState(
                id=notification_record_id("aiApprovalOperationState", tenant_id),
                tenant_id=tenant_id,
            )
            try:
                return await self._cas(
                    change(state).model_copy(
                        update={
                            "id": state.id,
                            "tenant_id": tenant_id,
                            "etag": state.etag,
                            "updated_at": utc_now(),
                        }
                    )
                )
            except ConflictError:
                continue
        raise ConflictError("The AI operational state kept changing; try again.")

    async def list_ai_recommendations(
        self, tenant_id: str, request_id: str
    ) -> list[AiApprovalRecommendation]:
        return await self._query(
            AiApprovalRecommendation,
            tenant_id,
            "aiApprovalRecommendation",
            " AND c.requestId = @requestId",
            [{"name": "@requestId", "value": request_id}],
        )

    async def get_evaluator_state(self, tenant_id: str) -> ApprovalEvaluatorState | None:
        return await self._read(
            ApprovalEvaluatorState,
            tenant_id,
            notification_record_id("approvalEvaluatorState", tenant_id),
        )

    async def update_evaluator_state(
        self, tenant_id: str, change: Callable[[ApprovalEvaluatorState], ApprovalEvaluatorState]
    ) -> ApprovalEvaluatorState:
        for _ in range(5):
            state = await self.get_evaluator_state(tenant_id) or ApprovalEvaluatorState(
                id=notification_record_id("approvalEvaluatorState", tenant_id), tenant_id=tenant_id
            )
            try:
                return await self._cas(
                    change(state).model_copy(
                        update={
                            "id": state.id,
                            "tenant_id": tenant_id,
                            "etag": state.etag,
                            "updated_at": utc_now(),
                        }
                    )
                )
            except ConflictError:
                continue
        raise ConflictError("The evaluator state kept changing; try again.")

    async def get_bulk_result(
        self, tenant_id: str, actor_object_id: str, idempotency_key: str
    ) -> BulkApprovalResult | None:
        record = await self._read(
            BulkApprovalRecord,
            tenant_id,
            notification_record_id("bulkApproval", tenant_id, actor_object_id, idempotency_key),
        )
        return record.result if record else None

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
        for _ in range(5):
            current = await self._read(BulkApprovalRecord, tenant_id, record_id)
            record = BulkApprovalRecord(
                id=record_id,
                tenant_id=tenant_id,
                actor_object_id=actor_object_id,
                input_fingerprint=input_fingerprint,
                result=result.model_copy(deep=True),
                etag=current.etag if current else None,
            )
            record = merge_bulk(current, record)
            try:
                return (await self._cas(record)).result
            except ConflictError:
                continue
        raise ConflictError("The approval batch kept changing; retry the same inputs.")

    async def claim_evaluator_lease(
        self, tenant_id: str, owner: str, *, now: datetime, lease_seconds: int
    ) -> SchedulerLease | None:
        lease_id = notification_record_id("approvalEvaluatorLease", tenant_id)
        current = await self._read(SchedulerLease, tenant_id, lease_id)
        if current and current.expires_at > now:
            return None
        lease = SchedulerLease(
            id=lease_id,
            tenant_id=tenant_id,
            scope="approvalPolicies",
            owner=owner,
            token=new_id("lease"),
            expires_at=now + timedelta(seconds=lease_seconds),
            etag=current.etag if current else None,
        )
        try:
            return await self._cas(lease)
        except ConflictError:
            return None

    async def check_evaluator_lease(self, lease: SchedulerLease, *, now: datetime) -> bool:
        current = await self._read(SchedulerLease, lease.tenant_id, lease.id)
        return bool(current and current.token == lease.token and current.expires_at > now)

    async def release_evaluator_lease(self, lease: SchedulerLease) -> None:
        try:
            await self._desired.delete_item(
                item=lease.id,
                partition_key=lease.tenant_id,
                etag=lease.etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except (
            exceptions.CosmosAccessConditionFailedError,
            exceptions.CosmosResourceNotFoundError,
        ):
            return
