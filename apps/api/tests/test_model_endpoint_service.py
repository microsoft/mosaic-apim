"""Model endpoint onboarding: access verification, discovery, and runtime readiness."""

import pytest
from aoai_double import (
    AI_ENDPOINT,
    AI_RESOURCE_ID,
    AI_SUBSCRIPTION_ID,
    AZURE_OPENAI_USER_ROLE_ID,
    COGNITIVE_SERVICES_USER_ROLE_ID,
    PARTIAL_PERMISSIONS,
    FakeCognitiveServices,
    role_assignment,
)
from apim_double import APIM_PRINCIPAL_ID, RESOURCE_ID, SERVICE_NAME
from conftest import build_endpoint_service
from mosaic_api.domain import (
    READER_ROLE_ID,
    AccessEvaluation,
    CognitiveServicesResourceId,
    EndpointAuthMode,
    Gateway,
    GatewayCapabilities,
    GatewaySyncStatus,
    ModelEndpointCreate,
    ModelEndpointStatus,
    ModelEndpointSyncRun,
    ModelEndpointUpdate,
    ModelProvider,
    RuntimeAccessEvaluation,
    SuggestionSource,
    new_id,
    utc_now,
)
from mosaic_api.errors import ConflictError, NotFoundError, ValidationError
from mosaic_api.observed import AiBackendKind, ObservedBackend
from mosaic_api.repositories import InMemoryGatewayRepository, InMemoryModelEndpointRepository
from mosaic_api.services.directory import Actor

ACTOR = Actor(object_id="admin-object-id", tenant_id="tenant-test")


def _create(**kwargs: object) -> ModelEndpointCreate:
    payload: dict[str, object] = {"azure_resource_id": AI_RESOURCE_ID}
    payload.update(kwargs)
    return ModelEndpointCreate.model_validate(payload)


def _gateway(*, principal_id: str | None = APIM_PRINCIPAL_ID) -> Gateway:
    return Gateway(
        id=new_id("gateway"),
        tenant_id=ACTOR.tenant_id,
        name=SERVICE_NAME,
        azure_resource_id=RESOURCE_ID,
        subscription_id=AI_SUBSCRIPTION_ID,
        resource_group="rg-contoso-dev",
        service_name=SERVICE_NAME,
        capabilities=GatewayCapabilities(
            principal_id=principal_id, identity_observed=True
        ),
    )


class TestResourceId:
    def test_canonicalises_account(self) -> None:
        parsed = CognitiveServicesResourceId.parse(AI_RESOURCE_ID.upper())
        assert parsed.canonical.endswith("/accounts/CONTOSO-AOAI")
        assert parsed.subscription_id == AI_SUBSCRIPTION_ID

    def test_project_resolves_up_to_account(self) -> None:
        parsed = CognitiveServicesResourceId.parse(f"{AI_RESOURCE_ID}/projects/team-a")
        assert parsed.project_name == "team-a"
        # Deployments are never children of a project.
        assert parsed.account_scope == AI_RESOURCE_ID
        assert parsed.canonical == f"{AI_RESOURCE_ID}/projects/team-a"

    def test_rejects_other_providers(self) -> None:
        with pytest.raises(ValueError, match=r"Microsoft\.CognitiveServices"):
            CognitiveServicesResourceId.parse(RESOURCE_ID)


