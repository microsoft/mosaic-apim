"""Approval data validation, without model calls, mutation, or gateway writes."""

from datetime import UTC, datetime, timedelta

import pytest
from mosaic_api.approvals import (
    MAX_BULK_APPROVALS,
    POLICY_SYSTEM_ACTOR,
    AiApprovalRecommendation,
    AiApprovalSettingsInput,
    AiRecommendationOutput,
    ApprovalDecisionProvenance,
    ApprovalPolicy,
    BulkApprovalInput,
    BulkApprovalItemResult,
    BulkApprovalPreviewInput,
)
from mosaic_api.domain import AccessRequestApproval
from mosaic_api.scheduling import WeeklySchedule, WeeklyTimeWindow
from pydantic import ValidationError

NOW = datetime(2026, 10, 16, 16, 0, tzinfo=UTC)


def _policy(**overrides: object) -> ApprovalPolicy:
    return ApprovalPolicy.model_validate(
        {
            "id": "policy-test",
            "tenantId": "tenant-test",
            "name": "Development waiting room",
            "scope": {"environments": ["development"], "resourceKinds": ["modelApi"]},
            "schedule": {
                "windows": [
                    {"daysOfWeek": [0, 1, 2, 3, 4], "startTime": "09:00", "endTime": "17:00"}
                ]
            },
            "grantTemplate": {
                "enforcement": {
                    "requests": {
                        "counterKeyExpression": "subject",
                        "callQuota": 100,
                        "callQuotaPeriod": "Monthly",
                    }
                }
            },
            **overrides,
        }
    )


def test_policies_are_disabled_and_production_is_not_implicitly_enabled() -> None:
    policy = _policy()
    assert policy.enabled is False
    assert policy.production_opt_in is False
    assert policy.schedule.time_zone == "UTC"
    assert policy.minimum_request_age_seconds == 3600
    assert policy.revision == 1
    assert "etag" not in policy.model_dump()
    assert policy.model_dump()["productionOptIn"] is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"minimumRequestAgeSeconds": -1},
        {"minimumRequestAgeSeconds": True},
        {"scope": {"environments": [], "resourceKinds": ["modelApi"]}},
        {"scope": {"environments": ["unclassified"], "resourceKinds": ["modelApi"]}},
        {"grantTemplate": {"enforcement": None}},
        {"grantTemplate": {"enforcement": {}}},
        {"revision": 0},
    ],
)
def test_invalid_policy_contracts_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _policy(**overrides)


def test_mcp_policies_cannot_accidentally_receive_model_token_limits() -> None:
    with pytest.raises(ValidationError, match="MCP policies"):
        _policy(
            scope={"environments": ["test"], "resourceKinds": ["mcpServer"]},
            grantTemplate={
                "enforcement": {
                    "tokens": {"counterKeyExpression": "subject", "tokensPerMinute": 500}
                }
            },
        )
    assert (
        _policy(
            scope={"environments": ["test"], "resourceKinds": ["mcpServer"]}
        ).grant_template.enforcement.requests
        is not None
    )


def test_schedule_supports_overnight_windows_and_explicit_dst_zones() -> None:
    schedule = WeeklySchedule(
        time_zone="America/New_York",
        windows=[WeeklyTimeWindow(days_of_week=[4, 0, 4], start_time="22:00", end_time="02:00")],
    )
    assert schedule.windows[0].days_of_week == [0, 4]
    assert schedule.windows[0].crosses_midnight is True


