"""Approval workflows use in-process repositories and fake model transports only."""

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from azure.cosmos import exceptions
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mosaic_api.approvals import (
    AiApprovalBatchInput,
    AiApprovalSettings,
    AiApprovalSettingsSaveInput,
    ApprovalPolicyDryRun,
    ApprovalPolicySaveInput,
    BulkApprovalInput,
)
from mosaic_api.approvals_api import approvals_router
from mosaic_api.auth import LocalAuthenticator
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from mosaic_api.domain import (
    AccessRequest,
    AccessRequestApproval,
    AccessRequestCreate,
    AuditEvent,
    ConnectionOperation,
    CostCenterRef,
    DirectoryMemberPage,
    DirectoryObject,
    Entitlement,
    EntitlementResource,
    EntitlementRuntime,
    EntitlementSubject,
    Gateway,
    Group,
    GroupMembership,
    ModelAccessSettings,
    ModelApi,
    ModelConnection,
    Principal,
    general_cost_center_id,
    new_id,
)
from mosaic_api.errors import (
    ConflictError,
    DirectoryDisabledError,
    DirectoryError,
    DomainError,
    ValidationError,
    domain_error_handler,
)
from mosaic_api.integrations.graph import FakeDirectoryLookup
from mosaic_api.repositories import (
    InMemoryDirectoryRepository,
    InMemoryEntitlementRepository,
    InMemoryGatewayRepository,
    InMemoryModelEndpointRepository,
)
from mosaic_api.repositories.cosmos_approvals import CosmosApprovalRepository
from mosaic_api.repositories.cosmos_entitlements import CosmosEntitlementRepository
from mosaic_api.repositories.memory_approvals import InMemoryApprovalRepository
from mosaic_api.scheduling import WeeklySchedule
from mosaic_api.services.approval_ai import ApprovalAiService, HttpGovernedApprovalModel
from mosaic_api.services.approval_events import GrantNotificationRecipientResolver
from mosaic_api.services.approvals import ApprovalService, schedule_open
from mosaic_api.services.directory import Actor
from mosaic_api.services.entitlements import EntitlementService
from mosaic_api.services.notification_events import access_request_event

TENANT = "00000000-0000-0000-0000-000000000001"
USER = "00000000-0000-0000-0000-000000000002"
CALLER = "00000000-0000-0000-0000-000000000003"
ADMIN = Actor(tenant_id=TENANT, object_id="admin")
NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
RESOURCE = EntitlementResource(kind="modelApi", id="model")
LIMITS = {"tokens": {"counterKeyExpression": "@(context.Subscription.Id)", "tokensPerMinute": 1000}}


def audit() -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id=TENANT,
        action="test",
        resource_type="test",
        resource_id="test",
        actor_object_id="test",
    )


class Harness:
    def __init__(self) -> None:
        self.approvals = InMemoryApprovalRepository()
        self.directory = InMemoryDirectoryRepository()
        self.repository = InMemoryEntitlementRepository(
            approval_policies=self.approvals.policies, directory_repository=self.directory
        )
        self.gateways = InMemoryGatewayRepository()
        self.entitlements = EntitlementService(
            self.repository,
            directory_repository=self.directory,
            gateway_repository=self.gateways,
            endpoint_repository=InMemoryModelEndpointRepository(),
        )
        self.now = NOW
        self.service = ApprovalService(
            self.approvals,
            entitlements=self.entitlements,
            clock=lambda: self.now,
            hosting_verified=True,
        )

    async def seed(self, environment: str | None = "development") -> None:
        await self.gateways.save_gateway(
            Gateway(
                id="gateway",
                tenant_id=TENANT,
                name="Fictional gateway",
                azure_resource_id=(
                    f"/subscriptions/{TENANT}/resourceGroups/rg/providers/"
                    "Microsoft.ApiManagement/service/gateway"
                ),
                subscription_id=TENANT,
                resource_group="rg",
                service_name="gateway",
                environment=environment,
            ),
            audit(),
        )
        await self.gateways.save_model_api(
            ModelApi(
                id=RESOURCE.id,
                tenant_id=TENANT,
                gateway_id="gateway",
                api_name="model",
                display_name="Fictional model",
                path="model",
                imported_from_snapshot_id="snapshot",
            ),
            audit(),
        )

    async def request(self, object_id: str = USER) -> AccessRequest:
        request = await self.entitlements.create_access_request(
            Actor(tenant_id=TENANT, object_id=object_id), AccessRequestCreate(resource=RESOURCE)
        )
        self.repository.access_requests[request.id] = request.model_copy(
            update={
                "created_at": NOW - timedelta(hours=2),
                "updated_at": NOW - timedelta(hours=2),
            }
        )
        return self.repository.access_requests[request.id]

    async def policy(self, **updates: Any) -> Any:
        payload: dict[str, Any] = {
            "name": "Development requests",
            "enabled": True,
            "enableConfirmed": True,
            "scope": {"environments": ["development"], "resourceKinds": ["modelApi"]},
            "schedule": {
                "timeZone": "UTC",
                "windows": [
                    {"daysOfWeek": list(range(7)), "startTime": "00:00", "endTime": "23:59"}
                ],
            },
            "minimumRequestAgeSeconds": 3600,
            "grantTemplate": {"enforcement": LIMITS},
            **updates,
        }
        return await self.service.save_policy(
            ADMIN, ApprovalPolicySaveInput.model_validate(payload)
        )


