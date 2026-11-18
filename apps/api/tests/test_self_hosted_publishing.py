"""Offline tests of the exact ARM association, proof and ownership boundaries."""

import asyncio
import xml.etree.ElementTree as ET
from datetime import timedelta

import httpx
import pytest
from aoai_double import AI_RESOURCE_ID
from apim_double import RESOURCE_ID
from mosaic_api.domain import (
    GatewayCreate,
    GatewayRuntimeChoice,
    GatewayUpdate,
    ModelAccessSettings,
    ModelEndpointCreate,
    PublicationCreate,
    PublishedResourceKind,
    PublishRunStatus,
    SelfHostedGatewayCreate,
    utc_now,
)
from mosaic_api.errors import ConflictError, UpstreamError, ValidationError
from mosaic_api.services.self_hosted import CHECKS, RUNTIME_RULES, RuntimeEvidence
from test_governed_lifecycle import ACTOR, PUBLICATION_ENFORCEMENT, Harness

LOCAL = "https://plant.edge.example.test"
SERVICE_POLICY = (
    "<policies><inbound/><backend><forward-request/></backend><outbound/><on-error/></policies>"
)


async def verified(scope: str) -> RuntimeEvidence:
    now = utc_now()
    return RuntimeEvidence(
        scope,
        RUNTIME_RULES,
        now - timedelta(minutes=1),
        now + timedelta(minutes=1),
        frozenset(CHECKS),
    )


async def prepare(*, qualified: bool = True):
    harness = Harness()
    harness.apim.seed_self_hosted("plant-edge")
    harness.apim.seed_self_hosted("other-edge")
    harness.apim.seed("policies/policy", {"properties": {"value": SERVICE_POLICY}})
    gateway = await harness.gateway_service.register(
        ACTOR, GatewayCreate(azure_resource_id=RESOURCE_ID)
    )
    runtime = await harness.gateway_service.register_self_hosted(
        ACTOR,
        gateway.id,
        SelfHostedGatewayCreate(
            gateway_name="plant-edge",
            image_version="2.13.2",
            image_digest=f"sha256:{'a' * 64}",
            ingress_origin=f"{LOCAL}/",
        ),
    )
    assert runtime.ingress_origin == LOCAL
    assert harness.apim.writes == []
    await harness.gateway_service.update(ACTOR, gateway.id, GatewayUpdate(management_mode="manage"))
    endpoint = await harness.endpoint_service.register(
        ACTOR, ModelEndpointCreate(azure_resource_id=AI_RESOURCE_ID)
    )
    await harness.endpoint_service.sync_now(ACTOR, endpoint.id)
    if qualified:
        harness.service._self_hosted_evidence = verified
    publication = await harness.service.create(
        ACTOR,
        PublicationCreate(
            gateway_id=gateway.id,
            model_endpoint_id=endpoint.id,
            deployment_name="gpt-4o-prod",
            api_name="mosaic-local-chat",
            api_path="local-chat",
            enforcement=PUBLICATION_ENFORCEMENT,
            governed_access=ModelAccessSettings(keys_enabled=False, entra_enabled=True),
            runtime_target=GatewayRuntimeChoice(kind="selfHosted", runtime_id=runtime.id),
        ),
    )
    return harness, gateway, runtime, publication


async def test_unqualified_runtime_produces_a_read_only_blocked_review() -> None:
    harness, gateway, runtime, publication = await prepare(qualified=False)
    plan = await harness.service.plan(ACTOR, publication.id)
    assert plan.runtime_snapshot is None
    assert plan.runtime_review is not None
    assert plan.runtime_review.ingress_origin == LOCAL
    assert plan.runtime_review.parent_ingress != LOCAL
    assert any("budget propagation" in reason for reason in plan.runtime_review.blockers)
    assert plan.runtime_review.association_id.endswith(
        "/gateways/plant-edge/apis/mosaic-local-chat"
    )
    with pytest.raises(ConflictError, match="not verified"):
        await harness.service.apply(ACTOR, publication.id, plan.id)
    assert harness.apim.writes == []
    with pytest.raises(ConflictError, match="selects this gateway"):
        await harness.gateway_service.remove_self_hosted(ACTOR, gateway.id, runtime.id)
    current = await harness.service.get_publication(ACTOR, publication.id)
    assert current.runtime_target == publication.runtime_target
    with pytest.raises(ConflictError, match="explicitly"):
        await harness.service.create(
            ACTOR,
            PublicationCreate(
                gateway_id=gateway.id,
                model_endpoint_id=publication.model_endpoint_id,
                deployment_name=publication.deployment_name,
                enforcement=PUBLICATION_ENFORCEMENT,
            ),
        )


