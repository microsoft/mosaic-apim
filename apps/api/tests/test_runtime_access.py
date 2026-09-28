"""Gateway runtime readiness: which grants let a gateway call what MOSAIC publishes.

ADR 0012. The check evaluates the account the published API calls, accepts any role whose data
actions cover the curated operations, and never claims "can invoke" on a grant it cannot prove.
Real built-in role definitions are evaluated throughout, never approximations of them.
"""

from typing import Any

import pytest
from aoai_double import (
    AI_ACCOUNT_NAME,
    AI_PROJECT_ID,
    AI_RESOURCE_GROUP,
    AI_RESOURCE_ID,
    AI_SUBSCRIPTION_ID,
    FakeCognitiveServices,
    deny_assignment,
    role_assignment,
)
from apim_double import APIM_PRINCIPAL_ID, APIM_PUBLIC_IP, RESOURCE_ID, SERVICE_NAME
from conftest import build_aoai_arm_client
from mosaic_api.domain import (
    AZURE_OPENAI_CONTRIBUTOR_ROLE_ID,
    AZURE_OPENAI_USER_ROLE_ID,
    COGNITIVE_SERVICES_USER_ROLE_ID,
    FOUNDRY_USER_ROLE_ID,
    READER_ROLE_ID,
    CognitiveServicesResourceId,
    Gateway,
    GatewayCapabilities,
    GatewayRuntimeAccess,
    ModelEndpointCapabilities,
    ModelProvider,
    NetworkReachability,
    RuntimeAccessEvaluation,
    RuntimeAccessReason,
    RuntimeRoleFindingKind,
    new_id,
)
from mosaic_api.integrations.aoai import (
    KNOWN_SUFFICIENT_ROLES,
    CognitiveServicesClient,
    RuntimeAccessCheck,
    evaluate_network_path,
    known_sufficient_roles,
    recommended_runtime_role,
    required_runtime_data_actions,
    verify_gateway_runtime_access,
)
from mosaic_api.integrations.aoai.runtime_access import judge_role
from mosaic_api.integrations.apim.model_apis import required_data_actions
from mosaic_api.integrations.rbac import condition_may_hold, condition_permits, grants_data_action
from mosaic_api.services.model_endpoints import provider_for
from role_definitions import (
    AZURE_AI_DEVELOPER_ROLE_ID,
    BUILT_IN_ROLE_DEFINITIONS,
    COGNITIVE_SERVICES_CONTRIBUTOR_ROLE_ID,
    COGNITIVE_SERVICES_DATA_CONTRIBUTOR_ROLE_ID,
    COGNITIVE_SERVICES_DATA_READER_ROLE_ID,
    FOUNDRY_OWNER_ROLE_ID,
    FOUNDRY_PROJECT_MANAGER_ROLE_ID,
)

AOAI_ACTIONS = required_data_actions(ModelProvider.AZURE_OPENAI)
FOUNDRY_ACTIONS = required_data_actions(ModelProvider.AZURE_AI_FOUNDRY)
CHAT = "Microsoft.CognitiveServices/accounts/OpenAI/deployments/chat/completions/action"
IMAGES = "Microsoft.CognitiveServices/accounts/OpenAI/images/generations/action"
RESPONSES = "Microsoft.CognitiveServices/accounts/OpenAI/responses/write"
MAAS_CHAT = "Microsoft.CognitiveServices/accounts/MaaS/chat/completions/action"

CUSTOM_ROLE_ID = "0f0f0f0f-0000-4000-8000-000000000001"
GROUP_ID = "44444444-4444-4444-4444-444444444444"
ALL_PRINCIPALS = "00000000-0000-0000-0000-000000000000"
SUBSCRIPTION_SCOPE = f"/subscriptions/{AI_SUBSCRIPTION_ID}"
RESOURCE_GROUP_SCOPE = f"{SUBSCRIPTION_SCOPE}/resourceGroups/{AI_RESOURCE_GROUP}"
# A test of an attribute MOSAIC cannot know, so whether it holds for a given call is unknowable.
ATTRIBUTE_CONDITION = (
    "@Request[Microsoft.CognitiveServices/accounts/deployments:name] StringEquals 'gpt-4o-prod'"
)
GATEWAY_PRINCIPAL = [{"id": APIM_PRINCIPAL_ID, "type": "ServicePrincipal"}]


def _client(
    fake: FakeCognitiveServices, resource_id: str = AI_RESOURCE_ID
) -> CognitiveServicesClient:
    return CognitiveServicesClient(
        build_aoai_arm_client(fake), CognitiveServicesResourceId.parse(resource_id)
    )


