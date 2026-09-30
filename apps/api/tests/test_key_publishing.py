"""Publishing from a key-authenticated endpoint through a Key Vault-backed named value (ADR 0018).

These drive the real publishing service, ARM client and writer against the API Management double,
which, like API Management, refuses a policy that names a named value that doesn't exist and
refuses to delete a named value a policy still names.
"""

import json
import re
import xml.etree.ElementTree as ET

import pytest
from apim_double import CONTRIBUTOR_PERMISSIONS, RESOURCE_ID, FakeApim
from conftest import build_arm_client, build_gateway_service, reviewed_unpublish
from key_vault_double import SECRET_NAME, SECRET_URI, VAULT_NAME
from mosaic_api.domain import (
    ApiShape,
    GatewayCreate,
    GatewayUpdate,
    ManagementMode,
    ModelAccessSettings,
    ModelEndpointUpdate,
    PublicationCreate,
    PublicationStatus,
    PublishAction,
    PublishedResourceKind,
    PublishRun,
    PublishRunStatus,
    PublishStepStatus,
    TokenEnforcement,
)
from mosaic_api.errors import ConflictError, ValidationError
from mosaic_api.integrations.apim import ApimClient, ApimWriter
from mosaic_api.repositories import InMemoryDirectoryRepository, InMemoryEntitlementRepository
from mosaic_api.services import PublishingService, publishing
from structlog.testing import capture_logs
from test_key_endpoints import ACTOR, KEY, KeyWorld

CLAUDE_API = "mosaic-fabrikam-partner-foundry-claude-sonnet-4-5"
GPT_API = "mosaic-fabrikam-partner-foundry-gpt-4-1"
CLAUDE_KEY = f"namedValues/{CLAUDE_API}-key"
GPT_KEY = f"namedValues/{GPT_API}-key"
# What the gateway would substitute for the named value at runtime.
GATEWAY_KEY = "the-key-api-management-read-from-key-vault"
CALLER = {
    "api-key": "caller-key",
    "x-api-key": "caller-anthropic-key",
    "Authorization": "Bearer caller-token",
    "Ocp-Apim-Subscription-Key": "caller-subscription-key",
}
TOKENS = TokenEnforcement(counter_key_expression="@(context.Subscription.Id)", tokens_per_minute=5)


def forwarded(fragment_xml: str, named_values: dict[str, str]) -> tuple[dict[str, str], set[str]]:
    """The headers a request carries past the fragment, and the query parameters it removes.

    Walks the fragment's top-level statements in order, as the gateway runs them for an
    authorized request, starting from every credential a caller could send.
    """

    headers = {name.casefold(): value for name, value in CALLER.items()}
    removed_query: set[str] = set()
    for element in ET.fromstring(fragment_xml):
        name = (element.get("name") or "").casefold()
        action = element.get("exists-action") or "override"
        if element.tag == "set-query-parameter" and action == "delete":
            removed_query.add(name)
        if element.tag != "set-header":
            continue
        if action == "delete":
            headers.pop(name, None)
            continue
        if action == "skip" and name in headers:
            continue
        value = element.findtext("value") or ""
        headers[name] = re.sub(r"\{\{([^{}]+)\}\}", lambda match: named_values[match[1]], value)
    return headers, removed_query


