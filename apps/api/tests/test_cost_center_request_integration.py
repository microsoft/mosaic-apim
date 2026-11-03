import pytest
from cost_center_management_double import ADMIN, BASELINE, TENANT, USER, management
from mosaic_api.cost_center_requests import CostCenterRequestApproval, CostCenterRequestCreate
from mosaic_api.repositories import InMemoryModelEndpointRepository
from mosaic_api.services.cost_center_requests import CostCenterRequestService
from mosaic_api.services.cost_centers import CostCenterService
from mosaic_api.services.directory import Actor
from mosaic_api.services.entitlements import EntitlementService


def services(backend: str, *, write_consent: bool = True):
    kwargs = {} if write_consent else {"permissions": BASELINE}
    h = management(backend, **kwargs)
    access = EntitlementService(
        h.entitlements,
        directory_repository=h.local_directory,
        gateway_repository=h.gateways,
        endpoint_repository=InMemoryModelEndpointRepository(),
        directory_lookup=h.lookup,
        cost_center_repository=h.repository,
        entra_group_claims_enabled=True,
    )
    h.service = CostCenterService(
        h.repository,
        directory_repository=h.local_directory,
        entitlement_repository=h.entitlements,
        gateway_repository=h.gateways,
        entitlements=access,
        directory_lookup=h.lookup,
        group_management=h.graph,
        directory_tenant_id=TENANT,
    )
    requests = CostCenterRequestService(
        h.entitlements,
        h.repository,
        h.service,
        validate_access=access.validate_cost_center_access,
    )
    return h, requests


@pytest.mark.parametrize("backend", ["memory", "cosmos"])
async def test_real_request_approval_uses_real_group_service_and_shared_graph_state(
    backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    h, requests = services(backend)
    requester = Actor(object_id=USER, tenant_id=TENANT)
    submitted = await requests.submit(
        requester, CostCenterRequestCreate(name="Fictional research", justification="Project work")
    )
    assert h.graph.writes == []
    create_group = h.graph.create_group

    async def verify_committed_approval(
        display_name: str,
        *,
        description: str | None = None,
        mail_nickname: str | None = None,
        request_id: str | None = None,
    ):
        saved = await h.entitlements.get_cost_center_request(TENANT, submitted.id)
        assert saved is not None and saved.state == "approved"
        return await create_group(
            display_name,
            description=description,
            mail_nickname=mail_nickname,
            request_id=request_id,
        )

    monkeypatch.setattr(h.graph, "create_group", verify_committed_approval)
    payload = CostCenterRequestApproval.model_validate(
        {
            "costCenter": {
                "name": "Fictional research",
                "code": "RESEARCH",
                "groupSetup": {"mode": "create"},
            },
            "addRequester": True,
        }
    )
    approved = await requests.approve(ADMIN, submitted.id, payload)
    assert approved.state == "approved" and approved.setup_state == "ready"
    assert approved.requester_membership_state == "succeeded"
    assert approved.cost_center is not None
    center = await h.repository.get_cost_center(TENANT, approved.cost_center.id)
    assert center is not None and center.verified_group_object_id is not None
    members = await h.graph.list_members(center.verified_group_object_id)
    assert [member.object_id for member in members.members] == [USER]
    assert [write.operation for write in h.graph.writes] == ["create_group", "add_member"]
    assert await h.entitlements.list_entitlements(TENANT) == []
    assert h.gateways.publish_runs == {}

    await h.service.remove_directory_member(
        ADMIN, center.id, USER, idempotency_key="explicit-removal-after-approval"
    )
    assert (await h.graph.list_members(center.verified_group_object_id)).members == ()
    resumed = await requests.approve(ADMIN, submitted.id, payload)
    assert resumed.cost_center == approved.cost_center
    assert resumed.requester_membership_state == "succeeded"
    assert (await h.graph.list_members(center.verified_group_object_id)).members == ()
    assert [write.operation for write in h.graph.writes] == [
        "create_group",
        "add_member",
        "remove_member",
    ]


@pytest.mark.parametrize("backend", ["memory", "cosmos"])
async def test_real_missing_group_consent_keeps_one_approved_incomplete_center(
    backend: str,
) -> None:
    h, requests = services(backend, write_consent=False)
    requester = Actor(object_id=USER, tenant_id=TENANT)
    submitted = await requests.submit(
        requester, CostCenterRequestCreate(name="Fictional research", justification="Project work")
    )
    payload = CostCenterRequestApproval.model_validate(
        {
            "costCenter": {
                "name": "Fictional research",
                "code": "RESEARCH",
                "groupSetup": {"mode": "create"},
            },
            "addRequester": True,
        }
    )
    for _ in range(2):
        result = await requests.approve(ADMIN, submitted.id, payload)
        assert result.state == "approved" and result.cost_center is not None
        assert result.setup_state == "setupRequired"
        assert result.needs_administrator_attention
        assert result.requester_membership_state == "pending"
    assert len(await h.repository.list_cost_centers(TENANT)) == 1
    assert h.graph.writes == []
    assert await h.entitlements.list_entitlements(TENANT) == []