def _gateway(
    *,
    name: str = SERVICE_NAME,
    virtual_network_type: str | None = "None",
    egress: tuple[str, ...] = (APIM_PUBLIC_IP,),
) -> Gateway:
    return Gateway(
        id=new_id("gateway"),
        tenant_id="tenant-test",
        name=name,
        azure_resource_id=RESOURCE_ID,
        subscription_id=AI_SUBSCRIPTION_ID,
        resource_group="rg-contoso-dev",
        service_name=SERVICE_NAME,
        capabilities=GatewayCapabilities(
            principal_id=APIM_PRINCIPAL_ID,
            identity_observed=True,
            virtual_network_type=virtual_network_type,
            egress_ip_addresses=list(egress),
        ),
    )


def _grant(
    fake: FakeCognitiveServices,
    role_id: str,
    scope: str = AI_RESOURCE_ID,
    *,
    condition: str | None = None,
) -> None:
    fake.role_assignments.append(
        role_assignment(role_id, scope, APIM_PRINCIPAL_ID, condition=condition)
    )


def _network(**kwargs: Any) -> ModelEndpointCapabilities:
    return ModelEndpointCapabilities.model_validate({"kind": "OpenAI", **kwargs})


async def _check(
    fake: FakeCognitiveServices,
    *,
    kind: str | None = "OpenAI",
    resource_id: str = AI_RESOURCE_ID,
    gateway: Gateway | None = None,
    capabilities: ModelEndpointCapabilities | None = None,
    client: CognitiveServicesClient | None = None,
    check: RuntimeAccessCheck | None = None,
) -> GatewayRuntimeAccess:
    return await verify_gateway_runtime_access(
        client or _client(fake, resource_id),
        gateway or _gateway(),
        kind=kind,
        provider=provider_for(kind, None),
        capabilities=capabilities,
        check=check,
    )


def _message(access: GatewayRuntimeAccess) -> str:
    assert access.message
    return access.message


def _permissions(role_id: str) -> list[dict[str, Any]]:
    return BUILT_IN_ROLE_DEFINITIONS[role_id][1]


class TestRequiredDataActions:
    def test_azure_openai_needs_only_the_openai_routes(self) -> None:
        assert required_runtime_data_actions("OpenAI") == AOAI_ACTIONS
        assert not any("/MaaS/" in action for action in AOAI_ACTIONS)

    def test_foundry_needs_only_the_models_routes(self) -> None:
        actions = required_runtime_data_actions("AIServices", ModelProvider.AZURE_AI_FOUNDRY)
        assert actions == FOUNDRY_ACTIONS

    def test_unknown_kind_needs_every_curated_shape(self) -> None:
        # Whatever MOSAIC accepts before it can read the resource must still hold once it can.
        assert set(required_runtime_data_actions(None)) == {*AOAI_ACTIONS, *FOUNDRY_ACTIONS}


class TestDataActionSemantics:
    @pytest.mark.parametrize(
        "pattern",
        [
            "Microsoft.CognitiveServices/*",
            "microsoft.cognitiveservices/accounts/openai/*",
            "Microsoft.CognitiveServices/accounts/*/chat/completions/action",
            "*",
        ],
    )
    def test_wildcards_match_case_insensitively(self, pattern: str) -> None:
        assert grants_data_action({"dataActions": [pattern]}, CHAT)

    def test_a_wildcard_does_not_reach_a_sibling_namespace(self) -> None:
        block = {"dataActions": ["Microsoft.CognitiveServices/accounts/OpenAI/*"]}

        assert grants_data_action(block, CHAT)
        assert not grants_data_action(block, MAAS_CHAT)

    def test_control_plane_actions_never_authorize_a_model_call(self) -> None:
        assert not grants_data_action({"actions": ["*"], "dataActions": []}, CHAT)

    def test_not_data_actions_are_subtracted_with_the_same_semantics(self) -> None:
        block = {
            "dataActions": ["Microsoft.CognitiveServices/*"],
            "notDataActions": ["MICROSOFT.COGNITIVESERVICES/accounts/*/responses/*"],
        }

        assert grants_data_action(block, CHAT)
        assert not grants_data_action(block, RESPONSES)
        verdict = judge_role([block], AOAI_ACTIONS)
        assert verdict.kind == RuntimeRoleFindingKind.INSUFFICIENT
        assert verdict.missing == (RESPONSES,)

    def test_not_data_actions_subtract_only_from_their_own_block(self) -> None:
        # Azure evaluates each block on its own, so one block's exclusion cannot cancel a grant
        # another block makes.
        excluding = {
            "dataActions": ["Microsoft.CognitiveServices/*"],
            "notDataActions": ["Microsoft.CognitiveServices/accounts/OpenAI/*"],
        }

        assert judge_role([excluding], (CHAT,)).kind == RuntimeRoleFindingKind.INSUFFICIENT
        restored = judge_role([excluding, {"dataActions": [CHAT]}], (CHAT,))
        assert restored.kind == RuntimeRoleFindingKind.SUFFICIENT


