import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from mosaic_api.approvals import POLICY_SYSTEM_ACTOR, ApprovalDecisionProvenance
from mosaic_api.domain import (
    AccessRequestApproval,
    AccessRequestCreate,
    RequestFormDefinition,
    RequestFormPublishInput,
    RequestFormSubmission,
    general_cost_center_id,
)
from mosaic_api.errors import ConflictError, ValidationError
from mosaic_api.request_forms import (
    form_hash,
    form_summary,
    resolve_form,
    snapshot_view,
    validate_submission,
)
from mosaic_api.services.approval_ai import ApprovalAiService
from mosaic_api.services.directory import Actor
from pydantic import ValidationError as SchemaError
from test_approval_workflows import ADMIN, RESOURCE, TENANT, USER, Harness


def definition() -> RequestFormDefinition:
    return RequestFormDefinition.model_validate(
        {
            "enabled": True,
            "questions": [
                {
                    "id": "purpose",
                    "kind": "paragraph",
                    "label": "Intended use",
                    "required": True,
                    "scope": {"environments": ["development"]},
                },
                {
                    "id": "data",
                    "kind": "singleChoice",
                    "label": "Data class",
                    "required": True,
                    "options": [
                        {"id": "synthetic", "label": "Public / synthetic"},
                        {"id": "unsure", "label": "Unsure"},
                    ],
                },
                {"id": "charge", "kind": "shortText", "label": "Charge reference"},
                {
                    "id": "ack",
                    "kind": "acknowledgment",
                    "label": "I follow the data policy.",
                    "required": True,
                },
            ],
        }
    )


def submission(config: RequestFormDefinition, revision: int = 1) -> RequestFormSubmission:
    resolved = resolve_form(
        config, revision, RESOURCE, "development", general_cost_center_id(TENANT)
    )
    return RequestFormSubmission.model_validate(
        {
            "revision": revision,
            "contextRevision": resolved.context_revision,
            "answers": [
                {
                    "questionId": "purpose",
                    "value": "  A fictional assistant.\r\nSynthetic notes.  ",
                },
                {"questionId": "data", "value": "synthetic"},
                {"questionId": "charge", "value": "LUNA-042"},
                {"questionId": "ack", "value": True},
            ],
        }
    )


def validated(config: RequestFormDefinition, body: RequestFormSubmission | None):
    return validate_submission(
        resolve_form(config, 1, RESOURCE, "development", general_cost_center_id(TENANT)),
        body,
        schema_hash=form_hash(config),
        resource=RESOURCE,
        environment="development",
        cost_center_id=general_cost_center_id(TENANT),
    )


@pytest.mark.parametrize(
    ("question", "value"),
    [
        ("purpose", ""),
        ("purpose", "x" * 2001),
        ("purpose", True),
        ("charge", "x" * 201),
        ("data", "injected"),
        ("data", False),
        ("ack", False),
        ("ack", "true"),
    ],
)
def test_invalid_answers_are_actionable_without_echo(question: str, value: str | bool) -> None:
    config = definition()
    body = submission(config)
    body.answers = [
        item.model_copy(update={"value": value}) if item.question_id == question else item
        for item in body.answers
    ]
    with pytest.raises(ValidationError) as failed:
        validated(config, body)
    assert failed.value.details["reason"] == "invalidRequestAnswers"
    assert failed.value.details["fields"][0]["questionId"] == question
    assert "A fictional assistant" not in str(failed.value.details)


def test_strict_envelope_and_utf8_bounds() -> None:
    with pytest.raises(SchemaError):
        RequestFormSubmission.model_validate(
            {
                "revision": 1,
                "contextRevision": "a" * 64,
                "answers": [{"questionId": "ack", "value": 1}],
            }
        )
    with pytest.raises(SchemaError, match="only once"):
        RequestFormSubmission.model_validate(
            {
                "revision": 1,
                "contextRevision": "a" * 64,
                "answers": [{"questionId": "a", "value": ""}] * 2,
            }
        )
    with pytest.raises(SchemaError, match="16 KiB"):
        RequestFormSubmission.model_validate(
            {
                "revision": 1,
                "contextRevision": "a" * 64,
                "answers": [{"questionId": str(i), "value": "\U0001f600" * 2000} for i in range(3)],
            }
        )
    payload = definition().model_dump()
    payload["questions"] = payload["questions"] * 6
    with pytest.raises(SchemaError):
        RequestFormDefinition.model_validate(payload)

@pytest.mark.parametrize(
    ("kind", "scope"),
    [("modelApi", ""), ("modelApi", "invented"), ("mcpServer", "invented"),
     ("poolModel", None), ("poolModel", " ")],
)
def test_scope_spelling_cannot_bypass_exact_offering_questions(kind: str, scope: str | None):
    with pytest.raises(SchemaError):
        AccessRequestCreate.model_validate({
            "resource": {"kind": kind, "id": "same-offering", "scopeId": scope},
        })


