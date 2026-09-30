import pytest
from conftest import reviewed_unpublish
from mosaic_api.domain import (
    AuditEvent,
    BindingSource,
    Entitlement,
    EntitlementBinding,
    EntitlementCreate,
    EntitlementEnforcement,
    EntitlementResource,
    EntitlementSubject,
    EntitlementSubjectKind,
    ModelAccessGrant,
    ModelAccessSettings,
    ModelAccessSnapshot,
    ModelProvider,
    PrincipalKind,
    Publication,
    PublicationStatus,
    PublishRun,
    PublishRunStatus,
    RequestEnforcement,
    mcp_server_id,
    new_id,
)
from mosaic_api.integrations.access_policy import grant_counter_identity
from mosaic_api.integrations.mcp_access_policy import (
    mcp_counter_key_expression,
    mcp_grant_counter_identity,
)
from mosaic_api.repositories import (
    InMemoryEntitlementRepository,
    InMemoryGatewayRepository,
    InMemoryModelEndpointRepository,
)
from mosaic_api.services.directory import Actor
from mosaic_api.services.publishing import DENY_ALL_POLICY, PublishingService
from test_mcp_publishing import ACTOR as MCP_ACTOR
from test_mcp_publishing import TENANT_ID as MCP_TENANT
from test_mcp_publishing import Harness as McpHarness
from test_mcp_publishing import request_limits

TENANT = "11111111-1111-1111-1111-111111111111"
ACTOR = Actor(object_id="admin-object-id", tenant_id=TENANT)


def _audit(resource_id: str) -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id=TENANT,
        action="entitlement.saved",
        resource_type="entitlement",
        resource_id=resource_id,
        actor_object_id=ACTOR.object_id,
    )


def _publication() -> Publication:
    return Publication(
        id="publication-model",
        tenant_id=TENANT,
        gateway_id="gateway",
        model_endpoint_id="endpoint",
        model_api_id="model-api",
        deployment_name="gpt-4o-prod",
        provider=ModelProvider.AZURE_OPENAI,
        display_name="Example model",
        api_name="mosaic-model",
        api_path="mosaic/model",
        backend_name="mosaic-model",
        fragment_name="mosaic-model",
        product_name="mosaic-model",
        subscription_name="mosaic-bootstrap",
        shape_version="1.0",
    )


def _grant(
    entitlement_id: str,
    *,
    subject_kind: EntitlementSubjectKind = EntitlementSubjectKind.USER,
    subscription_name: str | None = None,
) -> ModelAccessGrant:
    return ModelAccessGrant(
        entitlement_id=entitlement_id,
        subject=EntitlementSubject(kind=subject_kind, id=f"principal-{entitlement_id}"),
        object_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        display_name=entitlement_id,
        subscription_name=subscription_name,
        enabled=True,
        intent_digest=f"intent-{entitlement_id}",
    )


async def _save_model_entitlement(
    repository: InMemoryEntitlementRepository,
    entitlement_id: str,
    subject_kind: EntitlementSubjectKind,
) -> None:
    await repository.save_entitlement(
        Entitlement(
            id=entitlement_id,
            tenant_id=TENANT,
            subject=EntitlementSubject(kind=subject_kind, id=f"principal-{entitlement_id}"),
            resource=EntitlementResource(kind="modelApi", id="model-api"),
        ),
        _audit(entitlement_id),
    )