class TestRegistration:
    @pytest.mark.asyncio
    async def test_registers_and_preflights(
        self, endpoint_service, fake_aoai: FakeCognitiveServices
    ) -> None:
        endpoint = await endpoint_service.register(ACTOR, _create())

        assert endpoint.status == ModelEndpointStatus.CONNECTED
        assert endpoint.access.can_read is True
        assert endpoint.access.evaluation == AccessEvaluation.EFFECTIVE_PERMISSIONS
        assert endpoint.provider == ModelProvider.AZURE_OPENAI
        assert str(endpoint.endpoint) == AI_ENDPOINT
        assert endpoint.capabilities.kind == "OpenAI"

    @pytest.mark.asyncio
    async def test_duplicate_registration_conflicts(self, endpoint_service) -> None:
        await endpoint_service.register(ACTOR, _create())
        with pytest.raises(ConflictError):
            await endpoint_service.register(ACTOR, _create())

    @pytest.mark.asyncio
    async def test_missing_permissions_reports_reader_remediation(
        self, gateway_repository: InMemoryGatewayRepository
    ) -> None:
        fake = FakeCognitiveServices(permissions=PARTIAL_PERMISSIONS)
        service = build_endpoint_service(fake, gateway_repository=gateway_repository)

        endpoint = await service.register(ACTOR, _create())

        assert endpoint.status == ModelEndpointStatus.UNAUTHORIZED
        assert endpoint.access.can_read is False
        remediation = endpoint.access.remediation
        assert remediation is not None
        assert remediation.role_definition_id == READER_ROLE_ID
        assert remediation.scope == AI_RESOURCE_ID
        assert "az role assignment create" in remediation.command
        assert "Microsoft.CognitiveServices/accounts/deployments/read" in (
            endpoint.access.missing_actions
        )

    @pytest.mark.asyncio
    async def test_remediation_offers_least_privilege_custom_role(
        self, gateway_repository: InMemoryGatewayRepository
    ) -> None:
        fake = FakeCognitiveServices(permissions=PARTIAL_PERMISSIONS)
        service = build_endpoint_service(fake, gateway_repository=gateway_repository)

        endpoint = await service.register(ACTOR, _create())
        remediation = endpoint.access.remediation
        assert remediation is not None
        definition = remediation.custom_role_definition
        assert definition is not None
        permissions = definition["properties"]["permissions"][0]
        # The whole point of the custom role is that it grants no inference and no key access.
        assert permissions["dataActions"] == []
        assert "Microsoft.CognitiveServices/accounts/deployments/read" in permissions["actions"]
        assert not any("listkeys" in action.casefold() for action in permissions["actions"])

    @pytest.mark.asyncio
    async def test_unreachable_account_is_not_connected(
        self, gateway_repository: InMemoryGatewayRepository
    ) -> None:
        fake = FakeCognitiveServices(account_status=404)
        service = build_endpoint_service(fake, gateway_repository=gateway_repository)

        endpoint = await service.register(ACTOR, _create())
        assert endpoint.status == ModelEndpointStatus.UNREACHABLE
        assert endpoint.access.can_read is False

    @pytest.mark.asyncio
    async def test_unevaluable_permissions_degrade_to_probe(
        self, gateway_repository: InMemoryGatewayRepository
    ) -> None:
        fake = FakeCognitiveServices(permissions_status=403)
        service = build_endpoint_service(fake, gateway_repository=gateway_repository)

        endpoint = await service.register(ACTOR, _create())
        # The account read succeeded, so access is real but unconfirmed.
        assert endpoint.status == ModelEndpointStatus.CONNECTED
        assert endpoint.access.can_read is True
        assert endpoint.access.evaluation == AccessEvaluation.PROBE


