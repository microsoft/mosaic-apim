"""Model endpoint onboarding: access verification, discovery, and runtime readiness."""

import pytest
from aoai_double import (
    AI_ENDPOINT,
    AI_PROJECT_ID,
    AI_RESOURCE_ID,
    AI_SUBSCRIPTION_ID,
    AZURE_OPENAI_USER_ROLE_ID,
    COGNITIVE_SERVICES_USER_ROLE_ID,
    FOUNDRY_USER_ROLE_ID,
    PARTIAL_PERMISSIONS,
    READER_PERMISSIONS,
    FakeCognitiveServices,
    role_assignment,
)
from apim_double import APIM_PRINCIPAL_ID, APIM_PUBLIC_IP, RESOURCE_ID, SERVICE_NAME
from conftest import build_endpoint_service
from mosaic_api.domain import (
    AZURE_AI_DEVELOPER_ROLE_ID,
    FOUNDRY_USER_ROLE_NAME,
    READER_ROLE_ID,
    READER_ROLE_NAME,
    AccessEvaluation,
    CognitiveServicesResourceId,
    EndpointAuthMode,
    Gateway,
    GatewayCapabilities,
    GatewayRuntimeAccess,
    GatewaySyncStatus,
    ModelEndpointCreate,
    ModelEndpointStatus,
    ModelEndpointSyncRun,
    ModelEndpointUpdate,
    ModelProvider,
    NetworkReachability,
    RuntimeAccessEvaluation,
    RuntimeAccessReason,
    RuntimeRoleFindingKind,
    SubscriptionScanStatus,
    SuggestionSource,
    new_id,
    utc_now,
)
from mosaic_api.errors import ConflictError, NotFoundError, ValidationError
from mosaic_api.integrations.aoai.client import SubscriptionScanner
from mosaic_api.observed import AiBackendKind, ObservedBackend
from mosaic_api.repositories import InMemoryGatewayRepository, InMemoryModelEndpointRepository
from mosaic_api.services.directory import Actor
from mosaic_api.services.model_endpoints import (
    IDENTITY_PLACEHOLDER,
    SUBSCRIPTION_PLACEHOLDER,
    UNEXPECTED_LIST_FAILURE,
)

ACTOR = Actor(object_id="admin-object-id", tenant_id="tenant-test")
# Subscriptions that exist only as candidate scopes: MOSAIC cannot list them.
BOOTSTRAP_SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"
OTHER_SUBSCRIPTION_ID = "66666666-7777-8888-9999-000000000000"


def _create(**kwargs: object) -> ModelEndpointCreate:
    payload: dict[str, object] = {"azure_resource_id": AI_RESOURCE_ID}
    payload.update(kwargs)
    return ModelEndpointCreate.model_validate(payload)


def _gateway(
    *,
    principal_id: str | None = APIM_PRINCIPAL_ID,
    subscription_id: str = AI_SUBSCRIPTION_ID,
    name: str = SERVICE_NAME,
    virtual_network_type: str | None = None,
    egress_ip_addresses: tuple[str, ...] = (),
) -> Gateway:
    return Gateway(
        id=new_id("gateway"),
        tenant_id=ACTOR.tenant_id,
        name=name,
        azure_resource_id=RESOURCE_ID,
        subscription_id=subscription_id,
        resource_group="rg-contoso-dev",
        service_name=SERVICE_NAME,
        capabilities=GatewayCapabilities(
            principal_id=principal_id,
            identity_observed=True,
            virtual_network_type=virtual_network_type,
            egress_ip_addresses=list(egress_ip_addresses),
        ),
    )