class TestBuiltInRoles:
    @pytest.mark.parametrize(
        ("role_id", "covers_openai", "covers_foundry"),
        [
            (AZURE_OPENAI_USER_ROLE_ID, True, False),
            (AZURE_OPENAI_CONTRIBUTOR_ROLE_ID, True, False),
            (COGNITIVE_SERVICES_USER_ROLE_ID, True, True),
            (FOUNDRY_USER_ROLE_ID, True, True),
            (AZURE_AI_DEVELOPER_ROLE_ID, True, True),
            (COGNITIVE_SERVICES_DATA_CONTRIBUTOR_ROLE_ID, True, True),
            (FOUNDRY_OWNER_ROLE_ID, True, True),
            (FOUNDRY_PROJECT_MANAGER_ROLE_ID, True, True),
            (READER_ROLE_ID, False, False),
            (COGNITIVE_SERVICES_CONTRIBUTOR_ROLE_ID, False, False),
            (COGNITIVE_SERVICES_DATA_READER_ROLE_ID, False, False),
        ],
    )
    def test_real_definitions(
        self, role_id: str, covers_openai: bool, covers_foundry: bool
    ) -> None:
        permissions = _permissions(role_id)

        openai = judge_role(permissions, AOAI_ACTIONS).kind
        foundry = judge_role(permissions, FOUNDRY_ACTIONS).kind

        assert (openai == RuntimeRoleFindingKind.SUFFICIENT) is covers_openai
        assert (foundry == RuntimeRoleFindingKind.SUFFICIENT) is covers_foundry

    @pytest.mark.parametrize("shape", [ModelProvider.AZURE_OPENAI, ModelProvider.AZURE_AI_FOUNDRY])
    def test_fallback_list_is_exactly_the_sufficient_built_ins(self, shape: ModelProvider) -> None:
        # The list is only consulted when a definition cannot be read, so it must say exactly what
        # reading the definition would have said.
        actions = required_data_actions(shape)
        sufficient = {
            role_id
            for role_id, (_, permissions) in BUILT_IN_ROLE_DEFINITIONS.items()
            if judge_role(permissions, actions).kind == RuntimeRoleFindingKind.SUFFICIENT
        }

        assert set(KNOWN_SUFFICIENT_ROLES[shape]) == sufficient
        for role_id, role_name in KNOWN_SUFFICIENT_ROLES[shape].items():
            assert BUILT_IN_ROLE_DEFINITIONS[role_id][0] == role_name

    def test_unknown_kind_falls_back_only_to_roles_covering_both_shapes(self) -> None:
        both = known_sufficient_roles(None)

        assert AZURE_OPENAI_USER_ROLE_ID not in both
        assert FOUNDRY_USER_ROLE_ID in both
        assert set(both) == set(KNOWN_SUFFICIENT_ROLES[ModelProvider.AZURE_AI_FOUNDRY])

    @pytest.mark.parametrize("role_id", [FOUNDRY_OWNER_ROLE_ID, FOUNDRY_PROJECT_MANAGER_ROLE_ID])
    def test_delegation_conditions_hold_for_every_model_call(self, role_id: str) -> None:
        # These roles constrain only which roles their holder may assign. Treating the condition
        # as unknowable would report every Foundry Owner as unable to call a model.
        condition = _permissions(role_id)[0]["condition"]

        assert condition
        assert all(condition_permits(condition, action) for action in AOAI_ACTIONS)
        assert all(condition_permits(condition, action) for action in FOUNDRY_ACTIONS)
        assert not condition_permits(condition, "Microsoft.Authorization/roleAssignments/write")


class TestConditionEvaluation:
    @pytest.mark.parametrize("condition", [None, "", "   "])
    def test_no_condition_is_trivially_true(self, condition: str | None) -> None:
        assert condition_permits(condition, CHAT)

    def test_an_attribute_test_is_unknown(self) -> None:
        assert not condition_permits(ATTRIBUTE_CONDITION, CHAT)
        assert condition_may_hold(ATTRIBUTE_CONDITION, CHAT)

    def test_action_matches_is_decided_for_the_action_in_hand(self) -> None:
        only_openai = "ActionMatches{'Microsoft.CognitiveServices/accounts/OpenAI/*'}"

        assert condition_permits(only_openai, CHAT)
        assert not condition_may_hold(only_openai, MAAS_CHAT)

    def test_symbolic_operators_are_understood(self) -> None:
        condition = (
            f"!(ActionMatches{{'{MAAS_CHAT}'}}) || ({ATTRIBUTE_CONDITION})"
        )

        assert condition_permits(condition, CHAT)
        assert not condition_permits(condition, MAAS_CHAT)
        assert condition_may_hold(condition, MAAS_CHAT)

    @pytest.mark.parametrize(
        "condition",
        [
            "ActionMatches{'a'} AND ActionMatches{'b'} OR ActionMatches{'c'}",
            "((ActionMatches{'x'})",
            "ActionMatches{'unterminated}",
        ],
    )
    def test_ambiguous_or_malformed_conditions_are_unknown(self, condition: str) -> None:
        assert not condition_permits(condition, CHAT)
        assert condition_may_hold(condition, CHAT)

    def test_a_non_string_condition_is_unknown(self) -> None:
        assert not condition_permits(42, CHAT)
        assert condition_may_hold(42, CHAT)


