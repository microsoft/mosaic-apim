"""Verify MCP policy, caller URLs and shared discovery ownership using the ARM double."""

from datetime import timedelta

import httpx
import pytest
from apim_double import RESOURCE_ID
from mosaic_api.domain import (
    GatewayRuntimeChoice,
    McpAuthMode,
    PublishRunStatus,
    SelfHostedGatewayCreate,
    utc_now,
)
from mosaic_api.errors import ConflictError, ValidationError
from mosaic_api.repositories import InMemoryModelEndpointRepository
from mosaic_api.services.entitlements import EntitlementService
from mosaic_api.services.portal_access import PortalAccessService
from mosaic_api.services.self_hosted import MCP_CHECKS, MCP_RUNTIME_RULES, RuntimeEvidence
from test_mcp_publishing import ACTOR, Harness

ORIGIN = "https://plant.edge.example.test"
SERVER = "mosaic-mcp-orders-mcp"
DISCOVERY = "gateways/plant-edge/apis/mosaic-mcp-metadata"
NATIVE = f"gateways/plant-edge/apis/{SERVER}"


async def verified(scope: str) -> RuntimeEvidence:
    now = utc_now()
    return RuntimeEvidence(
        scope,
        MCP_RUNTIME_RULES,
        now - timedelta(minutes=1),
        now + timedelta(minutes=1),
        frozenset(MCP_CHECKS),
    )


async def prepare():
    harness = Harness()
    await harness.setup()
    harness.apim.seed_self_hosted("plant-edge")
    harness.apim.seed_self_hosted("other-edge")
    harness.apim.seed("policies/policy", {"properties": {"value": "<policies/>"}})
    runtime = await harness.gateways.register_self_hosted(
        ACTOR,
        harness.gateway_id,
        SelfHostedGatewayCreate(
            gateway_name="plant-edge",
            image_version="2.13.2",
            ingress_origin=ORIGIN,
            image_digest=f"sha256:{'a' * 64}",
        ),
    )
    return harness, GatewayRuntimeChoice(kind="selfHosted", runtime_id=runtime.id)


async def local_endpoint(harness: Harness) -> None:
    await harness.set_endpoint(
        auth_mode=McpAuthMode.MANAGED_IDENTITY,
        resource_audience="api://mcp-workload",
    )


async def apply_local(harness: Harness, publication_id: str):
    plan = await harness.service.plan(ACTOR, publication_id)
    run = await harness.service.apply(ACTOR, publication_id, plan.id)
    await harness.service.wait_for_idle()
    result = await harness.service.get_run(ACTOR, publication_id, run.id)
    assert result.status == PublishRunStatus.SUCCEEDED, result.errors
    return result


async def test_unqualified_mcp_review_lists_both_links_and_never_admits_apply() -> None:
    harness, choice = await prepare()
    with pytest.raises(ValidationError, match="workload identity"):
        await harness.create(runtime_target=choice)
    await local_endpoint(harness)
    publication_id = await harness.create(runtime_target=choice)
    plan = await harness.service.plan(ACTOR, publication_id)
    assert plan.runtime_snapshot is None and plan.runtime_review is not None
    assert plan.runtime_review.association_id == f"{RESOURCE_ID}/{NATIVE}"
    assert plan.runtime_review.discovery_association_id == f"{RESOURCE_ID}/{DISCOVERY}"
    assert plan.runtime_review.operation_path.endswith("/mcp")
    assert plan.runtime_review.discovery_path.startswith("/.well-known/")
    assert any("OAuth discovery" in item for item in plan.runtime_review.blockers)
    with pytest.raises(ConflictError, match="not verified"):
        await harness.service.apply(ACTOR, publication_id, plan.id)
    assert not harness.apim.writes