async def test_preview_is_read_only_and_context_stable() -> None:
    h = Harness()
    await h.seed()
    request = await h.request()
    first = await h.entitlements.preview_access_request(ADMIN, request.id)
    second = await h.entitlements.preview_access_request(ADMIN, request.id)
    assert first.revision.context_revision == second.revision.context_revision
    assert first.blockers == []
    assert first.current_environment == "development" and first.production is False
    assert h.directory.principals == {}
    assert h.repository.entitlements == {}


async def test_bulk_independent_outcomes_and_retries_converge() -> None:
    h = Harness()
    await h.seed()
    request = await h.request()
    preview = await h.entitlements.preview_access_request(ADMIN, request.id)
    payload = BulkApprovalInput.model_validate(
        {
            "idempotencyKey": "batch-key",
            "items": [
                {"requestId": request.id, "expectedContext": preview.revision.model_dump()},
                {"requestId": "missing"},
            ],
        }
    )
    result = await h.service.execute(ADMIN, payload)
    assert [item.status for item in result.items] == ["approved", "conflict"]
    assert result.items[0].requires_plan_apply
    assert len(h.repository.entitlements) == 1
    retried = await h.service.execute(ADMIN, payload)
    assert retried.batch_id == result.batch_id
    assert retried.items[0].entitlement_id == result.items[0].entitlement_id
    assert (
        len(
            [
                item
                for item in h.repository.audit_events.values()
                if item.action == "accessRequest.approved"
            ]
        )
        == 1
    )
    assert (
        len(
            [
                item
                for item in h.repository.notification_events.values()
                if str(item.event_type) == "request.approved"
            ]
        )
        == 1
    )
    stored = h.repository.access_requests[request.id]
    assert stored.approval_provenance and stored.approval_provenance["origin"] == "bulk"


async def test_reused_bulk_key_with_changed_input_fails_before_mutation() -> None:
    h = Harness()
    await h.seed()
    request = await h.request()
    await h.service.execute(
        ADMIN,
        BulkApprovalInput.model_validate(
            {"idempotencyKey": "same", "items": [{"requestId": "missing"}]}
        ),
    )
    with pytest.raises(ConflictError, match="different approval inputs"):
        await h.service.execute(
            ADMIN,
            BulkApprovalInput.model_validate(
                {"idempotencyKey": "same", "items": [{"requestId": request.id}]}
            ),
        )
    assert h.repository.entitlements == {}


async def test_bulk_refuses_stale_environment_but_manual_confirmation_still_works() -> None:
    h = Harness()
    await h.seed()
    request = await h.request()
    preview = await h.entitlements.preview_access_request(ADMIN, request.id)
    gateway = h.gateways.gateways["gateway"]
    h.gateways.gateways[gateway.id] = gateway.model_copy(update={"environment": "production"})
    outcome = await h.service.execute(
        ADMIN,
        BulkApprovalInput.model_validate(
            {
                "idempotencyKey": "stale",
                "items": [
                    {"requestId": request.id, "expectedContext": preview.revision.model_dump()}
                ],
            }
        ),
    )
    assert outcome.items[0].status == "conflict"
    approved = await h.entitlements.approve_access_request(
        ADMIN, request.id, AccessRequestApproval(confirmed_environment="production")
    )
    assert approved.state == "approved"


async def test_memory_decision_etag_conflict_has_no_partial_event_or_grant() -> None:
    h = Harness()
    await h.seed()
    request = await h.request()
    await h.entitlements.decide_access_request(ADMIN, request.id, state=cast(Any, "denied"))
    original_events = dict(h.repository.notification_events)
    grant = Entitlement(
        id="grant",
        tenant_id=TENANT,
        subject=EntitlementSubject(kind="user", id="person"),
        resource=RESOURCE,
    )
    approved = request.model_copy(update={"state": "approved", "granted_entitlement_id": "grant"})
    with pytest.raises(ConflictError):
        await h.repository.approve_access_request_with_events(
            approved, grant, [audit()], [access_request_event(approved)]
        )
    assert h.repository.entitlements == {}
    assert h.repository.notification_events == original_events