class TestAccountScope:
    @pytest.mark.asyncio
    async def test_project_grant_is_narrower_than_the_published_api_needs(self) -> None:
        # Models are deployed on the parent resource and the published API calls it, so a grant on
        # the project the endpoint was registered by authorizes none of those calls.
        fake = FakeCognitiveServices(kind="AIServices")
        _grant(fake, FOUNDRY_USER_ROLE_ID, AI_PROJECT_ID)

        access = await _check(fake, kind="AIServices", resource_id=AI_PROJECT_ID)

        assert access.can_invoke is False
        assert access.evaluation == RuntimeAccessEvaluation.ROLE_ASSIGNMENTS
        assert access.reason == RuntimeAccessReason.NARROWER_SCOPE
        assert access.evaluated_scope == AI_RESOURCE_ID
        finding = access.role_findings[0]
        assert finding.kind == RuntimeRoleFindingKind.NARROWER_SCOPE
        assert finding.scope == AI_PROJECT_ID
        assert finding.role_name == "Foundry User"
        message = _message(access)
        assert "narrower" in message
        assert AI_PROJECT_ID in message
        assert (
            f"Models are deployed on the parent resource, {AI_ACCOUNT_NAME}, and the published "
            "API calls that resource."
        ) in message
        assert access.remediation is not None
        assert access.remediation.scope == AI_RESOURCE_ID
        assert access.remediation.role_definition_id == FOUNDRY_USER_ROLE_ID

    @pytest.mark.asyncio
    async def test_project_registration_with_an_account_grant_can_invoke(self) -> None:
        fake = FakeCognitiveServices(kind="AIServices")
        _grant(fake, FOUNDRY_USER_ROLE_ID, AI_RESOURCE_ID)

        access = await _check(fake, kind="AIServices", resource_id=AI_PROJECT_ID)

        assert access.can_invoke is True
        assert access.reason == RuntimeAccessReason.GRANTED
        assert access.granted_role_name == "Foundry User"
        assert access.granted_role_definition_id == FOUNDRY_USER_ROLE_ID
        assert access.assignment_scope == AI_RESOURCE_ID
        assert access.inherited is False
        assert f"the parent resource {AI_ACCOUNT_NAME}" in _message(access)
        assert access.remediation is None

    @pytest.mark.asyncio
    async def test_project_registration_inherits_a_subscription_grant(self) -> None:
        fake = FakeCognitiveServices(kind="AIServices")
        _grant(fake, COGNITIVE_SERVICES_USER_ROLE_ID, SUBSCRIPTION_SCOPE)

        access = await _check(fake, kind="AIServices", resource_id=AI_PROJECT_ID)

        assert access.can_invoke is True
        assert access.inherited is True
        assert access.assignment_scope == SUBSCRIPTION_SCOPE

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "scope",
        [
            SUBSCRIPTION_SCOPE,
            RESOURCE_GROUP_SCOPE,
            "/providers/Microsoft.Management/managementGroups/contoso",
            "/",
        ],
    )
    async def test_grants_above_the_account_are_inherited(self, scope: str) -> None:
        fake = FakeCognitiveServices()
        _grant(fake, AZURE_OPENAI_USER_ROLE_ID, scope)

        access = await _check(fake)

        assert access.can_invoke is True
        assert access.inherited is True
        assert access.assignment_scope == scope
        assert "inherited" in _message(access)

    @pytest.mark.asyncio
    async def test_a_grant_on_a_similarly_named_resource_is_ignored(self) -> None:
        fake = FakeCognitiveServices()
        _grant(
            fake,
            AZURE_OPENAI_USER_ROLE_ID,
            f"{RESOURCE_GROUP_SCOPE}/providers/Microsoft.CognitiveServices/accounts/"
            f"{AI_ACCOUNT_NAME}-2",
        )

        access = await _check(fake)

        assert access.can_invoke is False
        assert access.reason == RuntimeAccessReason.MISSING_ROLE
        assert access.role_findings == []