async def test_mcp_applies_native_and_discovery_guards_and_issues_only_local_urls() -> None:
    harness, choice = await prepare()
    await local_endpoint(harness)
    harness.service._self_hosted_evidence = verified
    publication_id = await harness.create(runtime_target=choice)
    publication = await harness.service.get_publication(ACTOR, publication_id)
    user = await harness.principal("caller", "11111111-1111-1111-1111-111111111111")
    grant = await harness.entitlement("grant", "user", user.id, publication.mcp_server_id)
    await apply_local(harness, publication_id)

    calls = [
        item["body"]["properties"]
        for item in harness.apim.http_calls
        if item["path"] == f"apis/{SERVER}" and item["method"] == "PUT"
    ]
    assert calls[0]["subscriptionRequired"] is True and "backendId" not in calls[0]
    assert calls[-1]["subscriptionRequired"] is False and calls[-1]["backendId"] == SERVER
    native = harness.apim.written[f"apis/{SERVER}/policies/policy"]["properties"]["value"]
    operation = f"apis/mosaic-mcp-metadata/operations/{publication.metadata_api_name}"
    discovery = harness.apim.written[f"{operation}/policies/policy"]["properties"]["value"]
    fragment = harness.apim.written[f"policyFragments/{SERVER}"]["properties"]["value"]
    for policy in (native, discovery):
        assert policy.index("context.Deployment.Gateway.IsManaged") < policy.index("<base")
        assert ORIGIN in policy and "OriginalUrl.Host" not in policy
    assert 'authentication-provider="WorkloadIdentity"' in fragment and "Mcp.Invoke" in fragment

    def no_credentials(resource: object):
        pytest.fail(f"unexpected credential access for {resource}")

    portal = PortalAccessService(
        EntitlementService(
            harness.entitlement_repository,
            directory_repository=harness.directory_repository,
            gateway_repository=harness.gateway_repository,
            endpoint_repository=InMemoryModelEndpointRepository(),
        ),
        repository=harness.entitlement_repository,
        directory_repository=harness.directory_repository,
        gateway_repository=harness.gateway_repository,
        credential_factory=no_credentials,
        self_hosted_evidence=verified,
    )
    info = await portal.mcp_connection(ACTOR, grant.id, administrator=True)
    assert info.self_hosted and info.server_url == f"{ORIGIN}/{publication.api_path}/mcp"
    assert info.resource_metadata_url == (
        f"{ORIGIN}/.well-known/oauth-protected-resource/{publication.api_path}/mcp"
    )
    portal._self_hosted_evidence = None
    with pytest.raises(ConflictError, match="Local gateway access is not confirmed"):
        await portal.mcp_connection(ACTOR, grant.id, administrator=True)


async def test_shared_local_discovery_survives_until_the_last_mcp_server_unpublishes() -> None:
    harness, choice = await prepare()
    await local_endpoint(harness)
    harness.service._self_hosted_evidence = verified
    first = await harness.create(runtime_target=choice)
    await apply_local(harness, first)
    harness.endpoint_id = await harness.add_endpoint("second-mcp")
    await local_endpoint(harness)
    with pytest.raises(ConflictError, match="cannot be shared across runtimes"):
        await harness.create()
    second = await harness.create(runtime_target=choice)
    await apply_local(harness, second)
    assert harness.apim.write_paths("PUT").count(DISCOVERY) == 1
    for publication_id in (first, second):
        plan = await harness.service.plan_unpublish(ACTOR, publication_id)
        run = await harness.service.unpublish(ACTOR, publication_id, plan.id)
        await harness.service.wait_for_idle()
        finished = await harness.service.get_run(ACTOR, publication_id, run.id)
        assert finished.status == PublishRunStatus.SUCCEEDED, finished.errors
        assert (DISCOVERY in harness.apim.written) is (publication_id == first)
    assert harness.apim.write_paths("DELETE").count(DISCOVERY) == 1


@pytest.mark.parametrize("uncertain", [False, True])
async def test_existing_or_ambiguous_mcp_discovery_link_is_not_adopted(uncertain: bool) -> None:
    harness, choice = await prepare()
    await local_endpoint(harness)
    harness.service._self_hosted_evidence = verified
    publication_id = await harness.create(runtime_target=choice)
    plan = await harness.service.plan(ACTOR, publication_id)
    if uncertain:
        original = harness.apim._write

        def drop_response(request: httpx.Request, path: str) -> httpx.Response:
            response = original(request, path)
            if request.method == "PUT" and path == DISCOVERY:
                raise httpx.ReadError("fake response lost")
            return response

        harness.apim._write = drop_response
    else:
        harness.apim.create_outcomes[DISCOVERY] = 200
    run = await harness.service.apply(ACTOR, publication_id, plan.id)
    await harness.service.wait_for_idle()
    finished = await harness.service.get_run(ACTOR, publication_id, run.id)
    assert finished.status == (
        PublishRunStatus.INTERRUPTED if uncertain else PublishRunStatus.FAILED
    )
    assert choice.runtime_id is not None
    record = await harness.gateway_repository.get_self_hosted_gateway(
        ACTOR.tenant_id,
        choice.runtime_id,
    )
    assert record and record.mcp_discovery_link is None
    assert DISCOVERY not in harness.apim.write_paths("DELETE")
    if uncertain:
        with pytest.raises(ConflictError, match="unconfirmed MCP resource"):
            await harness.service.recover_interrupted(
                ACTOR,
                publication_id,
                run_id=run.id,
                confirm_quiesced=True,
            )
