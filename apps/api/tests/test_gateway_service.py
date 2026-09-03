import json

import pytest
from apim_double import (
    CONTRIBUTOR_PERMISSIONS,
    RESOURCE_GROUP,
    RESOURCE_ID,
    SERVICE_NAME,
    FakeApim,
)
from conftest import build_gateway_service
from mosaic_api.domain import (
    APIM_CONTRIBUTOR_ROLE_ID,
    APIM_READER_ROLE_ID,
    AccessEvaluation,
    ApimResourceId,
    CapabilitySupport,
    Gateway,
    GatewayCreate,
    GatewayStatus,
    GatewaySyncRun,
    GatewaySyncStatus,
    GatewayUpdate,
    ManagementMode,
    new_id,
)
from mosaic_api.errors import ConflictError, NotFoundError, ValidationError
from mosaic_api.observed import AiBackendKind
from mosaic_api.repositories import InMemoryGatewayRepository
from mosaic_api.services import GatewayService
from mosaic_api.services.directory import Actor
from pydantic import ValidationError as PydanticValidationError

ACTOR = Actor(object_id="admin-object-id", tenant_id="tenant-test")


async def _register(service: GatewayService, **kwargs: str) -> Gateway:
    return await service.register(
        ACTOR, GatewayCreate(azure_resource_id=RESOURCE_ID, **kwargs)
    )


def test_resource_ids_are_validated_and_canonicalised() -> None:
    parsed = ApimResourceId.parse(
        f"/subscriptions/{ApimResourceId.parse(RESOURCE_ID).subscription_id.upper()}"
        f"/resourcegroups/{RESOURCE_GROUP}"
        f"/providers/microsoft.apimanagement/service/{SERVICE_NAME}/"
    )

    assert parsed.service_name == SERVICE_NAME
    assert parsed.canonical.startswith("/subscriptions/")
    assert "Microsoft.ApiManagement" in parsed.canonical


@pytest.mark.parametrize(
    "value",
    [
        "not-a-resource-id",
        "/subscriptions/nope/resourceGroups/rg/providers/Microsoft.ApiManagement/service/x",
        "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/providers/"
        "Microsoft.Web/sites/x",
    ],
)
def test_non_apim_resource_ids_are_rejected(value: str) -> None:
    with pytest.raises(PydanticValidationError):
        GatewayCreate(azure_resource_id=value)


async def test_registration_records_access_capabilities_and_status(
    gateway_service: GatewayService,
) -> None:
    gateway = await _register(gateway_service, name="Development gateway")

    assert gateway.name == "Development gateway"
    assert gateway.service_name == SERVICE_NAME
    assert gateway.status == GatewayStatus.CONNECTED
    assert gateway.access.can_read is True
    assert gateway.access.evaluation == AccessEvaluation.EFFECTIVE_PERMISSIONS
    assert gateway.capabilities.sku_name == "Developer"
    assert str(gateway.capabilities.gateway_url).startswith("https://")
    assert gateway.management_mode == ManagementMode.OBSERVE


async def test_read_only_access_reports_what_enrollment_will_need(
    gateway_service: GatewayService,
) -> None:
    gateway = await _register(gateway_service)

    assert gateway.access.can_write is False
    assert gateway.access.remediation is not None
    assert gateway.access.remediation.role_definition_id == APIM_CONTRIBUTOR_ROLE_ID
    assert "az role assignment create" in gateway.access.remediation.command
    assert "mosaic-managed-identity" in gateway.access.remediation.command
    assert RESOURCE_ID in gateway.access.remediation.command


async def test_contributor_access_needs_no_remediation() -> None:
    service = build_gateway_service(FakeApim(permissions=CONTRIBUTOR_PERMISSIONS))

    gateway = await service.register(ACTOR, GatewayCreate(azure_resource_id=RESOURCE_ID))

    assert gateway.access.can_write is True
    assert gateway.access.remediation is None


async def test_denied_read_produces_reader_remediation_and_unauthorized_status() -> None:
    service = build_gateway_service(FakeApim(service_status=403))

    gateway = await service.register(ACTOR, GatewayCreate(azure_resource_id=RESOURCE_ID))

    assert gateway.status == GatewayStatus.UNAUTHORIZED
    assert gateway.access.can_read is False
    assert gateway.access.remediation is not None
    assert gateway.access.remediation.role_definition_id == APIM_READER_ROLE_ID
    assert "reader role" in (gateway.access.message or "")