def test_missing_unknown_and_optional_answers_never_become_defaults() -> None:
    config = definition()
    with pytest.raises(ConflictError) as required:
        validated(config, None)
    assert required.value.details["reason"] == "requestFormRequired"
    body = submission(config)
    body.answers = [item for item in body.answers if item.question_id != "charge"]
    snapshot = validated(config, body)
    assert snapshot is not None
    assert snapshot.answers[0].value == "A fictional assistant.\nSynthetic notes."
    assert len(snapshot.answers) == 3
    body = body.model_copy(
        update={
            "answers": [
                *body.answers,
                body.answers[0].model_copy(update={"question_id": "unknown"}),
            ]
        }
    )
    with pytest.raises(ValidationError) as unknown:
        validated(config, body)
    assert unknown.value.details["fields"][0]["questionId"] == "unknown"


def test_applicability_intersects_exact_pool_scope_and_environment() -> None:
    payload = definition().model_dump()
    payload["questions"] = [payload["questions"][0]]
    payload["questions"][0]["scope"] = {
        "resourceKinds": ["poolModel"],
        "resources": [{"kind": "poolModel", "id": "same-model", "scopeId": "pool-a"}],
        "environments": ["development"],
    }
    config = RequestFormDefinition.model_validate(payload)
    from mosaic_api.domain import EntitlementResource

    for pool, environment, matched in [
        ("pool-a", "development", True),
        ("pool-b", "development", False),
        ("pool-a", "production", False),
        ("pool-a", None, False),
    ]:
        result = resolve_form(
            config,
            1,
            EntitlementResource(kind="poolModel", id="same-model", scope_id=pool),
            environment,
            "center",
        )
        assert bool(result.questions) is matched
        assert result.model_dump()["environment"] == environment
        assert all("scope" not in item.model_dump() for item in result.questions)


async def test_history_is_immutable_and_does_not_depend_on_active_schema() -> None:
    h = Harness()
    await h.seed()
    config = definition()
    await h.entitlements.publish_request_form(
        ADMIN,
        RequestFormPublishInput(
            expected_revision=0,
            definition=config,
            enable_confirmed=True,
        ),
    )
    created = await h.entitlements.create_access_request(
        Actor(tenant_id=TENANT, object_id=USER),
        AccessRequestCreate(
            resource=RESOURCE,
            justification="Existing optional note.",
            request_form=submission(config),
        ),
    )
    before = await h.entitlements.preview_access_request(ADMIN, created.id)
    changed = deepcopy(config)
    changed.questions[0].label = "Changed wording"
    await h.entitlements.publish_request_form(
        ADMIN,
        RequestFormPublishInput(
            expected_revision=1,
            definition=changed,
        ),
    )
    after = await h.entitlements.preview_access_request(ADMIN, created.id)
    assert after.revision.context_revision == before.revision.context_revision
    detail = await h.entitlements.access_request_detail(ADMIN, created.id)
    assert detail.recorded_form is not None
    assert detail.recorded_form.questions[0].label == "Intended use"
    assert "schemaHash" not in detail.recorded_form.model_dump()
    listed = (await h.entitlements.list_access_request_items(ADMIN))[0].model_dump()
    assert "requestForm" not in listed
    assert listed["requestFormSummary"]["revision"] == 1
    assert created.justification == "Existing optional note."
    with pytest.raises(ConflictError):
        await h.entitlements.create_access_request(
            Actor(tenant_id=TENANT, object_id="another"),
            AccessRequestCreate(resource=RESOURCE, request_form=submission(config)),
        )
    assert len(h.repository.access_requests) == 1
    stored = await h.repository.get_access_request(TENANT, created.id)
    assert stored is not None and stored.request_form is not None
    stored.request_form.questions[0].label = "Tampered"
    with pytest.raises(ConflictError, match="cannot be edited"):
        await h.repository.save_access_request(
            stored, next(iter(h.repository.audit_events.values()))
        )
    untouched = await h.repository.get_access_request(TENANT, created.id)
    assert untouched is not None and untouched.request_form is not None
    assert untouched.request_form.questions[0].label == "Intended use"
    assert form_summary(untouched.request_form).answer_count == 4
    assert snapshot_view(untouched.request_form).answers[-1].value is True


async def test_form_guard_race_requires_new_answers_without_a_partial_write() -> None:
    h = Harness()
    await h.seed()
    original = h.repository.create_access_request

    async def publish_during_write(record, audit, *, form_guard=None):
        await h.entitlements.publish_request_form(
            ADMIN,
            RequestFormPublishInput(
                expected_revision=0,
                definition=definition(),
                enable_confirmed=True,
            ),
        )
        return await original(record, audit, form_guard=form_guard)

    h.repository.create_access_request = AsyncMock(side_effect=publish_during_write)
    with pytest.raises(ConflictError) as raced:
        await h.entitlements.create_access_request(
            Actor(tenant_id=TENANT, object_id=USER), AccessRequestCreate(resource=RESOURCE)
        )
    assert raced.value.details["reason"] == "requestFormRequired"
    assert not h.repository.access_requests
    assert not h.repository.notification_events
    assert not any(e.action == "accessRequest.created" for e in h.repository.audit_events.values())


