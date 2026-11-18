from pydantic import ValidationError as SchemaValidationError

from mosaic_api.domain import (
    AuditEvent,
    RequestFormConfiguration,
    RequestFormHead,
    RequestFormPublishInput,
    RequestFormRevision,
    new_id,
    utc_now,
)
from mosaic_api.errors import ConflictError, NotFoundError, ValidationError
from mosaic_api.repositories.access_request_writes import (
    form_revision_id,
    initial_form_head,
)
from mosaic_api.repositories.base import EntitlementRepository
from mosaic_api.request_forms import form_hash
from mosaic_api.services.directory import Actor


class RequestFormsService:
    def __init__(self, repository: EntitlementRepository) -> None:
        self._repository = repository

    async def current(
        self,
        tenant_id: str,
    ) -> tuple[RequestFormHead, RequestFormConfiguration]:
        try:
            head = await self._repository.get_request_form_head(tenant_id)
            if head is None or head.revision == 0:
                return head or initial_form_head(tenant_id), RequestFormConfiguration()
            revision = await self._repository.get_request_form_revision(tenant_id, head.revision)
        except SchemaValidationError:
            raise ConflictError(
                "The active request form cannot be read. Contact an administrator.",
                details={"reason": "requestFormUnavailable"},
            ) from None
        if revision is None or revision.schema_hash != form_hash(revision.definition):
            raise ConflictError(
                "The active request form is unavailable. Retry or contact an administrator.",
                details={"reason": "requestFormUnavailable"},
            )
        return head, RequestFormConfiguration(
            revision=revision.revision, definition=revision.definition
        )

    async def revision(self, actor: Actor, revision: int) -> RequestFormRevision:
        record = await self._repository.get_request_form_revision(actor.tenant_id, revision)
        if record is None:
            raise NotFoundError("The request form revision was not found.")
        return record

    async def publish(
        self,
        actor: Actor,
        payload: RequestFormPublishInput,
    ) -> RequestFormConfiguration:
        head, current = await self.current(actor.tenant_id)
        if head.revision != payload.expected_revision:
            raise ConflictError(
                "The request form changed. Reload before publishing.",
                details={"reason": "requestFormRevisionChanged"},
            )
        if (
            payload.definition.enabled
            and not current.definition.enabled
            and (not payload.enable_confirmed)
        ):
            raise ValidationError(
                "Confirm enabling required questions and human review for matching new requests.",
                details={"reason": "requestFormEnableConfirmation"},
            )
        previous = {item.id: item for item in current.definition.questions}
        if any(
            item.id in previous and previous[item.id].kind != item.kind
            for item in payload.definition.questions
        ):
            raise ValidationError("A changed question type needs a new question ID.")
        now = utc_now()
        record = RequestFormRevision(
            id=form_revision_id(actor.tenant_id, head.revision + 1),
            tenant_id=actor.tenant_id,
            revision=head.revision + 1,
            definition=payload.definition,
            schema_hash=form_hash(payload.definition),
            published_by=actor.object_id,
            created_at=now,
            updated_at=now,
        )
        await self._repository.publish_request_form(
            head.model_copy(update={"revision": record.revision, "updated_at": now}),
            record,
            AuditEvent(
                id=new_id("audit"),
                tenant_id=actor.tenant_id,
                actor_object_id=actor.object_id,
                action="requestForm.published",
                resource_type="requestForm",
                resource_id=record.id,
                details={
                    "revision": record.revision,
                    "schemaHash": record.schema_hash,
                    "questionCount": len(record.definition.questions),
                    "enabled": record.definition.enabled,
                },
            ),
        )
        return RequestFormConfiguration(revision=record.revision, definition=record.definition)