def _reader_command(assignee: str, scope: str) -> str:
    return (
        "az role assignment create"
        f' --assignee-object-id "{assignee}"'
        " --assignee-principal-type ServicePrincipal"
        ' --role "Reader"'
        f' --scope "{scope}"'
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
    async def test_ai_services_account_recommends_foundry_user(
        self, gateway_repository: InMemoryGatewayRepository
    ) -> None:
        await gateway_repository.record_gateway_state(_gateway())
        fake = FakeCognitiveServices(kind="AIServices")
        service = build_endpoint_service(fake, gateway_repository=gateway_repository)

        endpoint = await service.register(ACTOR, _create())
        access = endpoint.runtime_access[0]

        # Microsoft's Foundry RBAC guidance: Foundry User on the Foundry resource.
        assert access.required_role_definition_id == FOUNDRY_USER_ROLE_ID
        assert access.required_role_name == FOUNDRY_USER_ROLE_NAME
        assert access.remediation is not None
        assert access.remediation.scope == AI_RESOURCE_ID
        assert f'--role "{FOUNDRY_USER_ROLE_NAME}"' in access.remediation.command
        assert endpoint.provider == ModelProvider.AZURE_AI_FOUNDRY

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["OpenAI", "AIServices"])
    async def test_a_sufficient_role_other_than_the_recommended_one_is_accepted(
        self, kind: str, gateway_repository: InMemoryGatewayRepository
    ) -> None:
        # Cognitive Services User is recommended for neither kind, but it grants every data action
        # MOSAIC publishes. Matching one role definition ID reported this as "cannot invoke".
        await gateway_repository.record_gateway_state(_gateway())
        fake = FakeCognitiveServices(kind=kind)
        fake.role_assignments = [
            role_assignment(COGNITIVE_SERVICES_USER_ROLE_ID, AI_RESOURCE_ID, APIM_PRINCIPAL_ID)
        ]
        service = build_endpoint_service(fake, gateway_repository=gateway_repository)

        access = (await service.register(ACTOR, _create())).runtime_access[0]

        assert access.can_invoke is True
        assert access.reason == RuntimeAccessReason.GRANTED
        assert access.granted_role_definition_id == COGNITIVE_SERVICES_USER_ROLE_ID
        assert access.granted_role_name == "Cognitive Services User"
        assert access.required_role_definition_id != COGNITIVE_SERVICES_USER_ROLE_ID
        assert access.assignment_scope == AI_RESOURCE_ID

    @pytest.mark.asyncio
    async def test_readiness_covers_claude_alongside_other_models_on_ai_services(
        self, gateway_repository: InMemoryGatewayRepository
    ) -> None:
        # One AI Services account serves Llama through the Foundry Models API and Claude through
        # the Anthropic Messages API. Azure AI Developer covers the first and not the second, so
        # "can invoke" would precede a 401 or 403 on the first Claude call.
        await gateway_repository.record_gateway_state(_gateway())
        fake = FakeCognitiveServices(kind="AIServices")
        fake.deployments = [
            {
                "name": "llama-prod",
                "sku": {"name": "GlobalStandard", "capacity": 1},
                "properties": {
                    "model": {"format": "Meta", "name": "Llama-3.3-70B-Instruct", "version": "1"},
                    "provisioningState": "Succeeded",
                    "capabilities": {"chatCompletion": "true"},
                },
            },
            {
                "name": "claude-prod",
                "sku": {"name": "GlobalStandard", "capacity": 1},
                "properties": {
                    "model": {"format": "Anthropic", "name": "claude-sonnet-4-5", "version": "1"},
                    "provisioningState": "Succeeded",
                    "capabilities": {"chatCompletion": "true"},
                },
            },
        ]
        fake.role_assignments = [
            role_assignment(AZURE_AI_DEVELOPER_ROLE_ID, AI_RESOURCE_ID, APIM_PRINCIPAL_ID)
        ]
        service = build_endpoint_service(fake, gateway_repository=gateway_repository)

        endpoint = await service.register(ACTOR, _create())
        access = endpoint.runtime_access[0]

        provider_model = "Microsoft.CognitiveServices/accounts/AIServices/providers/action"
        assert endpoint.provider == ModelProvider.AZURE_AI_FOUNDRY
        assert provider_model in access.required_data_actions
        assert access.can_invoke is False
        assert access.reason == RuntimeAccessReason.MISSING_ROLE
        assert access.role_findings[0].missing_data_actions == [provider_model]
        assert "Anthropic Messages API" in (access.message or "")
        assert access.remediation is not None
        assert access.remediation.role_definition_id == FOUNDRY_USER_ROLE_ID

        # The administrator runs the recommended command.
        fake.role_assignments.append(
            role_assignment(FOUNDRY_USER_ROLE_ID, AI_RESOURCE_ID, APIM_PRINCIPAL_ID)
        )
        granted = (await service.runtime_access(ACTOR, endpoint.id))[0]

        assert granted.can_invoke is True
        assert granted.granted_role_definition_id == FOUNDRY_USER_ROLE_ID
        assert granted.remediation is None

    @pytest.mark.asyncio
    async def test_advice_given_before_the_kind_is_known_is_accepted_once_it_is(
        self, gateway_repository: InMemoryGatewayRepository
    ) -> None:
        # Observed live: before MOSAIC could read an Azure OpenAI account it recommended one role,
        # then rejected that same role once it learned the account's kind.
        await gateway_repository.record_gateway_state(_gateway())
        fake = FakeCognitiveServices(account_status=403)
        service = build_endpoint_service(fake, gateway_repository=gateway_repository)

        endpoint = await service.register(ACTOR, _create())
        before = endpoint.runtime_access[0]

        assert endpoint.capabilities.kind is None
        assert before.can_invoke is False
        assert before.remediation is not None
        assert before.remediation.role_definition_id == FOUNDRY_USER_ROLE_ID
        assert "does not know whether it is Azure OpenAI or Foundry" in (before.message or "")
        assert "Grant MOSAIC Reader" in (before.message or "")

        # The administrator runs the recommended command and grants MOSAIC Reader, so MOSAIC now
        # learns that the account is Azure OpenAI.
        fake.role_assignments = [
            role_assignment(
                before.remediation.role_definition_id,
                before.remediation.scope,
                APIM_PRINCIPAL_ID,
            )
        ]
        fake.account_status = 200
        checked = await service.preflight(ACTOR, endpoint.id)
        after = checked.runtime_access[0]

        assert checked.capabilities.kind == "OpenAI"
        assert after.can_invoke is True
        assert after.reason == RuntimeAccessReason.GRANTED
        assert after.granted_role_definition_id == FOUNDRY_USER_ROLE_ID
        assert after.required_role_definition_id == AZURE_OPENAI_USER_ROLE_ID

    @pytest.mark.asyncio
    async def test_project_registration_is_evaluated_at_the_parent_account(
        self, gateway_repository: InMemoryGatewayRepository
    ) -> None:
        # The published API calls the account, where the project's models are deployed. A grant on
        # the project confers nothing there, so reporting it as access would be a false positive.
        await gateway_repository.record_gateway_state(_gateway())
        fake = FakeCognitiveServices(kind="AIServices")
        fake.role_assignments = [
            role_assignment(FOUNDRY_USER_ROLE_ID, AI_PROJECT_ID, APIM_PRINCIPAL_ID)
        ]
        service = build_endpoint_service(fake, gateway_repository=gateway_repository)

        endpoint = await service.register(ACTOR, _create(azure_resource_id=AI_PROJECT_ID))
        access = endpoint.runtime_access[0]

        assert endpoint.azure_resource_id == AI_PROJECT_ID
        assert access.can_invoke is False
        assert access.reason == RuntimeAccessReason.NARROWER_SCOPE
        assert access.evaluation == RuntimeAccessEvaluation.ROLE_ASSIGNMENTS
        assert access.evaluated_scope == AI_RESOURCE_ID
        assert [finding.kind for finding in access.role_findings] == [
            RuntimeRoleFindingKind.NARROWER_SCOPE
        ]
        assert access.role_findings[0].scope == AI_PROJECT_ID
        assert "Models are deployed on the parent resource" in (access.message or "")
        assert access.remediation is not None
        assert access.remediation.scope == AI_RESOURCE_ID
        assert access.remediation.role_definition_id == FOUNDRY_USER_ROLE_ID

        fake.role_assignments.append(
            role_assignment(FOUNDRY_USER_ROLE_ID, AI_RESOURCE_ID, APIM_PRINCIPAL_ID)
        )
        granted = (await service.runtime_access(ACTOR, endpoint.id))[0]

        assert granted.can_invoke is True
        assert granted.assignment_scope == AI_RESOURCE_ID
        assert granted.inherited is False
        assert "the parent resource" in (granted.message or "")

    @pytest.mark.asyncio
    async def test_private_account_is_unreachable_from_a_gateway_outside_any_network(
        self, gateway_repository: InMemoryGatewayRepository
    ) -> None:
        # A gateway with no virtual network calling an account with public network access
        # disabled: the role is right, but no call can arrive, so "can invoke" would be false.
        await gateway_repository.record_gateway_state(
            _gateway(virtual_network_type="None", egress_ip_addresses=(APIM_PUBLIC_IP,))
        )
        fake = FakeCognitiveServices(public_network_access="Disabled")
        fake.role_assignments = [
            role_assignment(AZURE_OPENAI_USER_ROLE_ID, AI_RESOURCE_ID, APIM_PRINCIPAL_ID)
        ]
        service = build_endpoint_service(fake, gateway_repository=gateway_repository)

        endpoint = await service.register(ACTOR, _create())
        access = endpoint.runtime_access[0]

        assert access.can_invoke is False
        assert access.reason == RuntimeAccessReason.NETWORK_UNREACHABLE
        assert access.evaluation == RuntimeAccessEvaluation.ROLE_ASSIGNMENTS
        assert access.network_reachability == NetworkReachability.UNREACHABLE
        # The role is still reported, so the network is visibly the only thing to fix.
        assert access.granted_role_definition_id == AZURE_OPENAI_USER_ROLE_ID
        assert (access.message or "").startswith(
            "Public network access to this resource is disabled"
        )
        assert access.remediation is None
        assert any("Public network access is disabled" in n for n in endpoint.capabilities.notes)

    @pytest.mark.asyncio
    async def test_endpoint_firewall_is_recorded(
        self, gateway_repository: InMemoryGatewayRepository
    ) -> None:
        await gateway_repository.record_gateway_state(
            _gateway(virtual_network_type="None", egress_ip_addresses=(APIM_PUBLIC_IP,))
        )
        fake = FakeCognitiveServices(
            network_acls={
                "defaultAction": "Deny",
                "ipRules": [{"value": "203.0.113.0/24"}],
                "virtualNetworkRules": [{"id": f"{RESOURCE_ID}-vnet/subnets/apim"}],
            }
        )
        fake.role_assignments = [
            role_assignment(AZURE_OPENAI_USER_ROLE_ID, AI_RESOURCE_ID, APIM_PRINCIPAL_ID)
        ]
        service = build_endpoint_service(fake, gateway_repository=gateway_repository)

        endpoint = await service.register(ACTOR, _create())

        capabilities = endpoint.capabilities
        assert capabilities.network_default_action == "Deny"
        assert capabilities.network_ip_rules == ["203.0.113.0/24"]
        assert capabilities.network_virtual_network_rule_count == 1
        assert any(
            "firewall admits only" in note
            and "(1 address rule, 1 virtual network rule)" in note
            for note in capabilities.notes
        )
        # The gateway's published address falls inside the admitted range.
        access = endpoint.runtime_access[0]
        assert access.network_reachability == NetworkReachability.REACHABLE
        assert access.can_invoke is True

    @pytest.mark.asyncio
    async def test_role_definitions_are_read_once_per_check(
        self, gateway_repository: InMemoryGatewayRepository
    ) -> None:
        await gateway_repository.record_gateway_state(_gateway(name="apim-east"))
        await gateway_repository.record_gateway_state(_gateway(name="apim-west"))
        fake = FakeCognitiveServices()
        fake.role_assignments = [
            role_assignment(AZURE_OPENAI_USER_ROLE_ID, AI_RESOURCE_ID, APIM_PRINCIPAL_ID)
        ]
        service = build_endpoint_service(fake, gateway_repository=gateway_repository)

        endpoint = await service.register(ACTOR, _create())

        assert [access.can_invoke for access in endpoint.runtime_access] == [True, True]
        assert fake.role_definition_reads() == 1
        # Nothing outlives a check: a role an administrator edits is read afresh next time.
        await service.runtime_access(ACTOR, endpoint.id)
        assert fake.role_definition_reads() == 2

    @pytest.mark.asyncio
    async def test_results_recorded_before_the_reason_existed_still_load(self) -> None:
        # Stored endpoints predate ``reason`` and the other fields this check now reports.
        legacy = {
            "gatewayId": "gateway-1",
            "gatewayName": SERVICE_NAME,
            "apimPrincipalId": APIM_PRINCIPAL_ID,
            "canInvoke": False,
            "evaluation": "roleAssignments",
            "requiredRoleName": "Cognitive Services OpenAI User",
            "requiredRoleDefinitionId": AZURE_OPENAI_USER_ROLE_ID,
            "inherited": False,
            "message": "The gateway's managed identity does not hold the role.",
        }

        access = GatewayRuntimeAccess.model_validate(legacy)

        assert access.reason is None
        assert access.required_role_definition_id == AZURE_OPENAI_USER_ROLE_ID
        assert access.role_findings == []
        assert access.network_reachability == NetworkReachability.UNKNOWN
        serialized = access.model_dump(mode="json")
        assert serialized["requiredRoleName"] == "Cognitive Services OpenAI User"
        assert serialized["requiredRoleDefinitionId"] == AZURE_OPENAI_USER_ROLE_ID