class TestAnySufficientRole:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "role_id",
        [COGNITIVE_SERVICES_USER_ROLE_ID, AZURE_OPENAI_CONTRIBUTOR_ROLE_ID, FOUNDRY_USER_ROLE_ID],
    )
    async def test_roles_besides_the_recommended_one_are_accepted(self, role_id: str) -> None:
        fake = FakeCognitiveServices()
        _grant(fake, role_id)

        access = await _check(fake)

        assert access.can_invoke is True
        assert access.granted_role_definition_id == role_id
        assert access.granted_role_name == BUILT_IN_ROLE_DEFINITIONS[role_id][0]
        # The recommendation is what MOSAIC would suggest, not what happened to satisfy it.
        assert access.required_role_definition_id == AZURE_OPENAI_USER_ROLE_ID

    @pytest.mark.asyncio
    async def test_the_recommended_role_is_reported_ahead_of_other_sufficient_ones(self) -> None:
        fake = FakeCognitiveServices()
        _grant(fake, COGNITIVE_SERVICES_USER_ROLE_ID)
        _grant(fake, AZURE_OPENAI_USER_ROLE_ID)

        access = await _check(fake)

        assert access.granted_role_definition_id == AZURE_OPENAI_USER_ROLE_ID
        assert [finding.kind for finding in access.role_findings] == [
            RuntimeRoleFindingKind.SUFFICIENT,
            RuntimeRoleFindingKind.SUFFICIENT,
        ]

    @pytest.mark.asyncio
    async def test_a_custom_role_with_the_needed_data_actions_is_accepted(self) -> None:
        fake = FakeCognitiveServices()
        fake.add_custom_role(
            CUSTOM_ROLE_ID,
            "Contoso model caller",
            [{"dataActions": ["Microsoft.CognitiveServices/accounts/OpenAI/*"]}],
        )
        _grant(fake, CUSTOM_ROLE_ID)

        access = await _check(fake)

        assert access.can_invoke is True
        assert access.granted_role_name == "Contoso model caller"
        assert "Contoso model caller" in _message(access)

    @pytest.mark.asyncio
    async def test_a_custom_role_missing_a_route_is_reported_with_what_it_lacks(self) -> None:
        fake = FakeCognitiveServices()
        fake.add_custom_role(
            CUSTOM_ROLE_ID,
            "Contoso deployments only",
            [{"dataActions": ["Microsoft.CognitiveServices/accounts/OpenAI/deployments/*"]}],
        )
        _grant(fake, CUSTOM_ROLE_ID)

        access = await _check(fake)

        assert access.can_invoke is False
        assert access.evaluation == RuntimeAccessEvaluation.ROLE_ASSIGNMENTS
        assert access.reason == RuntimeAccessReason.MISSING_ROLE
        finding = access.role_findings[0]
        assert finding.kind == RuntimeRoleFindingKind.INSUFFICIENT
        assert finding.missing_data_actions == [IMAGES, RESPONSES]
        assert "Contoso deployments only" in _message(access)
        assert IMAGES in _message(access)
        assert access.remediation is not None
        assert access.remediation.role_definition_id == AZURE_OPENAI_USER_ROLE_ID

    @pytest.mark.asyncio
    async def test_openai_user_does_not_cover_foundry_models(self) -> None:
        fake = FakeCognitiveServices(kind="AIServices")
        _grant(fake, AZURE_OPENAI_USER_ROLE_ID)

        access = await _check(fake, kind="AIServices")

        assert access.can_invoke is False
        assert access.reason == RuntimeAccessReason.MISSING_ROLE
        assert MAAS_CHAT in access.role_findings[0].missing_data_actions
        assert access.remediation is not None
        assert access.remediation.role_definition_id == FOUNDRY_USER_ROLE_ID

    @pytest.mark.asyncio
    async def test_role_definitions_are_read_once_per_check(self) -> None:
        fake = FakeCognitiveServices()
        _grant(fake, AZURE_OPENAI_USER_ROLE_ID)
        client = _client(fake)
        check = RuntimeAccessCheck(client)

        for name in ("gateway-a", "gateway-b", "gateway-c"):
            access = await _check(fake, client=client, check=check, gateway=_gateway(name=name))
            assert access.can_invoke is True

        assert fake.role_definition_reads() == 1
        assert sum(path.endswith("/denyAssignments") for path in fake.requests) == 1

        # Nothing outlives a check: a role an administrator edits is read afresh next time.
        await _check(fake, client=client)
        assert fake.role_definition_reads() == 2