async def test_policy_worker_is_paused_by_default_and_saving_never_approves() -> None:
    h = Harness()
    await h.seed()
    request = await h.request()
    await h.policy()
    await h.service.run_once(TENANT)
    assert h.repository.access_requests[request.id].state == "pending"
    await h.service.pause(ADMIN, False)
    await h.service.run_once(TENANT)
    stored = h.repository.access_requests[request.id]
    assert stored.state == "approved"
    assert stored.decided_by_object_id == "system:approval-policies"
    assert stored.approval_provenance and stored.approval_provenance["policyRevision"] == 1
    assert stored.approval_provenance["effectiveEnforcement"]["tokens"]["tokensPerMinute"] == 1000
    assert stored.approval_provenance["effectiveCostCenterId"] == general_cost_center_id(TENANT)
    assert (await h.service.evaluator_state(ADMIN)).pending_count == 0


@pytest.mark.parametrize(
    "environment,updates,expected",
    [
        (
            "production",
            {"scope": {"environments": ["production"], "resourceKinds": ["modelApi"]}},
            "blocked",
        ),
        ("development", {"minimumRequestAgeSeconds": 10000}, "waiting"),
        (
            "development",
            {
                "schedule": {
                    "timeZone": "UTC",
                    "windows": [{"daysOfWeek": [0], "startTime": "13:00", "endTime": "14:00"}],
                }
            },
            "waiting",
        ),
    ],
)
async def test_policy_production_age_and_window_gates(
    environment: str, updates: dict[str, Any], expected: str
) -> None:
    h = Harness()
    await h.seed(environment)
    request = await h.request()
    policy = await h.policy(**updates)
    [evaluation] = await h.service.evaluate(ADMIN, request.id, [policy])
    assert evaluation.outcome == expected
    await h.service.pause(ADMIN, False)
    await h.service.run_once(TENANT)
    assert h.repository.entitlements == {}


async def test_explicit_production_policy_can_approve_without_applying_gateway() -> None:
    h = Harness()
    await h.seed("production")
    request = await h.request()
    await h.policy(
        scope={"environments": ["production"], "resourceKinds": ["modelApi"]},
        productionOptIn=True,
    )
    await h.service.pause(ADMIN, False)
    await h.service.run_once(TENANT)
    assert h.repository.access_requests[request.id].state == "approved"
    assert h.gateways.publications == {}


async def test_policy_moved_environment_and_conflicting_rules_stay_pending() -> None:
    h = Harness()
    await h.seed()
    request = await h.request()
    first = await h.policy()
    second = await h.policy(
        grantTemplate={
            "enforcement": {
                "tokens": {
                    "counterKeyExpression": "@(context.Subscription.Id)",
                    "tokensPerMinute": 2000,
                }
            }
        }
    )
    results = await h.service.evaluate(ADMIN, request.id, [first, second])
    assert {item.outcome for item in results} == {"ambiguous"}
    await h.service.pause(ADMIN, False)
    await h.service.run_once(TENANT)
    assert h.repository.entitlements == {}
    h.gateways.gateways["gateway"] = h.gateways.gateways["gateway"].model_copy(
        update={"environment": "production"}
    )
    production = await h.policy(
        scope={"environments": ["production"], "resourceKinds": ["modelApi"]},
        productionOptIn=True,
    )
    [evaluation] = await h.service.evaluate(ADMIN, request.id, [production])
    assert evaluation.outcome == "blocked"
    assert any(item.code == "environmentChanged" for item in evaluation.blockers)


async def test_policy_crud_revision_and_dry_run_do_not_mutate_requests() -> None:
    h = Harness()
    await h.seed()
    request = await h.request()
    policy = await h.policy(enabled=False)
    result = await h.service.dry_run(
        ADMIN, ApprovalPolicyDryRun(request_ids=[request.id], policy=policy)
    )
    assert result[0].outcome == "eligible"
    assert h.repository.entitlements == {}
    changed = ApprovalPolicySaveInput.model_validate(
        {
            **policy.model_dump(
                include=set(ApprovalPolicySaveInput.model_fields) & set(type(policy).model_fields)
            ),
            "expectedRevision": 99,
        }
    )
    with pytest.raises(ConflictError):
        await h.service.save_policy(ADMIN, changed, policy.id)
    with pytest.raises(ConflictError):
        await h.service.delete_policy(ADMIN, policy.id, 99)
    await h.service.delete_policy(ADMIN, policy.id, policy.revision)
    assert await h.service.list_policies(ADMIN) == []


async def test_expired_leases_recover_and_stale_tokens_cannot_release_new_owner() -> None:
    repository = InMemoryApprovalRepository()
    first = await repository.claim_evaluator_lease(TENANT, "one", now=NOW, lease_seconds=10)
    assert first
    assert await repository.claim_evaluator_lease(TENANT, "two", now=NOW, lease_seconds=10) is None
    second = await repository.claim_evaluator_lease(
        TENANT, "two", now=NOW + timedelta(seconds=11), lease_seconds=10
    )
    assert second
    await repository.release_evaluator_lease(first)
    assert await repository.check_evaluator_lease(second, now=NOW + timedelta(seconds=12))


