import pytest
from apim_double import RESOURCE_ID
from fastapi.testclient import TestClient
from mosaic_api.domain import (
    ApimResourceId,
    ApiShape,
    AuditEvent,
    CapabilitySupport,
    EntitlementEnforcement,
    EntitlementResource,
    Gateway,
    GatewayCapabilities,
    ModelApi,
    ModelEndpoint,
    ModelProvider,
    RequestEnforcement,
    TokenEnforcement,
    new_id,
)
from mosaic_api.errors import ValidationError
from mosaic_api.integrations.apim.policy_capabilities import (
    assess_gateway_policy_capabilities,
    changed_limit_policies,
    require_supported_limits,
    required_limit_policies,
)
from mosaic_api.observed import ObservedModelDeployment
from mosaic_api.services.policy_capabilities import deployment_policy_capabilities

RESOURCE = EntitlementResource(kind="poolModel", id="chat", scope_id="pool")


def limits(*, calls: int = 20, window: int = 60) -> EntitlementEnforcement:
    return EntitlementEnforcement(
        tokens=TokenEnforcement(
            counter_key_expression="@(context.Subscription.Id)",
            tokens_per_minute=1000,
        ),
        requests=RequestEnforcement(
            counter_key_expression="@(context.Subscription.Id)",
            calls=calls,
            renewal_period_seconds=window,
            call_quota=5000,
            call_quota_period="Weekly",
        ),
    )


@pytest.mark.parametrize(
    "sku", ["Developer", "Basic", "Standard", "Premium", "BasicV2", "StandardV2", "PremiumV2"]
)
def test_documented_tiers_accept_exact_call_and_token_policy_requirements(sku: str) -> None:
    capabilities = assess_gateway_policy_capabilities(
        "gateway", sku, api_shape=ApiShape.AZURE_OPENAI, resource=RESOURCE
    )
    requirements = required_limit_policies(limits())
    require_supported_limits(capabilities, requirements)
    assert [(item.policy, item.field, item.values[0]) for item in requirements] == [
        ("llm-token-limit", "enforcement.tokens.tokensPerMinute", 1000),
        ("rate-limit-by-key", "enforcement.requests.calls", 20),
        ("quota-by-key", "enforcement.requests.callQuota", 5000),
    ]
    assert all(
        item.support == CapabilitySupport.AVAILABLE for item in capabilities.policies.values()
    )


def test_consumption_refuses_limits_but_not_documented_token_metrics() -> None:
    capabilities = assess_gateway_policy_capabilities(
        "gateway", "Consumption", api_shape=ApiShape.AZURE_OPENAI, resource=RESOURCE
    )
    with pytest.raises(ValidationError) as refused:
        require_supported_limits(capabilities, required_limit_policies(limits()))
    assert refused.value.status_code == 422
    details = refused.value.details
    assert details["reason"] == "gatewayPolicyLimitsRejected"
    assert details["boundary"] == "save"
    assert {issue["policy"] for issue in details["issues"]} == {
        "llm-token-limit",
        "rate-limit-by-key",
        "quota-by-key",
    }
    assert all(issue["resource"] == RESOURCE.model_dump(mode="json") for issue in details["issues"])
    assert capabilities.policies["llm-emit-token-metric"].support == CapabilitySupport.AVAILABLE
    assert "counterKeyExpression" not in str(details)
    assert "@(" not in str(details)


@pytest.mark.parametrize("sku", [None, "", "FutureTier", "Isolated"])
def test_unread_or_unverified_tier_is_unknown_and_cannot_save_a_limit(sku: str | None) -> None:
    capabilities = assess_gateway_policy_capabilities(
        "gateway", sku, api_shape=ApiShape.AZURE_OPENAI
    )
    with pytest.raises(ValidationError) as refused:
        require_supported_limits(capabilities, required_limit_policies(limits()), boundary="apply")
    assert refused.value.details["boundary"] == "apply"
    assert all(
        issue["support"] == "unknown" and issue["nextAction"] == "checkGatewayAccess"
        for issue in refused.value.details["issues"]
    )


@pytest.mark.parametrize("sku", ["Developer", "Consumption"])
def test_anthropic_token_policies_are_refused_without_v2(sku: str) -> None:
    capabilities = assess_gateway_policy_capabilities(
        "gateway", sku, api_shape=ApiShape.ANTHROPIC_MESSAGES
    )
    assert capabilities.policies["llm-token-limit"].support == CapabilitySupport.UNAVAILABLE
    assert capabilities.policies["llm-emit-token-metric"].support == CapabilitySupport.UNAVAILABLE
    if sku == "Developer":
        assert capabilities.policies["quota-by-key"].support == CapabilitySupport.AVAILABLE


def test_anthropic_v2_support_does_not_infer_support_for_an_unknown_schema() -> None:
    known = assess_gateway_policy_capabilities(
        "gateway", "StandardV2", api_shape=ApiShape.ANTHROPIC_MESSAGES
    )
    require_supported_limits(known, required_limit_policies(limits()))
    unknown = assess_gateway_policy_capabilities("gateway", "StandardV2")
    assert unknown.policies["quota-by-key"].support == CapabilitySupport.AVAILABLE
    with pytest.raises(ValidationError) as refused:
        require_supported_limits(unknown, required_limit_policies(limits()))
    assert [issue["reason"] for issue in refused.value.details["issues"]] == ["unknownApiShape"]


