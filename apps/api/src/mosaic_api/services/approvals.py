"""Bounded human bulk decisions and deterministic explicit-policy automation.

All writes go through EntitlementService's approval mutation. Neither entry point applies APIM.
"""

import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import structlog

from mosaic_api.approvals import (
    APPROVAL_EXECUTION_CONCURRENCY,
    POLICY_SYSTEM_ACTOR,
    ApprovalBlocker,
    ApprovalDecisionProvenance,
    ApprovalEvaluatorState,
    ApprovalPolicy,
    ApprovalPolicyDryRun,
    ApprovalPolicyEvaluation,
    ApprovalPolicyInput,
    ApprovalPolicySaveInput,
    ApprovalPreview,
    BulkApprovalInput,
    BulkApprovalItem,
    BulkApprovalItemResult,
    BulkApprovalPreview,
    BulkApprovalPreviewInput,
    BulkApprovalResult,
    BulkApprovalUnavailableItem,
)
from mosaic_api.domain import AccessRequestApproval, AuditEvent, new_id, utc_now
from mosaic_api.errors import ConflictError, DomainError, NotFoundError, ValidationError
from mosaic_api.repositories.approvals import ApprovalRepository
from mosaic_api.scheduling import WeeklySchedule
from mosaic_api.services.directory import Actor
from mosaic_api.services.entitlements import EntitlementService
from mosaic_api.services.notification_events import notification_record_id

logger = structlog.get_logger()


def schedule_open(schedule: WeeklySchedule, now: datetime) -> bool:
    local = now.astimezone(ZoneInfo(schedule.time_zone))
    wall = local.strftime("%H:%M")
    today = local.weekday()
    yesterday = (today - 1) % 7
    return any(
        (today in window.days_of_week and window.start_time <= wall < window.end_time)
        if not window.crosses_midnight
        else (
            (today in window.days_of_week and wall >= window.start_time)
            or (yesterday in window.days_of_week and wall < window.end_time)
        )
        for window in schedule.windows
    )


def approval_for(policy: ApprovalPolicy) -> AccessRequestApproval:
    return AccessRequestApproval(
        enforcement=policy.grant_template.enforcement,
        cost_center_id=policy.grant_template.cost_center_id,
        note=policy.grant_template.note,
    )