@pytest.mark.parametrize(
    "instant,expected",
    [
        ("2026-10-19T23:30:00+00:00", True),
        ("2026-10-20T00:30:00+00:00", True),
        ("2026-10-20T01:00:00+00:00", False),
        ("2026-10-20T23:30:00+00:00", False),
    ],
)
def test_overnight_schedule_belongs_to_start_day(instant: str, expected: bool) -> None:
    schedule = WeeklySchedule.model_validate(
        {
            "timeZone": "UTC",
            "windows": [{"daysOfWeek": [0], "startTime": "23:00", "endTime": "01:00"}],
        }
    )
    assert schedule_open(schedule, datetime.fromisoformat(instant)) is expected


@pytest.mark.parametrize(
    "instant",
    [
        "2026-11-01T05:30:00+00:00",
        "2026-11-01T06:30:00+00:00",
    ],
)
def test_dst_fold_uses_current_local_window(instant: str) -> None:
    schedule = WeeklySchedule.model_validate(
        {
            "timeZone": "America/New_York",
            "windows": [{"daysOfWeek": [6], "startTime": "01:00", "endTime": "02:00"}],
        }
    )
    assert schedule_open(schedule, datetime.fromisoformat(instant))


async def test_cosmos_approval_batch_includes_source_grant_audit_and_event() -> None:
    repository = object.__new__(CosmosEntitlementRepository)
    repository._desired = cast(Any, type("Container", (), {"execute_item_batch": AsyncMock()})())
    repository._project_audit_event = AsyncMock()  # type: ignore[method-assign]
    request = AccessRequest(
        id="request",
        tenant_id=TENANT,
        requester_object_id=USER,
        resource=RESOURCE,
        etag="read-etag",
        state="approved",
        granted_entitlement_id="grant",
    )
    grant = Entitlement(
        id="grant",
        tenant_id=TENANT,
        subject=EntitlementSubject(kind="user", id="person"),
        resource=RESOURCE,
    )
    await repository.approve_access_request(request, grant, [audit()])
    call = cast(Any, repository._desired).execute_item_batch.call_args.kwargs
    operations = call["batch_operations"]
    assert call["partition_key"] == TENANT
    assert [item[0] for item in operations] == ["create", "replace", "create", "create", "create"]
    assert operations[1][2] == {"if_match_etag": "read-etag"}
    assert operations[-1][1][0]["entityType"] == "notificationEvent"


def connection() -> ModelConnection:
    return ModelConnection(
        entitlement_id="ai-grant",
        publication_id="publication",
        gateway_id="gateway",
        endpoint="https://fictional.azure-api.net/model",
        deployment_name="chat",
        tenant_id=TENANT,
        runtime=EntitlementRuntime(publication_id="publication", status="applied"),
        applied_methods=ModelAccessSettings(entra_enabled=True),
        entra_audience="audience",
        entra_scope="api://audience/.default",
        required_app_role="Models.Invoke.Application",
        operations=[
            ConnectionOperation(name="chat-completions", method="POST", path="/chat/completions")
        ],
        cost_center=CostCenterRef(
            id=general_cost_center_id(TENANT), name="General", code="general"
        ),
    )


class FakeModel:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.bad_identity = False
        self.malformed = False
        self.in_flight = 0
        self.maximum = 0

    async def caller_claims(self, connection: ModelConnection) -> dict[str, Any]:
        return {
            "oid": USER if self.bad_identity else CALLER,
            "tid": TENANT,
            "aud": connection.entra_audience,
            "roles": ["Models.Invoke.Application"],
        }

    async def invoke(
        self, connection: ModelConnection, settings: AiApprovalSettings, evidence: dict[str, Any]
    ) -> str:
        self.calls.append(evidence)
        self.in_flight += 1
        self.maximum = max(self.maximum, self.in_flight)
        await asyncio.sleep(0)
        self.in_flight -= 1
        if self.malformed:
            return '{"approve": true}'
        return json.dumps(
            {
                "suggestedOutcome": "needsReview",
                "reasons": [
                    {"explanation": "Needs human review.", "evidenceIds": [next(iter(evidence))]}
                ],
                "missingInformation": [],
                "riskFlags": [],
            }
        )