class TestKeyBasedEndpoints:
    @pytest.mark.asyncio
    async def test_registers_with_secret_uri_only(self, endpoint_service) -> None:
        endpoint = await endpoint_service.register(
            ACTOR,
            ModelEndpointCreate(
                endpoint="https://models.example.com/v1",
                credential_secret_uri="https://kv-contoso.vault.azure.net/secrets/model-key",
            ),
        )
        assert endpoint.auth_mode == EndpointAuthMode.API_KEY
        assert endpoint.provider == ModelProvider.OPENAI_COMPATIBLE
        assert endpoint.credential_reference_id is not None

    @pytest.mark.asyncio
    async def test_serialised_endpoint_never_carries_a_secret(self, endpoint_service) -> None:
        endpoint = await endpoint_service.register(
            ACTOR,
            ModelEndpointCreate(
                endpoint="https://models.example.com/v1",
                credential_secret_uri="https://kv-contoso.vault.azure.net/secrets/model-key",
            ),
        )
        payload = endpoint.model_dump_json()
        assert "vault.azure.net" not in payload
        assert "secretUri" not in payload

    def test_rejects_key_endpoint_without_secret(self) -> None:
        with pytest.raises(ValueError, match="Key Vault secret URI"):
            ModelEndpointCreate(endpoint="https://models.example.com/v1")

    def test_rejects_azure_provider_without_resource_id(self) -> None:
        with pytest.raises(ValueError, match="registered by resource ID"):
            ModelEndpointCreate(
                endpoint="https://contoso.openai.azure.com",
                provider=ModelProvider.AZURE_OPENAI,
                credential_secret_uri="https://kv.vault.azure.net/secrets/k",
            )

    def test_rejects_endpoint_with_neither_identifier(self) -> None:
        with pytest.raises(ValueError, match="Azure resource ID"):
            ModelEndpointCreate()

    @pytest.mark.asyncio
    async def test_key_endpoint_cannot_be_synced_yet(self, endpoint_service) -> None:
        endpoint = await endpoint_service.register(
            ACTOR,
            ModelEndpointCreate(
                endpoint="https://models.example.com/v1",
                credential_secret_uri="https://kv-contoso.vault.azure.net/secrets/model-key",
            ),
        )
        run = await endpoint_service.sync_now(ACTOR, endpoint.id)
        assert run.status == GatewaySyncStatus.FAILED

    @pytest.mark.asyncio
    async def test_rotating_a_key_on_a_managed_identity_endpoint_is_rejected(
        self, endpoint_service
    ) -> None:
        endpoint = await endpoint_service.register(ACTOR, _create())
        with pytest.raises(ValidationError):
            await endpoint_service.update(
                ACTOR,
                endpoint.id,
                ModelEndpointUpdate(
                    credential_secret_uri="https://kv.vault.azure.net/secrets/k"
                ),
            )