def test_mcp_and_known_unsupported_schema_never_receive_token_support() -> None:
    mcp = assess_gateway_policy_capabilities(
        "gateway", "StandardV2", resource=EntitlementResource(kind="mcpServer", id="tools")
    )
    assert mcp.policies["llm-token-limit"].reason == "unsupportedApiShape"
    unsupported = assess_gateway_policy_capabilities(
        "gateway",
        "StandardV2",
        api_shape=ApiShape.AZURE_OPENAI,
        token_schema_support=CapabilitySupport.UNAVAILABLE,
    )
    assert unsupported.policies["llm-token-limit"].support == CapabilitySupport.UNAVAILABLE


@pytest.mark.parametrize("window", [1, 300])
def test_supported_rate_window_boundaries_are_accepted(window: int) -> None:
    capabilities = assess_gateway_policy_capabilities(
        "gateway", "Developer", api_shape=ApiShape.AZURE_OPENAI
    )
    require_supported_limits(capabilities, required_limit_policies(limits(window=window)))


def test_301_second_window_has_precise_refusal_and_does_not_restrict_calendar_quota() -> None:
    capabilities = assess_gateway_policy_capabilities(
        "gateway", "Developer", api_shape=ApiShape.AZURE_OPENAI
    )
    with pytest.raises(ValidationError) as refused:
        require_supported_limits(capabilities, required_limit_policies(limits(window=301)))
    assert len(refused.value.details["issues"]) == 1
    issue = refused.value.details["issues"][0]
    assert issue["field"] == "enforcement.requests.renewalPeriodSeconds"
    assert issue["policy"] == "rate-limit-by-key"
    assert issue["support"] == "available"
    assert issue["reason"] == "invalidPolicyValue"
    assert (issue["minimum"], issue["maximum"]) == (1, 300)
    assert required_limit_policies(limits(window=301))[-1].values[:2] == (5000, "Weekly")


def test_changed_values_preserve_unchanged_legacy_limits_and_safe_removal() -> None:
    before = required_limit_policies(limits(window=3600))
    assert changed_limit_policies(before, required_limit_policies(limits(window=3600))) == ()
    assert changed_limit_policies(before, ()) == ()
    after = required_limit_policies(limits(calls=30, window=60))
    changed = changed_limit_policies(before, after)
    assert [(item.policy, item.field) for item in changed] == [
        ("rate-limit-by-key", "enforcement.requests.calls")
    ]
    capabilities = assess_gateway_policy_capabilities(
        "gateway", "Consumption", api_shape=ApiShape.AZURE_OPENAI
    )
    with pytest.raises(ValidationError) as refused:
        require_supported_limits(capabilities, changed)
    assert [issue["policy"] for issue in refused.value.details["issues"]] == ["rate-limit-by-key"]


def test_quota_period_and_effective_counter_changes_are_not_metadata_edits() -> None:
    before = limits()
    assert before.requests is not None
    after = before.model_copy(
        update={"requests": before.requests.model_copy(update={"call_quota_period": "Monthly"})}
    )
    changed = changed_limit_policies(
        required_limit_policies(before), required_limit_policies(after)
    )
    assert [item.field for item in changed] == ["enforcement.requests.callQuota"]
    assert before.tokens is not None
    counter_edit = before.model_copy(
        update={
            "tokens": before.tokens.model_copy(
                update={"counter_key_expression": "different-counter"}
            )
        }
    )
    assert (
        changed_limit_policies(
            required_limit_policies(before), required_limit_policies(counter_edit)
        )
        == ()
    )
    assert (
        len(
            changed_limit_policies(
                required_limit_policies(before, counter_key_effective=True),
                required_limit_policies(counter_edit, counter_key_effective=True),
            )
        )
        == 1
    )


def test_conditional_template_checks_values_without_claiming_target_support() -> None:
    unscoped = assess_gateway_policy_capabilities(None, None)
    require_supported_limits(unscoped, required_limit_policies(limits()), intrinsic_only=True)
    with pytest.raises(ValidationError) as refused:
        require_supported_limits(
            unscoped, required_limit_policies(limits(window=301)), intrinsic_only=True
        )
    assert [issue["reason"] for issue in refused.value.details["issues"]] == ["invalidPolicyValue"]