async def ai_harness() -> tuple[Harness, ApprovalAiService, FakeModel]:
    h = Harness()
    await h.seed()
    await h.directory.save_principal(
        Principal(id="ai-caller", tenant_id=TENANT, object_id=CALLER, kind="managedIdentity"),
        audit(),
    )
    await h.repository.save_entitlement(
        Entitlement(
            id="ai-grant",
            tenant_id=TENANT,
            subject=EntitlementSubject(kind="application", id="ai-caller"),
            resource=RESOURCE,
        ),
        audit(),
    )
    portal = cast(Any, type("Portal", (), {"connection": AsyncMock(return_value=connection())})())
    model = FakeModel()
    ai = ApprovalAiService(
        h.approvals,
        entitlements=h.entitlements,
        portal_access=portal,
        directory_repository=h.directory,
        model=model,
        clock=lambda: h.now,
    )
    await ai.save_settings(
        ADMIN,
        AiApprovalSettingsSaveInput(
            resource=RESOURCE,
            caller_principal_id="ai-caller",
            entitlement_id="ai-grant",
            cost_center_id=general_cost_center_id(TENANT),
            concurrency=1,
        ),
    )
    return h, ai, model


async def enable_ai(ai: ApprovalAiService) -> None:
    await ai.test(ADMIN)
    current = await ai.settings(ADMIN)
    await ai.save_settings(
        ADMIN,
        AiApprovalSettingsSaveInput(
            **current.model_dump(
                include=set(AiApprovalSettingsSaveInput.model_fields)
                - {"expected_revision", "enabled"}
            ),
            enabled=True,
            expected_revision=current.revision,
        ),
    )


async def test_ai_is_explicit_governed_and_never_mutates_request() -> None:
    h, ai, model = await ai_harness()
    request = await h.request()
    assert model.calls == []
    assert not (await ai.readiness(ADMIN)).ready
    await enable_ai(ai)
    [result] = await ai.recommend(ADMIN, AiApprovalBatchInput(request_ids=[request.id]))
    assert (
        result.recommendation
        and result.recommendation.recommendation.suggested_outcome == "needsReview"
    )
    assert h.repository.access_requests[request.id].state == "pending"
    assert len(h.repository.entitlements) == 1  # dedicated caller's pre-existing grant only
    assert (await ai.readiness(ADMIN)).ready
    assert len(await ai.recommendations(ADMIN, request.id)) == 1
    h.now += timedelta(minutes=16)
    assert await ai.recommendations(ADMIN, request.id) == []


async def test_ai_caller_mismatch_and_malformed_output_fail_closed() -> None:
    h, ai, model = await ai_harness()
    request = await h.request()
    model.bad_identity = True
    with pytest.raises(ConflictError, match="runtime token"):
        await ai.test(ADMIN)
    model.bad_identity = False
    await enable_ai(ai)
    model.malformed = True
    [result] = await ai.recommend(ADMIN, AiApprovalBatchInput(request_ids=[request.id]))
    assert result.recommendation is None and result.blocker
    assert h.repository.access_requests[request.id].state == "pending"
    readiness = await ai.readiness(ADMIN)
    assert readiness.last_error_code == "validation_error" and not readiness.ready


async def test_ai_config_requires_explicit_test_before_enabling() -> None:
    _, ai, model = await ai_harness()
    current = await ai.settings(ADMIN)
    payload = AiApprovalSettingsSaveInput(
        **current.model_dump(
            include=set(AiApprovalSettingsSaveInput.model_fields) - {"enabled", "expected_revision"}
        ),
        enabled=True,
        expected_revision=current.revision,
    )
    with pytest.raises(ConflictError, match="explicitly test"):
        await ai.save_settings(ADMIN, payload)
    assert model.calls == []


async def test_ai_validates_evidence_and_extra_fields() -> None:
    raw = json.dumps(
        {
            "suggestedOutcome": "recommendApprove",
            "reasons": [{"explanation": "Made up", "evidenceIds": ["nonexistent"]}],
        }
    )
    with pytest.raises(ValidationError, match="not supplied"):
        ApprovalAiService._validate_output(raw, {"environment"})
    with pytest.raises(ValidationError, match="schema"):
        ApprovalAiService._validate_output('{"approve":true}', {"environment"})


