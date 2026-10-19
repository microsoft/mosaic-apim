from datetime import UTC, datetime

import pytest
from mosaic_api.domain import (
    EntitlementEnforcement,
    EntitlementSubject,
    McpAccessSnapshot,
    McpModelCaller,
    McpPublication,
    ModelAccessGrant,
    ModelAccessSettings,
    ModelAccessSnapshot,
    Publication,
    PublishedResource,
    PublishPlan,
    PublishPlanStep,
    TokenEnforcement,
)
from mosaic_api.model_pools import ModelPool, PoolMember, PoolModel
from mosaic_api.services.change_review import build_review, review_snapshot

NOW = datetime(2026, 9, 1, tzinfo=UTC)


def publication() -> Publication:
    return Publication(
        id="publication",
        tenant_id="demo",
        gateway_id="gateway",
        model_endpoint_id="endpoint",
        deployment_name="chat",
        provider="azureOpenAi",
        display_name="Chat",
        api_name="chat",
        api_path="models/chat",
        backend_name="chat",
        fragment_name="chat",
        product_name="chat",
        subscription_name="chat",
        shape_version="1",
    )


def plan(**kwargs: object) -> PublishPlan:
    return PublishPlan.model_validate(
        {
            "id": "plan",
            "tenant_id": "demo",
            "publication_id": "publication",
            "gateway_id": "gateway",
            "digest": "digest",
            **kwargs,
        }
    )


def access() -> ModelAccessSnapshot:
    return ModelAccessSnapshot(
        version=1,
        settings=ModelAccessSettings(),
        audience="api://demo",
        grants=[
            ModelAccessGrant(
                entitlement_id=name,
                subject=EntitlementSubject(kind="user", id=name),
                object_id=f"object-{name}",
                display_name=name,
                subscription_name=f"key-{name}",
                enabled=True,
                intent_digest="digest",
                cost_center_id="research",
                cost_center_code="Research",
                limits_source="grant",
                enforcement=EntitlementEnforcement(
                    tokens=TokenEnforcement(
                        counter_key_expression="@(secret-counter-expression)",
                        tokens_per_minute=1000,
                    )
                ),
            )
            for name in ("Alex", "Research app")
        ],
    )


def test_new_target_has_additions_without_exposing_expression_contents() -> None:
    review = build_review(publication(), plan(), access())
    assert review.baseline == "new"
    assert review.changes
    assert all(change.previous_known and change.before is None for change in review.changes)
    assert "secret-counter-expression" not in review.model_dump_json()
    assert "<policies" not in review.model_dump_json()
    assert any("fingerprint" in field.label for field in review.target.fields)


def test_one_changed_grant_omits_every_unchanged_grant_and_snapshot_version() -> None:
    target = publication()
    before = access()
    target.applied_review = review_snapshot(target, before)
    target.last_applied_at = NOW
    after = before.model_copy(deep=True)
    after.version = 2
    assert after.grants[0].enforcement and after.grants[0].enforcement.tokens
    after.grants[0].enforcement.tokens.tokens_per_minute = 2000
    review = build_review(target, plan(), after)
    assert [(change.key, change.before, change.after) for change in review.changes] == [
        ("grants/Alex/tokensPerMinute", "1000", "2000"),
    ]
    assert review.applied_at == NOW


def test_disabling_access_does_not_invent_limit_removals() -> None:
    target = publication()
    before = access()
    target.applied_review = review_snapshot(target, before)
    target.last_applied_at = NOW
    after = before.model_copy(deep=True)
    after.grants[0].enabled = False
    assert [change.key for change in build_review(target, plan(), after).changes] == [
        "grants/Alex/enabled",
    ]


def test_older_target_compares_access_but_never_reconstructs_applied_routing() -> None:
    target = publication()
    target.last_applied_at = NOW
    target.applied_access = access()
    review = build_review(target, plan(), access())
    assert review.baseline == "partial"
    assert not any(change.key.startswith("grants/") for change in review.changes)
    assert all(not change.previous_known for change in review.changes)
    assert any(change.key == "publication/apiPath" for change in review.changes)


@pytest.mark.parametrize("state", ["failed", "unknown"])
def test_failed_or_interrupted_target_discards_full_baseline(state: str) -> None:
    target = publication()
    target.last_applied_at = NOW
    target.applied_review = review_snapshot(target, access())
    target = target.model_copy(update={"access_state": state})
    review = build_review(target, plan(), access())
    assert review.baseline == "unavailable"
    assert review.changes and all(not change.previous_known for change in review.changes)


def test_unpublish_removes_the_applied_snapshot_not_pending_configuration() -> None:
    target = publication()
    target.applied_review = review_snapshot(target, access())
    target.applied_access = access()
    target.last_applied_at = NOW
    target.display_name = "Saved but never applied"
    review = build_review(target, plan(operation="unpublish"), target.applied_access)
    name = next(change for change in review.changes if change.key == "publication/displayName")
    assert name.before == "Chat" and name.after is None
    assert all(change.after is None for change in review.changes)
    assert review.target.fields == []
    assert any("retained" in notice for notice in review.impact)


