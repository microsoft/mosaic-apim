"""Cost centers: codes, defaults, who may charge them, grants per cost center, and removal.

See ADR 0021. The gateway side is in ``test_cost_center_policy`` and keys in
``test_cost_center_keys``.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
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
    may_charge,
    normalize_code,
)
from mosaic_api.domain import (
    AccessRequestApproval,
    AccessRequestCreate,
    AuditEvent,
    Entitlement,
    EntitlementCreate,
    EntitlementResource,
    EntitlementSubject,
    ModelApi,
    Principal,
    PrincipalCreate,
    PrincipalKind,
    entitlement_id,
    general_cost_center_id,
    new_id,
)
from mosaic_api.errors import ConflictError, NotFoundError, ValidationError
from mosaic_api.main import create_app
from mosaic_api.repositories import InMemoryCostCenterRepository, InMemoryGatewayRepository
from mosaic_api.services.cost_centers import CostCenterService
from mosaic_api.services.directory import Actor, DirectoryService
from mosaic_api.services.entitlements import EntitlementService
from mosaic_api.services.model_access import cost_center_intent, entitlement_intent_digest

TENANT = "tenant-test"
ADMIN = Actor(object_id="local-admin", tenant_id=TENANT)
GENERAL = general_cost_center_id(TENANT)
RESOURCE = EntitlementResource(kind="modelApi", id="modelApi_seed")
PERSON = "0f5c9a3e-1111-4c2b-9d7e-000000000001"
OTHER = "0f5c9a3e-1111-4c2b-9d7e-000000000002"
GROUP = "0f5c9a3e-2222-4c2b-9d7e-00000000000a"


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings = Settings(
        environment=Environment.TEST,
        auth_mode=AuthMode.LOCAL,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id=TENANT,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _audit() -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id=TENANT,
        action="test.seed",
        resource_type="test",
        resource_id="seed",
        actor_object_id="local-admin",
    )


async def _seed_model(client: TestClient) -> None:
    gateways: InMemoryGatewayRepository = client.app.state.gateway_repository
    await gateways.save_model_api(
        ModelApi(
            id=RESOURCE.id,
            tenant_id=TENANT,
            gateway_id="gateway_seed",
            api_name="chat",
            display_name="Chat completions",
            path="chat",
            imported_from_snapshot_id="snapshot-1",
        ),
        _audit(),
    )


def _ok(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json() if response.content else None


def _create(client: TestClient, name: str, code: str, **values: object) -> dict[str, Any]:
    body: dict[str, Any] = _ok(
        client.post("/api/v1/cost-centers", json={"name": name, "code": code, **values}), 201
    )
    return body


def _principal(
    client: TestClient, object_id: str, kind: str = "user", **values: object
) -> dict[str, Any]:
    body: dict[str, Any] = _ok(
        client.post(
            "/api/v1/principals",
            json={
                "objectId": object_id,
                "kind": kind,
                "label": f"{kind} {object_id[-2:]}",
                **values,
            },
        ),
        201,
    )
    return body


def _grant(
    client: TestClient, principal: dict[str, Any], cost_center_id: str | None = None, **values: Any
) -> Any:
    kind = "securityGroup" if principal["kind"] == "securityGroup" else "user"
    payload: dict[str, Any] = {
        "subject": {"kind": kind, "id": principal["id"]},
        "resource": {"kind": "modelApi", "id": RESOURCE.id},
        **values,
    }
    if cost_center_id is not None:
        payload["costCenterId"] = cost_center_id
    return client.post("/api/v1/entitlements", json=payload)


def _service(client: TestClient) -> EntitlementService:
    service: EntitlementService = client.app.state.entitlement_service
    return service


def _cost_centers(client: TestClient) -> CostCenterService:
    service: CostCenterService = client.app.state.cost_center_service
    return service


# -- the domain ---------------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["CC-01", "research.eu", "a_b", "x" * 64, " padded "])
def test_codes_are_letters_digits_dots_hyphens_and_underscores(code: str) -> None:
    assert normalize_code(code) == code.strip()


@pytest.mark.parametrize("code", ["", "has space", "x" * 65, "café", "a/b", "{named}"])
def test_other_codes_are_refused(code: str) -> None:
    with pytest.raises(ValueError, match="letters, digits"):
        normalize_code(code)


def test_owners_are_distinct_email_addresses() -> None:
    created = CostCenterCreate(
        name="Research", code="RES", owners=["Ana@example.com", "ana@example.com", "b@example.com"]
    )
    assert created.owners == ["Ana@example.com", "b@example.com"]
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
    with pytest.raises(ValueError, match="model APIs and MCP servers"):
        CostCenterLimit(
            resource=EntitlementResource(kind="product", id="product", scope_id="gateway"),
            pool=PooledQuota(monthly_calls=10),
        )


def test_per_person_limits_become_grant_limits_on_the_grants_own_counter() -> None:
    enforcement = PersonLimits(
        tokens_per_minute=1000, token_quota=50000, token_quota_period="Monthly", calls_per_minute=5
    ).enforcement()
    assert enforcement.tokens is not None and enforcement.requests is not None
    assert enforcement.tokens.counter_key_expression == SUBSCRIPTION_COUNTER
    assert enforcement.tokens.tokens_per_minute == 1000
    assert enforcement.tokens.token_quota == 50000
    assert enforcement.requests.calls == 5
    assert enforcement.requests.renewal_period_seconds == 60


def _cost_center(cost_center_id: str, *members: str) -> CostCenter:
    return CostCenter(
        id=cost_center_id,
        tenant_id=TENANT,
        name=cost_center_id,
        code=cost_center_id,
        members=[CostCenterMember(principal_id=member) for member in members],
    )


def _person(
    principal_id: str, *, default: str | None = None, kind: PrincipalKind = PrincipalKind.USER
) -> Principal:
    return Principal(
        id=principal_id,
        tenant_id=TENANT,
        object_id=f"object-{principal_id}",
        kind=kind,
        default_cost_center_id=default,
    )


def test_who_may_charge_a_cost_center() -> None:
    research = _cost_center("research", "member", "group")
    settings = CostCenterSettings(
        id=cost_center_settings_id(TENANT), tenant_id=TENANT, default_cost_center_id="tenant"
    )
    tenant_default = _cost_center("tenant")

    # Everyone may charge the tenant's default, even someone MOSAIC hasn't onboarded.
    assert may_charge(tenant_default, principal=None, settings=settings)
    # A principal may charge its own default, and a listed member may charge it.
    assert may_charge(research, principal=_person("p", default="research"), settings=settings)
    assert may_charge(research, principal=_person("member"), settings=settings)
    # So may anyone in a listed security group, onboarded or not.
    assert may_charge(
        research, principal=_person("p"), settings=settings, group_principal_ids=["group"]
    )
    assert may_charge(research, principal=None, settings=settings, group_principal_ids=["group"])
    assert not may_charge(research, principal=_person("p"), settings=settings)
    assert not may_charge(
        research, principal=_person("p"), settings=settings, group_principal_ids=["other"]
    )
    # A security group charges only the tenant default or a cost center that lists it.
    group = _person("group", kind=PrincipalKind.SECURITY_GROUP)
    other_group = _person("other", default="research", kind=PrincipalKind.SECURITY_GROUP)
    assert may_charge(research, principal=group, settings=settings)
    assert not may_charge(
        research, principal=other_group, settings=settings, group_principal_ids=["group"]
    )


def test_every_tenant_has_general_and_it_is_the_default_until_changed() -> None:
    book = CostCenterBook(TENANT, [], None)
    general = book.get(GENERAL)
    assert general is not None and general.built_in and general.code == "general"
    assert book.tenant_default_id == GENERAL
    assert book.default_for(None) == GENERAL
    assert book.default_for(_person("p", default="research")) == "research"


def test_grant_identity_includes_the_cost_center() -> None:
    subject = EntitlementSubject(kind="user", id="principal")
    assert entitlement_id(TENANT, subject, RESOURCE, GENERAL) != entitlement_id(
        TENANT, subject, RESOURCE, "costCenter_research"
    )
    assert entitlement_id(TENANT, subject, RESOURCE, GENERAL) == entitlement_id(
        TENANT, subject, RESOURCE, GENERAL
    )


def test_turning_keys_off_changes_only_the_intent_of_grants_that_can_have_a_key() -> None:
    person = _person("principal", default=GENERAL)
    allowed = CostCenterBook(TENANT, [_cost_center(GENERAL)], None)
    off = CostCenterBook(
        TENANT, [_cost_center(GENERAL).model_copy(update={"keys_allowed": False})], None
    )

    def digests(resource: EntitlementResource, subject_kind: str = "user") -> set[str]:
        grant = Entitlement(
            id="grant",
            tenant_id=TENANT,
            subject=EntitlementSubject(kind=subject_kind, id="principal"),
            resource=resource,
            cost_center_id=GENERAL,
        )
        return {
            entitlement_intent_digest(grant, person, cost_center_intent(grant, person, book))
            for book in (allowed, off)
        }

    assert len(digests(RESOURCE)) == 2
    assert len(digests(EntitlementResource(kind="mcpServer", id="mcp_seed"))) == 1
    assert len(digests(RESOURCE, "securityGroup")) == 1


# -- administering cost centers -----------------------------------------------------------------


def test_general_is_built_in_and_the_tenant_default(client: TestClient) -> None:
    [general] = _ok(client.get("/api/v1/cost-centers"))

    assert general["id"] == GENERAL
    assert general["code"] == "general"
    assert general["builtIn"] is True
    assert general["isTenantDefault"] is True
    assert _ok(client.get("/api/v1/cost-center-settings"))["defaultCostCenterId"] == GENERAL


def test_codes_are_unique_whatever_their_case(client: TestClient) -> None:
    research = _create(client, "Research", "RES-01", owners=["lead@example.com"])
    assert research["code"] == "RES-01"
    assert research["owners"] == ["lead@example.com"]

    clash = client.post("/api/v1/cost-centers", json={"name": "Other", "code": "res-01"})
    assert clash.status_code == 409
    assert clash.json()["details"]["reason"] == "codeInUse"
    other = _create(client, "Other", "OTHER")
    renamed = client.patch(f"/api/v1/cost-centers/{other['id']}", json={"code": "GENERAL"})
    assert renamed.status_code == 409
    assert client.post(
        "/api/v1/cost-centers", json={"name": "Bad", "code": "has space"}
    ).status_code == 422


def test_general_can_be_renamed_and_recoded_but_not_deleted(client: TestClient) -> None:
    renamed = _ok(
        client.patch(f"/api/v1/cost-centers/{GENERAL}", json={"name": "Shared", "code": "SHARED"})
    )
    assert (renamed["name"], renamed["code"], renamed["builtIn"]) == ("Shared", "SHARED", True)
    assert _ok(client.get(f"/api/v1/cost-centers/{GENERAL}"))["name"] == "Shared"

    refused = client.delete(f"/api/v1/cost-centers/{GENERAL}")
    assert refused.status_code == 409
    assert refused.json()["details"]["reason"] == "builtIn"


async def test_a_cost_center_in_use_is_not_deleted(client: TestClient) -> None:
    await _seed_model(client)
    research = _create(client, "Research", "RES")

    _ok(client.put("/api/v1/cost-center-settings", json={"defaultCostCenterId": research["id"]}))
    assert client.delete(f"/api/v1/cost-centers/{research['id']}").json()["details"][
        "reason"
    ] == "tenantDefault"
    _ok(client.put("/api/v1/cost-center-settings", json={"defaultCostCenterId": GENERAL}))

    person = _principal(client, PERSON, defaultCostCenterId=research["id"])
    assert client.delete(f"/api/v1/cost-centers/{research['id']}").json()["details"][
        "reason"
    ] == "isDefault"
    _ok(client.patch(f"/api/v1/principals/{person['id']}", json={"defaultCostCenterId": GENERAL}))

    _ok(client.put(f"/api/v1/cost-centers/{research['id']}/members/{person['id']}"))
    _ok(_grant(client, person, research["id"]), 201)
    assert client.delete(f"/api/v1/cost-centers/{research['id']}").json()["details"][
        "reason"
    ] == "hasGrants"

    unused = _create(client, "Unused", "UNUSED")
    assert client.delete(f"/api/v1/cost-centers/{unused['id']}").status_code == 204
    assert client.get(f"/api/v1/cost-centers/{unused['id']}").status_code == 404


def test_the_tenant_setting_names_new_peoples_default(client: TestClient) -> None:
    research = _create(client, "Research", "RES")
    assert _principal(client, OTHER)["defaultCostCenterId"] == GENERAL

    _ok(client.put("/api/v1/cost-center-settings", json={"defaultCostCenterId": research["id"]}))
    person = _principal(client, PERSON)
    group = _principal(client, GROUP, kind="securityGroup")
    chosen = _principal(
        client, "0f5c9a3e-1111-4c2b-9d7e-000000000003", defaultCostCenterId=GENERAL
    )

    assert person["defaultCostCenterId"] == research["id"]
    # A group is never anyone's default; its members have their own.
    assert group["defaultCostCenterId"] is None
    # An administrator can pick another at onboarding.
    assert chosen["defaultCostCenterId"] == GENERAL
    assert client.put(
        "/api/v1/cost-center-settings", json={"defaultCostCenterId": "costCenter_missing"}
    ).status_code == 422


async def test_limits_name_models_mosaic_governs(client: TestClient) -> None:
    research = _create(client, "Research", "RES")
    limits = {
        "limits": [
            {
                "resource": {"kind": "modelApi", "id": RESOURCE.id},
                "person": {"tokensPerMinute": 2000},
                "pool": {"monthlyTokens": 1000000},
            }
        ]
    }
    refused = client.put(f"/api/v1/cost-centers/{research['id']}/limits", json=limits)
    assert refused.status_code == 422

    await _seed_model(client)
    saved = _ok(client.put(f"/api/v1/cost-centers/{research['id']}/limits", json=limits))
    assert saved["limits"][0]["pool"]["monthlyTokens"] == 1000000
    duplicate = {"limits": [limits["limits"][0], limits["limits"][0]]}
    assert client.put(
        f"/api/v1/cost-centers/{research['id']}/limits", json=duplicate
    ).status_code == 422


async def test_a_person_sees_only_the_cost_centers_they_may_charge(client: TestClient) -> None:
    research = _create(client, "Research", "RES")
    sales = _create(client, "Sales", "SALES")
    _create(client, "Hidden", "HIDDEN")
    person = _principal(client, PERSON, defaultCostCenterId=sales["id"])
    group = _principal(client, GROUP, kind="securityGroup")
    _ok(client.put(f"/api/v1/cost-centers/{research['id']}/members/{group['id']}"))

    seen = await _cost_centers(client).chargeable_for_caller(
        Actor(object_id=PERSON, tenant_id=TENANT, group_ids=frozenset({GROUP}))
    )

    assert [(item.code, item.is_default) for item in seen] == [
        ("general", False),
        ("RES", False),
        ("SALES", True),
    ]
    assert person["defaultCostCenterId"] == sales["id"]
    # Someone not in the group sees only General.
    stranger = await _cost_centers(client).chargeable_for_caller(
        Actor(object_id=OTHER, tenant_id=TENANT)
    )
    assert [(item.code, item.is_default) for item in stranger] == [("general", True)]
    # The portal route answers for the caller, from their token, with no members or owners.
    [only] = _ok(client.get("/api/v1/portal/cost-centers"))
    assert set(only) == {"id", "name", "code", "isDefault", "keysAllowed"}


def test_the_cost_center_page_lists_members_defaults_and_grants(client: TestClient) -> None:
    research = _create(client, "Research", "RES")
    member = _principal(client, PERSON)
    defaulted = _principal(client, OTHER, defaultCostCenterId=research["id"])
    _ok(client.put(f"/api/v1/cost-centers/{research['id']}/members/{member['id']}"))

    view = _ok(client.get(f"/api/v1/cost-centers/{research['id']}"))

    details = {item["principalId"]: item for item in view["memberDetails"]}
    assert details[member["id"]]["explicit"] is True
    assert details[defaulted["id"]]["explicit"] is False
    assert details[defaulted["id"]]["isDefault"] is True
    assert view["defaultFor"] == 1
    assert client.put(
        f"/api/v1/cost-centers/{research['id']}/members/principal_missing"
    ).status_code == 404


# -- grants per cost center ---------------------------------------------------------------------


async def test_one_model_under_two_cost_centers_is_two_grants(client: TestClient) -> None:
    await _seed_model(client)
    research = _create(client, "Research", "RES")
    person = _principal(client, PERSON)
    _ok(client.put(f"/api/v1/cost-centers/{research['id']}/members/{person['id']}"))

    general = _ok(_grant(client, person), 201)
    charged = _ok(_grant(client, person, research["id"]), 201)

    assert general["costCenterId"] == GENERAL
    assert charged["costCenterId"] == research["id"]
    assert general["id"] != charged["id"]
    assert _grant(client, person, research["id"]).status_code == 409
    listed = _ok(client.get(f"/api/v1/entitlements?costCenter={research['id']}"))
    assert [item["id"] for item in listed] == [charged["id"]]

    resolved = await _service(client).resolve_for_principal(ADMIN, person["id"])
    assert {item.entitlement.id for item in resolved if item.effective} == {
        general["id"],
        charged["id"],
    }
    assert {item.cost_center.code for item in resolved if item.cost_center} == {"general", "RES"}


async def test_a_grant_charges_only_a_cost_center_its_subject_may_charge(
    client: TestClient,
) -> None:
    await _seed_model(client)
    research = _create(client, "Research", "RES")
    person = _principal(client, PERSON)

    refused = _grant(client, person, research["id"])

    assert refused.status_code == 422
    assert refused.json()["details"]["reason"] == "notACostCenterMember"
    assert _grant(client, person, "costCenter_missing").status_code == 422


async def test_a_grant_naming_no_cost_center_charges_its_subjects_default(
    client: TestClient,
) -> None:
    await _seed_model(client)
    research = _create(client, "Research", "RES")
    person = _principal(client, PERSON, defaultCostCenterId=research["id"])

    granted = _ok(_grant(client, person), 201)

    assert granted["costCenterId"] == research["id"]


# -- requests and approvals ---------------------------------------------------------------------


async def test_requests_name_one_of_the_requesters_cost_centers(client: TestClient) -> None:
    await _seed_model(client)
    research = _create(client, "Research", "RES")
    service = _service(client)
    person = Actor(object_id=PERSON, tenant_id=TENANT)

    with pytest.raises(ValidationError, match="can't charge"):
        await service.create_access_request(
            person, AccessRequestCreate(resource=RESOURCE, cost_center_id=research["id"])
        )
    by_default = await service.create_access_request(person, AccessRequestCreate(resource=RESOURCE))
    assert by_default.cost_center_id == GENERAL
    # One open request per resource and cost center.
    with pytest.raises(ConflictError, match="under this cost center"):
        await service.create_access_request(
            person, AccessRequestCreate(resource=RESOURCE, cost_center_id=GENERAL)
        )


async def test_a_request_through_a_group_is_approved_on_the_groups_evidence(
    client: TestClient,
) -> None:
    await _seed_model(client)
    research = _create(client, "Research", "RES")
    group = _principal(client, GROUP, kind="securityGroup")
    _ok(client.put(f"/api/v1/cost-centers/{research['id']}/members/{group['id']}"))
    service = _service(client)
    caller = Actor(object_id=PERSON, tenant_id=TENANT, group_ids=frozenset({GROUP}))

    request = await service.create_access_request(
        caller, AccessRequestCreate(resource=RESOURCE, cost_center_id=research["id"])
    )
    assert request.cost_center_group_ids == [group["id"]]
    # A second request under General is a separate one.
    other = await service.create_access_request(caller, AccessRequestCreate(resource=RESOURCE))
    assert other.cost_center_id == GENERAL

    approved = await service.approve_access_request(ADMIN, request.id, AccessRequestApproval())
    grant = await service.get_entitlement(ADMIN, approved.granted_entitlement_id or "")
    assert grant.cost_center_id == research["id"]
    assert grant.enabled and grant.revocation is None


async def test_approval_rechecks_a_group_that_left_the_cost_center(client: TestClient) -> None:
    await _seed_model(client)
    research = _create(client, "Research", "RES")
    group = _principal(client, GROUP, kind="securityGroup")
    _ok(client.put(f"/api/v1/cost-centers/{research['id']}/members/{group['id']}"))
    service = _service(client)
    request = await service.create_access_request(
        Actor(object_id=PERSON, tenant_id=TENANT, group_ids=frozenset({GROUP})),
        AccessRequestCreate(resource=RESOURCE, cost_center_id=research["id"]),
    )
    _ok(client.delete(f"/api/v1/cost-centers/{research['id']}/members/{group['id']}"))

    with pytest.raises(ValidationError, match="can't charge"):
        await service.approve_access_request(ADMIN, request.id, AccessRequestApproval())
    # The administrator can charge General instead, which everyone may.
    approved = await service.approve_access_request(
        ADMIN, request.id, AccessRequestApproval(cost_center_id=GENERAL)
    )
    grant = await service.get_entitlement(ADMIN, approved.granted_entitlement_id or "")
    assert grant.cost_center_id == GENERAL


# -- leaving a cost center ----------------------------------------------------------------------


async def test_removing_a_member_revokes_their_grants_and_resets_their_default(
    client: TestClient,
) -> None:
    await _seed_model(client)
    research = _create(client, "Research", "RES")
    person = _principal(client, PERSON, defaultCostCenterId=research["id"])
    _ok(client.put(f"/api/v1/cost-centers/{research['id']}/members/{person['id']}"))
    charged = _ok(_grant(client, person, research["id"]), 201)
    general = _ok(_grant(client, person, GENERAL), 201)

    view = _ok(client.delete(f"/api/v1/cost-centers/{research['id']}/members/{person['id']}"))

    assert person["id"] not in {item["principalId"] for item in view["memberDetails"]}
    revoked = _ok(client.get(f"/api/v1/entitlements/{charged['id']}"))
    assert revoked["enabled"] is False
    assert revoked["revocation"]["costCenterId"] == research["id"]
    assert _ok(client.get(f"/api/v1/entitlements/{general['id']}"))["enabled"] is True
    assert _ok(client.get(f"/api/v1/principals/{person['id']}"))["defaultCostCenterId"] is None
    audit = client.app.state.entitlement_repository.audit_events.values()
    assert any(
        event.action == "entitlement.revoked" and event.resource_id == charged["id"]
        for event in audit
    )
    # It comes back only once they may charge the cost center again.
    assert client.patch(
        f"/api/v1/entitlements/{charged['id']}", json={"enabled": True}
    ).status_code == 422
    _ok(client.put(f"/api/v1/cost-centers/{research['id']}/members/{person['id']}"))
    restored = _ok(client.patch(f"/api/v1/entitlements/{charged['id']}", json={"enabled": True}))
    assert restored["enabled"] is True and restored["revocation"] is None
    assert client.delete(
        f"/api/v1/cost-centers/{research['id']}/members/principal_missing"
    ).status_code == 404


async def test_a_group_leaving_revokes_grants_that_relied_on_it(client: TestClient) -> None:
    await _seed_model(client)
    research = _create(client, "Research", "RES")
    group = _principal(client, GROUP, kind="securityGroup")
    listed = _principal(client, OTHER)
    for member in (group, listed):
        _ok(client.put(f"/api/v1/cost-centers/{research['id']}/members/{member['id']}"))
    service = _service(client)
    request = await service.create_access_request(
        Actor(object_id=PERSON, tenant_id=TENANT, group_ids=frozenset({GROUP})),
        AccessRequestCreate(resource=RESOURCE, cost_center_id=research["id"]),
    )
    approved = await service.approve_access_request(ADMIN, request.id, AccessRequestApproval())
    through_group = approved.granted_entitlement_id or ""
    direct = _ok(_grant(client, listed, research["id"]), 201)
    group_grant = _ok(_grant(client, group, research["id"]), 201)

    _ok(client.delete(f"/api/v1/cost-centers/{research['id']}/members/{group['id']}"))

    assert (await service.get_entitlement(ADMIN, through_group)).revocation is not None
    assert (await service.get_entitlement(ADMIN, group_grant["id"])).revocation is not None
    assert (await service.get_entitlement(ADMIN, direct["id"])).revocation is None


async def test_leaving_the_tenant_default_revokes_nothing(client: TestClient) -> None:
    await _seed_model(client)
    person = _principal(client, PERSON)
    _ok(client.put(f"/api/v1/cost-centers/{GENERAL}/members/{person['id']}"))
    granted = _ok(_grant(client, person, GENERAL), 201)

    _ok(client.delete(f"/api/v1/cost-centers/{GENERAL}/members/{person['id']}"))

    assert _ok(client.get(f"/api/v1/entitlements/{granted['id']}"))["enabled"] is True


async def test_a_grant_written_while_its_member_leaves_is_revoked(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_model(client)
    research = _create(client, "Research", "RES")
    person = _principal(client, PERSON)
    _ok(client.put(f"/api/v1/cost-centers/{research['id']}/members/{person['id']}"))
    service = _service(client)
    repository: InMemoryCostCenterRepository = client.app.state.cost_center_repository
    prepare = service._prepare_entitlement

    async def removed_meanwhile(*args: Any, **kwargs: Any) -> Any:
        record = await prepare(*args, **kwargs)
        # The removal saves the cost center after the grant was checked, before it's saved.
        current = await repository.get_cost_center(TENANT, research["id"])
        assert current is not None
        await repository.save_cost_center(current.model_copy(update={"members": []}), _audit())
        return record

    monkeypatch.setattr(service, "_prepare_entitlement", removed_meanwhile)
    created = await service.create_entitlement(
        ADMIN,
        EntitlementCreate(
            subject=EntitlementSubject(kind="user", id=person["id"]),
            resource=RESOURCE,
            cost_center_id=research["id"],
        ),
    )

    assert created.enabled is False
    assert created.revocation is not None


async def test_removing_someone_who_never_charged_it_is_not_found(client: TestClient) -> None:
    research = _create(client, "Research", "RES")
    person = _principal(client, PERSON)

    with pytest.raises(NotFoundError):
        await _cost_centers(client).remove_member(ADMIN, research["id"], person["id"])


# -- losing the right to charge another way -----------------------------------------------------


async def test_moving_a_default_revokes_grants_charged_only_through_it(client: TestClient) -> None:
    await _seed_model(client)
    research = _create(client, "Research", "RES")
    person = _principal(client, PERSON, defaultCostCenterId=research["id"])
    listed = _principal(client, OTHER, defaultCostCenterId=research["id"])
    _ok(client.put(f"/api/v1/cost-centers/{research['id']}/members/{listed['id']}"))
    charged = _ok(_grant(client, person, research["id"]), 201)
    kept = _ok(_grant(client, listed, research["id"]), 201)

    for principal in (person, listed):
        _ok(
            client.patch(
                f"/api/v1/principals/{principal['id']}", json={"defaultCostCenterId": GENERAL}
            )
        )

    revoked = _ok(client.get(f"/api/v1/entitlements/{charged['id']}"))
    assert revoked["enabled"] is False
    assert revoked["revocation"]["costCenterId"] == research["id"]
    # A listed member still charges it as a member.
    assert _ok(client.get(f"/api/v1/entitlements/{kept['id']}"))["enabled"] is True


async def test_moving_the_tenant_default_revokes_grants_charged_only_through_it(
    client: TestClient,
) -> None:
    await _seed_model(client)
    research = _create(client, "Research", "RES")
    # Onboarded while General was the tenant default, so General is their own default too.
    onboarded = _principal(client, PERSON)
    elsewhere = _principal(client, OTHER, defaultCostCenterId=research["id"])
    _ok(client.put(f"/api/v1/cost-centers/{research['id']}/members/{elsewhere['id']}"))
    own_default = _ok(_grant(client, onboarded, GENERAL), 201)
    through_tenant_default = _ok(_grant(client, elsewhere, GENERAL), 201)

    _ok(client.put("/api/v1/cost-center-settings", json={"defaultCostCenterId": research["id"]}))

    assert _ok(client.get(f"/api/v1/entitlements/{own_default['id']}"))["enabled"] is True
    revoked = _ok(client.get(f"/api/v1/entitlements/{through_tenant_default['id']}"))
    assert revoked["enabled"] is False
    assert revoked["revocation"]["costCenterId"] == GENERAL


async def test_a_principal_a_cost_center_lists_is_not_deleted(client: TestClient) -> None:
    research = _create(client, "Research", "RES")
    group = _principal(client, GROUP, kind="securityGroup")
    _ok(client.put(f"/api/v1/cost-centers/{research['id']}/members/{group['id']}"))

    refused = client.delete(f"/api/v1/principals/{group['id']}")
    assert refused.status_code == 409
    assert refused.json()["details"] == {
        "reason": "costCenterMember",
        "costCenterIds": [research["id"]],
    }

    _ok(client.delete(f"/api/v1/cost-centers/{research['id']}/members/{group['id']}"))
    _ok(client.delete(f"/api/v1/principals/{group['id']}"), 204)


async def test_turning_a_grant_back_on_checks_its_cost_center_again(client: TestClient) -> None:
    await _seed_model(client)
    research = _create(client, "Research", "RES")
    person = _principal(client, PERSON)
    _ok(client.put(f"/api/v1/cost-centers/{research['id']}/members/{person['id']}"))
    granted = _ok(_grant(client, person, research["id"], enabled=False), 201)
    # Their membership ends without the grant being revoked, as a concurrent change could.
    repository: InMemoryCostCenterRepository = client.app.state.cost_center_repository
    current = await repository.get_cost_center(TENANT, research["id"])
    assert current is not None
    await repository.save_cost_center(current.model_copy(update={"members": []}), _audit())

    refused = client.patch(f"/api/v1/entitlements/{granted['id']}", json={"enabled": True})

    assert refused.status_code == 422
    assert refused.json()["details"]["reason"] == "notACostCenterMember"
    assert _ok(client.get(f"/api/v1/entitlements/{granted['id']}"))["enabled"] is False


# -- rechecks that can't finish at once ---------------------------------------------------------


async def _busy(*_args: Any, **_kwargs: Any) -> Any:
    raise ConflictError("Another change to this model is already running")


async def test_a_recheck_a_busy_model_interrupts_is_finished_later(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_model(client)
    research = _create(client, "Research", "RES")
    elsewhere = _principal(client, OTHER, defaultCostCenterId=research["id"])
    _ok(client.put(f"/api/v1/cost-centers/{research['id']}/members/{elsewhere['id']}"))
    granted = _ok(_grant(client, elsewhere, GENERAL), 201)
    monkeypatch.setattr(_service(client), "revoke_for_cost_center", _busy)

    # The move is saved. Revoking waits for the model, so the recheck stays pending.
    _ok(client.put("/api/v1/cost-center-settings", json={"defaultCostCenterId": research["id"]}))

    general = _ok(client.get(f"/api/v1/cost-centers/{GENERAL}"))
    [pending] = general["pendingRechecks"]
    assert pending["reason"] == "tenantDefaultChanged"
    assert pending["entitlementIds"] == [granted["id"]]
    assert _ok(client.get(f"/api/v1/entitlements/{granted['id']}"))["enabled"] is True

    monkeypatch.undo()
    rechecked = _ok(client.post(f"/api/v1/cost-centers/{GENERAL}/recheck"))

    assert rechecked["pendingRechecks"] == []
    revoked = _ok(client.get(f"/api/v1/entitlements/{granted['id']}"))
    assert revoked["enabled"] is False
    assert revoked["revocation"]["costCenterId"] == GENERAL


async def test_repeating_a_group_removal_finishes_its_recheck(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_model(client)
    research = _create(client, "Research", "RES")
    group = _principal(client, GROUP, kind="securityGroup")
    _ok(client.put(f"/api/v1/cost-centers/{research['id']}/members/{group['id']}"))
    service = _service(client)
    request = await service.create_access_request(
        Actor(object_id=PERSON, tenant_id=TENANT, group_ids=frozenset({GROUP})),
        AccessRequestCreate(resource=RESOURCE, cost_center_id=research["id"]),
    )
    approved = await service.approve_access_request(ADMIN, request.id, AccessRequestApproval())
    through_group = approved.granted_entitlement_id or ""
    monkeypatch.setattr(service, "revoke_for_cost_center", _busy)

    view = _ok(client.delete(f"/api/v1/cost-centers/{research['id']}/members/{group['id']}"))

    assert group["id"] not in {item["principalId"] for item in view["memberDetails"]}
    [pending] = view["pendingRechecks"]
    assert pending["subjectId"] is None
    assert pending["entitlementIds"] == [through_group]
    assert (await service.get_entitlement(ADMIN, through_group)).revocation is None

    # The group isn't listed any more, but repeating its removal finishes the recheck.
    monkeypatch.undo()
    view = _ok(client.delete(f"/api/v1/cost-centers/{research['id']}/members/{group['id']}"))

    assert view["pendingRechecks"] == []
    assert (await service.get_entitlement(ADMIN, through_group)).revocation is not None


async def test_repeating_a_default_change_finishes_its_recheck(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_model(client)
    research = _create(client, "Research", "RES")
    person = _principal(client, PERSON, defaultCostCenterId=research["id"])
    granted = _ok(_grant(client, person, research["id"]), 201)
    monkeypatch.setattr(_service(client), "revoke_for_cost_center", _busy)

    _ok(client.patch(f"/api/v1/principals/{person['id']}", json={"defaultCostCenterId": GENERAL}))

    [pending] = _ok(client.get(f"/api/v1/cost-centers/{research['id']}"))["pendingRechecks"]
    assert (pending["reason"], pending["subjectId"]) == ("defaultChanged", person["id"])
    assert pending["entitlementIds"] == [granted["id"]]
    assert _ok(client.get(f"/api/v1/entitlements/{granted['id']}"))["enabled"] is True

    monkeypatch.undo()
    _ok(client.patch(f"/api/v1/principals/{person['id']}", json={"defaultCostCenterId": GENERAL}))

    assert _ok(client.get(f"/api/v1/cost-centers/{research['id']}"))["pendingRechecks"] == []
    revoked = _ok(client.get(f"/api/v1/entitlements/{granted['id']}"))
    assert revoked["revocation"]["costCenterId"] == research["id"]


async def test_a_default_change_that_fails_leaves_no_recheck_behind(client: TestClient) -> None:
    await _seed_model(client)
    research = _create(client, "Research", "RES")
    person = _principal(client, PERSON, defaultCostCenterId=research["id"])
    granted = _ok(_grant(client, person, research["id"]), 201)

    refused = client.patch(
        f"/api/v1/principals/{person['id']}", json={"defaultCostCenterId": "costCenter_missing"}
    )

    assert refused.status_code == 422
    assert _ok(client.get(f"/api/v1/cost-centers/{research['id']}"))["pendingRechecks"] == []
    assert _ok(client.get(f"/api/v1/entitlements/{granted['id']}"))["enabled"] is True


async def test_onboarding_cant_take_a_default_deleted_meanwhile(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    research = _create(client, "Research", "RES")
    directory: DirectoryService = client.app.state.directory_service
    cost_centers = _cost_centers(client)

    async def deleted_during_lookup(*_args: Any, **_kwargs: Any) -> None:
        await cost_centers.delete_cost_center(ADMIN, research["id"])

    monkeypatch.setattr(directory, "_verify_principal_request", deleted_during_lookup)

    with pytest.raises(ValidationError, match="No cost center has that ID"):
        await directory.create_principal(
            ADMIN,
            PrincipalCreate(object_id=PERSON, kind="user", default_cost_center_id=research["id"]),
        )
    assert await client.app.state.repository.find_principal_by_object_id(TENANT, PERSON) is None


async def test_removing_a_member_keeps_grants_they_still_charge_through_a_group(
    client: TestClient,
) -> None:
    await _seed_model(client)
    research = _create(client, "Research", "RES")
    group = _principal(client, GROUP, kind="securityGroup")
    for principal_id in (group["id"], _principal(client, PERSON)["id"]):
        _ok(client.put(f"/api/v1/cost-centers/{research['id']}/members/{principal_id}"))
    service = _service(client)
    request = await service.create_access_request(
        Actor(object_id=PERSON, tenant_id=TENANT, group_ids=frozenset({GROUP})),
        AccessRequestCreate(resource=RESOURCE, cost_center_id=research["id"]),
    )
    approved = await service.approve_access_request(ADMIN, request.id, AccessRequestApproval())
    granted = approved.granted_entitlement_id or ""
    person = await service.get_entitlement(ADMIN, granted)

    view = _ok(
        client.delete(f"/api/v1/cost-centers/{research['id']}/members/{person.subject.id}")
    )

    assert view["pendingRechecks"] == []
    assert (await service.get_entitlement(ADMIN, granted)).revocation is None