async def test_qualified_apply_orders_the_guard_binding_and_association_then_detaches_only_owned():
    harness, _gateway, _runtime, publication = await prepare()
    plan = await harness.service.plan(ACTOR, publication.id)
    assert plan.runtime_snapshot and plan.runtime_review
    assert not plan.runtime_review.blockers
    assert [
        step.name for step in plan.steps if step.kind == PublishedResourceKind.API_OPERATION
    ] == ["chat-completions"]
    run = await harness.service.apply(ACTOR, publication.id, plan.id)
    await harness.service.wait_for_idle()
    run = await harness.service.get_run(ACTOR, run.id)
    assert run.status == PublishRunStatus.SUCCEEDED, run.errors
    marker = plan.runtime_review.binding_id.removeprefix(RESOURCE_ID + "/")
    association = plan.runtime_review.association_id.removeprefix(RESOURCE_ID + "/")
    writes = harness.apim.write_paths("PUT")
    policy_path = "apis/mosaic-local-chat/policies/policy"
    assert writes.index(policy_path) < writes.index(
        "apis/mosaic-local-chat/operations/chat-completions"
    )
    assert writes.index(marker) < writes.index(association) < len(writes) - 1
    xml = harness.apim.written[policy_path]["properties"]["value"]
    assert xml.index("context.Deployment.Gateway.IsManaged") < xml.index("<base")
    version_guard = ET.fromstring(xml).findall("./inbound/choose/when")[1]
    condition = version_guard.attrib["condition"]
    assert 'names.Length != 1 || names[0] != "api-version"' in condition
    assert 'values.Length != 1 || values[0] != "2024-10-21"' in condition
    assert version_guard.find("./return-response/set-status[@code='400']") is not None
    assert "set-query-parameter" not in xml
    assert 'buffer-response="false"' in xml
    assert (
        'authentication-provider="WorkloadIdentity"'
        in (
            harness.apim.written[f"policyFragments/{publication.fragment_name}"]["properties"][
                "value"
            ]
        )
    )
    assert not any(path.startswith(("products/", "subscriptions/")) for path in writes)
    current = await harness.service.get_publication(ACTOR, publication.id)
    assert current.applied_runtime == plan.runtime_snapshot
    assert all(
        step.created_by_mosaic and step.write_outcome == "created"
        for step in run.steps
        if step.kind
        in {
            PublishedResourceKind.GATEWAY_API,
            PublishedResourceKind.PUBLICATION_BINDING,
        }
    )
    assert all(
        call["api_version"] == "2024-05-01"
        for call in harness.apim.http_calls
        if call["path"] == association
    )
    harness.apim.seed("gateways/other-edge/apis/sibling")
    removal = await harness.service.plan_unpublish(ACTOR, publication.id)
    removing = await harness.service.unpublish(ACTOR, publication.id, removal.id)
    await harness.service.wait_for_idle()
    assert (await harness.service.get_run(ACTOR, removing.id)).status == PublishRunStatus.SUCCEEDED
    assert harness.apim.write_paths("DELETE")[0] == association
    assert "gateways/other-edge/apis/sibling" in harness.apim.written
    assert marker not in harness.apim.written


@pytest.mark.parametrize("uncertain", [False, True])
async def test_existing_or_uncertain_link_never_becomes_owned(uncertain: bool) -> None:
    harness, _gateway, _runtime, publication = await prepare()
    plan = await harness.service.plan(ACTOR, publication.id)
    assert plan.runtime_review
    suffix = plan.runtime_review.association_id.removeprefix(RESOURCE_ID + "/")
    if uncertain:
        original = harness.apim._write

        def drop_response(request: httpx.Request, path: str) -> httpx.Response:
            response = original(request, path)
            if request.method == "PUT" and path == suffix:
                raise httpx.ReadError("fake response lost")
            return response

        harness.apim._write = drop_response
    else:
        harness.apim.create_outcomes[suffix] = 200
    started = await harness.service.apply(ACTOR, publication.id, plan.id)
    await harness.service.wait_for_idle()
    run = await harness.service.get_run(ACTOR, started.id)
    assert run.status == (PublishRunStatus.INTERRUPTED if uncertain else PublishRunStatus.FAILED)
    assert harness.apim.write_paths("PUT").count(suffix) == 1
    assert suffix in harness.apim.written and suffix not in harness.apim.write_paths("DELETE")
    current = await harness.service.get_publication(ACTOR, publication.id)
    assert current.applied_runtime is None
    assert not any(item.kind == PublishedResourceKind.GATEWAY_API for item in current.resources)
    if uncertain:
        assert (
            await harness.gateways.get_publication_lock(ACTOR.tenant_id, publication.id) == run.id
        )
        with pytest.raises(ConflictError, match="unconfirmed APIM resource still exists"):
            await harness.service.recover_interrupted(
                ACTOR, publication.id, run_id=run.id, confirm_quiesced=True
            )