def test_existing_unowned_resources_are_not_a_known_absent_baseline() -> None:
    review = build_review(
        publication(),
        plan(
            steps=[
                {
                    "kind": "backend",
                    "name": "chat",
                    "resource_id": "/backends/chat",
                    "action": "update",
                    "reason": "Exists",
                    "existed": True,
                }
            ]
        ),
        None,
    )
    assert review.baseline == "unavailable"
    assert all(not change.previous_known for change in review.changes)


def test_identical_intent_keeps_actual_interruption_and_replacement_visible() -> None:
    target = publication()
    target.last_applied_at = NOW
    target.applied_review = review_snapshot(target, access())
    review = build_review(
        target,
        plan(
            steps=[
                PublishPlanStep(
                    kind="apiPolicy",
                    name="chat",
                    resource_id="/apis/chat/policies/policy",
                    action="update",
                    reason="Deny",
                    existed=True,
                    stage="prepare",
                ),
                PublishPlanStep(
                    kind="subscription",
                    name="key-Alex",
                    resource_id="/subscriptions/Alex",
                    action="update",
                    reason="Suspend",
                    existed=True,
                    subscription_state="suspended",
                ),
            ]
        ),
        access(),
    )
    assert review.changes == []
    assert any("temporarily blocks calls" in notice for notice in review.impact)
    assert any("keys are suspended" in notice for notice in review.impact)
    assert any("replaced, not merged" in notice for notice in review.impact)


def pool() -> ModelPool:
    return ModelPool(
        id="pool",
        tenant_id="demo",
        gateway_id="gateway",
        display_name="Pool",
        api_name="pool",
        api_path="models/pool",
        fragment_name="pool",
        product_name="pool",
        subscription_name="pool",
        models=[
            PoolModel(
                id="chat",
                public_name="chat",
                display_name="Chat",
                backend_pool_name="pool-chat",
                members=[
                    PoolMember(model_endpoint_id=name, deployment_name="chat", backend_name=name)
                    for name in ("east", "west")
                ],
            )
        ],
    )


def test_pool_collection_order_only_changes_linear_routing() -> None:
    target = pool()
    target.last_applied_at = NOW
    target.applied_review = review_snapshot(target, None)
    target.models[0].members.reverse()
    assert build_review(target, plan(target="pool"), None).changes == []
    target.pool_type = "linear"
    target.applied_review = review_snapshot(target, None)
    target.models[0].members.reverse()
    assert len(build_review(target, plan(target="pool"), None).changes) == 2


def test_pool_removal_and_weight_changes_are_keyed_by_member() -> None:
    target = pool()
    target.last_applied_at = NOW
    target.applied_review = review_snapshot(target, None)
    target.models[0].members.pop()
    target.models[0].members[0].weight = 10
    review = build_review(target, plan(target="pool"), None)
    assert any(
        change.key.endswith("east/weight") and change.after == "10" for change in review.changes
    )
    assert all(change.after is None for change in review.changes if "/west/" in change.key)


def test_mcp_identity_change_with_same_label_is_not_hidden() -> None:
    target = McpPublication(
        id="mcp",
        tenant_id="demo",
        gateway_id="gateway",
        mcp_endpoint_id="endpoint",
        display_name="Docs",
        api_name="docs",
        api_path="docs",
        backend_name="docs",
        fragment_name="docs",
        metadata_api_name="metadata",
        mcp_server_id="server",
    )
    before = McpAccessSnapshot(
        version=1,
        audience="api://demo",
        model_caller=McpModelCaller(
            principal_id="caller",
            object_id="old",
            display_name="Docs application",
        ),
    )
    target.last_applied_at = NOW
    target.applied_review = review_snapshot(target, before)
    after = before.model_copy(deep=True)
    assert after.model_caller
    after.model_caller.object_id = "new"
    assert [change.key for change in build_review(target, plan(target="mcp"), after).changes] == [
        "mcp/modelCallerObject",
    ]


def test_restricted_access_baseline_keeps_its_denial_and_unknown_routing() -> None:
    target = publication()
    target.last_applied_at = NOW
    target.applied_access = access()
    target.applied_access.grants[0].enabled = False
    target.access_state = "failed"
    target.resources = [
        PublishedResource(
            kind="api",
            name="chat",
            resource_id="/apis/chat",
            created_by_mosaic=True,
        )
    ]
    review = build_review(target, plan(), access())
    assert review.baseline == "partial"
    change = next(change for change in review.changes if change.key == "grants/Alex/enabled")
    assert change.before == "Disabled" and change.previous_known
    assert not next(
        change for change in review.changes if change.key == "publication/apiPath"
    ).previous_known