@pytest.mark.parametrize(
    "flag,expected",
    [
        ("unknown", "unknown"),
        ("notEvaluated", "unknown"),
        ("false", "unknown"),
        ("", "unknown"),
        ("true", "available"),
        ("1", "available"),
    ],
)
def test_gateway_policy_observed_schema_requires_affirmative_flag_evidence(
    flag: str, expected: str
) -> None:
    arm = ApimResourceId.parse(RESOURCE_ID)
    gateway = Gateway(
        id="gateway",
        tenant_id="tenant-test",
        name="Fictional observed gateway",
        azure_resource_id=arm.canonical,
        subscription_id=arm.subscription_id,
        resource_group=arm.resource_group,
        service_name=arm.service_name,
        capabilities=GatewayCapabilities(sku_name="Developer"),
    )
    endpoint = ModelEndpoint(
        id="endpoint",
        tenant_id="tenant-test",
        name="Fictional endpoint",
        provider=ModelProvider.AZURE_OPENAI,
        endpoint="https://fixture.openai.azure.com",
    )
    observed = ObservedModelDeployment(
        id="deployment",
        tenant_id="tenant-test",
        endpoint_id=endpoint.id,
        snapshot_id="fixture-snapshot",
        deployment_name="unknown-model",
        capabilities={"chatCompletion": flag},
    )
    result = deployment_policy_capabilities(gateway, endpoint, observed)
    assert result.policies["llm-token-limit"].support == expected
    assert result.policies["llm-emit-token-metric"].support == expected
    unsupported = observed.model_copy(update={"model_name": "text-embedding-3-large"})
    assert (
        deployment_policy_capabilities(gateway, endpoint, unsupported)
        .policies["llm-token-limit"]
        .support
        == "unavailable"
    )


async def seed_read_target(client: TestClient, sku: str | None) -> tuple[str, str]:
    tenant = client.app.state.settings.tenant_id
    resource = ApimResourceId.parse(RESOURCE_ID)
    gateway = Gateway(
        id="gateway-policy-read",
        tenant_id=tenant,
        name="Fictional policy gateway",
        azure_resource_id=resource.canonical,
        subscription_id=resource.subscription_id,
        resource_group=resource.resource_group,
        service_name=resource.service_name,
        capabilities=GatewayCapabilities(sku_name=sku),
    )
    audit = AuditEvent(
        id=new_id("audit"),
        tenant_id=tenant,
        action="test.seed",
        resource_type="test",
        resource_id=gateway.id,
        actor_object_id="local-admin",
    )
    await client.app.state.gateway_repository.save_gateway(gateway, audit)
    model = ModelApi(
        id="model-policy-read",
        tenant_id=tenant,
        gateway_id=gateway.id,
        api_name="chat",
        display_name="Fictional chat",
        path="chat",
        imported_from_snapshot_id="fixture-snapshot",
    )
    await client.app.state.gateway_repository.save_model_api(model, audit)
    return gateway.id, model.id


async def test_gateway_policy_read_api_has_exact_complete_policy_results(
    client: TestClient,
) -> None:
    gateway_id, model_id = await seed_read_target(client, "Consumption")
    response = client.get(
        f"/api/v1/gateways/{gateway_id}/policy-capabilities",
        params={"resourceKind": "modelApi", "resourceId": model_id},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["gatewayId"] == gateway_id
    assert body["skuName"] == "Consumption"
    assert body["tier"] == "consumption"
    assert body["apiShape"] is None
    assert set(body["policies"]) == {
        "rate-limit-by-key",
        "quota-by-key",
        "llm-token-limit",
        "llm-emit-token-metric",
    }
    assert body["policies"]["quota-by-key"]["support"] == "unavailable"
    assert body["policies"]["llm-emit-token-metric"]["support"] == "unknown"
    assert body["policies"]["rate-limit-by-key"]["maximumRenewalPeriodSeconds"] == 300


async def test_gateway_policy_imported_schema_is_unknown_and_caller_hint_cannot_override(
    client: TestClient,
) -> None:
    gateway_id, model_id = await seed_read_target(client, "Developer")
    url = f"/api/v1/gateways/{gateway_id}/policy-capabilities"
    params = {"resourceKind": "modelApi", "resourceId": model_id}
    response = client.get(url, params=params)
    assert response.status_code == 200
    body = response.json()
    assert body["policies"]["rate-limit-by-key"]["support"] == "available"
    assert body["policies"]["llm-token-limit"]["support"] == "unknown"
    assert body["policies"]["llm-token-limit"]["reason"] == "unknownApiShape"
    hinted = client.get(url, params={**params, "apiShape": "azureOpenAi"})
    assert hinted.status_code == 422
    assert client.get(url, params=params).json() == body


async def test_gateway_policy_read_rejects_unbound_or_incomplete_target(client: TestClient) -> None:
    gateway_id, _ = await seed_read_target(client, "Developer")
    url = f"/api/v1/gateways/{gateway_id}/policy-capabilities"
    assert (
        client.get(url, params={"resourceKind": "poolModel", "resourceId": "same"}).status_code
        == 422
    )
    assert client.get(url, params={"resourceId": "same"}).status_code == 422
    assert (
        client.get(
            url,
            params={
                "resourceKind": "modelDeployment",
                "resourceId": "deployment",
                "scopeId": "endpoint",
            },
        ).status_code
        == 422
    )
    assert (
        client.get(
            url,
            params={"resourceKind": "poolModel", "resourceId": "same", "scopeId": "missing-pool"},
        ).status_code
        == 404
    )