@pytest.mark.parametrize("configured", [False, True])
async def test_concurrent_distinct_admissions_retry_unchanged_form_guards(configured: bool) -> None:
    h = Harness()
    await h.seed()
    if configured:
        await h.entitlements.publish_request_form(
            ADMIN, RequestFormPublishInput(
                expected_revision=0, definition=definition(), enable_confirmed=True,
            ),
        )
    current = h.entitlements.request_forms.current
    barriers = [asyncio.Barrier(4), asyncio.Barrier(3), asyncio.Barrier(2)]
    reads = 0

    async def synchronized_current(tenant: str):
        nonlocal reads
        result = await current(tenant)
        position = reads
        reads += 1
        if position < 4:
            await barriers[0].wait()
        elif position < 7:
            await barriers[1].wait()
        elif position < 9:
            await barriers[2].wait()
        return result

    h.entitlements.request_forms.current = AsyncMock(side_effect=synchronized_current)
    results = await asyncio.gather(*(
        h.entitlements.create_access_request(
            Actor(tenant_id=TENANT, object_id=f"fictional-concurrent-{index}"),
            AccessRequestCreate(
                resource=RESOURCE,
                request_form=submission(definition()) if configured else None,
            ),
        )
        for index in range(4)
    ), return_exceptions=True)
    assert not [item for item in results if isinstance(item, BaseException)]
    assert len(h.repository.access_requests) == 4
    assert len(h.repository.notification_events) == 4
    assert len([
        event for event in h.repository.audit_events.values()
        if event.action == "accessRequest.created"
    ]) == 4


async def test_admission_contention_stays_bounded_without_claiming_the_schema_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = Harness()
    await h.seed()
    write = AsyncMock(side_effect=ConflictError(
        "Conflicting form-head ETag.", details={"reason": "requestFormWriteConflict"},
    ))
    h.repository.create_access_request = write
    wait = AsyncMock()
    monkeypatch.setattr("mosaic_api.services.entitlements.sleep", wait)
    with pytest.raises(ConflictError, match="Nothing was saved") as busy:
        await h.request()
    assert busy.value.details["reason"] == "requestFormWriteConflict"
    assert 4 <= write.await_count <= 16
    assert wait.await_count == write.await_count - 1
    assert all(0 <= call.args[0] <= 0.4 for call in wait.await_args_list)
    assert not h.repository.access_requests
    assert not h.repository.notification_events
    assert not h.repository.audit_events


async def test_form_manual_review_preserves_legacy_policies_and_ai_allowlist() -> None:
    h = Harness()
    await h.seed()
    legacy = await h.request()
    policy = await h.policy()
    await h.entitlements.publish_request_form(
        ADMIN,
        RequestFormPublishInput(
            expected_revision=0,
            definition=definition(),
            enable_confirmed=True,
        ),
    )
    created = await h.entitlements.create_access_request(
        Actor(tenant_id=TENANT, object_id="new-user"),
        AccessRequestCreate(resource=RESOURCE, request_form=submission(definition())),
    )
    results = await h.service.evaluate(ADMIN, created.id, [policy])
    assert results[0].outcome == "blocked"
    assert any(item.code == "manualGovernanceReviewRequired" for item in results[0].blockers)
    old = await h.service.evaluate(ADMIN, legacy.id, [policy])
    assert old[0].outcome == "eligible"
    evidence = ApprovalAiService._evidence(
        await h.entitlements.preview_access_request(ADMIN, created.id)
    )
    assert "LUNA-042" not in str(evidence)
    assert "A fictional assistant" not in str(evidence)
    assert not h.repository.entitlements


async def test_manual_approval_preserves_answers_and_creates_only_one_grant_intent() -> None:
    h = Harness()
    await h.seed()
    await h.entitlements.publish_request_form(ADMIN, RequestFormPublishInput(
        expected_revision=0, definition=definition(), enable_confirmed=True,
    ))
    created = await h.entitlements.create_access_request(
        Actor(tenant_id=TENANT, object_id=USER),
        AccessRequestCreate(resource=RESOURCE, request_form=submission(definition())),
    )
    with pytest.raises(ConflictError) as automatic:
        await h.entitlements.approve_access_request(
            Actor(tenant_id=TENANT, object_id=POLICY_SYSTEM_ACTOR),
            created.id, AccessRequestApproval(),
            provenance=ApprovalDecisionProvenance(
                origin="policy", actor_object_id=POLICY_SYSTEM_ACTOR, policy_id="policy",
                policy_revision=1, context_revision="a" * 64,
            ),
        )
    assert automatic.value.details["reason"] == "manualGovernanceReviewRequired"
    assert not h.repository.entitlements
    gateway_state = deepcopy(h.gateways.publications)
    approved = await h.entitlements.approve_access_request(
        ADMIN, created.id, AccessRequestApproval(note="Reviewed fictional evidence."),
    )
    assert approved.request_form == created.request_form
    assert approved.granted_entitlement_id in h.repository.entitlements
    assert h.gateways.publications == gateway_state
    retried = await h.entitlements.approve_access_request(
        ADMIN, created.id, AccessRequestApproval(),
    )
    assert retried.granted_entitlement_id == approved.granted_entitlement_id
    assert len(h.repository.entitlements) == 1