class KeyHarness:
    """A managed gateway, and a Foundry resource in another tenant reached with an API key."""

    def __init__(self, *, governed: bool = False) -> None:
        self.world = KeyWorld()
        self.apim = FakeApim(permissions=CONTRIBUTOR_PERMISSIONS)
        self.gateways = build_gateway_service(self.apim, self.world.gateway_repository)
        arm = build_arm_client(self.apim)
        self.service = PublishingService(
            self.world.gateway_repository,
            endpoint_repository=self.world.endpoint_repository,
            client_factory=lambda resource: ApimClient(arm, resource),
            writer_factory=lambda resource: ApimWriter(arm, resource),
            directory_repository=InMemoryDirectoryRepository() if governed else None,
            entitlement_repository=InMemoryEntitlementRepository() if governed else None,
        )
        self.gateway_id = ""
        self.endpoint_id = ""

    async def setup(self) -> None:
        gateway = await self.gateways.register(ACTOR, GatewayCreate(azure_resource_id=RESOURCE_ID))
        await self.gateways.sync_now(ACTOR, gateway.id)
        gateway = await self.gateways.update(
            ACTOR, gateway.id, GatewayUpdate(management_mode=ManagementMode.MANAGE)
        )
        self.gateway_id = gateway.id
        self.world.grant()
        endpoint = await self.world.register()
        self.endpoint_id = endpoint.id

    async def publish(self, deployment: str = "claude-sonnet-4-5", **overrides: object) -> str:
        payload: dict[str, object] = {
            "gateway_id": self.gateway_id,
            "model_endpoint_id": self.endpoint_id,
            "deployment_name": deployment,
        }
        payload.update(overrides)
        publication = await self.service.create(ACTOR, PublicationCreate.model_validate(payload))
        return publication.id

    async def apply(self, publication_id: str) -> PublishRun:
        plan = await self.service.plan(ACTOR, publication_id)
        run = await self.service.apply(ACTOR, publication_id, plan.id)
        await self.service.wait_for_idle()
        return await self.service.get_run(ACTOR, run.id)

    def fragment(self, api_name: str) -> str:
        return str(self.apim.written[f"policyFragments/{api_name}"]["properties"]["value"])


@pytest.fixture
async def harness() -> KeyHarness:
    built = KeyHarness()
    await built.setup()
    return built


async def test_declared_deployments_are_offered_with_the_api_declared(
    harness: KeyHarness,
) -> None:
    models = {
        item.deployment_name: item
        for item in await harness.service.publishable_models(ACTOR, harness.gateway_id)
    }

    claude = models["claude-sonnet-4-5"]
    assert claude.declared is True
    assert claude.api_shape == ApiShape.ANTHROPIC_MESSAGES
    assert claude.publishable is True
    # A Developer gateway is a classic tier, where Claude gets call limits but no token limits.
    assert claude.token_limits_supported is False
    assert claude.runtime_access is not None and claude.runtime_access.can_invoke is True
    assert models["gpt-4-1"].api_shape == ApiShape.AZURE_OPENAI


async def test_only_a_declared_deployment_can_be_published(harness: KeyHarness) -> None:
    with pytest.raises(ValidationError, match="Declare this deployment"):
        await harness.publish("gpt-4o", enforcement=TOKENS)


async def test_a_gateway_without_an_identity_cannot_read_the_key() -> None:
    harness = KeyHarness()
    harness.apim.identity = None
    await harness.setup()
    with pytest.raises(ValidationError, match="has none"):
        await harness.publish()


async def test_the_plan_creates_the_named_value_before_anything_names_it(
    harness: KeyHarness,
) -> None:
    publication_id = await harness.publish()
    publication = await harness.service.get_publication(ACTOR, publication_id)
    assert publication.backend_key_name == f"{CLAUDE_API}-key"
    assert publication.api_shape == ApiShape.ANTHROPIC_MESSAGES

    plan = await harness.service.plan(ACTOR, publication_id)

    assert [step.kind for step in plan.steps] == [
        PublishedResourceKind.NAMED_VALUE,
        PublishedResourceKind.BACKEND,
        PublishedResourceKind.POLICY_FRAGMENT,
        PublishedResourceKind.API,
        PublishedResourceKind.API_OPERATION,
        PublishedResourceKind.API_OPERATION,
        PublishedResourceKind.API_POLICY,
        PublishedResourceKind.PRODUCT,
        PublishedResourceKind.PRODUCT_API,
        PublishedResourceKind.SUBSCRIPTION,
    ]
    first = plan.steps[0]
    assert first.name == f"{CLAUDE_API}-key"
    assert first.action == PublishAction.CREATE
    assert "Key Vault" in first.reason
    ranks = [publishing.CREATE_ORDER.index(step.kind) for step in plan.steps]
    assert ranks == sorted(ranks)
    summaries = [facet.summary for facet in plan.facets]
    assert "Sends the model endpoint's API key" in " ".join(summaries)
    assert not any(facet.element == "authentication-managed-identity" for facet in plan.facets)


