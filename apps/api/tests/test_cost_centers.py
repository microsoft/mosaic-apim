"""Cost-center domain and administrator API contracts after retiring local membership."""

import base64
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from cost_center_management_double import (
    BASELINE,
    GROUP,
    OTHER_GROUP,
    TENANT,
    USER,
    WORKLOAD,
    Management,
    audit,
    management,
)
from fastapi.testclient import TestClient
from mosaic_api.auth import LocalAuthenticator
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from mosaic_api.cost_center_directory import EntraGroupBinding
from mosaic_api.cost_centers import (
    SUBSCRIPTION_COUNTER,
    CostCenter,
    CostCenterBook,
    CostCenterCreate,
    CostCenterLimit,
    CostCenterMember,
    CostCenterSettings,
    PersonLimits,
    PooledQuota,
    cost_center_settings_id,
    general_cost_center,
    may_charge,
    normalize_code,
)
from mosaic_api.domain import (
    DirectoryObject,
    Entitlement,
    EntitlementResource,
    EntitlementSubject,
    ModelApi,
    Principal,
    entitlement_id,
    general_cost_center_id,
    utc_now,
)
from mosaic_api.integrations.graph import GroupFailureKind, GroupManagementError, GroupWriteOutcome
from mosaic_api.main import create_app
from mosaic_api.model_pools import ModelPool, PoolModel
from mosaic_api.services.directory import Actor
from mosaic_api.services.model_access import cost_center_intent, entitlement_intent_digest

GENERAL = general_cost_center_id(TENANT)
RESOURCE = EntitlementResource(kind="modelApi", id="modelApi_seed")


@pytest.fixture
def estate() -> Management:
    return management()


@pytest.fixture
def client(estate: Management) -> Iterator[TestClient]:
    settings = Settings(
        _env_file=None,
        environment=Environment.TEST,
        auth_mode=AuthMode.LOCAL,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id=TENANT,
    )
    with TestClient(
        create_app(settings, directory_lookup=estate.lookup, group_management=estate.graph)
    ) as test_client:
        estate.lookup.gateways = test_client.app.state.gateway_repository
        yield test_client