async def test_model_projection_records_gateway_attribution_for_direct_and_group_grants() -> None:
    entitlements = InMemoryEntitlementRepository()
    service = PublishingService(
        InMemoryGatewayRepository(),
        endpoint_repository=InMemoryModelEndpointRepository(),
        entitlement_repository=entitlements,
        client_factory=lambda _resource: None,  # type: ignore[arg-type,return-value]
        writer_factory=lambda _resource: None,  # type: ignore[arg-type,return-value]
    )
    publication = _publication()
    direct = _grant("grant-direct", subscription_name="mosaic-grant-direct")
    group = _grant("grant-group", subject_kind=EntitlementSubjectKind.SECURITY_GROUP)
    await _save_model_entitlement(entitlements, direct.entitlement_id, direct.subject.kind)
    await _save_model_entitlement(entitlements, group.entitlement_id, group.subject.kind)

    await service._project_bindings(
        publication,
        ModelAccessSnapshot(
            version=1,
            settings=ModelAccessSettings(),
            audience="22222222-2222-2222-2222-222222222222",
            grants=[direct, group],
        ),
        PublishRun(
            id="run-model",
            tenant_id=TENANT,
            publication_id=publication.id,
            gateway_id=publication.gateway_id,
            plan_id="plan",
            plan_digest="digest",
            actor_object_id=ACTOR.object_id,
        ),
    )

    records = {
        item.id: item
        for item in await entitlements.list_entitlements(
            TENANT, resource_id=publication.model_api_id
        )
    }
    direct_binding = records[direct.entitlement_id].binding
    group_binding = records[group.entitlement_id].binding
    assert direct_binding is not None
    assert direct_binding.attribution_key == grant_counter_identity(publication, direct)
    assert direct_binding.attribution_per_member is False
    assert direct_binding.apim_subscription_name == direct.subscription_name
    assert group_binding is not None
    assert group_binding.attribution_key == grant_counter_identity(publication, group)
    assert group_binding.attribution_per_member is True
    assert group_binding.apim_subscription_name is None


async def test_mcp_apply_projects_bindings_and_unpublish_clears_them() -> None:
    harness = McpHarness()
    await harness.setup()
    publication_id = await harness.create()
    publication = await harness.service.get_publication(MCP_ACTOR, publication_id)
    direct_principal = await harness.principal(
        "principal-direct", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    )
    group_principal = await harness.principal(
        "principal-group",
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        kind=PrincipalKind.SECURITY_GROUP,
    )
    direct = await harness.entitlement(
        "grant-direct",
        EntitlementSubjectKind.USER,
        direct_principal.id,
        publication.mcp_server_id,
        enforcement=request_limits(),
    )
    group = await harness.entitlement(
        "grant-group",
        EntitlementSubjectKind.SECURITY_GROUP,
        group_principal.id,
        publication.mcp_server_id,
        enforcement=EntitlementEnforcement(
            requests=RequestEnforcement(
                counter_key_expression="@('mcp-test')",
                calls=5,
                renewal_period_seconds=60,
            )
        ),
    )

    plan = await harness.service.plan(MCP_ACTOR, publication_id)
    await harness.service.apply(MCP_ACTOR, publication_id, plan.id)
    await harness.service.wait_for_idle()

    applied = await harness.service.get_publication(MCP_ACTOR, publication_id)
    records = {
        item.id: item
        for item in await harness.entitlement_repository.list_entitlements(
            MCP_TENANT, resource_id=mcp_server_id(MCP_TENANT, harness.gateway_id, applied.api_name)
        )
    }
    direct_binding = records[direct.id].binding
    group_binding = records[group.id].binding
    assert applied.applied_access is not None
    direct_grant = next(
        grant for grant in applied.applied_access.grants if grant.entitlement_id == direct.id
    )
    group_grant = next(
        grant for grant in applied.applied_access.grants if grant.entitlement_id == group.id
    )
    assert direct_binding is not None
    assert direct_binding.source == BindingSource.ORCHESTRATED
    assert direct_binding.apim_subscription_name is None
    assert direct_binding.counter_key_expression == mcp_counter_key_expression(
        applied, direct_grant
    )
    assert direct_binding.attribution_key == mcp_grant_counter_identity(applied, direct_grant)
    assert direct_binding.attribution_per_member is False
    assert group_binding is not None
    assert group_binding.attribution_key == mcp_grant_counter_identity(applied, group_grant)
    assert group_binding.attribution_per_member is True

    await reviewed_unpublish(harness.service, MCP_ACTOR, publication_id)
    await harness.service.wait_for_idle()

    cleared = await harness.entitlement_repository.list_entitlements(
        MCP_TENANT, resource_id=applied.mcp_server_id
    )
    assert all(item.binding is None for item in cleared)


class _UnavailableEntitlementWrites:
    def __init__(self) -> None:
        self.attempts = 0

    async def __call__(self, *_args: object, **_kwargs: object) -> Entitlement:
        self.attempts += 1
        raise RuntimeError("Entitlement storage is unavailable")