async def test_apply_hands_api_management_the_secret_identifier_and_never_the_key(
    harness: KeyHarness,
) -> None:
    publication_id = await harness.publish()

    run = await harness.apply(publication_id)

    assert run.status == PublishRunStatus.SUCCEEDED
    puts = harness.apim.write_paths("PUT")
    assert puts[0] == CLAUDE_KEY
    assert puts.index(CLAUDE_KEY) < puts.index(f"policyFragments/{CLAUDE_API}")
    # API Management would have refused a policy naming a named value that didn't exist yet.
    assert harness.apim.dangling_references == []
    [body] = [
        call["body"]
        for call in harness.apim.http_calls
        if call["method"] == "PUT" and call["path"] == CLAUDE_KEY
    ]
    assert body == {
        "properties": {
            "displayName": f"{CLAUDE_API}-key",
            "secret": True,
            "keyVault": {"secretIdentifier": SECRET_URI},
        }
    }
    assert KEY not in json.dumps(harness.apim.http_calls)
    assert harness.apim.list_value_calls == []
    publication = await harness.service.get_publication(ACTOR, publication_id)
    assert publication.status == PublicationStatus.PUBLISHED
    assert any(
        item.kind == PublishedResourceKind.NAMED_VALUE and item.created_by_mosaic
        for item in publication.resources
    )


@pytest.mark.parametrize(
    ("deployment", "api_name", "header", "enforcement"),
    [
        ("claude-sonnet-4-5", CLAUDE_API, "x-api-key", None),
        ("gpt-4-1", GPT_API, "api-key", TOKENS),
    ],
)
async def test_the_backend_gets_the_gateway_key_and_no_caller_credential(
    harness: KeyHarness,
    deployment: str,
    api_name: str,
    header: str,
    enforcement: TokenEnforcement | None,
) -> None:
    publication_id = await harness.publish(deployment, enforcement=enforcement)
    assert (await harness.apply(publication_id)).status == PublishRunStatus.SUCCEEDED
    fragment = harness.fragment(api_name)

    headers, removed_query = forwarded(fragment, {f"{api_name}-key": GATEWAY_KEY})

    assert headers[header] == GATEWAY_KEY
    assert {name: value for name, value in headers.items() if value.startswith("caller")} == {}
    assert "authorization" not in headers
    assert "ocp-apim-subscription-key" not in headers
    assert removed_query == {"subscription-key", "api-key"}
    root = ET.fromstring(fragment)
    assert root.find("authentication-managed-identity") is None
    statements = list(root)
    key_at = next(
        index
        for index, element in enumerate(statements)
        if element.tag == "set-header" and element.get("exists-action") == "override"
    )
    # Nothing after the key header touches a credential header.
    later = {
        (element.get("name") or "").casefold()
        for element in statements[key_at + 1 :]
        if element.tag == "set-header"
    }
    assert later.isdisjoint({"api-key", "x-api-key", "authorization"})


async def test_governed_access_reads_the_key_the_same_way() -> None:
    harness = KeyHarness(governed=True)
    await harness.setup()
    publication_id = await harness.publish(
        "gpt-4-1",
        enforcement=TOKENS,
        governed_access=ModelAccessSettings(keys_enabled=True, entra_enabled=False),
    )

    plan = await harness.service.plan(ACTOR, publication_id)
    kinds = [step.kind for step in plan.steps]
    assert kinds.index(PublishedResourceKind.NAMED_VALUE) < kinds.index(
        PublishedResourceKind.BACKEND
    )
    assert kinds.index(PublishedResourceKind.NAMED_VALUE) < kinds.index(
        PublishedResourceKind.POLICY_FRAGMENT
    )
    run = await harness.service.apply(ACTOR, publication_id, plan.id)
    await harness.service.wait_for_idle()
    assert (await harness.service.get_run(ACTOR, run.id)).status == PublishRunStatus.SUCCEEDED

    fragment = harness.fragment(GPT_API)
    headers, removed_query = forwarded(fragment, {f"{GPT_API}-key": GATEWAY_KEY})
    assert headers["api-key"] == GATEWAY_KEY
    assert not any(value.startswith("caller") for value in headers.values())
    assert removed_query == {"subscription-key", "api-key"}
    # Each credential is removed exactly once, even where the governed policy already did.
    removals = [
        (element.get("name") or "").casefold()
        for element in ET.fromstring(fragment)
        if element.tag == "set-header" and element.get("exists-action") == "delete"
    ]
    assert len(removals) == len(set(removals))
    assert ET.fromstring(fragment).find("authentication-managed-identity") is None


