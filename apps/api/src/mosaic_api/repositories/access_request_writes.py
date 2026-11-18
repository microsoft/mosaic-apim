from mosaic_api.domain import AccessRequest, RequestFormHead, deterministic_id
from mosaic_api.errors import ConflictError


def form_head_id(tenant_id: str) -> str:
    return deterministic_id("requestFormHead", tenant_id)


def form_revision_id(tenant_id: str, revision: int) -> str:
    return deterministic_id("requestFormRevision", tenant_id, str(revision))


def initial_form_head(tenant_id: str) -> RequestFormHead:
    return RequestFormHead(id=form_head_id(tenant_id), tenant_id=tenant_id)


def require_unchanged_form(before: AccessRequest | None, after: AccessRequest) -> None:
    if before is None or before.tenant_id != after.tenant_id:
        raise ConflictError("The access request was not found or changed; reload it.")
    form = after.request_form
    if before.request_form != form or (form is not None and (
        form.resource != after.resource
        or form.cost_center_id != after.cost_center_id
        or form.environment != after.requested_environment
    )):
        raise ConflictError(
            "Submitted questions and answers cannot be edited.",
            details={"reason": "requestFormImmutable"},
        )


def require_human_form_review(request: AccessRequest, *, policy: bool) -> None:
    if policy and request.request_form is not None:
        raise ConflictError(
            "Recorded governance answers require human review; their values were not evaluated.",
            details={"reason": "manualGovernanceReviewRequired"},
        )
