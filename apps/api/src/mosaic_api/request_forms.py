"""Bounded request evidence, never authority or an approval predicate."""

import hashlib
import json

from pydantic import ValidationError as SchemaValidationError

from mosaic_api.cost_centers import exact_resource_key
from mosaic_api.domain import (
    EntitlementResource,
    RequestFormAnswer,
    RequestFormDefinition,
    RequestFormResolution,
    RequestFormSnapshot,
    RequestFormSnapshotView,
    RequestFormSubmission,
    RequestFormSummary,
    RequestParagraphQuestion,
    RequestQuestion,
    RequestQuestionView,
    RequestShortTextQuestion,
)
from mosaic_api.errors import ConflictError, ValidationError

MANUAL_FORM_REVIEW = "manualGovernanceReviewRequired"


def form_hash(definition: RequestFormDefinition) -> str:
    return hashlib.sha256(
        json.dumps(
            definition.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def question_view(question: RequestQuestion) -> RequestQuestionView:
    return RequestQuestionView(
        id=question.id,
        kind=question.kind,
        label=question.label,
        help_text=question.help_text,
        required=question.required,
        max_length=question.max_length
        if isinstance(question, RequestShortTextQuestion | RequestParagraphQuestion)
        else None,
        options=question.options if question.kind == "singleChoice" else [],
    )


def resolve_form(
    definition: RequestFormDefinition,
    revision: int,
    resource: EntitlementResource,
    environment: str | None,
    cost_center_id: str,
) -> RequestFormResolution:
    questions = [
        question_view(item)
        for item in definition.questions
        if definition.enabled
        and resource.kind in {"modelApi", "poolModel", "mcpServer"}
        and (not item.scope.resource_kinds or resource.kind in item.scope.resource_kinds)
        and (
            not item.scope.resources
            or exact_resource_key(resource)
            in {exact_resource_key(target) for target in item.scope.resources}
        )
        and (
            not item.scope.environments
            or (environment or "unclassified") in item.scope.environments
        )
    ]
    context = {
        "revision": revision,
        "resource": resource.model_dump(mode="json"),
        "environment": environment,
        "costCenterId": cost_center_id,
        "questions": [item.model_dump(mode="json") for item in questions],
    }
    return RequestFormResolution(
        status="configured" if questions else "notRequired",
        revision=revision,
        context_revision=hashlib.sha256(
            json.dumps(context, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        environment=environment,
        questions=questions,
    )


def validate_submission(
    resolution: RequestFormResolution,
    submission: RequestFormSubmission | None,
    *,
    schema_hash: str,
    resource: EntitlementResource,
    environment: str | None,
    cost_center_id: str,
) -> RequestFormSnapshot | None:
    if submission is None:
        if resolution.questions:
            raise ConflictError(
                "This offering now requires a request form. Reload it and review the questions.",
                details={"reason": "requestFormRequired"},
            )
        return None
    if (
        submission.revision != resolution.revision
        or submission.context_revision != resolution.context_revision
    ):
        raise ConflictError(
            "The request form or offering context changed. Reload and review before submitting.",
            details={"reason": "requestFormChanged"},
        )
    questions = {item.id: item for item in resolution.questions}
    supplied = {item.question_id: item.value for item in submission.answers}
    errors: list[dict[str, str]] = []
    answers: list[RequestFormAnswer] = []
    for unknown in supplied.keys() - questions.keys():
        errors.append({"questionId": unknown, "message": "This question does not apply."})
    for question in resolution.questions:
        value = supplied.get(question.id)
        if isinstance(value, str) and question.kind in {"shortText", "paragraph"}:
            value = value.replace("\r\n", "\n").replace("\r", "\n").strip() or None
        message = None
        if value is None:
            if question.required:
                message = "Answer this required question."
        elif question.kind == "acknowledgment":
            if not isinstance(value, bool):
                message = "Choose an acknowledgment, not a text or number."
            elif question.required and not value:
                message = "Acknowledge this statement before submitting."
        elif not isinstance(value, str):
            message = "Enter a text answer."
        elif question.kind == "singleChoice":
            if value not in {item.id for item in question.options}:
                message = "Choose one of the recorded options."
        elif len(value) > (question.max_length or 0):
            message = f"Use at most {question.max_length} characters."
        if message:
            errors.append({"questionId": question.id, "message": message})
        elif value is not None:
            answers.append(RequestFormAnswer(question_id=question.id, value=value))
    if errors:
        raise ValidationError(
            "Check the highlighted request questions.",
            details={"reason": "invalidRequestAnswers", "fields": errors},
        )
    if not resolution.questions:
        return None
    try:
        return RequestFormSnapshot(
            revision=resolution.revision,
            schema_hash=schema_hash,
            context_revision=resolution.context_revision,
            resource=resource,
            environment=environment,
            cost_center_id=cost_center_id,
            questions=resolution.questions,
            answers=answers,
        )
    except SchemaValidationError:
        raise ValidationError(
            "The form and answers cannot be recorded within the supported bounds. "
            "Reduce the answers or ask an administrator to review this form.",
            details={"reason": "requestFormSnapshotInvalid"},
        ) from None


def form_summary(snapshot: RequestFormSnapshot | None) -> RequestFormSummary | None:
    if snapshot is None:
        return None
    return RequestFormSummary(
        revision=snapshot.revision,
        question_count=len(snapshot.questions),
        answer_count=len(snapshot.answers),
    )


def snapshot_view(snapshot: RequestFormSnapshot | None) -> RequestFormSnapshotView | None:
    if snapshot is None:
        return None
    return RequestFormSnapshotView(
        format_version=snapshot.format_version,
        revision=snapshot.revision,
        questions=[item.model_copy(deep=True) for item in snapshot.questions],
        answers=[item.model_copy(deep=True) for item in snapshot.answers],
    )