def ok(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json() if response.content else None


def create(client: TestClient, name: str = "Research", code: str = "RES", **values: Any) -> Any:
    return ok(
        client.post(
            "/api/v1/cost-centers",
            json={
                "name": name,
                "code": code,
                **values,
            },
        ),
        201,
    )


def ready(client: TestClient, group_id: str = GROUP, **values: Any) -> Any:
    return create(
        client,
        groupSetup={
            "mode": "attach",
            "groupObjectId": group_id,
            "acknowledgeMembershipChanges": True,
        },
        **values,
    )


def principal(client: TestClient, object_id: str = USER, **values: Any) -> Any:
    return ok(
        client.post(
            "/api/v1/principals",
            json={
                "objectId": object_id,
                "kind": "user",
                **values,
            },
        ),
        201,
    )


def bound_center(center_id: str = "center") -> CostCenter:
    return CostCenter(
        id=center_id,
        tenant_id=TENANT,
        name="Research",
        code="RES",
        entra_binding=EntraGroupBinding(
            directory_tenant_id=TENANT,
            group_object_id=GROUP,
            provenance="attached",
            verification_state="verified",
            verified_at=utc_now(),
        ),
    )


@pytest.mark.parametrize("code", ["CC-01", "research.eu", "a_b", "x" * 64, " padded "])
def test_codes_are_letters_digits_dots_hyphens_and_underscores(code: str) -> None:
    assert normalize_code(code) == code.strip()


@pytest.mark.parametrize("code", ["", "has space", "x" * 65, "café", "a/b", "{named}"])
def test_other_codes_are_refused(code: str) -> None:
    with pytest.raises(ValueError, match="letters, digits"):
        normalize_code(code)


def test_owners_are_distinct_email_addresses_not_members_or_entra_owners() -> None:
    request = CostCenterCreate(
        name="Research",
        code="RES",
        owners=["Ana@example.test", "ana@example.test", "b@example.test"],
    )
    assert request.owners == ["Ana@example.test", "b@example.test"]
    with pytest.raises(ValueError, match="email"):
        CostCenterCreate(name="Research", code="RES", owners=["not an address"])


def test_limits_need_something_to_limit_and_mcp_servers_count_calls() -> None:
    with pytest.raises(ValueError, match="at least one"):
        PersonLimits()
    with pytest.raises(ValueError, match="period"):
        PersonLimits(token_quota=10)
    with pytest.raises(ValueError, match="monthly"):
        PooledQuota()
    with pytest.raises(ValueError, match="calls only"):
        CostCenterLimit(
            resource=EntitlementResource(kind="mcpServer", id="server"),
            pool=PooledQuota(monthly_tokens=10),
        )
    with pytest.raises(ValueError, match="model APIs, MCP servers, and model pool models"):
        CostCenterLimit(
            resource=EntitlementResource(kind="product", id="product", scope_id="gateway"),
            pool=PooledQuota(monthly_calls=10),
        )
    with pytest.raises(ValueError, match="scope"):
        EntitlementResource(kind="poolModel", id="poolModel_1")


def test_per_person_limits_become_grant_limits_on_the_grants_own_counter() -> None:
    limits = PersonLimits(
        tokens_per_minute=1000, token_quota=50000, token_quota_period="Monthly", calls_per_minute=5
    ).enforcement()
    assert limits.tokens is not None and limits.requests is not None
    assert limits.tokens.counter_key_expression == SUBSCRIPTION_COUNTER
    assert limits.tokens.tokens_per_minute == 1000 and limits.tokens.token_quota == 50000
    assert limits.requests.calls == 5 and limits.requests.renewal_period_seconds == 60


def test_custom_chargeability_requires_verified_real_group_not_defaults_or_local_roster() -> None:
    person = Principal(
        id="person", tenant_id=TENANT, object_id=USER, kind="user", default_cost_center_id="legacy"
    )
    legacy = CostCenter(
        id="legacy",
        tenant_id=TENANT,
        name="Old",
        code="old",
        members=[CostCenterMember(principal_id=person.id)],
    )
    settings = CostCenterSettings(
        id=cost_center_settings_id(TENANT), tenant_id=TENANT, default_cost_center_id=legacy.id
    )
    assert not may_charge(
        legacy, principal=person, settings=settings, group_principal_ids=[person.id]
    )
    center = bound_center()
    assert not may_charge(center, principal=person, settings=settings)
    assert not may_charge(center, principal=person, settings=settings, group_principal_ids=[GROUP])
    assert may_charge(center, principal=person, settings=settings, group_object_ids=[GROUP.upper()])
    assert may_charge(center, principal=None, settings=None, group_object_ids=[GROUP])
    matching_group = Principal(
        id="bound-group", tenant_id=TENANT, object_id=GROUP, kind="securityGroup"
    )
    assert may_charge(center, principal=matching_group, settings=None)
    assert may_charge(general_cost_center(TENANT), principal=None, settings=None)


def test_every_tenant_has_stable_general_and_it_is_the_initial_default() -> None:
    book = CostCenterBook(TENANT, [], None)
    general = book.get(GENERAL)
    assert general is not None and general.is_general and general.built_in
    assert general.setup_state == "exempt" and general.code == "general"
    assert book.tenant_default_id == GENERAL and book.default_for(None) == GENERAL


def test_grant_identity_includes_the_cost_center() -> None:
    subject = EntitlementSubject(kind="user", id="person")
    assert entitlement_id(TENANT, subject, RESOURCE, GENERAL) != entitlement_id(
        TENANT, subject, RESOURCE, "research"
    )


def test_general_key_restrictions_change_individual_intent_only() -> None:
    person = Principal(id="person", tenant_id=TENANT, object_id=USER, kind="user")
    allowed = CostCenterBook(TENANT, [general_cost_center(TENANT)], None)
    off = CostCenterBook(
        TENANT, [general_cost_center(TENANT).model_copy(update={"keys_allowed": False})], None
    )
    grant = Entitlement(
        id="grant",
        tenant_id=TENANT,
        subject=EntitlementSubject(kind="user", id=person.id),
        resource=RESOURCE,
        cost_center_id=GENERAL,
    )
    assert (
        len(
            {
                entitlement_intent_digest(grant, person, cost_center_intent(grant, person, book))
                for book in (allowed, off)
            }
        )
        == 2
    )


def test_general_admin_view_is_exempt_and_renamable_not_deletable(client: TestClient) -> None:
    [general] = ok(client.get("/api/v1/cost-centers"))
    assert general["id"] == GENERAL and general["isTenantDefault"]
    assert general["setupState"] == "exempt"
    renamed = ok(
        client.patch(f"/api/v1/cost-centers/{GENERAL}", json={"name": "Shared", "code": "SHARED"})
    )
    assert renamed["setupState"] == "exempt" and renamed["builtIn"]
    assert client.delete(f"/api/v1/cost-centers/{GENERAL}").status_code == 409
    assert (
        client.patch(
            f"/api/v1/cost-centers/{GENERAL}",
            json={
                "groupSetup": {"mode": "create"},
            },
        ).status_code
        == 422
    )


def test_draft_custom_centers_are_saved_but_not_usable_or_projected_to_groups(
    client: TestClient,
) -> None:
    created = create(client, owners=["contact@example.test"], keysAllowed=True)
    assert created["setupState"] == "setupRequired" and created["entraBinding"] is None
    assert created["groupId"] is None and created["members"] == created["memberDetails"] == []
    assert created["keysAllowed"] is True and created["directoryOperations"] == []
    assert ok(client.get("/api/v1/groups")) == []
    assert (
        client.put(
            "/api/v1/cost-center-settings",
            json={
                "defaultCostCenterId": created["id"],
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/principals",
            json={
                "objectId": USER,
                "kind": "user",
                "defaultCostCenterId": created["id"],
            },
        ).status_code
        == 422
    )
    assert [item["id"] for item in ok(client.get("/api/v1/portal/cost-centers"))] == [GENERAL]


def test_codes_are_unique_case_insensitively_and_names_cannot_be_blank(client: TestClient) -> None:
    created = create(client)
    assert (
        client.post(
            "/api/v1/cost-centers",
            json={
                "name": "Other",
                "code": "res",
            },
        ).json()["details"]["reason"]
        == "codeInUse"
    )
    assert (
        client.patch(
            f"/api/v1/cost-centers/{created['id']}",
            json={
                "code": "GENERAL",
            },
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/v1/cost-centers",
            json={
                "name": "  ",
                "code": "blank",
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/cost-centers",
            json={
                "name": "Bad",
                "code": "has space",
            },
        ).status_code
        == 422
    )


def test_attach_requires_acknowledgment_and_rejects_client_tenant_or_url(
    client: TestClient,
) -> None:
    for setup in [
        {"mode": "attach", "groupObjectId": GROUP},
        {"mode": "attach", "groupObjectId": GROUP, "acknowledgeMembershipChanges": False},
        {
            "mode": "attach",
            "groupObjectId": GROUP,
            "acknowledgeMembershipChanges": True,
            "directoryTenantId": USER,
        },
        {
            "mode": "attach",
            "groupObjectId": "https://graph.microsoft.com/groups/anything",
            "acknowledgeMembershipChanges": True,
        },
    ]:
        assert (
            client.post(
                "/api/v1/cost-centers",
                json={
                    "name": "Research",
                    "code": "res",
                    "groupSetup": setup,
                },
            ).status_code
            == 422
        )
    assert len(ok(client.get("/api/v1/cost-centers"))) == 1
    configured = ready(client)
    assert configured["setupState"] == "ready"
    assert configured["entraBinding"]["groupObjectId"] == GROUP
    assert configured["entraBinding"]["provenance"] == "attached"


def test_create_failure_returns_saved_setup_required_center_with_a_tracked_error(
    client: TestClient,
    estate: Management,
) -> None:
    estate.graph.failures["create_group"] = GroupManagementError(
        "Fictional missing Group.Create consent", kind=GroupFailureKind.FORBIDDEN
    )
    created = create(client, groupSetup={"mode": "create"})
    assert created["setupState"] == "setupRequired"
    [operation] = created["directoryOperations"]
    assert operation["state"] == "failed" and operation["canRetry"]
    assert operation["errorCode"] == "forbidden"
    assert not operation["directoryUpdated"] and not estate.graph.writes
    estate.graph.failures.clear()
    retried = ok(
        client.post(
            f"/api/v1/cost-centers/{created['id']}/directory-operations/{operation['id']}/retry"
        )
    )
    assert retried["state"] == "succeeded"
    assert ok(client.get(f"/api/v1/cost-centers/{created['id']}"))["setupState"] == "ready"


def test_capability_route_is_advisory_read_only_and_not_caught_by_center_id_route(
    client: TestClient,
    estate: Management,
) -> None:
    estate.lookup._application_permissions = frozenset(BASELINE)
    capabilities = ok(client.get("/api/v1/cost-centers/group-capabilities"))
    assert capabilities["advisory"] and capabilities["canReadGroups"]
    assert not capabilities["canCreateGroups"] and not capabilities["canAddUsers"]
    assert capabilities["memberListingApi"] == "beta"
    assert "preview" in capabilities["memberListingNotice"]
    assert estate.graph.writes == []


def test_directory_member_routes_require_idempotency_and_return_real_tracked_state(
    client: TestClient,
    estate: Management,
) -> None:
    center = ready(client)
    url = f"/api/v1/cost-centers/{center['id']}/directory-members/{WORKLOAD}"
    assert client.put(url, json={"memberKind": "servicePrincipal"}).status_code == 422
    added = ok(
        client.put(
            url,
            json={"memberKind": "servicePrincipal"},
            headers={"Idempotency-Key": "add-workload"},
        )
    )
    assert added["state"] == "succeeded" and added["directoryUpdated"]
    assert added["propagationNotice"] and not added["keyRevocationNotice"]
    assert estate.lookup.state.group_members[GROUP] == [WORKLOAD]
    repeated = ok(
        client.put(
            url,
            json={"memberKind": "servicePrincipal"},
            headers={"Idempotency-Key": "add-workload"},
        )
    )
    assert repeated["id"] == added["id"] and len(estate.graph.writes) == 1
    assert (
        client.put(
            url,
            json={
                "memberKind": "servicePrincipal",
                "groupObjectId": OTHER_GROUP,
            },
            headers={"Idempotency-Key": "arbitrary"},
        ).status_code
        == 422
    )
    removed = ok(client.delete(url, headers={"Idempotency-Key": "remove-workload"}))
    assert removed["state"] == "succeeded" and removed["keyRevocationNotice"]
    operation_url = f"/api/v1/cost-centers/{center['id']}/directory-operations"
    history = ok(client.get(operation_url))
    assert {item["id"] for item in history} == {added["id"], removed["id"]}
    assert ok(client.get(f"{operation_url}/{removed['id']}"))["state"] == "succeeded"
    assert client.post(f"{operation_url}/{removed['id']}/retry").status_code == 409
    assert ok(client.post(f"{operation_url}/{removed['id']}/recover"))["state"] == "succeeded"


def test_unknown_member_result_is_accepted_not_reported_as_directory_success(
    client: TestClient,
    estate: Management,
) -> None:
    center = ready(client)
    estate.graph.failures["remove_member"] = GroupManagementError(
        "Fictional timed-out write",
        kind=GroupFailureKind.UNKNOWN_OUTCOME,
        outcome=GroupWriteOutcome.UNKNOWN,
    )
    operation = ok(
        client.delete(
            f"/api/v1/cost-centers/{center['id']}/directory-members/{USER}",
            headers={"Idempotency-Key": "uncertain"},
        ),
        202,
    )
    assert operation["state"] == "unknown" and not operation["directoryUpdated"]
    assert operation["recoveryNotice"] and operation["keyRevocationNotice"]
    assert not operation["canRetry"]


def test_operation_openapi_declares_tracked_and_unknown_response_bodies() -> None:
    app = create_app(
        Settings(
            _env_file=None,
            environment="test",
            auth_mode="local",
            repository_backend="memory",
            tenant_id=TENANT,
        )
    )
    paths = app.openapi()["paths"]
    prefix = "/api/v1/cost-centers/{cost_center_id}"
    for path, methods in [
        (f"{prefix}/directory-members/{{object_id}}", ["put", "delete"]),
        (f"{prefix}/directory-operations/{{operation_id}}/retry", ["post"]),
        (f"{prefix}/directory-operations/{{operation_id}}/recover", ["post"]),
    ]:
        for method in methods:
            responses = paths[path][method]["responses"]
            for status in ("200", "202"):
                assert responses[status]["content"]["application/json"]["schema"]["$ref"] == (
                    "#/components/schemas/CostCenterDirectoryOperationView"
                )
            assert "inspect state" in responses["200"]["description"]


def test_paged_member_api_returns_direct_rows_preview_and_scoped_opaque_cursor(
    client: TestClient,
    estate: Management,
) -> None:
    center = ready(client)
    estate.lookup.state.group_members[GROUP] = [USER, WORKLOAD]
    url = f"/api/v1/cost-centers/{center['id']}/directory-members"
    first = ok(client.get(url, params={"pageSize": 1}))
    assert len(first["members"]) == 1 and first["nextCursor"]
    assert first["members"][0]["membershipType"] == "direct"
    assert "Graph beta" in first["previewNotice"]
    second = ok(client.get(url, params={"pageSize": 1, "cursor": first["nextCursor"]}))
    assert len(second["members"]) == 1 and second["nextCursor"] is None
    assert client.get(url, params={"pageSize": 1000}).status_code == 422
    assert client.get(url, params={"cursor": "not-base64!"}).status_code == 422
    foreign_cursor = base64.urlsafe_b64encode(
        f"https://graph.microsoft.com/beta/groups/{OTHER_GROUP}/members?$skiptoken=1".encode()
    ).decode()
    assert client.get(url, params={"cursor": foreign_cursor}).status_code == 502


async def test_defaults_are_preferences_and_do_not_grant_custom_membership(
    client: TestClient,
    estate: Management,
) -> None:
    center = ready(client)
    ok(client.put("/api/v1/cost-center-settings", json={"defaultCostCenterId": center["id"]}))
    person = principal(client)
    assert person["defaultCostCenterId"] == center["id"]
    service = client.app.state.cost_center_service
    outsider = await service.chargeable_for_caller(Actor(object_id=USER, tenant_id=TENANT))
    assert center["id"] not in {item.id for item in outsider}
    member = await service.chargeable_for_caller(
        Actor(object_id=USER, tenant_id=TENANT, group_ids=frozenset({GROUP}))
    )
    assert [(item.id, item.is_default) for item in member] == [(center["id"], True)]
    overage = await service.chargeable_for_caller(
        Actor(object_id=USER, tenant_id=TENANT, group_ids=frozenset({GROUP}), groups_overage=True)
    )
    assert center["id"] not in {item.id for item in overage}
    assert estate.lookup.state.group_members.get(GROUP, []) == []


def test_cost_center_detail_does_not_misrepresent_defaults_as_directory_members(
    client: TestClient,
) -> None:
    center = ready(client)
    principal(client, defaultCostCenterId=center["id"])
    view = ok(client.get(f"/api/v1/cost-centers/{center['id']}"))
    assert view["defaultFor"] == 1 and view["members"] == view["memberDetails"] == []
    assert (
        client.put(f"/api/v1/cost-centers/{center['id']}/members/missing-principal").status_code
        == 404
    )


async def test_limits_keep_resource_validation_and_exact_pool_scope(client: TestClient) -> None:
    center = create(client)
    url = f"/api/v1/cost-centers/{center['id']}/limits"
    model_limit = {
        "resource": {"kind": "modelApi", "id": RESOURCE.id},
        "person": {"tokensPerMinute": 2000},
        "pool": {"monthlyTokens": 1000000},
    }
    assert client.put(url, json={"limits": [model_limit]}).status_code == 422
    gateways = client.app.state.gateway_repository
    await gateways.save_model_api(
        ModelApi(
            id=RESOURCE.id,
            tenant_id=TENANT,
            gateway_id="gateway",
            api_name="chat",
            display_name="Chat",
            path="chat",
            imported_from_snapshot_id="snapshot",
        ),
        audit(),
    )
    assert ok(client.put(url, json={"limits": [model_limit]}))["limits"][0]["pool"] == {
        "monthlyTokens": 1000000,
        "monthlyCalls": None,
    }
    assert client.put(url, json={"limits": [model_limit, model_limit]}).status_code == 422
    pool = ModelPool(
        id="pool",
        tenant_id=TENANT,
        gateway_id="gateway",
        display_name="Pool",
        vendor="Fictional",
        api_name="pool",
        api_path="pool",
        fragment_name="pool",
        product_name="pool",
        subscription_name="pool",
        models=[
            PoolModel(
                id="model", public_name="model", display_name="Model", backend_pool_name="model"
            )
        ],
    )
    await gateways.save_model_pool(pool, audit())
    pool_limit = {
        "resource": {"kind": "poolModel", "id": "model", "scopeId": "pool"},
        "pool": {"monthlyTokens": 1000},
    }
    assert (
        ok(client.put(url, json={"limits": [pool_limit]}))["limits"][0]["resource"]
        == (pool_limit["resource"])
    )
    pool_limit["resource"]["scopeId"] = "wrong-pool"  # type: ignore[index]
    assert client.put(url, json={"limits": [pool_limit]}).status_code == 422


def test_resource_access_route_uses_validation_then_desired_grant_sync(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    center = ready(client)
    entitlements = client.app.state.entitlement_service
    validate, sync = AsyncMock(), AsyncMock(return_value=[])
    monkeypatch.setattr(entitlements, "validate_cost_center_access", validate)
    monkeypatch.setattr(entitlements, "sync_cost_center_access", sync)
    definitions = [
        {
            "resource": {"kind": "modelApi", "id": RESOURCE.id, "scopeId": None},
            "mode": "requestOnly",
        }
    ]
    updated = ok(
        client.patch(f"/api/v1/cost-centers/{center['id']}", json={"resourceAccess": definitions})
    )
    assert updated["resourceAccess"] == definitions
    assert validate.await_count == sync.await_count == 1
    assert validate.await_args.args[1].resource_access[0].mode == "requestOnly"
    assert sync.await_args.args[1] == center["id"]


def test_generic_group_crud_remains_local_and_separate_from_cost_center_graph_writes(
    client: TestClient,
    estate: Management,
) -> None:
    center = ready(client)
    person = principal(client)
    group = ok(client.post("/api/v1/groups", json={"name": "Local reporting"}), 201)
    membership_url = f"/api/v1/groups/{group['id']}/members/{person['id']}"
    ok(client.put(membership_url), 201)
    assert len(ok(client.get(f"/api/v1/groups/{group['id']}/members"))) == 1
    assert client.delete(membership_url).status_code == 204
    assert client.delete(f"/api/v1/groups/{group['id']}").status_code == 204
    assert estate.graph.writes == []
    assert ok(client.get(f"/api/v1/cost-centers/{center['id']}"))["members"] == []
    assert client.post(f"/api/v1/cost-centers/{center['id']}/group").status_code == 404


def test_principal_kind_and_default_update_verifies_directory_outside_cosmos_lease(
    client: TestClient,
    estate: Management,
) -> None:
    person = principal(client)
    estate.lookup.state.objects[USER] = DirectoryObject(
        object_id=USER,
        kind="agentUser",
        display_name="Fictional agent user",
        identity_parent_id=OTHER_GROUP,
    )
    updated = ok(
        client.patch(
            f"/api/v1/principals/{person['id']}",
            json={"kind": "agentUser", "defaultCostCenterId": GENERAL},
        )
    )
    assert updated["kind"] == "agentUser" and updated["directoryVerifiedAt"]
    assert updated["defaultCostCenterId"] == GENERAL
    assert estate.graph.writes == []


def test_management_routes_remain_admin_only(client: TestClient) -> None:
    client.app.state.authenticator = LocalAuthenticator(TENANT, ["User"])
    for response in [
        client.get("/api/v1/cost-centers/group-capabilities"),
        client.get("/api/v1/cost-centers/center/directory-members"),
        client.put(
            "/api/v1/cost-centers/center/directory-members/" + USER,
            json={"memberKind": "user"},
            headers={"Idempotency-Key": "admin-only"},
        ),
        client.delete(
            "/api/v1/cost-centers/center/directory-members/" + USER,
            headers={"Idempotency-Key": "admin-only"},
        ),
        client.get("/api/v1/cost-centers/center/directory-operations"),
        client.post("/api/v1/cost-centers/center/directory-operations/op/recover"),
        client.post("/api/v1/cost-centers/center/directory-operations/op/retry"),
    ]:
        assert response.status_code == 403


def test_local_default_di_never_creates_fake_directory_success() -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        auth_mode="local",
        repository_backend="memory",
        tenant_id=TENANT,
    )
    with TestClient(create_app(settings)) as client:
        capabilities = ok(client.get("/api/v1/cost-centers/group-capabilities"))
        assert not capabilities["available"]
        created = create(client, groupSetup={"mode": "create"})
        assert created["setupState"] == "setupRequired"
        assert created["directoryOperations"][0]["state"] == "failed"
        assert client.app.state.group_management is None
        assert ok(client.get("/api/v1/groups")) == []


def test_retired_feature_flag_cannot_reenable_local_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MOSAIC_MANAGED_COST_CENTER_GROUPS", "true")
    settings = Settings(
        _env_file=None,
        environment="test",
        auth_mode="local",
        repository_backend="memory",
        tenant_id=TENANT,
    )
    assert "managed_cost_center_groups" not in Settings.model_fields
    with TestClient(create_app(settings)) as client:
        created = create(client)
        assert created["groupId"] is None and created["setupState"] == "setupRequired"
        assert ok(client.get("/api/v1/groups")) == []


def test_non_guid_legacy_tenant_keeps_baseline_api_but_disables_directory_writes(
    estate: Management,
) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        auth_mode="entra",
        repository_backend="memory",
        tenant_id="tenant-fixture",
        api_client_id="fictional-api",
    )
    with TestClient(create_app(settings, directory_lookup=estate.lookup)) as client:
        client.app.state.authenticator = LocalAuthenticator(settings.tenant_id, ["Admin"])
        assert client.get("/healthz").status_code == 200
        assert client.app.state.group_management is None
        capabilities = ok(client.get("/api/v1/cost-centers/group-capabilities"))
        assert not capabilities["available"]
        assert "tenant GUID" in capabilities["message"]
        assert estate.lookup.reads == 0


def test_government_di_keeps_new_group_workflows_unavailable(estate: Management) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        auth_mode="local",
        repository_backend="memory",
        tenant_id=TENANT,
        graph_endpoint="https://graph.microsoft.us",
    )
    with TestClient(
        create_app(settings, directory_lookup=estate.lookup, group_management=estate.graph)
    ) as client:
        capabilities = ok(client.get("/api/v1/cost-centers/group-capabilities"))
        assert not capabilities["available"] and "Government" in capabilities["message"]
        created = create(client, groupSetup={"mode": "create"})
        assert created["setupState"] == "setupRequired" and estate.graph.writes == []
        assert client.app.state.group_management is None


def test_fake_adapter_lifecycle_is_closed_without_touching_real_cloud(estate: Management) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        auth_mode="local",
        repository_backend="memory",
        tenant_id=TENANT,
    )
    with TestClient(
        create_app(settings, directory_lookup=estate.lookup, group_management=estate.graph)
    ):
        assert not estate.graph.closed and not estate.lookup.closed
    assert estate.graph.closed and estate.lookup.closed