class TestDiscovery:
    @pytest.mark.asyncio
    async def test_discovers_deployments_and_models(self, endpoint_service) -> None:
        endpoint = await endpoint_service.register(ACTOR, _create())
        run = await endpoint_service.sync_now(ACTOR, endpoint.id)

        assert run.status == GatewaySyncStatus.SUCCEEDED
        assert run.counts.deployments == 2
        assert run.counts.available_models == 2

        deployments = await endpoint_service.list_deployments(ACTOR, endpoint.id)
        names = [item.deployment_name for item in deployments]
        assert names == ["gpt-4o-prod", "text-embedding-3-large"]
        chat = deployments[0]
        assert chat.model_name == "gpt-4o"
        assert chat.model_version == "2024-11-20"
        assert chat.sku_capacity == 50
        assert chat.request_paths == ["/chat/completions"]

    @pytest.mark.asyncio
    async def test_available_models_carry_lifecycle(self, endpoint_service) -> None:
        endpoint = await endpoint_service.register(ACTOR, _create())
        await endpoint_service.sync_now(ACTOR, endpoint.id)

        models = await endpoint_service.list_available_models(ACTOR, endpoint.id)
        deprecated = [m for m in models if m.lifecycle_status == "Deprecated"]
        assert [m.model_name for m in deprecated] == ["gpt-35-turbo"]

    @pytest.mark.asyncio
    async def test_resync_is_idempotent(self, endpoint_service) -> None:
        endpoint = await endpoint_service.register(ACTOR, _create())
        await endpoint_service.sync_now(ACTOR, endpoint.id)
        first = await endpoint_service.list_deployments(ACTOR, endpoint.id)
        second_run = await endpoint_service.sync_now(ACTOR, endpoint.id)
        second = await endpoint_service.list_deployments(ACTOR, endpoint.id)

        assert second_run.removed == 0
        assert [item.id for item in first] == [item.id for item in second]

    @pytest.mark.asyncio
    async def test_removed_deployment_is_swept(
        self, endpoint_service, fake_aoai: FakeCognitiveServices
    ) -> None:
        endpoint = await endpoint_service.register(ACTOR, _create())
        await endpoint_service.sync_now(ACTOR, endpoint.id)

        fake_aoai.deployments = fake_aoai.deployments[:1]
        run = await endpoint_service.sync_now(ACTOR, endpoint.id)

        assert run.removed == 1
        remaining = await endpoint_service.list_deployments(ACTOR, endpoint.id)
        assert [item.deployment_name for item in remaining] == ["gpt-4o-prod"]

    @pytest.mark.asyncio
    async def test_failed_read_is_not_mistaken_for_deletion(
        self, endpoint_service, fake_aoai: FakeCognitiveServices
    ) -> None:
        endpoint = await endpoint_service.register(ACTOR, _create())
        await endpoint_service.sync_now(ACTOR, endpoint.id)

        fake_aoai.fail_always("deployments", 429)
        run = await endpoint_service.sync_now(ACTOR, endpoint.id)

        assert run.status == GatewaySyncStatus.PARTIAL
        # The deployments MOSAIC could not re-read must survive.
        survivors = await endpoint_service.list_deployments(ACTOR, endpoint.id)
        assert len(survivors) == 2

    @pytest.mark.asyncio
    async def test_sync_blocked_without_read_access(
        self, gateway_repository: InMemoryGatewayRepository
    ) -> None:
        fake = FakeCognitiveServices(permissions=PARTIAL_PERMISSIONS)
        service = build_endpoint_service(fake, gateway_repository=gateway_repository)
        endpoint = await service.register(ACTOR, _create())

        with pytest.raises(ConflictError):
            await service.start_sync(ACTOR, endpoint.id)

    @pytest.mark.asyncio
    async def test_endpoint_removed_mid_sync_discards_snapshot(
        self,
        endpoint_service,
        endpoint_repository: InMemoryModelEndpointRepository,
    ) -> None:
        endpoint = await endpoint_service.register(ACTOR, _create())
        run = ModelEndpointSyncRun(
            id=new_id("syncrun"), tenant_id=ACTOR.tenant_id, endpoint_id=endpoint.id
        )
        # The administrator removes the endpoint while ARM is being read. Writing back the copy
        # captured when the sync started would resurrect it.
        endpoint_repository.endpoints.pop(endpoint.id)

        await endpoint_service._collect_and_persist(endpoint, run, utc_now())

        assert endpoint_repository.observed == {}
        assert endpoint.id not in endpoint_repository.endpoints

    @pytest.mark.asyncio
    async def test_unknown_endpoint_is_not_found(self, endpoint_service) -> None:
        with pytest.raises(NotFoundError):
            await endpoint_service.get_endpoint(ACTOR, "endpoint_missing")