async def test_unreadable_role_assignments_fall_back_to_probing() -> None:
    service = build_gateway_service(FakeApim(permissions_status=403))

    gateway = await service.register(ACTOR, GatewayCreate(azure_resource_id=RESOURCE_ID))

    assert gateway.status == GatewayStatus.CONNECTED
    assert gateway.access.can_read is True
    assert gateway.access.can_write is False
    assert gateway.access.evaluation == AccessEvaluation.PROBE


async def test_registering_the_same_service_twice_conflicts(
    gateway_service: GatewayService,
) -> None:
    await _register(gateway_service)

    with pytest.raises(ConflictError):
        await _register(gateway_service)


async def test_registration_defaults_the_name_to_the_service_name(
    gateway_service: GatewayService,
) -> None:
    gateway = await _register(gateway_service)

    assert gateway.name == SERVICE_NAME


async def test_manage_mode_is_refused_while_mosaic_is_read_only(
    gateway_service: GatewayService,
) -> None:
    gateway = await _register(gateway_service)

    with pytest.raises(ValidationError):
        await gateway_service.update(
            ACTOR, gateway.id, GatewayUpdate(management_mode=ManagementMode.MANAGE)
        )


async def test_update_changes_label_and_name(gateway_service: GatewayService) -> None:
    gateway = await _register(gateway_service)

    updated = await gateway_service.update(
        ACTOR, gateway.id, GatewayUpdate(name="  Production  ", environment_label="prod")
    )

    assert updated.name == "Production"
    assert updated.environment_label == "prod"


async def test_unknown_gateway_is_a_not_found(gateway_service: GatewayService) -> None:
    with pytest.raises(NotFoundError):
        await gateway_service.get_gateway(ACTOR, "gateway_missing")


async def test_sync_requires_read_access() -> None:
    service = build_gateway_service(FakeApim(service_status=403))
    gateway = await service.register(ACTOR, GatewayCreate(azure_resource_id=RESOURCE_ID))

    with pytest.raises(ConflictError):
        await service.start_sync(ACTOR, gateway.id)


async def test_sync_collects_the_full_inventory(
    gateway_service: GatewayService,
) -> None:
    gateway = await _register(gateway_service)

    run = await gateway_service.sync_now(ACTOR, gateway.id)

    assert run.status == GatewaySyncStatus.SUCCEEDED
    assert run.errors == []
    counts = run.counts
    assert counts.apis == 2
    assert counts.ai_apis == 1
    assert counts.operations == 2
    assert counts.products == 1
    assert counts.subscriptions == 1
    assert counts.users == 1
    assert counts.groups == 1
    assert counts.backends == 1
    assert counts.named_values == 1
    assert counts.policy_documents == 3
    assert counts.policy_fragments == 1
    assert counts.unrecognized_facets == 1


async def test_sync_updates_gateway_summary_and_timestamps(
    gateway_service: GatewayService,
) -> None:
    gateway = await _register(gateway_service)
    await gateway_service.sync_now(ACTOR, gateway.id)

    refreshed = await gateway_service.get_gateway(ACTOR, gateway.id)

    assert refreshed.last_synced_at is not None
    assert refreshed.last_sync_error is None
    assert refreshed.status == GatewayStatus.CONNECTED
    assert refreshed.inventory.apis == 2
    assert refreshed.capabilities.ai_gateway_policies == CapabilitySupport.AVAILABLE


async def test_ai_surface_is_detected_from_backend_and_operations(
    gateway_service: GatewayService,
) -> None:
    gateway = await _register(gateway_service)
    await gateway_service.sync_now(ACTOR, gateway.id)

    apis = {api.name: api for api in await gateway_service.list_apis(ACTOR, gateway.id)}

    assert apis["chat-api"].ai_kind == AiBackendKind.AZURE_OPENAI
    assert apis["chat-api"].ai_signals
    assert apis["echo-api"].ai_kind == AiBackendKind.NONE
    assert apis["chat-api"].product_names == ["gold"]


async def test_policy_view_is_plain_language_and_free_of_markup(
    gateway_service: GatewayService,
) -> None:
    gateway = await _register(gateway_service)
    await gateway_service.sync_now(ACTOR, gateway.id)

    view = await gateway_service.policy_view(ACTOR, gateway.id)
    serialized = json.dumps(view.model_dump(mode="json"))

    assert "<" not in serialized
    assert "sk-live-not-a-real-key" not in serialized
    assert view.recognized_count > 0
    assert view.unrecognized_count == 1
    assert view.mosaic_managed_count == 1
    assert any(
        "10,000 tokens per minute" in facet.summary
        for document in view.documents
        for facet in document.facets
    )