class TestRecommendation:
    @pytest.mark.parametrize(
        ("kind", "role_id"),
        [
            ("OpenAI", AZURE_OPENAI_USER_ROLE_ID),
            ("AIServices", FOUNDRY_USER_ROLE_ID),
            ("CognitiveServices", FOUNDRY_USER_ROLE_ID),
            (None, FOUNDRY_USER_ROLE_ID),
        ],
    )
    def test_least_privileged_role_for_the_kind(self, kind: str | None, role_id: str) -> None:
        role_name, recommended = recommended_runtime_role(kind)

        assert recommended == role_id
        assert role_name == BUILT_IN_ROLE_DEFINITIONS[role_id][0]

    @pytest.mark.asyncio
    async def test_unknown_kind_is_explained_and_the_command_works_for_either(self) -> None:
        fake = FakeCognitiveServices()

        access = await _check(fake, kind=None)

        assert access.can_invoke is False
        assert access.required_role_definition_id == FOUNDRY_USER_ROLE_ID
        assert set(access.required_data_actions) == {*AOAI_ACTIONS, *FOUNDRY_ACTIONS}
        message = _message(access)
        assert "does not know whether it is Azure OpenAI or Foundry" in message
        assert "Grant MOSAIC Reader on the resource and it will recommend the exact role" in message
        assert "Foundry User is accepted for either kind" in message
        assert access.remediation is not None
        assert '--role "Foundry User"' in access.remediation.command

    @pytest.mark.asyncio
    async def test_openai_user_is_not_enough_while_the_kind_is_unknown(self) -> None:
        # It would stop being enough the moment the resource turned out to be Foundry.
        fake = FakeCognitiveServices()
        _grant(fake, AZURE_OPENAI_USER_ROLE_ID)

        access = await _check(fake, kind=None)

        assert access.can_invoke is False
        assert access.reason == RuntimeAccessReason.MISSING_ROLE

    @pytest.mark.asyncio
    @pytest.mark.parametrize("resource_id", [AI_RESOURCE_ID, AI_PROJECT_ID])
    @pytest.mark.parametrize("kind", [None, "OpenAI", "AIServices"])
    async def test_what_mosaic_recommends_the_check_accepts(
        self, kind: str | None, resource_id: str
    ) -> None:
        fake = FakeCognitiveServices()
        before = await _check(fake, kind=kind, resource_id=resource_id)
        remediation = before.remediation
        assert remediation is not None
        # The command assigns by name, so the name must be the one the evaluated definition has.
        assert BUILT_IN_ROLE_DEFINITIONS[remediation.role_definition_id][0] == (
            remediation.role_name
        )
        assert f'--role "{remediation.role_name}"' in remediation.command
        assert f'--scope "{remediation.scope}"' in remediation.command

        # An administrator runs exactly that command.
        fake.role_assignments = [
            role_assignment(remediation.role_definition_id, remediation.scope, APIM_PRINCIPAL_ID)
        ]

        # Accepted now, still accepted once MOSAIC learns the kind, and accepted by the fallback
        # when definitions cannot be read. Anything else would be MOSAIC contradicting itself.
        later_kinds = [kind] if kind else [None, "OpenAI", "AIServices"]
        for readable in (True, False):
            fake.role_definitions_status = 200 if readable else 403
            for later in later_kinds:
                after = await _check(fake, kind=later, resource_id=resource_id)
                assert after.can_invoke is True, (kind, later, readable, after.message)
                assert after.remediation is None


class TestUnreadableRoleDefinitions:
    @pytest.mark.asyncio
    async def test_built_ins_known_to_suffice_stand_in(self) -> None:
        fake = FakeCognitiveServices(role_definitions_status=403)
        _grant(fake, COGNITIVE_SERVICES_USER_ROLE_ID)

        access = await _check(fake)

        assert access.can_invoke is True
        assert access.granted_role_name == "Cognitive Services User"
        assert fake.role_definition_reads() == 1

    @pytest.mark.asyncio
    async def test_an_unknown_role_is_not_evaluated_rather_than_denied(self) -> None:
        fake = FakeCognitiveServices(role_definitions_status=403)
        _grant(fake, CUSTOM_ROLE_ID)

        access = await _check(fake)

        assert access.can_invoke is False
        assert access.evaluation == RuntimeAccessEvaluation.NOT_EVALUATED
        assert access.reason == RuntimeAccessReason.ROLE_UNREADABLE
        assert access.role_findings[0].kind == RuntimeRoleFindingKind.UNREADABLE
        assert "not a denial" in _message(access)
        assert access.remediation is not None

    @pytest.mark.asyncio
    async def test_the_fallback_is_specific_to_the_published_shape(self) -> None:
        # OpenAI User suffices for an Azure OpenAI resource but not for Foundry models, so it
        # cannot stand in for an unread definition on an AIServices resource.
        fake = FakeCognitiveServices(role_definitions_status=403, kind="AIServices")
        _grant(fake, AZURE_OPENAI_USER_ROLE_ID)

        access = await _check(fake, kind="AIServices")

        assert access.can_invoke is False
        assert access.reason == RuntimeAccessReason.ROLE_UNREADABLE

    @pytest.mark.asyncio
    async def test_a_definition_that_no_longer_exists_is_not_evaluated(self) -> None:
        fake = FakeCognitiveServices()
        _grant(fake, CUSTOM_ROLE_ID)

        access = await _check(fake)

        assert access.evaluation == RuntimeAccessEvaluation.NOT_EVALUATED
        assert access.reason == RuntimeAccessReason.ROLE_UNREADABLE