class TestGatewayRuntimeAccess:
    @pytest.mark.asyncio
    async def test_reports_missing_role_with_remediation(
        self, endpoint_service, gateway_repository: InMemoryGatewayRepository
    ) -> None:
        gateway = _gateway()
        await gateway_repository.record_gateway_state(gateway)

        endpoint = await endpoint_service.register(ACTOR, _create())

        assert len(endpoint.runtime_access) == 1
        access = endpoint.runtime_access[0]
        assert access.can_invoke is False
        assert access.evaluation == RuntimeAccessEvaluation.ROLE_ASSIGNMENTS
        # kind is OpenAI, so the runtime role is Cognitive Services OpenAI User.
        assert access.required_role_definition_id == AZURE_OPENAI_USER_ROLE_ID
        assert access.remediation is not None
        assert APIM_PRINCIPAL_ID in access.remediation.command

    @pytest.mark.asyncio
    async def test_reports_granted_role(
        self,
        endpoint_service,
        gateway_repository: InMemoryGatewayRepository,
        fake_aoai: FakeCognitiveServices,
    ) -> None:
        await gateway_repository.record_gateway_state(_gateway())
        fake_aoai.role_assignments = [
            role_assignment(AZURE_OPENAI_USER_ROLE_ID, AI_RESOURCE_ID, APIM_PRINCIPAL_ID)
        ]

        endpoint = await endpoint_service.register(ACTOR, _create())
        access = endpoint.runtime_access[0]

        assert access.can_invoke is True
        assert access.inherited is False
        assert access.remediation is None

    @pytest.mark.asyncio
    async def test_inherited_assignment_is_labelled(
        self,
        endpoint_service,
        gateway_repository: InMemoryGatewayRepository,
        fake_aoai: FakeCognitiveServices,
    ) -> None:
        await gateway_repository.record_gateway_state(_gateway())
        # `$filter=principalId eq` returns assignments above the scope too. A subscription-wide
        # grant does confer access, but must not be shown as a direct assignment.
        fake_aoai.role_assignments = [
            role_assignment(
                AZURE_OPENAI_USER_ROLE_ID,
                f"/subscriptions/{AI_SUBSCRIPTION_ID}",
                APIM_PRINCIPAL_ID,
            )
        ]

        endpoint = await endpoint_service.register(ACTOR, _create())
        access = endpoint.runtime_access[0]

        assert access.can_invoke is True
        assert access.inherited is True
        assert access.assignment_scope == f"/subscriptions/{AI_SUBSCRIPTION_ID}"
        assert "inherited" in (access.message or "")

    @pytest.mark.asyncio
    async def test_direct_assignment_wins_over_an_inherited_one(
        self,
        endpoint_service,
        gateway_repository: InMemoryGatewayRepository,
        fake_aoai: FakeCognitiveServices,
    ) -> None:
        await gateway_repository.record_gateway_state(_gateway())
        # The broader subscription grant is returned first. The direct assignment on the endpoint
        # must still win, or a correctly-assigned endpoint reads as merely inheriting the role.
        fake_aoai.role_assignments = [
            role_assignment(
                AZURE_OPENAI_USER_ROLE_ID,
                f"/subscriptions/{AI_SUBSCRIPTION_ID}",
                APIM_PRINCIPAL_ID,
            ),
            role_assignment(AZURE_OPENAI_USER_ROLE_ID, AI_RESOURCE_ID, APIM_PRINCIPAL_ID),
        ]

        endpoint = await endpoint_service.register(ACTOR, _create())
        access = endpoint.runtime_access[0]

        assert access.can_invoke is True
        assert access.inherited is False
        assert access.assignment_scope == AI_RESOURCE_ID

    @pytest.mark.asyncio
    async def test_assignment_below_the_endpoint_does_not_grant_access(
        self,
        endpoint_service,
        gateway_repository: InMemoryGatewayRepository,
        fake_aoai: FakeCognitiveServices,
    ) -> None:
        await gateway_repository.record_gateway_state(_gateway())
        # RBAC inherits downward only. A grant on one project confers nothing at the account, so
        # reporting it as access would claim the gateway can call every model on the account.
        fake_aoai.role_assignments = [
            role_assignment(
                AZURE_OPENAI_USER_ROLE_ID,
                f"{AI_RESOURCE_ID}/projects/team-a",
                APIM_PRINCIPAL_ID,
            )
        ]

        endpoint = await endpoint_service.register(ACTOR, _create())
        access = endpoint.runtime_access[0]

        assert access.can_invoke is False
        assert access.remediation is not None
        assert "narrower" in (access.message or "")
        assert "projects/team-a" in (access.message or "")

    @pytest.mark.asyncio
    async def test_unobserved_gateway_identity_is_not_a_denial(
        self, endpoint_service, gateway_repository: InMemoryGatewayRepository
    ) -> None:
        # A gateway registered before MOSAIC recorded identities has no principal ID, which is not
        # evidence that it lacks one.
        gateway = _gateway(principal_id=None)
        await gateway_repository.record_gateway_state(
            gateway.model_copy(
                update={
                    "capabilities": gateway.capabilities.model_copy(
                        update={"identity_observed": False}
                    )
                }
            )
        )

        endpoint = await endpoint_service.register(ACTOR, _create())
        access = endpoint.runtime_access[0]

        assert access.evaluation == RuntimeAccessEvaluation.NOT_EVALUATED
        assert access.can_invoke is False
        assert "has not read" in (access.message or "")

    @pytest.mark.asyncio
    async def test_wrong_role_does_not_count(
        self,
        endpoint_service,
        gateway_repository: InMemoryGatewayRepository,
        fake_aoai: FakeCognitiveServices,
    ) -> None:
        await gateway_repository.record_gateway_state(_gateway())
        # Reader lets the gateway *see* the account but never call a model.
        fake_aoai.role_assignments = [
            role_assignment(READER_ROLE_ID, AI_RESOURCE_ID, APIM_PRINCIPAL_ID)
        ]

        endpoint = await endpoint_service.register(ACTOR, _create())
        assert endpoint.runtime_access[0].can_invoke is False

    @pytest.mark.asyncio
    async def test_gateway_without_identity_is_distinct_from_missing_role(
        self, endpoint_service, gateway_repository: InMemoryGatewayRepository
    ) -> None:
        await gateway_repository.record_gateway_state(_gateway(principal_id=None))

        endpoint = await endpoint_service.register(ACTOR, _create())
        access = endpoint.runtime_access[0]

        assert access.evaluation == RuntimeAccessEvaluation.NO_GATEWAY_IDENTITY
        # The fix is to enable an identity, not to assign a role, so no command is offered.
        assert access.remediation is None
        assert "no managed identity" in (access.message or "")

    @pytest.mark.asyncio
    async def test_unreadable_assignments_are_not_evaluated(
        self, gateway_repository: InMemoryGatewayRepository
    ) -> None:
        await gateway_repository.record_gateway_state(_gateway())
        fake = FakeCognitiveServices(role_assignments_status=403)
        service = build_endpoint_service(fake, gateway_repository=gateway_repository)

        endpoint = await service.register(ACTOR, _create())
        access = endpoint.runtime_access[0]

        # Not knowing must never be reported as a denial.
        assert access.evaluation == RuntimeAccessEvaluation.NOT_EVALUATED
        assert access.can_invoke is False
        assert "cannot confirm" in (access.message or "")

    @pytest.mark.asyncio
    async def test_ai_services_account_requires_cognitive_services_user(
        self, gateway_repository: InMemoryGatewayRepository
    ) -> None:
        await gateway_repository.record_gateway_state(_gateway())
        fake = FakeCognitiveServices(kind="AIServices")
        service = build_endpoint_service(fake, gateway_repository=gateway_repository)

        endpoint = await service.register(ACTOR, _create())
        access = endpoint.runtime_access[0]

        assert access.required_role_definition_id == COGNITIVE_SERVICES_USER_ROLE_ID
        assert endpoint.provider == ModelProvider.AZURE_AI_FOUNDRY