@pytest.mark.parametrize(
    "window",
    [
        {"daysOfWeek": [], "startTime": "09:00", "endTime": "17:00"},
        {"daysOfWeek": [7], "startTime": "09:00", "endTime": "17:00"},
        {"daysOfWeek": [True], "startTime": "09:00", "endTime": "17:00"},
        {"daysOfWeek": [0], "startTime": "24:00", "endTime": "17:00"},
        {"daysOfWeek": [0], "startTime": "9:00", "endTime": "17:00"},
        {"daysOfWeek": [0], "startTime": "09:00", "endTime": "09:00"},
    ],
)
def test_invalid_local_windows_are_rejected(window: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        WeeklyTimeWindow.model_validate(window)


def test_time_zone_must_be_an_iana_zone() -> None:
    with pytest.raises(ValidationError, match="IANA"):
        WeeklySchedule.model_validate({"timeZone": "not/a-zone", "windows": []})


@pytest.mark.parametrize("count", [0, MAX_BULK_APPROVALS + 1])
def test_both_bulk_inputs_are_bounded(count: int) -> None:
    with pytest.raises(ValidationError):
        BulkApprovalPreviewInput(request_ids=[f"r-{index}" for index in range(count)])
    with pytest.raises(ValidationError):
        BulkApprovalInput.model_validate(
            {
                "idempotencyKey": "batch-key",
                "items": [{"requestId": f"r-{index}"} for index in range(count)],
            }
        )


def test_bulk_ids_are_explicit_unique_and_preserve_per_item_approval_inputs() -> None:
    with pytest.raises(ValidationError, match="only once"):
        BulkApprovalPreviewInput(request_ids=["request-1", "request-1"])
    with pytest.raises(ValidationError, match="only once"):
        BulkApprovalInput.model_validate(
            {"idempotencyKey": "batch", "items": [{"requestId": "r"}, {"requestId": "r"}]}
        )
    batch = BulkApprovalInput.model_validate(
        {
            "idempotencyKey": "batch",
            "items": [
                {"requestId": "r1"},
                {
                    "requestId": "r2",
                    "approval": {"costCenterId": "finance", "confirmedEnvironment": "test"},
                },
            ],
        }
    )
    assert batch.items[0].approval == AccessRequestApproval()
    assert batch.items[1].approval.cost_center_id == "finance"
    assert batch.items[1].approval.confirmed_environment == "test"


def test_successful_bulk_results_name_winning_grant_and_require_separate_apply() -> None:
    for status in ("approved", "alreadyApproved"):
        with pytest.raises(ValidationError, match="winning grant"):
            BulkApprovalItemResult.model_validate({"requestId": "r", "status": status})
        result = BulkApprovalItemResult.model_validate(
            {"requestId": "r", "status": status, "entitlementId": "grant"}
        )
        assert result.requires_plan_apply is True
    assert BulkApprovalItemResult(request_id="r", status="conflict").requires_plan_apply is False


def test_policy_decisions_cannot_impersonate_the_rule_author() -> None:
    fields = {
        "origin": "policy",
        "actorObjectId": "author",
        "policyId": "p",
        "policyRevision": 2,
        "contextRevision": "context-2",
    }
    with pytest.raises(ValidationError, match="system attribution"):
        ApprovalDecisionProvenance.model_validate(fields)
    provenance = ApprovalDecisionProvenance.model_validate(
        {**fields, "actorObjectId": POLICY_SYSTEM_ACTOR}
    )
    assert provenance.actor_object_id == POLICY_SYSTEM_ACTOR


def test_ai_settings_are_opt_in_and_require_a_dedicated_governed_caller() -> None:
    assert AiApprovalSettingsInput().enabled is False
    with pytest.raises(ValidationError, match="dedicated caller"):
        AiApprovalSettingsInput(enabled=True)
    with pytest.raises(ValidationError, match="governed model"):
        AiApprovalSettingsInput.model_validate({"resource": {"kind": "mcpServer", "id": "mcp"}})
    configured = AiApprovalSettingsInput.model_validate(
        {
            "enabled": True,
            "resource": {"kind": "modelApi", "id": "model"},
            "callerPrincipalId": "mosaic",
            "entitlementId": "grant",
            "costCenterId": "cc",
        }
    )
    assert configured.max_batch_size == 10
    with pytest.raises(ValidationError):
        AiApprovalSettingsInput(concurrency=100)


def test_ai_output_is_strict_advisory_and_requires_evidence() -> None:
    response = {
        "suggestedOutcome": "recommendApprove",
        "reasons": [{"explanation": "A current development scope", "evidenceIds": ["environment"]}],
    }
    assert AiRecommendationOutput.model_validate(response).suggested_outcome == "recommendApprove"
    for extra in ({"approve": True}, {"tools": ["approve"]}, {"recipients": ["someone"]}):
        with pytest.raises(ValidationError):
            AiRecommendationOutput.model_validate({**response, **extra})
    with pytest.raises(ValidationError):
        AiRecommendationOutput.model_validate(
            {**response, "reasons": [{"explanation": "Trust me", "evidenceIds": []}]}
        )
    with pytest.raises(ValidationError):
        AiRecommendationOutput.model_validate({**response, "suggestedOutcome": "approved"})


def test_recommendations_pin_versions_and_require_aware_nonempty_lifetimes() -> None:
    fields = {
        "id": "recommendation",
        "tenantId": "tenant",
        "requestId": "request",
        "context": {"contextRevision": "request-context-2"},
        "configRevision": 1,
        "modelResource": {"kind": "modelApi", "id": "model"},
        "generatedAt": NOW,
        "expiresAt": NOW + timedelta(minutes=15),
        "recommendation": {
            "suggestedOutcome": "needsReview",
            "reasons": [{"explanation": "Missing evidence", "evidenceIds": ["eligibility"]}],
        },
    }
    assert AiApprovalRecommendation.model_validate(fields).config_revision == 1
    with pytest.raises(ValidationError, match="expire after"):
        AiApprovalRecommendation.model_validate({**fields, "expiresAt": NOW})
    with pytest.raises(ValidationError):
        AiApprovalRecommendation.model_validate({**fields, "generatedAt": NOW.replace(tzinfo=None)})