class TestConditionalAssignments:
    @pytest.mark.asyncio
    async def test_a_conditional_assignment_is_never_claimed_as_access(self) -> None:
        fake = FakeCognitiveServices()
        _grant(fake, AZURE_OPENAI_USER_ROLE_ID, condition=ATTRIBUTE_CONDITION)

        access = await _check(fake)

        assert access.can_invoke is False
        assert access.evaluation == RuntimeAccessEvaluation.NOT_EVALUATED
        assert access.reason == RuntimeAccessReason.CONDITIONAL
        assert access.role_findings[0].kind == RuntimeRoleFindingKind.CONDITIONAL
        assert "ABAC condition" in _message(access)
        assert access.remediation is not None
        assert access.remediation.role_definition_id == AZURE_OPENAI_USER_ROLE_ID

    @pytest.mark.asyncio
    async def test_a_condition_on_an_unread_built_in_is_still_conditional(self) -> None:
        fake = FakeCognitiveServices(role_definitions_status=403)
        _grant(fake, COGNITIVE_SERVICES_USER_ROLE_ID, condition=ATTRIBUTE_CONDITION)

        access = await _check(fake)

        assert access.can_invoke is False
        assert access.reason == RuntimeAccessReason.CONDITIONAL

    @pytest.mark.asyncio
    async def test_a_condition_that_holds_for_every_published_route_counts(self) -> None:
        fake = FakeCognitiveServices()
        _grant(
            fake,
            AZURE_OPENAI_USER_ROLE_ID,
            condition=(
                "(!(ActionMatches{'Microsoft.CognitiveServices/accounts/OpenAI/assistants/*'})) "
                f"OR ({ATTRIBUTE_CONDITION})"
            ),
        )

        access = await _check(fake)

        assert access.can_invoke is True

    @pytest.mark.asyncio
    async def test_an_unconditional_grant_beside_a_conditional_one_wins(self) -> None:
        fake = FakeCognitiveServices()
        _grant(fake, AZURE_OPENAI_USER_ROLE_ID, condition=ATTRIBUTE_CONDITION)
        _grant(fake, COGNITIVE_SERVICES_USER_ROLE_ID)

        access = await _check(fake)

        assert access.can_invoke is True
        assert access.granted_role_definition_id == COGNITIVE_SERVICES_USER_ROLE_ID


class TestDenyAssignments:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "principals",
        [GATEWAY_PRINCIPAL, [{"id": ALL_PRINCIPALS, "type": "SystemDefined"}]],
    )
    async def test_a_deny_that_applies_blocks_a_sufficient_role(
        self, principals: list[dict[str, str]]
    ) -> None:
        fake = FakeCognitiveServices()
        _grant(fake, AZURE_OPENAI_USER_ROLE_ID)
        fake.deny_assignments = [
            deny_assignment(
                SUBSCRIPTION_SCOPE,
                data_actions=["Microsoft.CognitiveServices/accounts/OpenAI/*"],
                principals=principals,
            )
        ]

        access = await _check(fake)

        assert access.can_invoke is False
        assert access.evaluation == RuntimeAccessEvaluation.ROLE_ASSIGNMENTS
        assert access.reason == RuntimeAccessReason.DENY_ASSIGNMENT
        # The role is fine; another role would not help, so none is offered.
        assert access.granted_role_definition_id == AZURE_OPENAI_USER_ROLE_ID
        assert access.remediation is None
        assert "deny assignment deployment-stack-deny" in _message(access)

    @pytest.mark.asyncio
    async def test_a_deny_that_depends_on_a_group_is_unconfirmed(self) -> None:
        fake = FakeCognitiveServices()
        _grant(fake, AZURE_OPENAI_USER_ROLE_ID)
        fake.deny_assignments = [
            deny_assignment(
                AI_RESOURCE_ID,
                data_actions=["Microsoft.CognitiveServices/*"],
                principals=[{"id": GROUP_ID, "type": "Group"}],
            )
        ]

        access = await _check(fake)

        assert access.can_invoke is False
        assert access.evaluation == RuntimeAccessEvaluation.NOT_EVALUATED
        assert access.reason == RuntimeAccessReason.DENY_ASSIGNMENT
        assert "not a denial" in _message(access)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "deny",
        [
            deny_assignment(
                AI_RESOURCE_ID,
                data_actions=["Microsoft.CognitiveServices/*"],
                principals=[{"id": ALL_PRINCIPALS, "type": "SystemDefined"}],
                exclude_principals=GATEWAY_PRINCIPAL,
            ),
            deny_assignment(
                AI_RESOURCE_ID,
                data_actions=["Microsoft.CognitiveServices/accounts/OpenAI/assistants/*"],
                principals=GATEWAY_PRINCIPAL,
            ),
            deny_assignment(
                SUBSCRIPTION_SCOPE,
                data_actions=["Microsoft.CognitiveServices/*"],
                principals=GATEWAY_PRINCIPAL,
                do_not_apply_to_child_scopes=True,
            ),
            deny_assignment(
                AI_RESOURCE_ID,
                data_actions=["Microsoft.CognitiveServices/*"],
                principals=[{"id": "55555555-5555-5555-5555-555555555555", "type": "User"}],
            ),
        ],
        ids=["gateway-excluded", "other-actions", "not-inherited", "someone-else"],
    )
    async def test_a_deny_that_does_not_apply_leaves_access_standing(
        self, deny: dict[str, Any]
    ) -> None:
        fake = FakeCognitiveServices()
        _grant(fake, AZURE_OPENAI_USER_ROLE_ID)
        fake.deny_assignments = [deny]

        access = await _check(fake)

        assert access.can_invoke is True

    @pytest.mark.asyncio
    async def test_unreadable_deny_assignments_leave_the_verdict_standing(self) -> None:
        # Only Azure creates deny assignments, and not being able to list them is not evidence of
        # one. Treating it as a block would turn every Reader-less check into a false negative.
        fake = FakeCognitiveServices(deny_assignments_status=403)
        _grant(fake, AZURE_OPENAI_USER_ROLE_ID)

        access = await _check(fake)

        assert access.can_invoke is True