class TestSuggestions:
    @pytest.mark.asyncio
    async def test_subscription_scan_skips_non_model_kinds(self, endpoint_service) -> None:
        view = await endpoint_service.suggestions(ACTOR)

        scanned = [s for s in view.suggestions if s.source == SuggestionSource.SUBSCRIPTION_SCAN]
        assert [s.account_name for s in scanned] == ["contoso-aoai"]
        assert view.subscriptions_scanned == 1

    @pytest.mark.asyncio
    async def test_forbidden_subscription_degrades_with_remediation(
        self, endpoint_service, fake_aoai: FakeCognitiveServices
    ) -> None:
        fake_aoai.forbidden_subscriptions = {AI_SUBSCRIPTION_ID}

        view = await endpoint_service.suggestions(ACTOR)

        assert view.subscriptions_scanned == 0
        assert len(view.scan_issues) == 1
        issue = view.scan_issues[0]
        assert issue.subscription_id == AI_SUBSCRIPTION_ID
        assert issue.remediation is not None
        assert issue.remediation.scope == f"/subscriptions/{AI_SUBSCRIPTION_ID}"
        assert issue.remediation.role_definition_id == READER_ROLE_ID

    @pytest.mark.asyncio
    async def test_registered_endpoints_are_marked(self, endpoint_service) -> None:
        await endpoint_service.register(ACTOR, _create())

        view = await endpoint_service.suggestions(ACTOR)
        scanned = [s for s in view.suggestions if s.source == SuggestionSource.SUBSCRIPTION_SCAN]
        assert scanned[0].already_registered is True
        assert scanned[0].model_endpoint_id is not None

    @pytest.mark.asyncio
    async def test_suggests_ai_backends_observed_in_a_gateway(
        self, endpoint_service, gateway_repository: InMemoryGatewayRepository
    ) -> None:
        gateway = _gateway()
        await gateway_repository.record_gateway_state(gateway)
        await gateway_repository.replace_observed(
            ACTOR.tenant_id,
            gateway.id,
            [
                ObservedBackend(
                    id=new_id("obsBackend"),
                    tenant_id=ACTOR.tenant_id,
                    gateway_id=gateway.id,
                    snapshot_id="snap-1",
                    name="aoai-backend",
                    url="https://other-account.openai.azure.com/openai",
                    ai_kind=AiBackendKind.AZURE_OPENAI,
                ),
                ObservedBackend(
                    id=new_id("obsBackend"),
                    tenant_id=ACTOR.tenant_id,
                    gateway_id=gateway.id,
                    snapshot_id="snap-1",
                    name="plain-backend",
                    url="https://contoso-fn.azurewebsites.net/api",
                ),
            ],
            "snap-1",
        )

        view = await endpoint_service.suggestions(ACTOR)
        from_gateway = [
            s for s in view.suggestions if s.source == SuggestionSource.GATEWAY_BACKEND
        ]

        # Only the AI host is offered; the Functions backend is not a model endpoint.
        assert len(from_gateway) == 1
        assert str(from_gateway[0].endpoint) == "https://other-account.openai.azure.com/"
        assert from_gateway[0].provider == ModelProvider.AZURE_OPENAI
        assert SERVICE_NAME in from_gateway[0].reason

    @pytest.mark.asyncio
    async def test_scan_absent_when_no_scanner(
        self, fake_aoai: FakeCognitiveServices, gateway_repository: InMemoryGatewayRepository
    ) -> None:
        service = build_endpoint_service(
            fake_aoai, gateway_repository=gateway_repository, scanner=False
        )
        view = await service.suggestions(ACTOR)
        assert view.suggestions == []
        assert view.subscriptions_scanned == 0


class TestStaleRuns:
    @pytest.mark.asyncio
    async def test_orphaned_runs_are_reaped(
        self, endpoint_service, endpoint_repository: InMemoryModelEndpointRepository
    ) -> None:
        endpoint = await endpoint_service.register(ACTOR, _create())
        await endpoint_service.sync_now(ACTOR, endpoint.id)
        run = (await endpoint_service.list_sync_runs(ACTOR, endpoint.id))[0]
        endpoint_repository.sync_runs[run.id] = run.model_copy(
            update={"status": GatewaySyncStatus.RUNNING}
        )

        reaped = await endpoint_service.reap_stale_sync_runs(ACTOR.tenant_id)

        assert reaped == 1
        reaped_run = await endpoint_service.get_sync_run(ACTOR, run.id)
        assert reaped_run.status == GatewaySyncStatus.FAILED
        assert "restarted" in reaped_run.errors[-1]