async def test_a_key_api_management_cannot_read_is_removed_and_explained(
    harness: KeyHarness,
) -> None:
    harness.apim.named_value_status[f"{CLAUDE_API}-key"] = "Forbidden"
    publication_id = await harness.publish()

    run = await harness.apply(publication_id)

    assert run.status == PublishRunStatus.ROLLED_BACK
    [step] = run.steps
    assert step.kind == PublishedResourceKind.NAMED_VALUE
    assert step.status == PublishStepStatus.FAILED
    assert "Key Vault Secrets User" in (step.error or "")
    assert "MOSAIC removed it again" in (step.error or "")
    assert CLAUDE_KEY not in harness.apim.written
    assert f"policyFragments/{CLAUDE_API}" not in harness.apim.written


async def test_azure_repeating_the_secret_identifier_never_reaches_a_run_or_a_log(
    harness: KeyHarness,
) -> None:
    harness.apim.fail_write(
        CLAUDE_KEY,
        400,
        error={
            "code": "ValidationError",
            "message": f"Can't read {SECRET_URI} with this identity.",
        },
    )
    publication_id = await harness.publish()

    with capture_logs() as structured:
        run = await harness.apply(publication_id)

    assert run.status == PublishRunStatus.ROLLED_BACK
    recorded = json.dumps(
        [run.model_dump(mode="json"), structured, (await harness.service.get_publication(
            ACTOR, publication_id
        )).model_dump(mode="json")],
        default=str,
    )
    assert SECRET_NAME not in recorded
    assert "[redacted]" in (run.steps[0].error or "")


async def test_pointing_the_endpoint_at_another_secret_needs_a_new_review(
    harness: KeyHarness,
) -> None:
    publication_id = await harness.publish()
    plan = await harness.service.plan(ACTOR, publication_id)

    await harness.world.service.update(
        ACTOR,
        harness.endpoint_id,
        ModelEndpointUpdate(
            credential_secret_uri=f"https://{VAULT_NAME}.vault.azure.net/secrets/rotated-key"
        ),
    )

    with pytest.raises(ConflictError, match="changed after the plan"):
        await harness.service.apply(ACTOR, publication_id, plan.id)
    replanned = await harness.service.plan(ACTOR, publication_id)
    assert replanned.digest != plan.digest


async def test_unpublish_reviews_and_removes_the_named_value_last(harness: KeyHarness) -> None:
    publication_id = await harness.publish()
    await harness.apply(publication_id)

    review = await harness.service.plan_unpublish(ACTOR, publication_id)
    last = review.steps[-1]
    assert last.kind == PublishedResourceKind.NAMED_VALUE
    assert last.action == PublishAction.DELETE
    assert "stays in Key Vault" in last.reason

    harness.apim.writes.clear()
    started = await reviewed_unpublish(harness.service, ACTOR, publication_id)
    await harness.service.wait_for_idle()
    run = await harness.service.get_run(ACTOR, started.id)

    assert run.status == PublishRunStatus.SUCCEEDED
    deleted = harness.apim.write_paths("DELETE")
    # API Management refuses to delete a named value a policy still names, so the order matters.
    assert deleted.index(f"policyFragments/{CLAUDE_API}") < deleted.index(CLAUDE_KEY)
    assert deleted[-1] == CLAUDE_KEY
    assert CLAUDE_KEY not in harness.apim.written
    publication = await harness.service.get_publication(ACTOR, publication_id)
    assert publication.created_resources() == []