async def test_http_adapter_uses_apim_application_bearer_and_cost_center() -> None:
    received: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    token = jwt.encode(
        {"oid": CALLER, "aud": "audience", "tid": TENANT, "roles": ["Models.Invoke.Application"]},
        "fictional-test-only-key-with-32-characters",
        algorithm="HS256",
    )
    credential = cast(
        Any,
        type(
            "Credential",
            (),
            {"get_token": AsyncMock(return_value=type("Token", (), {"token": token})())},
        )(),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = HttpGovernedApprovalModel(credential, client=client)
        output = await adapter.invoke(
            connection(), AiApprovalSettings(id="settings", tenant_id=TENANT), {"test": "synthetic"}
        )
    assert output == "{}" and len(received) == 1
    assert str(received[0].url) == "https://fictional.azure-api.net/model/chat/completions"
    assert received[0].headers["Authorization"] == f"Bearer {token}"
    assert received[0].headers[connection().cost_center_header] == "general"
    assert "Ocp-Apim-Subscription-Key" not in received[0].headers
    assert b'"tools"' not in received[0].content
    credential.get_token.assert_awaited_once_with("api://audience/.default")


def test_approval_routes_require_admin() -> None:
    app = FastAPI()
    app.state.authenticator = LocalAuthenticator(TENANT, roles=["User"])
    app.state.settings = Settings(
        tenant_id=TENANT,
        auth_mode=AuthMode.LOCAL,
        environment=Environment.TEST,
        repository_backend=RepositoryBackend.MEMORY,
    )
    app.include_router(approvals_router)
    app.add_exception_handler(DomainError, domain_error_handler)
    with TestClient(app) as client:
        response = client.post("/api/v1/access-requests/bulk/preview", json={"requestIds": ["one"]})
        assert response.status_code == 403
        assert client.get("/api/v1/settings/approval-ai").status_code == 403


async def test_equivalent_policies_choose_stable_system_attribution_once() -> None:
    h = Harness()
    await h.seed()
    request = await h.request()
    first = await h.policy()
    second = await h.policy()
    await h.service.pause(ADMIN, False)
    await asyncio.gather(h.service.run_once(TENANT), h.service.run_once(TENANT))
    approved = h.repository.access_requests[request.id]
    assert approved.state == "approved" and approved.approval_provenance
    assert approved.approval_provenance["policyId"] == min(first.id, second.id)
    assert len(h.repository.entitlements) == 1


async def test_stale_policy_cannot_commit_request_grant_or_notification() -> None:
    h = Harness()
    await h.seed()
    request = await h.request()
    policy = await h.policy()
    h.approvals.policies[(TENANT, policy.id)] = policy.model_copy(
        update={"enabled": False, "etag": "new-version"}
    )
    approved = request.model_copy(update={"state": "approved", "granted_entitlement_id": "grant"})
    grant = Entitlement(
        id="grant",
        tenant_id=TENANT,
        resource=RESOURCE,
        subject=EntitlementSubject(kind="user", id="person"),
    )
    before_events = dict(h.repository.notification_events)
    with pytest.raises(ConflictError, match="policy changed"):
        await h.repository.approve_access_request_with_events(
            approved, grant, [audit()], [access_request_event(approved)], policy_guard=policy
        )
    assert h.repository.access_requests[request.id].state == "pending"
    assert h.repository.entitlements == {}
    assert h.repository.notification_events == before_events


async def test_ai_configured_concurrency_and_context_expiration() -> None:
    h, ai, model = await ai_harness()
    first = await h.request()
    second = await h.request("00000000-0000-0000-0000-000000000004")
    await enable_ai(ai)
    await ai.recommend(ADMIN, AiApprovalBatchInput(request_ids=[first.id, second.id]))
    assert model.maximum == 1
    h.gateways.gateways["gateway"] = h.gateways.gateways["gateway"].model_copy(
        update={"environment": "production"}
    )
    assert await ai.recommendations(ADMIN, first.id) == []


async def test_unsupported_governed_offering_is_visible_with_explanation() -> None:
    _, ai, _ = await ai_harness()
    [offering] = await ai.offerings(ADMIN)
    assert offering.resource == RESOURCE and not offering.compatible
    assert offering.blockers[0].code == "unsupportedGovernedOffering"


async def test_direct_grant_mutations_emit_atomic_safe_personal_events() -> None:
    h = Harness()
    await h.seed()
    await h.directory.save_principal(
        Principal(id="person", tenant_id=TENANT, object_id=USER, kind="user"), audit()
    )
    grant = Entitlement(
        id="grant",
        tenant_id=TENANT,
        resource=RESOURCE,
        subject=EntitlementSubject(kind="user", id="person"),
        notes="private free text must not be copied to event",
    )
    await h.repository.create_entitlement(grant, audit())
    await h.repository.save_entitlement(grant.model_copy(update={"notes": "changed"}), audit())
    await h.repository.delete_entitlement(grant, audit())
    events = list(h.repository.notification_events.values())
    assert [str(item.event_type) for item in events] == [
        "grant.created",
        "grant.changed",
        "grant.revoked",
    ]
    assert all(item.audience.user_object_ids == [USER] for item in events)
    assert all("private free text" not in item.model_dump_json() for item in events)
    assert h.repository.entitlements == {}


async def test_failed_direct_grant_create_does_not_publish_event() -> None:
    h = Harness()
    await h.seed()
    grant = Entitlement(
        id="grant",
        tenant_id=TENANT,
        resource=RESOURCE,
        subject=EntitlementSubject(kind="user", id="person"),
    )
    await h.repository.create_entitlement(grant, audit())
    before = dict(h.repository.notification_events)
    with pytest.raises(ConflictError):
        await h.repository.create_entitlement(grant, audit())
    assert h.repository.notification_events == before


async def test_mosaic_group_source_expands_only_current_persisted_human_members() -> None:
    h = Harness()
    group = Group(id="mosaic-group", tenant_id=TENANT, name="Fictional group")
    await h.directory.save_group(group, audit())
    human = Principal(id="member", tenant_id=TENANT, object_id=USER, kind="user")
    application = Principal(
        id="app-member", tenant_id=TENANT, object_id=CALLER, kind="servicePrincipal"
    )
    for principal in [human, application]:
        await h.directory.save_principal(principal, audit())
        await h.directory.create_membership(
            GroupMembership(
                id=f"membership-{principal.id}",
                tenant_id=TENANT,
                group_id=group.id,
                principal_id=principal.id,
            ),
            group,
            principal,
            audit(),
        )
    grant = Entitlement(
        id="group-grant",
        tenant_id=TENANT,
        resource=RESOURCE,
        subject=EntitlementSubject(kind="group", id=group.id),
    )
    await h.repository.create_entitlement(grant, audit())
    [event] = list(h.repository.notification_events.values())
    assert event.subject.grant_subject == grant.subject
    assert event.audience.user_object_ids == []  # a MOSAIC group record ID is not a person
    resolver = GrantNotificationRecipientResolver(h.directory)
    assert await resolver.expand(event) == [USER]
    [membership] = await h.directory.list_memberships(TENANT, principal_id=human.id)
    await h.directory.delete_membership(membership, audit())
    assert await resolver.expand(event) == []  # fresh evidence on the send claim


async def test_security_group_source_retains_actual_identity_after_grant_and_principal_delete() -> (
    None
):
    h = Harness()
    group_oid = "00000000-0000-0000-0000-000000000010"
    nested_oid = "00000000-0000-0000-0000-000000000011"
    await h.directory.save_principal(
        Principal(
            id="security-group-record", tenant_id=TENANT, kind="securityGroup", object_id=group_oid
        ),
        audit(),
    )
    grant = Entitlement(
        id="group-grant",
        tenant_id=TENANT,
        resource=RESOURCE,
        subject=EntitlementSubject(kind="securityGroup", id="security-group-record"),
    )
    await h.repository.create_entitlement(grant, audit())
    await h.repository.delete_entitlement(grant, audit())
    event = list(h.repository.notification_events.values())[-1]
    assert str(event.event_type) == "grant.revoked"
    assert event.subject.grant_subject == grant.subject
    assert event.subject.grant_subject_object_id == group_oid
    assert event.audience.user_object_ids == []
    del h.directory.principals["security-group-record"]
    graph = FakeDirectoryLookup(
        [
            DirectoryObject(object_id=USER, kind="user"),
            DirectoryObject(object_id=CALLER, kind="servicePrincipal"),
            DirectoryObject(object_id=nested_oid, kind="securityGroup"),
        ],
        group_members={group_oid: [nested_oid, CALLER], nested_oid: [USER]},
    )
    resolver = GrantNotificationRecipientResolver(h.directory, directory_lookup=graph)
    assert await resolver.expand(event) == [
        USER
    ]  # transitive grant semantics, not app-role semantics
    graph._members[nested_oid] = []
    assert await resolver.expand(event) == []  # no stale former roster at send time


@pytest.mark.parametrize("failure", ["disabled", "unavailable", "truncated", "wrongGroup"])
async def test_security_group_expansion_never_guesses_incomplete_personal_audience(
    failure: str,
) -> None:
    h = Harness()
    group_oid = "00000000-0000-0000-0000-000000000010"
    await h.directory.save_principal(
        Principal(
            id="security-group-record", tenant_id=TENANT, kind="securityGroup", object_id=group_oid
        ),
        audit(),
    )
    grant = Entitlement(
        id="group-grant",
        tenant_id=TENANT,
        resource=RESOURCE,
        subject=EntitlementSubject(kind="securityGroup", id="security-group-record"),
    )
    await h.repository.create_entitlement(grant, audit())
    [event] = list(h.repository.notification_events.values())
    graph: Any = None
    if failure == "unavailable":
        graph = FakeDirectoryLookup([], unavailable=True)
    elif failure in {"truncated", "wrongGroup"}:
        graph = cast(
            Any,
            type(
                "Lookup",
                (),
                {
                    "group_members": AsyncMock(
                        return_value=DirectoryMemberPage(
                            group_object_id=group_oid if failure == "truncated" else USER,
                            members=[DirectoryObject(object_id=USER, kind="user")],
                            truncated=failure == "truncated",
                        )
                    )
                },
            )(),
        )
    resolver = GrantNotificationRecipientResolver(h.directory, directory_lookup=graph)
    with pytest.raises((DirectoryError, DirectoryDisabledError)):
        await resolver.expand(event)


async def test_group_expansion_can_exceed_source_event_hundred_person_limit() -> None:
    h = Harness()
    group_oid = "00000000-0000-0000-0000-000000000010"
    await h.directory.save_principal(
        Principal(
            id="security-group-record", tenant_id=TENANT, kind="securityGroup", object_id=group_oid
        ),
        audit(),
    )
    grant = Entitlement(
        id="group-grant",
        tenant_id=TENANT,
        resource=RESOURCE,
        subject=EntitlementSubject(kind="securityGroup", id="security-group-record"),
    )
    await h.repository.create_entitlement(grant, audit())
    [event] = list(h.repository.notification_events.values())
    users = [
        DirectoryObject(object_id=f"00000000-0000-0000-0001-{index:012d}", kind="user")
        for index in range(150)
    ]
    graph = FakeDirectoryLookup(
        users, group_members={group_oid: [item.object_id for item in users]}
    )
    expanded = await GrantNotificationRecipientResolver(h.directory, directory_lookup=graph).expand(
        event
    )
    assert len(expanded) == 150 and group_oid not in expanded
    assert event.audience.user_object_ids == []  # actual IDs are bounded durable fanout jobs


class FakeCosmosContainer:
    """CAS semantics only, not an Azure client or live network fixture."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.revision = 0
        self.race_once = False

    async def read_item(self, *, item: str, partition_key: str) -> dict[str, Any]:
        value = self.items.get((partition_key, item))
        if value is None:
            raise exceptions.CosmosResourceNotFoundError(message="missing")
        return deepcopy(value)

    def write(self, body: dict[str, Any]) -> dict[str, Any]:
        self.revision += 1
        body = {**deepcopy(body), "_etag": str(self.revision)}
        self.items[(body["tenantId"], body["id"])] = body
        return deepcopy(body)

    async def create_item(self, body: dict[str, Any]) -> dict[str, Any]:
        if (body["tenantId"], body["id"]) in self.items:
            raise exceptions.CosmosResourceExistsError(message="exists")
        return self.write(body)

    async def upsert_item(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.write(body)

    async def replace_item(
        self, *, item: str, body: dict[str, Any], etag: str, match_condition: Any
    ) -> dict[str, Any]:
        existing = await self.read_item(item=item, partition_key=body["tenantId"])
        if self.race_once:
            self.race_once = False
            self.write(existing)
            raise exceptions.CosmosAccessConditionFailedError(message="raced")
        if existing["_etag"] != etag:
            raise exceptions.CosmosAccessConditionFailedError(message="stale")
        return self.write(body)

    async def delete_item(
        self, *, item: str, partition_key: str, etag: str, match_condition: Any
    ) -> None:
        existing = await self.read_item(item=item, partition_key=partition_key)
        if existing["_etag"] != etag:
            raise exceptions.CosmosAccessConditionFailedError(message="stale")
        del self.items[(partition_key, item)]


async def test_cosmos_approval_recovery_records_leases_and_state_cas() -> None:
    repository = object.__new__(CosmosApprovalRepository)
    container = FakeCosmosContainer()
    repository._desired = cast(Any, container)
    state = await repository.update_evaluator_state(
        TENANT, lambda value: value.model_copy(update={"paused": False})
    )
    assert not state.paused and state.etag
    container.race_once = True
    changed = await repository.update_evaluator_state(
        TENANT, lambda value: value.model_copy(update={"pending_count": 7})
    )
    assert changed.pending_count == 7 and not changed.paused
    first = await repository.claim_evaluator_lease(TENANT, "worker", now=NOW, lease_seconds=10)
    assert first
    assert (
        await repository.claim_evaluator_lease(TENANT, "other", now=NOW, lease_seconds=10) is None
    )
    second = await repository.claim_evaluator_lease(
        TENANT, "other", now=NOW + timedelta(seconds=11), lease_seconds=10
    )
    assert second
    await repository.release_evaluator_lease(first)
    assert await repository.check_evaluator_lease(second, now=NOW + timedelta(seconds=12))
    from mosaic_api.approvals import BulkApprovalItemResult, BulkApprovalResult

    result = BulkApprovalResult(
        batch_id="batch",
        idempotency_key="key",
        items=[
            BulkApprovalItemResult(request_id="request", status="approved", entitlement_id="grant")
        ],
    )
    await repository.save_bulk_result(
        TENANT, ADMIN.object_id, result, input_fingerprint="fingerprint"
    )
    retried = result.model_copy(
        update={"items": [BulkApprovalItemResult(request_id="request", status="failed")]}
    )
    recovered = await repository.save_bulk_result(
        TENANT, ADMIN.object_id, retried, input_fingerprint="fingerprint"
    )
    assert recovered.items[0].entitlement_id == "grant"
    with pytest.raises(ConflictError, match="different approval inputs"):
        await repository.save_bulk_result(
            TENANT, ADMIN.object_id, result, input_fingerprint="other-fingerprint"
        )
    assert await repository.get_bulk_result("different-tenant", ADMIN.object_id, "key") is None