async def test_rejects_foreign_association_drift_and_invalid_ingress_before_writes() -> None:
    harness, gateway, _runtime, publication = await prepare()
    with pytest.raises(ValidationError, match="HTTPS"):
        await harness.gateway_service.register_self_hosted(
            ACTOR,
            gateway.id,
            SelfHostedGatewayCreate(
                gateway_name="other-edge",
                image_version="2.13.2",
                ingress_origin="http://not-an-approved-origin.example.test/path",
            ),
        )
    path = f"gateways/other-edge/apis/{publication.api_name}"
    harness.apim.seed(path)
    with pytest.raises(ConflictError, match="different or unowned"):
        await harness.service.plan(ACTOR, publication.id)
    assert harness.apim.writes == []


@pytest.mark.parametrize(
    ("path", "response", "error"),
    [
        ("policies/policy", {"properties": {}}, "invalid policy"),
        ("gateways", {"value": [], "nextLink": 7}, "invalid collection continuation"),
    ],
)
async def test_malformed_successful_arm_reads_are_not_missing_evidence(path, response, error):
    harness, _gateway, _runtime, publication = await prepare()
    harness.apim.seed(path, response)
    with pytest.raises(UpstreamError, match=error):
        await harness.service.plan(ACTOR, publication.id)
    assert harness.apim.writes == []


@pytest.mark.parametrize("override", [True, False])
async def test_unowned_operation_or_override_refuses_future_review(override: bool) -> None:
    harness, _gateway, _runtime, publication = await prepare()
    plan = await harness.service.plan(ACTOR, publication.id)
    await harness.service.apply(ACTOR, publication.id, plan.id)
    await harness.service.wait_for_idle()
    path = f"apis/{publication.api_name}/operations"
    if override:
        harness.apim.seed(
            f"{path}/chat-completions/policies/policy",
            {"properties": {"value": "<policies><inbound/></policies>"}},
        )
    else:
        harness.apim.seed(f"{path}/another-operation")
    before = len(harness.apim.writes)
    with pytest.raises(ConflictError, match=r"only the reviewed Chat|operation policy overrides"):
        await harness.service.plan(ACTOR, publication.id)
    assert len(harness.apim.writes) == before


async def test_incomplete_child_inventory_blocks_unpublish_without_deleting() -> None:
    harness, _gateway, _runtime, publication = await prepare()
    plan = await harness.service.plan(ACTOR, publication.id)
    await harness.service.apply(ACTOR, publication.id, plan.id)
    await harness.service.wait_for_idle()
    removal = await harness.service.plan_unpublish(ACTOR, publication.id)
    harness.apim.seed("gateways", {"partial": True})
    run = await harness.service.unpublish(ACTOR, publication.id, removal.id)
    with pytest.raises(UpstreamError, match="invalid collection"):
        await harness.service.wait_for_idle()
    assert (await harness.service.get_run(ACTOR, run.id)).status == PublishRunStatus.INTERRUPTED
    assert not harness.apim.write_paths("DELETE")


async def test_exact_direct_grant_uses_applied_local_url_and_never_falls_back() -> None:
    harness, _gateway, _runtime, publication = await prepare()
    harness.publication_id = publication.id
    assert (await harness.apply()).status == PublishRunStatus.SUCCEEDED
    published = await harness.service.get_publication(ACTOR, publication.id)
    assert published.model_api_id
    harness.model_id = published.model_api_id
    grant = await harness.grant()
    assert (await harness.apply()).status == PublishRunStatus.SUCCEEDED
    portal = harness.keys()
    portal._self_hosted_evidence = verified
    info = await portal.connection(ACTOR, grant.id, administrator=True)
    assert info.endpoint == f"{LOCAL}/local-chat"
    assert info.api_version == "2024-10-21" and not info.keys_available
    assert [operation.name for operation in info.operations] == ["chat-completions"]
    portal._self_hosted_evidence = None
    with pytest.raises(ConflictError, match="Local gateway access is not confirmed"):
        await portal.connection(ACTOR, grant.id, administrator=True)


async def test_operation_policy_appearing_mid_apply_blocks_activation() -> None:
    harness, _gateway, _runtime, publication = await prepare()
    plan = await harness.service.plan(ACTOR, publication.id)
    assert plan.runtime_review
    harness.apim.pause_suffix = plan.runtime_review.association_id.removeprefix(RESOURCE_ID + "/")
    run = await harness.service.apply(ACTOR, publication.id, plan.id)
    try:
        await asyncio.wait_for(harness.apim.paused.wait(), timeout=3)
        harness.apim.seed(
            f"apis/{publication.api_name}/operations/chat-completions/policies/policy",
            {"properties": {"value": "<policies><inbound/></policies>"}},
        )
    finally:
        harness.apim.resume.set()
    await harness.service.wait_for_idle()
    finished = await harness.service.get_run(ACTOR, run.id)
    assert finished.status == PublishRunStatus.FAILED
    assert any("operation policy overrides" in error for error in finished.errors)
    api_writes = [
        payload for path, payload in harness.apim.payloads if path == f"apis/{publication.api_name}"
    ]
    assert api_writes and all(
        payload["properties"]["subscriptionRequired"] for payload in api_writes
    )