async def test_governed_unpublish_reviews_and_removes_the_named_value_last() -> None:
    harness = KeyHarness(governed=True)
    await harness.setup()
    publication_id = await harness.publish(
        "gpt-4-1",
        enforcement=TOKENS,
        governed_access=ModelAccessSettings(keys_enabled=True, entra_enabled=False),
    )
    assert (await harness.apply(publication_id)).status == PublishRunStatus.SUCCEEDED

    review = await harness.service.plan_unpublish(ACTOR, publication_id)
    kinds = [step.kind for step in review.steps]
    # The API goes first, while the deny policy is attached; the key goes after its last reader.
    assert kinds[0] == PublishedResourceKind.API
    assert kinds[-1] == PublishedResourceKind.NAMED_VALUE
    assert "stays in Key Vault" in review.steps[-1].reason
    assert any("refuses every call" in warning for warning in review.warnings)

    harness.apim.writes.clear()
    started = await reviewed_unpublish(harness.service, ACTOR, publication_id)
    await harness.service.wait_for_idle()
    run = await harness.service.get_run(ACTOR, started.id)

    assert run.status == PublishRunStatus.SUCCEEDED
    deleted = harness.apim.write_paths("DELETE")
    assert deleted.index(f"policyFragments/{GPT_API}") < deleted.index(GPT_KEY)
    assert deleted[-1] == GPT_KEY
    assert GPT_KEY not in harness.apim.written
    publication = await harness.service.get_publication(ACTOR, publication_id)
    assert publication.created_resources() == []


async def test_a_failed_apply_rolls_the_named_value_back(harness: KeyHarness) -> None:
    harness.apim.fail_write(f"products/{CLAUDE_API}", 500)
    publication_id = await harness.publish()

    run = await harness.apply(publication_id)

    assert run.status == PublishRunStatus.ROLLED_BACK
    assert CLAUDE_KEY in harness.apim.write_paths("DELETE")
    assert CLAUDE_KEY not in harness.apim.written
    publication = await harness.service.get_publication(ACTOR, publication_id)
    assert publication.created_resources() == []


async def test_a_named_value_mosaic_did_not_create_is_never_taken_over(
    harness: KeyHarness,
) -> None:
    harness.apim.seed(CLAUDE_KEY, {"properties": {"displayName": f"{CLAUDE_API}-key"}})
    publication_id = await harness.publish()

    with pytest.raises(ConflictError, match="does not own"):
        await harness.service.plan(ACTOR, publication_id)


async def test_a_published_declaration_cannot_be_removed(harness: KeyHarness) -> None:
    publication_id = await harness.publish()
    await harness.apply(publication_id)

    with pytest.raises(ConflictError, match="Unpublish claude-sonnet-4-5"):
        await harness.world.service.remove_declared_deployment(
            ACTOR, harness.endpoint_id, "claude-sonnet-4-5"
        )

    started = await reviewed_unpublish(harness.service, ACTOR, publication_id)
    await harness.service.wait_for_idle()
    assert (await harness.service.get_run(ACTOR, started.id)).status == PublishRunStatus.SUCCEEDED
    endpoint = await harness.world.service.remove_declared_deployment(
        ACTOR, harness.endpoint_id, "claude-sonnet-4-5"
    )
    assert [item.deployment_name for item in endpoint.declared_deployments] == ["gpt-4-1"]
    # The unpublished publication owned nothing, so it went with the declaration.
    assert await harness.world.gateway_repository.get_publication(
        ACTOR.tenant_id, publication_id
    ) is None


async def test_a_managed_identity_publication_record_is_unchanged(harness: KeyHarness) -> None:
    publication_id = await harness.publish()
    keyed = await harness.service.get_publication(ACTOR, publication_id)
    assert "backendKeyName" in keyed.model_dump(mode="json", by_alias=True)

    managed = keyed.model_copy(update={"backend_key_name": None})
    # The previous release forbids fields it doesn't know, so an unused one stays out.
    assert "backendKeyName" not in managed.model_dump(mode="json", by_alias=True)