async def test_unrecognized_policy_elements_are_surfaced_by_name(
    gateway_service: GatewayService,
) -> None:
    gateway = await _register(gateway_service)
    await gateway_service.sync_now(ACTOR, gateway.id)

    view = await gateway_service.policy_view(ACTOR, gateway.id)
    unrecognized = [
        element for document in view.documents for element in document.unrecognized_elements
    ]

    assert unrecognized == ["acme-custom-guard"]


async def test_named_value_secrets_are_never_stored(gateway_service: GatewayService) -> None:
    gateway = await _register(gateway_service)
    await gateway_service.sync_now(ACTOR, gateway.id)

    values = await gateway_service.list_named_values(ACTOR, gateway.id)
    serialized = json.dumps([value.model_dump(mode="json") for value in values])

    assert "sk-live-should-never-be-stored" not in serialized
    assert values[0].secret is True
    assert values[0].key_vault_secret_identifier is not None


async def test_apim_users_carry_their_entra_object_id_and_groups(
    gateway_service: GatewayService,
) -> None:
    gateway = await _register(gateway_service)
    await gateway_service.sync_now(ACTOR, gateway.id)

    users = await gateway_service.list_users(ACTOR, gateway.id)

    assert users[0].entra_object_id == "11111111-2222-3333-4444-555555555555"
    assert users[0].group_names == ["developers"]
    assert users[0].display_name == "Ada Lovelace"


async def test_subscription_scope_is_resolved_to_a_product(
    gateway_service: GatewayService,
) -> None:
    gateway = await _register(gateway_service)
    await gateway_service.sync_now(ACTOR, gateway.id)

    subscriptions = await gateway_service.list_subscriptions(ACTOR, gateway.id)

    assert subscriptions[0].scope_kind == "product"
    assert subscriptions[0].scope_name == "gold"
    assert subscriptions[0].owner_label == "user-ada"


async def test_partial_failures_degrade_rather_than_abort(fake_apim: FakeApim) -> None:
    service = build_gateway_service(fake_apim)
    gateway = await service.register(ACTOR, GatewayCreate(azure_resource_id=RESOURCE_ID))
    fake_apim.fail_always("subscriptions", 500)

    run = await service.sync_now(ACTOR, gateway.id)

    assert run.status == GatewaySyncStatus.PARTIAL
    assert any("subscriptions" in error for error in run.errors)
    assert run.counts.apis == 2
    refreshed = await service.get_gateway(ACTOR, gateway.id)
    assert refreshed.status == GatewayStatus.DEGRADED
    assert refreshed.last_sync_error is not None


async def test_resync_replaces_stale_documents(
    gateway_service: GatewayService,
    gateway_repository: InMemoryGatewayRepository,
    fake_apim: FakeApim,
) -> None:
    gateway = await _register(gateway_service)
    await gateway_service.sync_now(ACTOR, gateway.id)
    first = len(gateway_repository.observed)

    second_run = await gateway_service.sync_now(ACTOR, gateway.id)

    assert len(gateway_repository.observed) == first
    assert second_run.removed == 0
    assert all(
        entity.snapshot_id == next(iter(gateway_repository.observed.values())).snapshot_id
        for entity in gateway_repository.observed.values()
    )


async def test_deleting_a_gateway_removes_only_mosaic_state(
    gateway_service: GatewayService,
    gateway_repository: InMemoryGatewayRepository,
) -> None:
    gateway = await _register(gateway_service)
    await gateway_service.sync_now(ACTOR, gateway.id)
    assert gateway_repository.observed

    await gateway_service.delete(ACTOR, gateway.id)

    assert gateway_repository.observed == {}
    assert gateway_repository.gateways == {}
    assert gateway_repository.sync_runs == {}


async def test_mutations_are_audited(
    gateway_service: GatewayService,
    gateway_repository: InMemoryGatewayRepository,
) -> None:
    gateway = await _register(gateway_service)
    await gateway_service.update(ACTOR, gateway.id, GatewayUpdate(environment_label="dev"))
    await gateway_service.delete(ACTOR, gateway.id)

    actions = sorted(event.action for event in gateway_repository.audit_events.values())

    assert actions == ["gateway.registered", "gateway.removed", "gateway.updated"]
    assert all(
        event.actor_object_id == ACTOR.object_id
        for event in gateway_repository.audit_events.values()
    )


async def test_stale_sync_runs_are_reaped_rather_than_left_pending(
    gateway_service: GatewayService,
    gateway_repository: InMemoryGatewayRepository,
) -> None:
    gateway = await _register(gateway_service)
    orphaned = GatewaySyncRun(
        id=new_id("syncrun"), tenant_id=ACTOR.tenant_id, gateway_id=gateway.id
    )
    await gateway_repository.save_sync_run(orphaned)

    reaped = await gateway_service.reap_stale_sync_runs(ACTOR.tenant_id)
    reaped_run = await gateway_service.get_sync_run(ACTOR, orphaned.id)

    assert reaped == 1
    assert reaped_run.status == GatewaySyncStatus.FAILED
    assert any("restarted" in error for error in reaped_run.errors)