async def _mcp_publication_with_grant(harness: McpHarness) -> tuple[str, Entitlement]:
    await harness.setup()
    publication_id = await harness.create()
    publication = await harness.service.get_publication(MCP_ACTOR, publication_id)
    principal = await harness.principal("principal-direct", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    entitlement = await harness.entitlement(
        "grant-direct",
        EntitlementSubjectKind.USER,
        principal.id,
        publication.mcp_server_id,
        enforcement=request_limits(),
    )
    return publication_id, entitlement


def _last_mcp_policy_write(harness: McpHarness) -> str:
    writes = [
        call["body"]["properties"]["value"]
        for call in harness.apim.http_calls
        if call["method"] == "PUT" and call["path"] == "apis/mosaic-mcp-orders-mcp/policies/policy"
    ]
    return writes[-1]


async def test_mcp_binding_write_failure_leaves_a_successful_apply_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = McpHarness()
    publication_id, entitlement = await _mcp_publication_with_grant(harness)
    plan = await harness.service.plan(MCP_ACTOR, publication_id)
    unavailable = _UnavailableEntitlementWrites()
    monkeypatch.setattr(harness.entitlement_repository, "save_entitlement", unavailable)

    run = await harness.service.apply(MCP_ACTOR, publication_id, plan.id)
    await harness.service.wait_for_idle()

    completed = await harness.service.get_run(MCP_ACTOR, publication_id, run.id)
    published = await harness.service.get_publication(MCP_ACTOR, publication_id)
    assert unavailable.attempts == 1
    assert completed.status == PublishRunStatus.SUCCEEDED
    assert published.status == PublicationStatus.PUBLISHED
    assert published.access_state == "applied"
    assert published.applied_access is not None
    assert all(grant.enabled for grant in published.applied_access.grants)
    assert _last_mcp_policy_write(harness) != DENY_ALL_POLICY
    assert await harness.gateway_repository.get_publication_lock(MCP_TENANT, publication_id) is None
    stored = await harness.entitlement_repository.get_entitlement(MCP_TENANT, entitlement.id)
    assert stored is not None
    assert stored.binding is None


async def test_mcp_binding_clear_failure_leaves_a_successful_unpublish_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = McpHarness()
    publication_id, entitlement = await _mcp_publication_with_grant(harness)
    plan = await harness.service.plan(MCP_ACTOR, publication_id)
    await harness.service.apply(MCP_ACTOR, publication_id, plan.id)
    await harness.service.wait_for_idle()
    unavailable = _UnavailableEntitlementWrites()
    monkeypatch.setattr(harness.entitlement_repository, "save_entitlement", unavailable)

    run = await reviewed_unpublish(harness.service, MCP_ACTOR, publication_id)
    await harness.service.wait_for_idle()

    completed = await harness.service.get_run(MCP_ACTOR, publication_id, run.id)
    unpublished = await harness.service.get_publication(MCP_ACTOR, publication_id)
    assert unavailable.attempts == 1
    assert completed.status == PublishRunStatus.SUCCEEDED
    assert unpublished.status == PublicationStatus.DRAFT
    assert unpublished.access_state == "applied"
    assert await harness.gateway_repository.get_publication_lock(MCP_TENANT, publication_id) is None
    stored = await harness.entitlement_repository.get_entitlement(MCP_TENANT, entitlement.id)
    assert stored is not None
    # The clear failed, so the stale binding stays until the server is next applied.
    assert stored.binding is not None


@pytest.mark.parametrize(
    "binding",
    [
        EntitlementBinding(gateway_id="gateway", attribution_key="grant-counter"),
        EntitlementBinding(gateway_id="gateway", attribution_per_member=True),
    ],
)
def test_manual_bindings_cannot_set_gateway_attribution(binding: EntitlementBinding) -> None:
    with pytest.raises(ValueError, match="Gateway attribution is recorded only"):
        EntitlementCreate(
            subject=EntitlementSubject(kind=EntitlementSubjectKind.USER, id="principal"),
            resource=EntitlementResource(kind="modelApi", id="model-api"),
            binding=binding,
        )
