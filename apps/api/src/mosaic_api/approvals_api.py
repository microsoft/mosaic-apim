"""Admin-only approval controls; portal request APIs remain self-scoped and unchanged."""

from typing import cast

from fastapi import APIRouter, Query, Request, Response

from mosaic_api.api import Admin, _actor
from mosaic_api.approvals import (
    AiApprovalBatchInput,
    AiApprovalItemResult,
    AiApprovalOffering,
    AiApprovalReadiness,
    AiApprovalRecommendation,
    AiApprovalSettings,
    AiApprovalSettingsSaveInput,
    ApprovalEvaluatorState,
    ApprovalEvaluatorUpdate,
    ApprovalPolicy,
    ApprovalPolicyDryRun,
    ApprovalPolicyEvaluation,
    ApprovalPolicySaveInput,
    BulkApprovalInput,
    BulkApprovalPreview,
    BulkApprovalPreviewInput,
    BulkApprovalResult,
)
from mosaic_api.services.approval_ai import ApprovalAiService
from mosaic_api.services.approvals import ApprovalService

approvals_router = APIRouter(prefix="/api/v1", tags=["admin"])


def _approvals(request: Request) -> ApprovalService:
    return cast(ApprovalService, request.app.state.approval_service)


def _ai(request: Request) -> ApprovalAiService:
    return cast(ApprovalAiService, request.app.state.approval_ai_service)


@approvals_router.post("/approval-batches/preview")
@approvals_router.post("/access-requests/bulk/preview")
async def preview_bulk(
    request: Request, auth: Admin, payload: BulkApprovalPreviewInput
) -> BulkApprovalPreview:
    return await _approvals(request).preview(_actor(auth), payload)


@approvals_router.post("/approval-batches/execute")
@approvals_router.post("/access-requests/bulk/approve")
async def execute_bulk(
    request: Request, auth: Admin, payload: BulkApprovalInput
) -> BulkApprovalResult:
    return await _approvals(request).execute(_actor(auth), payload)


@approvals_router.get("/approval-policies")
async def list_policies(request: Request, auth: Admin) -> list[ApprovalPolicy]:
    return await _approvals(request).list_policies(_actor(auth))


@approvals_router.post("/approval-policies", status_code=201)
async def create_policy(
    request: Request, auth: Admin, payload: ApprovalPolicySaveInput
) -> ApprovalPolicy:
    return await _approvals(request).save_policy(_actor(auth), payload)


@approvals_router.post("/approval-policies/dry-run")
async def dry_run(
    request: Request, auth: Admin, payload: ApprovalPolicyDryRun
) -> list[ApprovalPolicyEvaluation]:
    return await _approvals(request).dry_run(_actor(auth), payload)


@approvals_router.post("/approval-policies/{policy_id}/preview")
async def preview_saved_policy(
    request: Request, auth: Admin, policy_id: str, payload: BulkApprovalPreviewInput
) -> list[ApprovalPolicyEvaluation]:
    policies = await _approvals(request).list_policies(_actor(auth))
    from mosaic_api.errors import NotFoundError

    selected = next((item for item in policies if item.id == policy_id), None)
    if selected is None:
        raise NotFoundError("Approval policy was not found.")
    return [
        result
        for request_id in payload.request_ids
        for result in await _approvals(request).evaluate(
            _actor(auth), request_id, [selected], persist=False, preview_disabled=True
        )
    ]


@approvals_router.put("/approval-policies/{policy_id}")
async def save_policy(
    request: Request, auth: Admin, policy_id: str, payload: ApprovalPolicySaveInput
) -> ApprovalPolicy:
    return await _approvals(request).save_policy(_actor(auth), payload, policy_id)


@approvals_router.delete("/approval-policies/{policy_id}", status_code=204)
async def delete_policy(
    request: Request,
    auth: Admin,
    policy_id: str,
    expected_revision: int = Query(ge=1),
) -> Response:
    await _approvals(request).delete_policy(_actor(auth), policy_id, expected_revision)
    return Response(status_code=204)


@approvals_router.get("/approval-evaluator")
async def evaluator_state(request: Request, auth: Admin) -> ApprovalEvaluatorState:
    return await _approvals(request).evaluator_state(_actor(auth))


@approvals_router.put("/approval-evaluator")
async def pause_evaluator(
    request: Request, auth: Admin, payload: ApprovalEvaluatorUpdate
) -> ApprovalEvaluatorState:
    return await _approvals(request).pause(_actor(auth), payload.paused)


@approvals_router.get("/approval-ai/settings")
@approvals_router.get("/settings/approval-ai")
async def ai_settings(request: Request, auth: Admin) -> AiApprovalSettings:
    return await _ai(request).settings(_actor(auth))


@approvals_router.put("/approval-ai/settings")
@approvals_router.put("/settings/approval-ai")
async def save_ai_settings(
    request: Request, auth: Admin, payload: AiApprovalSettingsSaveInput
) -> AiApprovalSettings:
    return await _ai(request).save_settings(_actor(auth), payload)


@approvals_router.get("/approval-ai/readiness")
async def ai_readiness(request: Request, auth: Admin) -> AiApprovalReadiness:
    return await _ai(request).readiness(_actor(auth))


@approvals_router.get("/approval-ai/offerings")
async def ai_offerings(request: Request, auth: Admin) -> list[AiApprovalOffering]:
    return await _ai(request).offerings(_actor(auth))


@approvals_router.post("/approval-ai/test")
async def test_ai_model(request: Request, auth: Admin) -> AiApprovalReadiness:
    return await _ai(request).test(_actor(auth))


@approvals_router.post("/approval-ai/recommend")
async def recommend(
    request: Request, auth: Admin, payload: AiApprovalBatchInput
) -> list[AiApprovalItemResult]:
    return await _ai(request).recommend(_actor(auth), payload)


@approvals_router.get("/approval-ai/recommendations/{request_id}")
@approvals_router.get("/access-requests/{request_id}/recommendation")
async def recommendations(
    request: Request, auth: Admin, request_id: str
) -> list[AiApprovalRecommendation]:
    return await _ai(request).recommendations(_actor(auth), request_id)