class TestSuggestions:
    @pytest.mark.asyncio
    async def test_subscription_scan_skips_non_model_kinds(self, endpoint_service) -> None:
        view = await endpoint_service.suggestions(ACTOR)

        scanned = [s for s in view.suggestions if s.source == SuggestionSource.SUBSCRIPTION_SCAN]
        assert [s.account_name for s in scanned] == ["contoso-aoai"]
        assert view.subscriptions_scanned == 1
        assert view.scan_status == SubscriptionScanStatus.SCANNED
        assert view.scan_message is None
        assert view.scan_remediation == []

    @pytest.mark.asyncio
    async def test_forbidden_subscription_degrades_with_remediation(
        self, endpoint_service, fake_aoai: FakeCognitiveServices
    ) -> None:
        fake_aoai.forbidden_subscriptions = {AI_SUBSCRIPTION_ID}

        view = await endpoint_service.suggestions(ACTOR)

        assert view.subscriptions_scanned == 0
        # The subscription was visible, so the fix belongs to it rather than to the whole scan.
        assert view.scan_status == SubscriptionScanStatus.SCANNED
        assert view.scan_remediation == []
        assert len(view.scan_issues) == 1
        issue = view.scan_issues[0]
        assert issue.subscription_id == AI_SUBSCRIPTION_ID
        assert issue.remediation is not None
        assert issue.remediation.scope == f"/subscriptions/{AI_SUBSCRIPTION_ID}"
        assert issue.remediation.role_definition_id == READER_ROLE_ID
        assert issue.remediation.command == _reader_command(
            "mosaic-managed-identity", f"/subscriptions/{AI_SUBSCRIPTION_ID}"
        )
        # It could not be listed at all, which is a different finding from a partial read.
        assert view.partial_scans == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "permissions",
        [
            READER_PERMISSIONS,
            # A narrower role still reads every account when it is held at the subscription.
            [{"actions": ["Microsoft.CognitiveServices/accounts/read"], "notActions": []}],
        ],
        ids=["reader", "account-read-role"],
    )
    async def test_subscription_readable_in_full_is_not_partial(
        self,
        endpoint_service,
        fake_aoai: FakeCognitiveServices,
        permissions: list[dict[str, object]],
    ) -> None:
        fake_aoai.subscription_permissions[AI_SUBSCRIPTION_ID] = permissions

        view = await endpoint_service.suggestions(ACTOR)

        assert (
            f"/subscriptions/{AI_SUBSCRIPTION_ID}/providers/Microsoft.Authorization/permissions"
            in fake_aoai.requests
        )
        assert view.partial_scans == []
        assert view.scan_issues == []
        assert view.subscriptions_scanned == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "permissions",
        [
            # Every role MOSAIC holds sits on a resource group or a resource, so nothing is granted
            # at the subscription itself even though its account list still answers 200.
            [],
            [{"actions": ["*/read"], "notActions": ["Microsoft.CognitiveServices/*"]}],
        ],
        ids=["roles-below-the-subscription", "reader-except-azure-ai"],
    )
    async def test_partly_readable_subscription_is_reported_and_still_suggests(
        self,
        endpoint_service,
        fake_aoai: FakeCognitiveServices,
        permissions: list[dict[str, object]],
    ) -> None:
        fake_aoai.subscription_permissions[AI_SUBSCRIPTION_ID] = permissions

        view = await endpoint_service.suggestions(ACTOR)

        # What MOSAIC could read is still offered, and the subscription still counts as scanned.
        scanned = [s for s in view.suggestions if s.source == SuggestionSource.SUBSCRIPTION_SCAN]
        assert [s.account_name for s in scanned] == ["contoso-aoai"]
        assert view.subscriptions_scanned == 1
        assert view.scan_status == SubscriptionScanStatus.SCANNED
        assert view.scan_issues == []
        assert view.scan_remediation == []
        [partial] = view.partial_scans
        assert partial.subscription_id == AI_SUBSCRIPTION_ID
        assert partial.display_name == "Contoso dev"
        assert partial.message == (
            "MOSAIC can read only some resources in this subscription, so any Azure AI resources "
            "it cannot read are not suggested here. Endpoints can still be registered by "
            "resource ID."
        )
        scope = f"/subscriptions/{AI_SUBSCRIPTION_ID}"
        assert partial.remediation is not None
        assert partial.remediation.role_name == READER_ROLE_NAME
        assert partial.remediation.role_definition_id == READER_ROLE_ID
        assert partial.remediation.scope == scope
        assert partial.remediation.command == _reader_command("mosaic-managed-identity", scope)

    @pytest.mark.asyncio
    async def test_empty_list_from_a_partly_readable_subscription_is_not_a_clean_result(
        self, endpoint_service, fake_aoai: FakeCognitiveServices
    ) -> None:
        # Observed live: ARM answered 200 with no accounts because it had filtered out every
        # account MOSAIC could not read. A subscription MOSAIC reads in full is unaffected.
        fake_aoai.subscriptions.append(
            {"subscriptionId": OTHER_SUBSCRIPTION_ID, "displayName": "Contoso prod"}
        )
        fake_aoai.accounts_by_subscription[AI_SUBSCRIPTION_ID] = []
        fake_aoai.subscription_permissions[AI_SUBSCRIPTION_ID] = []

        view = await endpoint_service.suggestions(ACTOR)

        assert view.suggestions == []
        assert view.subscriptions_scanned == 2
        assert view.scan_issues == []
        assert [item.subscription_id for item in view.partial_scans] == [AI_SUBSCRIPTION_ID]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code", [403, 404, 503])
    async def test_unreadable_subscription_permissions_claim_nothing(
        self, endpoint_service, fake_aoai: FakeCognitiveServices, status_code: int
    ) -> None:
        # Whether the list was complete is unknown, so the scan is reported as it always was.
        fake_aoai.subscription_permissions_status = status_code

        view = await endpoint_service.suggestions(ACTOR)

        assert view.partial_scans == []
        assert view.scan_issues == []
        assert view.subscriptions_scanned == 1
        scanned = [s for s in view.suggestions if s.source == SuggestionSource.SUBSCRIPTION_SCAN]
        assert [s.account_name for s in scanned] == ["contoso-aoai"]

    @pytest.mark.asyncio
    async def test_unexpected_permissions_failure_claims_nothing(
        self, endpoint_service, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fail(self: SubscriptionScanner, subscription_id: str) -> None:
            raise RuntimeError("Bearer secret-token")

        monkeypatch.setattr(SubscriptionScanner, "subscription_permissions", fail)

        view = await endpoint_service.suggestions(ACTOR)

        assert view.partial_scans == []
        assert view.scan_issues == []
        assert view.subscriptions_scanned == 1
        assert "secret-token" not in view.model_dump_json()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "listed",
        [[], [{"displayName": "Listed without an ID"}]],
        ids=["empty", "unusable-entry"],
    )
    async def test_no_visible_subscription_offers_reader_on_each_candidate_scope(
        self,
        fake_aoai: FakeCognitiveServices,
        gateway_repository: InMemoryGatewayRepository,
        listed: list[dict[str, object]],
    ) -> None:
        fake_aoai.subscriptions = listed
        for gateway in (
            # The deployment subscription again, in the lowercase ARM returns it in.
            _gateway(subscription_id=BOOTSTRAP_SUBSCRIPTION_ID, name="apim-a"),
            _gateway(subscription_id=OTHER_SUBSCRIPTION_ID, name="apim-b"),
            _gateway(subscription_id=OTHER_SUBSCRIPTION_ID.upper(), name="apim-c"),
        ):
            await gateway_repository.record_gateway_state(gateway)
        service = build_endpoint_service(
            fake_aoai,
            gateway_repository=gateway_repository,
            bootstrap_subscription_id=BOOTSTRAP_SUBSCRIPTION_ID.upper(),
        )

        view = await service.suggestions(ACTOR)

        assert view.scan_status == SubscriptionScanStatus.NO_VISIBLE_SUBSCRIPTIONS
        assert view.subscriptions_scanned == 0
        assert view.scan_issues == []
        assert view.scan_message is None
        # Case-insensitively unique, deployment subscription first, then gateways by name.
        scopes = [
            f"/subscriptions/{BOOTSTRAP_SUBSCRIPTION_ID.upper()}",
            f"/subscriptions/{OTHER_SUBSCRIPTION_ID}",
        ]
        assert [item.scope for item in view.scan_remediation] == scopes
        for item, scope in zip(view.scan_remediation, scopes, strict=True):
            assert item.role_name == READER_ROLE_NAME
            assert item.role_definition_id == READER_ROLE_ID
            assert item.principal_id == "mosaic-managed-identity"
            assert item.command == _reader_command("mosaic-managed-identity", scope)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bootstrap_subscription_id",
        [None, "   ", '0" --role "Owner'],
        ids=["unset", "blank", "not-a-subscription-id"],
    )
    async def test_no_known_subscription_falls_back_to_a_placeholder_scope(
        self,
        fake_aoai: FakeCognitiveServices,
        gateway_repository: InMemoryGatewayRepository,
        bootstrap_subscription_id: str | None,
    ) -> None:
        fake_aoai.subscriptions = []
        service = build_endpoint_service(
            fake_aoai,
            gateway_repository=gateway_repository,
            principal_id=None,
            bootstrap_subscription_id=bootstrap_subscription_id,
        )

        view = await service.suggestions(ACTOR)

        assert view.scan_status == SubscriptionScanStatus.NO_VISIBLE_SUBSCRIPTIONS
        placeholder_scope = f"/subscriptions/{SUBSCRIPTION_PLACEHOLDER}"
        assert len(view.scan_remediation) == 1
        remediation = view.scan_remediation[0]
        assert remediation.scope == "/subscriptions/<subscription-id>"
        assert remediation.principal_id is None
        assert remediation.command == _reader_command(IDENTITY_PLACEHOLDER, placeholder_scope)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status_code", "message"),
        [
            (403, "MOSAIC's identity is not authorized for this Azure resource."),
            (503, "Azure Resource Manager did not return a usable response."),
        ],
    )
    async def test_list_failure_is_explained_and_other_sources_still_suggest(
        self,
        fake_aoai: FakeCognitiveServices,
        gateway_repository: InMemoryGatewayRepository,
        status_code: int,
        message: str,
    ) -> None:
        fake_aoai.subscriptions_status = status_code
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
                )
            ],
            "snap-1",
        )
        service = build_endpoint_service(
            fake_aoai,
            gateway_repository=gateway_repository,
            bootstrap_subscription_id=BOOTSTRAP_SUBSCRIPTION_ID,
        )

        view = await service.suggestions(ACTOR)

        assert view.scan_status == SubscriptionScanStatus.LIST_FAILED
        assert view.scan_message == message
        # The upstream error body never reaches the response.
        assert "denied" not in view.model_dump_json()
        assert view.subscriptions_scanned == 0
        assert view.scan_issues == []
        assert [item.scope for item in view.scan_remediation] == [
            f"/subscriptions/{BOOTSTRAP_SUBSCRIPTION_ID}",
            f"/subscriptions/{AI_SUBSCRIPTION_ID}",
        ]
        assert [s.source for s in view.suggestions] == [SuggestionSource.GATEWAY_BACKEND]

    @pytest.mark.asyncio
    async def test_unexpected_list_failure_never_returns_exception_text(
        self,
        fake_aoai: FakeCognitiveServices,
        gateway_repository: InMemoryGatewayRepository,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fail(self: SubscriptionScanner) -> list[dict[str, object]]:
            raise RuntimeError("Authorization: Bearer secret-token")

        monkeypatch.setattr(SubscriptionScanner, "list_subscriptions", fail)
        service = build_endpoint_service(fake_aoai, gateway_repository=gateway_repository)

        view = await service.suggestions(ACTOR)

        assert view.scan_status == SubscriptionScanStatus.LIST_FAILED
        assert view.scan_message == UNEXPECTED_LIST_FAILURE
        assert "secret-token" not in view.model_dump_json()
        assert [item.scope for item in view.scan_remediation] == [
            f"/subscriptions/{SUBSCRIPTION_PLACEHOLDER}"
        ]

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
        # A deployment that never enabled the scan has nothing to explain or remediate.
        assert view.scan_status == SubscriptionScanStatus.NOT_CONFIGURED
        assert view.scan_message is None
        assert view.scan_remediation == []


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