class TestNetworkPath:
    @pytest.mark.asyncio
    async def test_private_resource_and_a_gateway_without_a_network_is_unreachable(self) -> None:
        # A classic tier gateway with no virtual network has no path to a resource whose public
        # network access is disabled, whatever roles it holds.
        fake = FakeCognitiveServices()
        _grant(fake, AZURE_OPENAI_USER_ROLE_ID)

        access = await _check(fake, capabilities=_network(public_network_access="Disabled"))

        assert access.can_invoke is False
        assert access.evaluation == RuntimeAccessEvaluation.ROLE_ASSIGNMENTS
        assert access.reason == RuntimeAccessReason.NETWORK_UNREACHABLE
        assert access.network_reachability == NetworkReachability.UNREACHABLE
        assert _message(access).startswith(
            "Public network access to this resource is disabled, and "
            f"{SERVICE_NAME} is not connected to a virtual network"
        )
        assert access.granted_role_definition_id == AZURE_OPENAI_USER_ROLE_ID
        assert access.remediation is None

    @pytest.mark.asyncio
    async def test_unreachable_with_a_missing_role_still_offers_the_role(self) -> None:
        fake = FakeCognitiveServices()

        access = await _check(fake, capabilities=_network(public_network_access="Disabled"))

        assert access.reason == RuntimeAccessReason.NETWORK_UNREACHABLE
        assert access.remediation is not None
        assert "does not hold a role" in _message(access)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("network_type", ["External", "Internal"])
    async def test_private_resource_and_a_gateway_in_a_network_is_unverified(
        self, network_type: str
    ) -> None:
        fake = FakeCognitiveServices()
        _grant(fake, AZURE_OPENAI_USER_ROLE_ID)

        access = await _check(
            fake,
            gateway=_gateway(virtual_network_type=network_type),
            capabilities=_network(public_network_access="Disabled"),
        )

        assert access.can_invoke is False
        assert access.evaluation == RuntimeAccessEvaluation.NOT_EVALUATED
        assert access.reason == RuntimeAccessReason.NETWORK_UNVERIFIED
        assert access.network_reachability == NetworkReachability.UNVERIFIED
        assert "not a denial" in _message(access)

    @pytest.mark.asyncio
    async def test_a_gateway_network_never_read_is_unverified(self) -> None:
        fake = FakeCognitiveServices()
        _grant(fake, AZURE_OPENAI_USER_ROLE_ID)

        access = await _check(
            fake,
            gateway=_gateway(virtual_network_type=None),
            capabilities=_network(public_network_access="Disabled"),
        )

        assert access.reason == RuntimeAccessReason.NETWORK_UNVERIFIED
        assert "Re-run the gateway's access check" in _message(access)

    @pytest.mark.asyncio
    async def test_a_firewall_admitting_the_gateway_is_reachable(self) -> None:
        fake = FakeCognitiveServices()
        _grant(fake, AZURE_OPENAI_USER_ROLE_ID)

        access = await _check(
            fake,
            capabilities=_network(
                network_default_action="Deny", network_ip_rules=["203.0.113.0/24"]
            ),
        )

        assert access.can_invoke is True
        assert access.network_reachability == NetworkReachability.REACHABLE

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("rules", "egress"),
        [
            (["198.51.100.7"], (APIM_PUBLIC_IP,)),
            (["203.0.113.0/24"], ()),
            (["203.0.113.10"], (APIM_PUBLIC_IP, "198.51.100.20")),
        ],
        ids=["not-listed", "no-known-egress", "one-region-missing"],
    )
    async def test_a_firewall_mosaic_cannot_match_is_unverified(
        self, rules: list[str], egress: tuple[str, ...]
    ) -> None:
        fake = FakeCognitiveServices()
        _grant(fake, AZURE_OPENAI_USER_ROLE_ID)

        access = await _check(
            fake,
            gateway=_gateway(egress=egress),
            capabilities=_network(network_default_action="Deny", network_ip_rules=rules),
        )

        assert access.can_invoke is False
        assert access.reason == RuntimeAccessReason.NETWORK_UNVERIFIED
        assert "firewall" in _message(access)

    def test_without_the_resource_network_settings_the_path_is_unknown(self) -> None:
        # Reported as unknown rather than guessed: the role verdict stands on its own.
        assert evaluate_network_path(None, _gateway()).reachability == NetworkReachability.UNKNOWN
        unread = evaluate_network_path(ModelEndpointCapabilities(), _gateway())
        assert unread.reachability == NetworkReachability.UNKNOWN