class ApprovalService:
    def __init__(
        self,
        repository: ApprovalRepository,
        *,
        entitlements: EntitlementService,
        clock: Callable[[], datetime] = utc_now,
        owner: str | None = None,
        hosting_verified: bool = False,
    ) -> None:
        self._repository = repository
        self._entitlements = entitlements
        self._clock = clock
        self._owner = owner or new_id("approvalWorker")
        self.hosting_verified = hosting_verified
        self._semaphore = asyncio.Semaphore(APPROVAL_EXECUTION_CONCURRENCY)

    async def preview(self, actor: Actor, payload: BulkApprovalPreviewInput) -> BulkApprovalPreview:
        items: list[ApprovalPreview] = []
        unavailable: list[BulkApprovalUnavailableItem] = []
        for request_id in payload.request_ids:
            try:
                items.append(await self._entitlements.preview_access_request(actor, request_id))
            except DomainError as error:
                unavailable.append(
                    BulkApprovalUnavailableItem(
                        request_id=request_id,
                        blocker=ApprovalBlocker(code=error.code, message=error.summary[:1000]),
                    )
                )
            except Exception:
                unavailable.append(
                    BulkApprovalUnavailableItem(
                        request_id=request_id,
                        blocker=ApprovalBlocker(
                            code="previewUnavailable",
                            message="This request's current approval context could not be loaded.",
                            next_action="Retry preview or review this request individually.",
                        ),
                    )
                )
        return BulkApprovalPreview(items=items, unavailable=unavailable)

    async def execute(self, actor: Actor, payload: BulkApprovalInput) -> BulkApprovalResult:
        fingerprint = hashlib.sha256(
            json.dumps(
                payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        result = BulkApprovalResult(
            batch_id=new_id("approvalBatch"),
            idempotency_key=payload.idempotency_key,
            items=[
                BulkApprovalItemResult(
                    request_id=item.request_id,
                    status="failed",
                    message="This item has not completed. Retry with the same inputs.",
                )
                for item in payload.items
            ],
        )
        # Reserve the key before any approval. A crash here is recoverable from source requests.
        result = await self._repository.save_bulk_result(
            actor.tenant_id, actor.object_id, result, input_fingerprint=fingerprint
        )
        previous = {item.request_id: item for item in result.items}

        async def run(item: BulkApprovalItem) -> BulkApprovalItemResult:
            if previous[item.request_id].status in {"approved", "alreadyApproved"}:
                return previous[item.request_id]
            async with self._semaphore:
                try:
                    request = await self._entitlements.get_access_request(actor, item.request_id)
                    already = request.state == "approved" and request.granted_entitlement_id
                    if not already:
                        preview = await self._entitlements.preview_access_request(
                            actor, item.request_id, item.approval
                        )
                        blockers = [
                            value
                            for value in preview.blockers
                            if not (
                                value.code == "environmentChanged"
                                and item.approval.confirmed_environment is not None
                            )
                        ]
                        if blockers:
                            error_type = (
                                ValidationError
                                if blockers[0].code
                                in {"mcpTokenLimits", "notACostCenterMember", "costCenterMissing"}
                                else ConflictError
                            )
                            raise error_type(
                                blockers[0].message, details={"reason": blockers[0].code}
                            )
                    context = (item.expected_context or preview.revision) if not already else None
                    approved = await self._entitlements.approve_access_request(
                        actor,
                        item.request_id,
                        item.approval,
                        expected_context=context,
                        provenance=ApprovalDecisionProvenance(
                            origin="bulk",
                            actor_object_id=actor.object_id,
                            batch_id=result.batch_id,
                            context_revision=context.context_revision if context else None,
                        ),
                    )
                    outcome = BulkApprovalItemResult(
                        request_id=item.request_id,
                        status="alreadyApproved" if already else "approved",
                        entitlement_id=approved.granted_entitlement_id,
                        next_action="Review and apply the resource's access plan.",
                    )
                except (ConflictError, NotFoundError) as error:
                    outcome = BulkApprovalItemResult(
                        request_id=item.request_id,
                        status="conflict",
                        message=error.summary[:1000],
                        next_action="Reload and review this request before retrying.",
                    )
                except ValidationError as error:
                    outcome = BulkApprovalItemResult(
                        request_id=item.request_id,
                        status="validationFailed",
                        message=error.summary[:1000],
                        next_action="Correct the approval settings.",
                    )
                except Exception:
                    logger.exception("bulk_approval_item_failed", request_id=item.request_id)
                    outcome = BulkApprovalItemResult(
                        request_id=item.request_id,
                        status="failed",
                        message="Approval did not complete. Retry this item with the same inputs.",
                        next_action="Retry; an already committed approval will not be duplicated.",
                    )
                # Each independent result is durable, not just the final HTTP response.
                snapshot = result.model_copy(
                    update={
                        "items": [
                            outcome if value.request_id == item.request_id else value
                            for value in result.items
                        ]
                    }
                )
                await self._repository.save_bulk_result(
                    actor.tenant_id, actor.object_id, snapshot, input_fingerprint=fingerprint
                )
                return outcome

        outcomes = await asyncio.gather(*(run(item) for item in payload.items))
        return await self._repository.save_bulk_result(
            actor.tenant_id,
            actor.object_id,
            result.model_copy(update={"items": outcomes}),
            input_fingerprint=fingerprint,
        )

    async def list_policies(self, actor: Actor) -> list[ApprovalPolicy]:
        return await self._repository.list_approval_policies(actor.tenant_id)

    async def save_policy(
        self, actor: Actor, payload: ApprovalPolicySaveInput, policy_id: str | None = None
    ) -> ApprovalPolicy:
        current = (
            await self._repository.get_approval_policy(actor.tenant_id, policy_id)
            if policy_id
            else None
        )
        if policy_id and current is None:
            raise NotFoundError("Approval policy was not found.")
        if current and current.revision != payload.expected_revision:
            raise ConflictError("The approval policy changed; reload its current revision.")
        if payload.enabled and not payload.enable_confirmed:
            raise ValidationError(
                "Confirm the scope, grant limits, production gate, minimum age and schedule "
                "explicitly before enabling this approval policy."
            )
        policy = ApprovalPolicy(
            **payload.model_dump(exclude={"expected_revision", "enable_confirmed"}),
            id=policy_id or new_id("approvalPolicy"),
            tenant_id=actor.tenant_id,
            revision=current.revision + 1 if current else 1,
            created_at=current.created_at if current else self._clock(),
            updated_at=self._clock(),
            etag=current.etag if current else None,
            created_by=current.created_by if current else actor.object_id,
            updated_by=actor.object_id,
        )
        return await self._repository.save_approval_policy(
            policy,
            self._audit(actor, "approvalPolicy.saved", policy.id, {"revision": policy.revision}),
        )

    async def delete_policy(self, actor: Actor, policy_id: str, expected_revision: int) -> None:
        policy = await self._repository.get_approval_policy(actor.tenant_id, policy_id)
        if policy is None:
            raise NotFoundError("Approval policy was not found.")
        if policy.revision != expected_revision:
            raise ConflictError("The approval policy changed; reload it.")
        await self._repository.delete_approval_policy(
            policy, self._audit(actor, "approvalPolicy.deleted", policy.id)
        )

    @staticmethod
    def _audit(
        actor: Actor, action: str, resource_id: str, details: dict[str, object] | None = None
    ) -> AuditEvent:
        return AuditEvent(
            id=new_id("audit"),
            tenant_id=actor.tenant_id,
            actor_object_id=actor.object_id,
            action=action,
            resource_type="approvalPolicy",
            resource_id=resource_id,
            details=details or {},
        )

    async def evaluate(
        self,
        actor: Actor,
        request_id: str,
        policies: list[ApprovalPolicy],
        *,
        persist: bool = True,
        preview_disabled: bool = False,
        ignore_mutation_lock: bool = False,
    ) -> list[ApprovalPolicyEvaluation]:
        now = self._clock()
        evaluations: list[ApprovalPolicyEvaluation] = []
        eligible: list[ApprovalPolicy] = []
        for policy in policies:
            preview = await self._entitlements.preview_access_request(
                actor, request_id, approval_for(policy), automation=True
            )
            scope = policy.scope
            matched = (
                (policy.enabled or preview_disabled)
                and preview.current_environment in scope.environments
                and preview.request.resource.kind in scope.resource_kinds
                and (not scope.resource_ids or preview.request.resource.id in scope.resource_ids)
                and (
                    not scope.cost_center_ids
                    or preview.effective_cost_center_id in scope.cost_center_ids
                )
            )
            outcome = "notMatched"
            explanation = "The policy is disabled or its scope does not match."
            blockers = [
                value
                for value in preview.blockers
                if not (ignore_mutation_lock and value.code == "mutationLocked")
            ]
            ready_at = preview.request.created_at + timedelta(
                seconds=policy.minimum_request_age_seconds
            )
            if matched:
                if preview.production is None or preview.current_environment is None:
                    blockers.append(
                        ApprovalBlocker(
                            code="unclassified",
                            message="Unclassified environments require manual review.",
                        )
                    )
                if preview.production and not policy.production_opt_in:
                    blockers.append(
                        ApprovalBlocker(
                            code="productionOptIn",
                            message="This policy does not explicitly allow production.",
                        )
                    )
                if blockers:
                    outcome, explanation = "blocked", blockers[0].message
                elif now < ready_at:
                    outcome, explanation = "waiting", "The minimum request age has not elapsed."
                elif not schedule_open(policy.schedule, now):
                    outcome, explanation = (
                        "waiting",
                        "The policy's local approval window is closed.",
                    )
                else:
                    outcome, explanation = "eligible", "All current deterministic conditions match."
                    eligible.append(policy)
            evaluation = ApprovalPolicyEvaluation(
                id=notification_record_id(
                    "approvalEvaluation", actor.tenant_id, request_id, policy.id
                ),
                tenant_id=actor.tenant_id,
                request_id=request_id,
                context=preview.revision,
                policy_id=policy.id,
                policy_revision=policy.revision,
                outcome=outcome,
                evaluated_at=now,
                next_eligible_at=ready_at if now < ready_at else None,
                explanation=explanation,
                blockers=blockers if matched else [],
            )
            evaluations.append(evaluation)
        # Equal outcomes mean the same actual grant template, not a coincidental policy order.
        signatures = {
            json.dumps(approval_for(policy).model_dump(mode="json"), sort_keys=True)
            for policy in eligible
        }
        if len(signatures) > 1:
            evaluations = [
                value.model_copy(
                    update={
                        "outcome": "ambiguous",
                        "explanation": (
                            "Matching policies configure incompatible grants; "
                            "manual review is required."
                        ),
                        "blockers": [
                            ApprovalBlocker(
                                code="ambiguousPolicies",
                                message="Matching policies disagree on the approval outcome.",
                            )
                        ],
                    }
                )
                if value.outcome == "eligible"
                else value
                for value in evaluations
            ]
        if persist:
            for value in evaluations:
                await self._repository.save_policy_evaluation(value)
        return evaluations

    async def dry_run(
        self, actor: Actor, payload: ApprovalPolicyDryRun
    ) -> list[ApprovalPolicyEvaluation]:
        policies = await self.list_policies(actor)
        if payload.policy:
            policies = [
                ApprovalPolicy(
                    **payload.policy.model_dump(include=set(ApprovalPolicyInput.model_fields)),
                    id="draft",
                    tenant_id=actor.tenant_id,
                )
            ]
        results: list[ApprovalPolicyEvaluation] = []
        for request_id in dict.fromkeys(payload.request_ids):
            results.extend(
                await self.evaluate(
                    actor,
                    request_id,
                    policies,
                    persist=False,
                    preview_disabled=bool(payload.policy),
                )
            )
        return results

    async def evaluator_state(self, actor: Actor) -> ApprovalEvaluatorState:
        return await self._repository.get_evaluator_state(
            actor.tenant_id
        ) or ApprovalEvaluatorState(
            id=notification_record_id("approvalEvaluatorState", actor.tenant_id),
            tenant_id=actor.tenant_id,
        )

    async def pause(self, actor: Actor, paused: bool) -> ApprovalEvaluatorState:
        if not paused and not self.hosting_verified:
            raise ConflictError("The approval worker host is not verified as always on.")
        return await self._repository.update_evaluator_state(
            actor.tenant_id, lambda value: value.model_copy(update={"paused": paused})
        )

    async def run_once(self, tenant_id: str, *, limit: int = 100) -> ApprovalEvaluatorState:
        actor = Actor(tenant_id=tenant_id, object_id=POLICY_SYSTEM_ACTOR)
        state = await self.evaluator_state(actor)
        if state.paused or not self.hosting_verified:
            return state
        lease = await self._repository.claim_evaluator_lease(
            tenant_id, self._owner, now=self._clock(), lease_seconds=120
        )
        if lease is None:
            return state
        try:
            pending = sorted(
                await self._entitlements.list_access_requests(actor, state="pending"),
                key=lambda value: value.id,
            )
            later = [value for value in pending if value.id > (state.checkpoint or "")]
            earlier = [value for value in pending if value.id <= (state.checkpoint or "")]
            work = (later + earlier)[: max(1, min(limit, 100))]
            await self._repository.update_evaluator_state(
                tenant_id,
                lambda value: value.model_copy(
                    update={
                        "last_started_at": self._clock(),
                        "pending_count": len(pending),
                        "last_error": None,
                    }
                ),
            )
            for request in work:
                state = await self.evaluator_state(actor)
                if state.paused or not await self._repository.check_evaluator_lease(
                    lease, now=self._clock()
                ):
                    break
                policies = await self.list_policies(actor)
                evaluations = await self.evaluate(actor, request.id, policies)
                eligible = sorted(
                    [value for value in evaluations if value.outcome == "eligible"],
                    key=lambda value: value.policy_id,
                )
                if eligible:
                    chosen = eligible[0]
                    policy = next(value for value in policies if value.id == chosen.policy_id)

                    async def guard(
                        policy: ApprovalPolicy = policy, request_id: str = request.id
                    ) -> ApprovalPolicy | None:
                        current_state = await self.evaluator_state(actor)
                        if current_state.paused or not await self._repository.check_evaluator_lease(
                            lease, now=self._clock()
                        ):
                            raise ConflictError(
                                "The approval evaluator is paused or lost its lease."
                            )
                        current_policies = await self.list_policies(actor)
                        current = next(
                            (value for value in current_policies if value.id == policy.id), None
                        )
                        if (
                            current is None
                            or current.revision != policy.revision
                            or not current.enabled
                        ):
                            raise ConflictError("The approval policy changed or was disabled.")
                        checked = await self.evaluate(
                            actor,
                            request_id,
                            current_policies,
                            persist=False,
                            ignore_mutation_lock=True,
                        )
                        if not any(
                            value.policy_id == policy.id and value.outcome == "eligible"
                            for value in checked
                        ):
                            raise ConflictError(
                                "Current policy conditions no longer permit approval."
                            )
                        if not await self._repository.check_evaluator_lease(
                            lease, now=self._clock()
                        ):
                            raise ConflictError("The approval evaluator lost its lease.")
                        if not schedule_open(current.schedule, self._clock()):
                            raise ConflictError(
                                "The policy's approval window closed during evaluation."
                            )
                        return current

                    try:
                        await self._entitlements.approve_access_request(
                            actor,
                            request.id,
                            approval_for(policy),
                            expected_context=chosen.context,
                            provenance=ApprovalDecisionProvenance(
                                origin="policy",
                                actor_object_id=POLICY_SYSTEM_ACTOR,
                                policy_id=policy.id,
                                policy_revision=policy.revision,
                                context_revision=chosen.context.context_revision,
                                matched_facts=[
                                    f"environment:{request.requested_environment}",
                                    f"minimumAgeSeconds:{policy.minimum_request_age_seconds}",
                                    f"timeZone:{policy.schedule.time_zone}",
                                ],
                            ),
                            decision_guard=guard,
                        )
                    except DomainError:
                        # Conflicts and insufficient fresh evidence keep this request pending.
                        logger.info("approval_policy_request_deferred", request_id=request.id)

                def advance(
                    value: ApprovalEvaluatorState, request_id: str = request.id
                ) -> ApprovalEvaluatorState:
                    return value.model_copy(update={"checkpoint": request_id})

                await self._repository.update_evaluator_state(
                    tenant_id,
                    advance,
                )
            remaining = await self._entitlements.list_access_requests(actor, state="pending")
            return await self._repository.update_evaluator_state(
                tenant_id,
                lambda value: value.model_copy(
                    update={
                        "last_succeeded_at": self._clock(),
                        "last_error": None,
                        "pending_count": len(remaining),
                    }
                ),
            )
        except Exception:
            logger.exception("approval_evaluator_failed", tenant_id=tenant_id)
            return await self._repository.update_evaluator_state(
                tenant_id,
                lambda value: value.model_copy(
                    update={
                        "last_error": (
                            "Approval evaluation failed; inspect worker diagnostics. "
                            "Requests remain pending."
                        )
                    }
                ),
            )
        finally:
            await self._repository.release_evaluator_lease(lease)

    async def run_forever(self, tenant_id: str, *, interval_seconds: int = 30) -> None:
        while True:
            try:
                await self.run_once(tenant_id)
            except Exception:
                logger.exception("approval_worker_tick_failed", tenant_id=tenant_id)
            await asyncio.sleep(max(5, interval_seconds))