async def test_operation_policy_is_read_on_demand(gateway_service: GatewayService) -> None:
    gateway = await _register(gateway_service)

    view = await gateway_service.operation_policy(
        ACTOR, gateway.id, "chat-api", "chat-completions"
    )

    assert view.exists is False
    assert view.scope_label == "Operation: chat-api/chat-completions"


async def test_bootstrap_gateway_is_suggested_until_it_is_onboarded(
    gateway_service: GatewayService,
) -> None:
    before = await gateway_service.suggestions(ACTOR)

    assert len(before) == 1
    assert before[0].already_registered is False
    assert before[0].service_name == SERVICE_NAME

    gateway = await _register(gateway_service)
    after = await gateway_service.suggestions(ACTOR)

    assert after[0].already_registered is True
    assert after[0].gateway_id == gateway.id


async def test_no_bootstrap_hint_means_no_suggestions(fake_apim: FakeApim) -> None:
    service = build_gateway_service(fake_apim, bootstrap_resource_id=None)

    assert await service.suggestions(ACTOR) == []


async def test_identity_is_resolved_lazily_when_not_configured(fake_apim: FakeApim) -> None:
    calls: list[int] = []

    async def resolver() -> str | None:
        calls.append(1)
        return "resolved-principal-id"

    service = build_gateway_service(fake_apim, principal_id=None)
    service._identity_resolver = resolver
    service._identity_resolved = False

    gateway = await service.register(ACTOR, GatewayCreate(azure_resource_id=RESOURCE_ID))

    assert gateway.access.remediation is not None
    assert "resolved-principal-id" in gateway.access.remediation.command
    assert len(calls) == 1

    await service.preflight(ACTOR, gateway.id)
    assert len(calls) == 1


async def test_unresolvable_identity_still_produces_a_usable_command(
    fake_apim: FakeApim,
) -> None:
    async def resolver() -> str | None:
        raise RuntimeError("no managed identity available")

    service = build_gateway_service(fake_apim, principal_id=None)
    service._identity_resolver = resolver
    service._identity_resolved = False

    gateway = await service.register(ACTOR, GatewayCreate(azure_resource_id=RESOURCE_ID))

    assert gateway.access.remediation is not None
    assert "<mosaic-managed-identity-object-id>" in gateway.access.remediation.command


async def test_bootstrap_seeds_the_deployed_gateway_once(fake_apim: FakeApim) -> None:
    service = build_gateway_service(fake_apim)

    first = await service.ensure_bootstrap_gateway(ACTOR.tenant_id)
    second = await service.ensure_bootstrap_gateway(ACTOR.tenant_id)

    assert first is not None
    assert second is not None
    assert first.id == second.id
    assert first.service_name == SERVICE_NAME
    assert len(await service.list_gateways(ACTOR)) == 1


async def test_bootstrap_records_a_system_actor(
    fake_apim: FakeApim, gateway_repository: InMemoryGatewayRepository
) -> None:
    service = build_gateway_service(fake_apim, gateway_repository)

    await service.ensure_bootstrap_gateway(ACTOR.tenant_id)

    events = list(gateway_repository.audit_events.values())
    assert [event.actor_object_id for event in events] == ["system:bootstrap"]


async def test_bootstrap_does_nothing_without_a_hint(fake_apim: FakeApim) -> None:
    service = build_gateway_service(fake_apim, bootstrap_resource_id=None)

    assert await service.ensure_bootstrap_gateway(ACTOR.tenant_id) is None


async def test_bootstrap_ignores_a_malformed_hint(fake_apim: FakeApim) -> None:
    service = build_gateway_service(fake_apim, bootstrap_resource_id="/subscriptions/broken")

    assert await service.ensure_bootstrap_gateway(ACTOR.tenant_id) is None


async def test_bootstrap_still_registers_when_access_is_missing() -> None:
    service = build_gateway_service(FakeApim(service_status=403))

    gateway = await service.ensure_bootstrap_gateway(ACTOR.tenant_id)

    assert gateway is not None
    assert gateway.status == GatewayStatus.UNAUTHORIZED
    assert gateway.access.remediation is not None


async def test_tenants_cannot_see_each_others_gateways(
    gateway_service: GatewayService,
) -> None:
    await _register(gateway_service)
    other = Actor(object_id="other-admin", tenant_id="tenant-other")

    assert await gateway_service.list_gateways(other) == []
