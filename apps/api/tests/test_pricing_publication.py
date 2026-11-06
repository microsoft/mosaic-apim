"""Tests for billing candidate mapping review, atomic rate publication, and connected sources."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mosaic_api.billing_pricing import (
    BillingAcquisitionRun,
    BillingCandidateMapping,
    BillingConnection,
    BillingConnectionCheckResult,
    BillingConnectionCreate,
    BillingConnectionUpdate,
    BillingConnectorPayloadPart,
    BillingConnectorStepResult,
    BillingMappingsUpdateRequest,
    BillingPublication,
    BillingPublishRequest,
    BillingRunTriggerRequest,
    BillingSchedule,
    billing_evidence_ref,
)
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from mosaic_api.domain import (
    AuditEvent,
    KeyVaultSecretId,
    ModelEndpoint,
    ModelEndpointCapabilities,
    ModelProvider,
    new_id,
)
from mosaic_api.errors import ConflictError, NotFoundError, ValidationError
from mosaic_api.main import create_app
from mosaic_api.observed import ObservedModelDeployment
from mosaic_api.pricing import (
    PriceCreate,
    PriceSeed,
    token_amount,
)
from mosaic_api.repositories import (
    InMemoryGatewayRepository,
    InMemoryModelEndpointRepository,
    InMemoryPricingRepository,
)
from mosaic_api.services.directory import Actor
from mosaic_api.services.pricing import PricingService
from pydantic import SecretStr

TENANT = "tenant-pricing-publication"
TODAY = date(2026, 10, 15)


def _clock() -> datetime:
    return datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def _actor(tenant_id: str = TENANT) -> Actor:
    return Actor(object_id="admin-pub-1", tenant_id=tenant_id)


def _audit(tenant_id: str = TENANT) -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id=tenant_id,
        action="test.seed",
        resource_type="seed",
        resource_id="seed",
        actor_object_id="admin-pub-1",
    )


def _settings(roles: list[str]) -> Settings:
    return Settings(
        environment=Environment.TEST,
        auth_mode=AuthMode.LOCAL,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id=TENANT,
        local_roles=roles,
    )


@pytest.fixture
def admin_client() -> Iterator[TestClient]:
    with TestClient(create_app(_settings(["Admin", "User"]))) as client:
        yield client


class _FakeSecretVault:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.deleted: list[str] = []

    def new_secret(self, name: str) -> KeyVaultSecretId:
        return KeyVaultSecretId.parse(f"https://contoso-mgmt.vault.azure.net/secrets/{name}")

    async def put(
        self, secret: KeyVaultSecretId, value: SecretStr, *, tags: dict[str, str]
    ) -> None:
        _ = tags
        self.values[secret.versionless] = value.get_secret_value()

    async def delete(self, secret: KeyVaultSecretId) -> bool:
        self.deleted.append(secret.versionless)
        return self.values.pop(secret.versionless, None) is not None

    async def read(self, uri: str) -> str:
        parsed = KeyVaultSecretId.parse(uri)
        return self.values[parsed.versionless]


class _FakeBillingConnector:
    def __init__(
        self,
        provider: Any = "azure",
        *,
        steps: list[BillingConnectorStepResult] | None = None,
    ) -> None:
        self.provider = provider
        self.steps = list(steps or [])
        self.seen_secrets: list[str | None] = []
        self.polled_run_ids: list[str] = []

    async def check_access(
        self,
        connection: BillingConnection,
        *,
        secret_value: str | None = None,
    ) -> BillingConnectionCheckResult:
        self.seen_secrets.append(secret_value)
        return BillingConnectionCheckResult(
            connection_id=connection.id,
            provider=connection.provider,
            status="ready",
            message=f"Verified read-only billing access for {connection.name}.",
            scope_summary=connection.azure_scope or connection.aws_cur_bucket,
            supported_source_months=["2026-08", "2026-09"],
            warnings=[
                "Late billing adjustments for September may arrive up to 5 days after close."
            ],
        )

    async def start_or_poll_acquisition(
        self,
        connection: BillingConnection,
        run: BillingAcquisitionRun,
        *,
        secret_value: str | None = None,
    ) -> BillingConnectorStepResult:
        _ = connection
        self.seen_secrets.append(secret_value)
        self.polled_run_ids.append(run.id)
        if self.steps:
            return self.steps.pop(0)
        raise AssertionError("No fake connector steps remaining")


async def _build_estate(
    *,
    seed: PriceSeed | None = None,
) -> tuple[
    PricingService,
    InMemoryPricingRepository,
    InMemoryModelEndpointRepository,
    _FakeSecretVault,
]:
    pricing_repo = InMemoryPricingRepository()
    endpoint_repo = InMemoryModelEndpointRepository()
    gateway_repo = InMemoryGatewayRepository()
    vault = _FakeSecretVault()

    aoai_res_east = (
        "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-ai"
        "/providers/Microsoft.CognitiveServices/accounts/contoso-aoai-east"
    )
    aoai_res_west = (
        "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-ai"
        "/providers/Microsoft.CognitiveServices/accounts/contoso-aoai-west"
    )

    await endpoint_repo.save_endpoint(
        ModelEndpoint(
            id="endpoint-east",
            tenant_id=TENANT,
            name="Contoso AOAI East",
            provider=ModelProvider.AZURE_OPENAI,
            endpoint="https://contoso-aoai-east.openai.azure.com",
            azure_resource_id=aoai_res_east,
            account_name="contoso-aoai-east",
            capabilities=ModelEndpointCapabilities(location="eastus2"),
        ),
        _audit(),
    )
    await endpoint_repo.replace_observed_for_endpoint(
        TENANT,
        "endpoint-east",
        [
            ObservedModelDeployment(
                id="obs-chat",
                tenant_id=TENANT,
                endpoint_id="endpoint-east",
                snapshot_id="snap-1",
                deployment_name="chat",
                model_name="gpt-4o",
                model_version="2024-11-20",
                model_format="OpenAI",
                sku_name="GlobalStandard",
                sku_capacity=100,
            ),
            ObservedModelDeployment(
                id="obs-mini-a",
                tenant_id=TENANT,
                endpoint_id="endpoint-east",
                snapshot_id="snap-1",
                deployment_name="mini-a",
                model_name="gpt-4o-mini",
                model_version="2024-07-18",
                model_format="OpenAI",
                sku_name="GlobalStandard",
                sku_capacity=50,
            ),
            ObservedModelDeployment(
                id="obs-mini-b",
                tenant_id=TENANT,
                endpoint_id="endpoint-east",
                snapshot_id="snap-1",
                deployment_name="mini-b",
                model_name="gpt-4o-mini",
                model_version="2024-07-18",
                model_format="OpenAI",
                sku_name="GlobalStandard",
                sku_capacity=50,
            ),
        ],
        "snap-1",
    )

    await endpoint_repo.save_endpoint(
        ModelEndpoint(
            id="endpoint-west",
            tenant_id=TENANT,
            name="Contoso AOAI West",
            provider=ModelProvider.AZURE_OPENAI,
            endpoint="https://contoso-aoai-west.openai.azure.com",
            azure_resource_id=aoai_res_west,
            account_name="contoso-aoai-west",
            capabilities=ModelEndpointCapabilities(location="westus2"),
        ),
        _audit(),
    )
    await endpoint_repo.replace_observed_for_endpoint(
        TENANT,
        "endpoint-west",
        [
            ObservedModelDeployment(
                id="obs-west-chat",
                tenant_id=TENANT,
                endpoint_id="endpoint-west",
                snapshot_id="snap-1",
                deployment_name="chat",
                model_name="gpt-4o",
                model_version="2024-11-20",
                model_format="OpenAI",
                sku_name="GlobalStandard",
                sku_capacity=100,
            ),
        ],
        "snap-1",
    )

    service = PricingService(
        pricing_repo,
        endpoint_repository=endpoint_repo,
        gateway_repository=gateway_repo,
        seed=seed,
        clock=_clock,
        secret_store=vault,
        secret_reader=vault.read,
    )
    return service, pricing_repo, endpoint_repo, vault


def _sample_azure_csv(
    *,
    include_mini: bool = False,
    include_partial_o1: bool = False,
    include_unamortized_blocker: bool = False,
) -> bytes:
    res_east = (
        "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-ai"
        "/providers/Microsoft.CognitiveServices/accounts/contoso-aoai-east"
    )
    lines = [
        "Date,MeterCategory,MeterName,Product,Quantity,UnitOfMeasure,EffectivePrice,"
        "CostInBillingCurrency,BillingCurrencyCode,ChargeType,PricingModel,BenefitId,"
        "ResourceId,ResourceLocation",
        f"2026-09-10,Foundry Models,gpt 4o 1120 Inp glbl Tokens,Azure OpenAI,2000,1K Tokens,"
        f"0.00175,3.50,USD,Usage,OnDemand,,{res_east},East US 2",
        f"2026-09-10,Foundry Models,gpt 4o 1120 Outp glbl Tokens,Azure OpenAI,500,1K Tokens,"
        f"0.007,3.50,USD,Usage,OnDemand,,{res_east},East US 2",
    ]
    if include_mini:
        lines.extend(
            [
                f"2026-09-11,Foundry Models,gpt 4o mini 0718 Inp glbl Tokens,Azure OpenAI,1000,"
                f"1K Tokens,0.00012,0.12,USD,Usage,OnDemand,,{res_east},East US 2",
                f"2026-09-11,Foundry Models,gpt 4o mini 0718 Outp glbl Tokens,Azure OpenAI,500,"
                f"1K Tokens,0.00048,0.24,USD,Usage,OnDemand,,{res_east},East US 2",
            ]
        )
    if include_partial_o1:
        # Input-only row for a generative chat model (missing output tokens -> publishable=False)
        lines.append(
            f"2026-09-12,Foundry Models,o1 Inp glbl Tokens,Azure OpenAI,100,"
            f"1K Tokens,0.015,1.50,USD,Usage,OnDemand,,{res_east},East US 2"
        )
    if include_unamortized_blocker:
        lines.append(
            f"2026-09-12,Foundry Models,Provisioned Managed Global Unit,Azure OpenAI,100,1 Hour,"
            f"0.00,0.00,USD,Usage,Reservation,/providers/Microsoft.Capacity/reservationOrders/r1,"
            f"{res_east},East US 2"
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


# -- 1. Candidate mapping resolution, ambiguity confirmation, and manual override conflicts -------


async def test_update_mappings_enforces_cas_ambiguity_confirmation_and_manual_conflict() -> None:
    service, _, _, _ = await _build_estate()
    actor = _actor()

    # Add a manual override on endpoint-east/mini-a, but NOT on endpoint-east/mini-b
    manual_price = await service.add_price(
        actor,
        PriceCreate(
            cloud="commercial",
            publisher="OpenAI",
            model="gpt-4o-mini",
            version="2024-07-18",
            deployment_type="GlobalStandard",
            deployment="endpoint-east/mini-a",
            input_per_million=0.14,
            output_per_million=0.56,
            effective_from=date(2026, 9, 1),
            source_url="https://contoso.example/manual-mini-a",
            note="Manual rate for mini-a",
        ),
    )

    detail = await service.import_billing_data(
        actor,
        provider="azure",
        pricing_cloud="commercial",
        source_month="2026-09",
        file_name="sep-azure.csv",
        content=_sample_azure_csv(include_mini=True),
    )
    assert detail.preview_revision == 1
    assert detail.version is not None

    mini_cand = next(c for c in detail.candidates if c.model == "gpt-4o-mini")
    gpt4o_cand = next(c for c in detail.candidates if c.model == "gpt-4o")
    assert mini_cand.mapping_confidence == "ambiguous"
    assert mini_cand.ambiguous_deployment_keys == [
        "endpoint-east/mini-a",
        "endpoint-east/mini-b",
    ]

    # 1. Ambiguous candidate mapped to deployment without confirmed=True -> rejected
    with pytest.raises(ValidationError, match="ambiguous deployment mapping"):
        await service.update_billing_mappings(
            actor,
            detail.id,
            BillingMappingsUpdateRequest(
                mappings=[
                    BillingCandidateMapping(
                        candidate_id=mini_cand.candidate_id,
                        scope="deployment",
                        deployment_key="endpoint-east/mini-a",
                        confirmed=False,
                    )
                ],
                preview_revision=detail.preview_revision,
                version=detail.version,
            ),
        )

    # 2. Cross-account mapping (contoso-aoai-east candidate -> endpoint-west/chat) without confirmed
    with pytest.raises(ValidationError, match="resource/account"):
        await service.update_billing_mappings(
            actor,
            detail.id,
            BillingMappingsUpdateRequest(
                mappings=[
                    BillingCandidateMapping(
                        candidate_id=gpt4o_cand.candidate_id,
                        scope="deployment",
                        deployment_key="endpoint-west/chat",
                        confirmed=False,
                    )
                ],
                preview_revision=detail.preview_revision,
                version=detail.version,
            ),
        )

    # 3. Unknown deployment key -> rejected
    with pytest.raises(ValidationError, match="no record of deployment"):
        await service.update_billing_mappings(
            actor,
            detail.id,
            BillingMappingsUpdateRequest(
                mappings=[
                    BillingCandidateMapping(
                        candidate_id=mini_cand.candidate_id,
                        scope="deployment",
                        deployment_key="endpoint-east/nonexistent",
                        confirmed=True,
                    )
                ],
                preview_revision=detail.preview_revision,
                version=detail.version,
            ),
        )

    # 4. Confirmed mapping to endpoint-east/mini-a detects manual override conflict
    updated_a = await service.update_billing_mappings(
        actor,
        detail.id,
        BillingMappingsUpdateRequest(
            mappings=[
                BillingCandidateMapping(
                    candidate_id=mini_cand.candidate_id,
                    scope="deployment",
                    deployment_key="endpoint-east/mini-a",
                    confirmed=True,
                )
            ],
            preview_revision=detail.preview_revision,
            version=detail.version,
        ),
    )
    assert updated_a.preview_revision == 2
    assert updated_a.version != detail.version
    mini_after_a = next(c for c in updated_a.candidates if c.model == "gpt-4o-mini")
    assert mini_after_a.conflicts_with_manual_override is True
    assert mini_after_a.existing_price_id == manual_price.id
    assert mini_after_a.existing_origin == "admin"

    # 5. Stale version or stale preview_revision -> ConflictError
    with pytest.raises(ConflictError):
        await service.update_billing_mappings(
            actor,
            detail.id,
            BillingMappingsUpdateRequest(
                mappings=[],
                preview_revision=detail.preview_revision,
                version=updated_a.version,
            ),
        )
    with pytest.raises(ConflictError):
        await service.update_billing_mappings(
            actor,
            detail.id,
            BillingMappingsUpdateRequest(
                mappings=[],
                preview_revision=updated_a.preview_revision,
                version="stale-etag",
            ),
        )

    # 6. Switching mapping to endpoint-east/mini-b clears manual override conflict (seed only)
    updated_b = await service.update_billing_mappings(
        actor,
        detail.id,
        BillingMappingsUpdateRequest(
            mappings=[
                BillingCandidateMapping(
                    candidate_id=mini_cand.candidate_id,
                    scope="deployment",
                    deployment_key="endpoint-east/mini-b",
                    confirmed=True,
                )
            ],
            preview_revision=updated_a.preview_revision,
            version=updated_a.version,
        ),
    )
    assert updated_b.preview_revision == 3
    mini_after_b = next(c for c in updated_b.candidates if c.model == "gpt-4o-mini")
    assert mini_after_b.conflicts_with_manual_override is False
    assert mini_after_b.existing_origin == "seed"


# -- 2. Pre-publish safeguards: manual conflict, backdating, partial candidates, blockers --------


async def test_publish_validations_for_manual_conflict_backdating_and_blockers() -> None:
    service, _, _, _ = await _build_estate()
    actor = _actor()

    # Add manual override on endpoint-east/chat
    manual_chat = await service.add_price(
        actor,
        PriceCreate(
            cloud="commercial",
            publisher="OpenAI",
            model="gpt-4o",
            version="2024-11-20",
            deployment_type="GlobalStandard",
            deployment="endpoint-east/chat",
            input_per_million=2.10,
            output_per_million=8.40,
            effective_from=date(2026, 9, 1),
            source_url="https://contoso.example/manual-chat",
            note="Manual override for chat",
        ),
    )

    detail = await service.import_billing_data(
        actor,
        provider="azure",
        pricing_cloud="commercial",
        source_month="2026-09",
        file_name="sep-complex.csv",
        content=_sample_azure_csv(
            include_partial_o1=True,
            include_unamortized_blocker=True,
        ),
    )
    by_model = {c.model: c for c in detail.candidates}
    gpt4o_cand = by_model["gpt-4o"]
    o1_cand = by_model["o1"]
    ptu_cand = by_model["*"]

    assert gpt4o_cand.publishable is True
    assert gpt4o_cand.conflicts_with_manual_override is True
    assert o1_cand.publishable is False
    assert ptu_cand.publishable is False

    # 1. Publishing while unamortized blocker is not excluded -> rejected
    # First try including the blocked PTU candidate:
    detail = await service.update_billing_mappings(
        actor,
        detail.id,
        BillingMappingsUpdateRequest(
            mappings=[
                BillingCandidateMapping(
                    candidate_id=ptu_cand.candidate_id,
                    scope="cloudModel",
                    cloud="commercial",
                    confirmed=True,
                )
            ],
            preview_revision=detail.preview_revision,
            version=detail.version,
        ),
    )
    with pytest.raises(ValidationError, match="unamortizedPrepaidUsage"):
        await service.publish_billing_run(
            actor,
            detail.id,
            BillingPublishRequest(
                preview_revision=detail.preview_revision,
                version=detail.version,
            ),
        )

    # 2. Exclude the blocked PTU candidate, but try including the partial o1 candidate -> rejected
    detail = await service.update_billing_mappings(
        actor,
        detail.id,
        BillingMappingsUpdateRequest(
            mappings=[
                BillingCandidateMapping(
                    candidate_id=ptu_cand.candidate_id,
                    scope="excluded",
                ),
                BillingCandidateMapping(
                    candidate_id=o1_cand.candidate_id,
                    scope="cloudModel",
                    cloud="commercial",
                    confirmed=True,
                ),
            ],
            preview_revision=detail.preview_revision,
            version=detail.version,
        ),
    )
    with pytest.raises(ValidationError, match="No output token usage"):
        await service.publish_billing_run(
            actor,
            detail.id,
            BillingPublishRequest(
                preview_revision=detail.preview_revision,
                version=detail.version,
            ),
        )

    # 3. Exclude o1 too; now only gpt-4o is included, which conflicts with manual_chat
    detail = await service.update_billing_mappings(
        actor,
        detail.id,
        BillingMappingsUpdateRequest(
            mappings=[
                BillingCandidateMapping(
                    candidate_id=ptu_cand.candidate_id,
                    scope="excluded",
                ),
                BillingCandidateMapping(
                    candidate_id=o1_cand.candidate_id,
                    scope="excluded",
                ),
                BillingCandidateMapping(
                    candidate_id=gpt4o_cand.candidate_id,
                    scope="deployment",
                    deployment_key="endpoint-east/chat",
                    confirmed=True,
                    replace_manual_override=False,
                ),
            ],
            preview_revision=detail.preview_revision,
            version=detail.version,
        ),
    )
    # Without replace_manual_override + acknowledge_manual_override_replacement -> rejected
    with pytest.raises(ValidationError, match="manual administrator price override"):
        await service.publish_billing_run(
            actor,
            detail.id,
            BillingPublishRequest(
                acknowledge_manual_override_replacement=True,
                preview_revision=detail.preview_revision,
                version=detail.version,
            ),
        )

    # Set replace_manual_override=True on mapping, test missing request acknowledgment -> rejected
    detail = await service.update_billing_mappings(
        actor,
        detail.id,
        BillingMappingsUpdateRequest(
            mappings=[
                BillingCandidateMapping(
                    candidate_id=gpt4o_cand.candidate_id,
                    scope="deployment",
                    deployment_key="endpoint-east/chat",
                    confirmed=True,
                    replace_manual_override=True,
                ),
            ],
            preview_revision=detail.preview_revision,
            version=detail.version,
        ),
    )
    with pytest.raises(ValidationError, match="manual administrator price override"):
        await service.publish_billing_run(
            actor,
            detail.id,
            BillingPublishRequest(
                acknowledge_manual_override_replacement=False,
                preview_revision=detail.preview_revision,
                version=detail.version,
            ),
        )

    # 4. Backdating before today without acknowledge_backdate_impact -> rejected
    with pytest.raises(ValidationError, match="acknowledgeBackdateImpact"):
        await service.publish_billing_run(
            actor,
            detail.id,
            BillingPublishRequest(
                effective_from=date(2026, 9, 1),
                acknowledge_backdate_impact=False,
                acknowledge_manual_override_replacement=True,
                preview_revision=detail.preview_revision,
                version=detail.version,
            ),
        )

    # 5. Publish with both acknowledgments succeeds and records replaced manual override ID
    pub = await service.publish_billing_run(
        actor,
        detail.id,
        BillingPublishRequest(
            effective_from=date(2026, 9, 1),
            acknowledge_backdate_impact=True,
            acknowledge_manual_override_replacement=True,
            note="Backdated September billing rate replacing manual override",
            preview_revision=detail.preview_revision,
            version=detail.version,
        ),
    )
    assert pub.effective_from == date(2026, 9, 1)
    assert pub.backdated is True
    assert pub.replaced_manual_override_ids == [manual_chat.id]
    assert pub.affected_deployment_keys == ["endpoint-east/chat"]
    assert len(pub.price_version_ids) == 1

    # 6. Idempotent repeat call with same effective_from (or None) returns identical publication
    pub_repeat_none = await service.publish_billing_run(actor, detail.id, BillingPublishRequest())
    assert pub_repeat_none.id == pub.id
    pub_repeat_same = await service.publish_billing_run(
        actor,
        detail.id,
        BillingPublishRequest(effective_from=date(2026, 9, 1)),
    )
    assert pub_repeat_same.id == pub.id

    # Requesting a different effective_from on an already-published run raises ConflictError
    with pytest.raises(ConflictError, match="already published"):
        await service.publish_billing_run(
            actor,
            detail.id,
            BillingPublishRequest(effective_from=date(2026, 10, 15)),
        )


# -- 3. Two-phase atomic failure injection, effective dates, and seed refresh protection ---------


async def test_failure_injection_between_prepared_versions_and_publication_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves that if create_billing_publication fails after create_prepared_price_versions,
    neither PricingService.book() nor PricingService.pricer() exposes any prepared version."""

    service, pricing_repo, _, _ = await _build_estate()
    actor = _actor()

    detail = await service.import_billing_data(
        actor,
        provider="azure",
        pricing_cloud="commercial",
        source_month="2026-09",
        file_name="sep-atomic.csv",
        content=_sample_azure_csv(),
    )

    real_create_pub = pricing_repo.create_billing_publication

    async def _failing_create_pub(
        publication: BillingPublication,
        run: BillingAcquisitionRun,
        audit_event: AuditEvent,
    ) -> BillingPublication:
        _ = (publication, run, audit_event)
        raise ConflictError("Simulated transactional failure committing BillingPublication")

    monkeypatch.setattr(pricing_repo, "create_billing_publication", _failing_create_pub)

    with pytest.raises(ConflictError, match="Simulated transactional failure"):
        await service.publish_billing_run(
            actor,
            detail.id,
            BillingPublishRequest(
                preview_revision=detail.preview_revision,
                version=detail.version,
            ),
        )

    # Prepared PriceVersion documents exist in the repository...
    raw_stored_versions = await pricing_repo.list_price_versions(TENANT)
    assert len(raw_stored_versions) == 1
    assert raw_stored_versions[0].origin == "billing"
    assert raw_stored_versions[0].publication_id is not None

    # ...yet neither book() nor pricer() nor price_list() exposes them!
    book_mid = await service.book(TENANT)
    assert not any(entry.origin == "billing" for entry in book_mid.entries)

    pricer_mid = await service.pricer(TENANT)
    rate_mid = pricer_mid.rate("endpoint-east/chat", TODAY)
    assert rate_mid.entry is not None
    assert rate_mid.entry.origin == "seed"
    assert rate_mid.entry.input_per_million == pytest.approx(2.50)
    assert rate_mid.entry.output_per_million == pytest.approx(10.0)

    # Unrelated endpoint also stays on seed
    rate_west_mid = pricer_mid.rate("endpoint-west/chat", TODAY)
    assert rate_west_mid.entry is not None
    assert rate_west_mid.entry.origin == "seed"
    assert rate_west_mid.entry.input_per_million == pytest.approx(2.50)

    # Restore real create_billing_publication and retry publication (default effective_from = TODAY)
    monkeypatch.setattr(pricing_repo, "create_billing_publication", real_create_pub)
    pub = await service.publish_billing_run(
        actor,
        detail.id,
        BillingPublishRequest(
            preview_revision=detail.preview_revision,
            version=detail.version,
        ),
    )
    assert pub.effective_from == TODAY
    assert pub.backdated is False

    # Now pricer() immediately prices endpoint-east/chat at the billing rate from TODAY (2026-10-15)
    pricer_after = await service.pricer(TENANT)
    day_before = TODAY - timedelta(days=1)
    rate_before = pricer_after.rate("endpoint-east/chat", day_before)
    assert rate_before.entry is not None
    assert rate_before.entry.origin == "seed"
    assert rate_before.entry.input_per_million == pytest.approx(2.50)

    rate_on_today = pricer_after.rate("endpoint-east/chat", TODAY)
    assert rate_on_today.entry is not None
    assert rate_on_today.entry.origin == "billing"
    assert rate_on_today.entry.input_per_million == pytest.approx(1.75)
    assert rate_on_today.entry.output_per_million == pytest.approx(7.00)
    assert token_amount(rate_on_today.entry, 1_000_000, 1_000_000) == pytest.approx(8.75)

    # Unrelated account's deployment (endpoint-west/chat) is NOT affected by endpoint-east's rate
    rate_west_after = pricer_after.rate("endpoint-west/chat", TODAY)
    assert rate_west_after.entry is not None
    assert rate_west_after.entry.origin == "seed"
    assert rate_west_after.entry.input_per_million == pytest.approx(2.50)


async def test_later_seed_refresh_never_displaces_deployment_bound_billing_rate() -> None:
    """Proves a later global seed refresh with a newer effectiveFrom does not displace
    a deployment-bound billing rate."""

    service, pricing_repo, endpoint_repo, _ = await _build_estate()
    actor = _actor()

    detail = await service.import_billing_data(
        actor,
        provider="azure",
        pricing_cloud="commercial",
        source_month="2026-09",
        file_name="sep.csv",
        content=_sample_azure_csv(),
    )
    await service.publish_billing_run(
        actor,
        detail.id,
        BillingPublishRequest(
            preview_revision=detail.preview_revision,
            version=detail.version,
        ),
    )

    # Simulate a later release shipping a refreshed global seed with effectiveFrom in 2027
    refreshed_seed = PriceSeed.model_validate(
        {
            "schemaVersion": 1,
            "lastUpdated": "2027-02-01",
            "currency": "USD",
            "sources": [
                {
                    "id": "retail-2027",
                    "title": "Azure Retail Prices API",
                    "url": "https://prices.azure.com/api/retail/prices",
                    "retrievedOn": "2027-02-01",
                }
            ],
            "prices": [
                {
                    "id": "commercial.openai.gpt-4o.2024-11-20.globalstandard",
                    "cloud": "commercial",
                    "publisher": "OpenAI",
                    "model": "gpt-4o",
                    "version": "2024-11-20",
                    "deploymentType": "GlobalStandard",
                    "inputPerMillion": 3.00,
                    "outputPerMillion": 12.00,
                    "effectiveFrom": "2027-01-01",
                    "sourceIds": ["retail-2027"],
                }
            ],
        }
    )

    refreshed_service = PricingService(
        pricing_repo,
        endpoint_repository=endpoint_repo,
        gateway_repository=InMemoryGatewayRepository(),
        seed=refreshed_seed,
        clock=lambda: datetime(2027, 2, 15, 12, 0, tzinfo=UTC),
    )

    pricer_2027 = await refreshed_service.pricer(TENANT)
    # Deployment-bound billing rate on endpoint-east/chat still wins in 2027!
    east_rate = pricer_2027.rate("endpoint-east/chat", date(2027, 2, 15))
    assert east_rate.entry is not None
    assert east_rate.entry.origin == "billing"
    assert east_rate.entry.input_per_million == pytest.approx(1.75)

    # Meanwhile endpoint-west/chat (no deployment-bound rate) uses the refreshed 2027 seed
    west_rate = pricer_2027.rate("endpoint-west/chat", date(2027, 2, 15))
    assert west_rate.entry is not None
    assert west_rate.entry.origin == "seed"
    assert west_rate.entry.input_per_million == pytest.approx(3.00)


async def test_failed_approval_retry_cannot_activate_old_dates_or_excluded_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository, _, _ = await _build_estate()
    actor = _actor()
    detail = await service.import_billing_data(
        actor,
        provider="azure",
        pricing_cloud="commercial",
        source_month="2026-09",
        file_name="retry.csv",
        content=_sample_azure_csv(include_mini=True),
    )
    mini = next(c for c in detail.candidates if c.model == "gpt-4o-mini")
    detail = await service.update_billing_mappings(
        actor,
        detail.id,
        BillingMappingsUpdateRequest(
            mappings=[
                BillingCandidateMapping(
                    candidate_id=mini.candidate_id,
                    scope="deployment",
                    deployment_key="endpoint-east/mini-b",
                    confirmed=True,
                )
            ],
            version=detail.version,
            preview_revision=detail.preview_revision,
        ),
    )
    commit = repository.create_billing_publication

    async def fail_commit(*args: Any) -> BillingPublication:
        raise ConflictError("Injected publication failure")

    monkeypatch.setattr(repository, "create_billing_publication", fail_commit)
    with pytest.raises(ConflictError, match="Injected"):
        await service.publish_billing_run(actor, detail.id)
    failed_ids = {v.id for v in await repository.list_price_versions(TENANT)}
    assert len(failed_ids) == 2
    monkeypatch.setattr(repository, "create_billing_publication", commit)

    detail = await service.update_billing_mappings(
        actor,
        detail.id,
        BillingMappingsUpdateRequest(
            mappings=[BillingCandidateMapping(candidate_id=mini.candidate_id, scope="excluded")],
            version=detail.version,
            preview_revision=detail.preview_revision,
        ),
    )
    tomorrow = TODAY + timedelta(days=1)
    publication = await service.publish_billing_run(
        actor, detail.id, BillingPublishRequest(effective_from=tomorrow)
    )
    assert not failed_ids.intersection(publication.price_version_ids)
    book = await service.book(TENANT)
    assert {e.id for e in book.entries if e.origin == "billing"} == set(
        publication.price_version_ids
    )
    pricer = await service.pricer(TENANT)
    assert pricer.rate("endpoint-east/chat", TODAY).entry.origin == "seed"
    assert pricer.rate("endpoint-east/chat", tomorrow).entry.origin == "billing"
    assert pricer.rate("endpoint-east/mini-b", tomorrow).entry.origin == "seed"


async def test_book_snapshots_markers_before_reading_prepared_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository, _, _ = await _build_estate()
    detail = await service.import_billing_data(
        _actor(),
        provider="azure",
        pricing_cloud="commercial",
        source_month="2026-09",
        file_name="interleaved.csv",
        content=_sample_azure_csv(),
    )
    publication = await service.publish_billing_run(_actor(), detail.id)
    committed = await repository.get_billing_publication(TENANT, publication.id)
    assert committed is not None
    repository.billing_publications.clear()
    read_versions = repository.list_price_versions

    async def interleaved_versions(tenant_id: str) -> list[Any]:
        versions = await read_versions(tenant_id)
        repository.billing_publications[(TENANT, committed.id)] = committed
        return versions

    monkeypatch.setattr(repository, "list_price_versions", interleaved_versions)
    book = await service.book(TENANT)
    assert not any(e.origin == "billing" for e in book.entries)
    book_after = await service.book(TENANT)
    assert any(e.origin == "billing" for e in book_after.entries)

    async def missing_versions(tenant_id: str) -> list[Any]:
        return []

    monkeypatch.setattr(repository, "list_price_versions", missing_versions)
    with pytest.raises(ConflictError, match="not fully readable"):
        await service.book(TENANT)


async def test_cloud_model_billing_scope_requires_deliberate_confirmation() -> None:
    service, _, _, _ = await _build_estate()
    detail = await service.import_billing_data(
        _actor(),
        provider="azure",
        pricing_cloud="commercial",
        source_month="2026-09",
        file_name="unmatched.csv",
        content=_sample_azure_csv().replace(b"contoso-aoai-east", b"unregistered-account"),
    )
    candidate = detail.candidates[0]
    with pytest.raises(ValidationError, match="At least one"):
        await service.publish_billing_run(_actor(), detail.id)
    with pytest.raises(ValidationError, match="explicit confirmation"):
        await service.update_billing_mappings(
            _actor(),
            detail.id,
            BillingMappingsUpdateRequest(
                mappings=[
                    BillingCandidateMapping(
                        candidate_id=candidate.candidate_id, scope="cloudModel", confirmed=False
                    )
                ]
            ),
        )
    detail = await service.update_billing_mappings(
        _actor(),
        detail.id,
        BillingMappingsUpdateRequest(
            mappings=[
                BillingCandidateMapping(
                    candidate_id=candidate.candidate_id, scope="cloudModel", confirmed=True
                )
            ]
        ),
    )
    await service.publish_billing_run(_actor(), detail.id)
    pricer = await service.pricer(TENANT)
    assert pricer.rate("endpoint-west/chat", TODAY).entry.origin == "billing"


@pytest.mark.parametrize("operation", ["rotate", "clear", "delete"])
async def test_external_billing_credentials_are_never_deleted(operation: str) -> None:
    service, _, _, vault = await _build_estate()
    external = vault.new_secret("externally-owned-shared")
    await vault.put(external, SecretStr("external-value"), tags={})
    connection = await service.create_billing_connection(
        _actor(),
        BillingConnectionCreate(
            name="Externally managed credential",
            provider="azure",
            pricing_cloud="commercial",
            azure_scope="/subscriptions/00000000-0000-0000-0000-000000000001",
            credential_secret_uri=external.versionless,
        ),
    )
    if operation != "delete":
        await service.update_billing_connection(
            _actor(),
            connection.id,
            BillingConnectionUpdate(
                credential_secret=SecretStr("new-owned-value") if operation == "rotate" else None,
                clear_stored_secret=operation == "clear",
            ),
        )
    await service.delete_billing_connection(_actor(), connection.id)
    assert vault.values == {external.versionless: "external-value"}
    assert external.versionless not in vault.deleted


async def test_failed_connection_delete_cas_preserves_owned_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository, _, vault = await _build_estate()
    connection = await service.create_billing_connection(
        _actor(),
        BillingConnectionCreate(
            name="Owned credential",
            provider="azure",
            pricing_cloud="commercial",
            azure_scope="/subscriptions/00000000-0000-0000-0000-000000000001",
            credential_secret=SecretStr("owned-value"),
        ),
    )

    async def fail_delete(*args: Any) -> None:
        raise ConflictError("Concurrent connection update")

    monkeypatch.setattr(repository, "delete_billing_connection", fail_delete)
    with pytest.raises(ConflictError, match="Concurrent"):
        await service.delete_billing_connection(_actor(), connection.id)
    assert list(vault.values.values()) == ["owned-value"]
    assert vault.deleted == []


async def test_secret_cleanup_failure_retains_owned_retirement_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository, _, vault = await _build_estate()
    connection = await service.create_billing_connection(
        _actor(),
        BillingConnectionCreate(
            name="Retry cleanup",
            provider="azure",
            pricing_cloud="commercial",
            azure_scope="/subscriptions/00000000-0000-0000-0000-000000000001",
            credential_secret=SecretStr("old-value"),
        ),
    )
    old_uri = next(iter(vault.values))
    delete = vault.delete

    async def unavailable_delete(secret: KeyVaultSecretId) -> bool:
        raise RuntimeError("Injected vault outage")

    monkeypatch.setattr(vault, "delete", unavailable_delete)
    with pytest.raises(RuntimeError, match="Injected vault outage"):
        await service.update_billing_connection(
            _actor(),
            connection.id,
            BillingConnectionUpdate(credential_secret=SecretStr("new-value")),
        )
    stored = await repository.get_billing_connection(TENANT, connection.id)
    assert stored is not None
    assert stored.credential_secret_uri != old_uri
    assert stored.retired_secret_uris == [old_uri]
    assert old_uri in stored.owned_secret_uris
    with pytest.raises(ValidationError, match="retired that credential"):
        await service.update_billing_connection(
            _actor(),
            connection.id,
            BillingConnectionUpdate(credential_secret_uri=old_uri),
        )

    monkeypatch.setattr(vault, "delete", delete)
    await service.update_billing_connection(
        _actor(), connection.id, BillingConnectionUpdate(name="Cleanup retried")
    )
    assert old_uri not in vault.values
    assert list(vault.values.values()) == ["new-value"]


# -- 4. Internal evidence references in PriceListView and PriceHistoryView ----------------------


async def test_price_list_and_history_expose_internal_billing_evidence_references() -> None:
    service, _, _, _ = await _build_estate()
    actor = _actor()

    detail = await service.import_billing_data(
        actor,
        provider="azure",
        pricing_cloud="commercial",
        source_month="2026-09",
        file_name="sep.csv",
        content=_sample_azure_csv(),
    )
    cand = detail.candidates[0]
    pub = await service.publish_billing_run(
        actor,
        detail.id,
        BillingPublishRequest(
            preview_revision=detail.preview_revision,
            version=detail.version,
        ),
    )

    price_list = await service.price_list(actor, "commercial")
    billing_line = next(
        line
        for line in price_list.lines
        if line.current is not None and line.current.id in pub.price_version_ids
    )
    current = billing_line.current
    assert current is not None
    assert current.origin == "billing"
    assert current.billing_run_id == detail.id
    assert current.publication_id == pub.id
    assert current.source_month == "2026-09"
    assert current.billing_provider == "azure"
    assert len(current.sources) == 1
    src = current.sources[0]
    assert src.kind == "billingRun"
    assert src.url is None
    assert src.run_id == detail.id
    assert src.source_month == "2026-09"
    assert src.reference == billing_evidence_ref(detail.id, cand.candidate_id)
    assert src.reference.startswith("mosaic://pricing/billing-runs/")

    history = await service.history(actor, billing_line.line_id)
    assert len(history.versions) == 1
    hist_ver = history.versions[0]
    assert hist_ver.origin == "billing"
    assert hist_ver.sources[0].url is None
    assert hist_ver.sources[0].reference == billing_evidence_ref(detail.id, cand.candidate_id)


# -- 5. Connected billing source CRUD, secret management, check, runs, and poll ----------------


async def test_connected_billing_crud_secret_rotation_check_and_async_polling() -> None:
    service, pricing_repo, _, vault = await _build_estate()
    actor = _actor()

    poll_time = datetime(2026, 10, 15, 12, 5, tzinfo=UTC)
    fake_connector = _FakeBillingConnector(
        provider="azure",
        steps=[
            BillingConnectorStepResult(
                status="running",
                operation_id="op-cost-details-1",
                operation_location="https://management.azure.com/operations/op-cost-details-1",
                poll_after=poll_time,
                status_message="Azure Cost Details report is generating (50%).",
            ),
            BillingConnectorStepResult(
                status="completed",
                operation_id="op-cost-details-1",
                parts=[
                    BillingConnectorPayloadPart(
                        name="part-0001.csv",
                        content=_sample_azure_csv(),
                        content_type="text/csv",
                    )
                ],
                source_scope=("/subscriptions/00000000-0000-0000-0000-000000000001"),
                source_manifest_id="manifest-2026-09-v1",
                total_read_bytes=len(_sample_azure_csv()),
                warnings=["Partition 1 of 1 downloaded."],
            ),
        ],
    )
    service.register_billing_connector(fake_connector)

    # 1. Create connection with credential_secret
    created = await service.create_billing_connection(
        actor,
        BillingConnectionCreate(
            name="Contoso Azure Cost Details",
            provider="azure",
            pricing_cloud="commercial",
            azure_cloud="commercial",
            azure_scope="/subscriptions/00000000-0000-0000-0000-000000000001",
            credential_secret=SecretStr("initial-billing-sp-secret"),
            schedule=BillingSchedule(enabled=True, day_of_month=4, time_utc="05:00"),
        ),
    )
    assert created.has_stored_secret is True
    assert created.status == "untested"
    assert created.version is not None
    assert "initial-billing-sp-secret" not in created.model_dump_json()
    assert len(vault.values) == 1
    initial_secret_uri = next(iter(vault.values))
    assert vault.values[initial_secret_uri] == "initial-billing-sp-secret"

    # Stored entity holds only versionless KeyVaultSecretId, never the raw secret
    stored_conn = await pricing_repo.get_billing_connection(TENANT, created.id)
    assert stored_conn is not None
    assert stored_conn.credential_secret_uri == initial_secret_uri

    # 2. Check connection access -> passes secret to connector and updates connection status
    check_res = await service.check_billing_connection(actor, created.id)
    assert check_res.status == "ready"
    assert check_res.has_stored_secret is True
    assert fake_connector.seen_secrets[-1] == "initial-billing-sp-secret"

    after_check = await service.get_billing_connection(actor, created.id)
    assert after_check.status == "ready"
    assert after_check.checked_at == _clock()

    # 3. Update connection with secret rotation and CAS check
    with pytest.raises(ConflictError):
        await service.update_billing_connection(
            actor,
            created.id,
            BillingConnectionUpdate(
                name="Stale Update",
                version="stale-etag",
            ),
        )

    updated = await service.update_billing_connection(
        actor,
        created.id,
        BillingConnectionUpdate(
            name="Contoso Azure EA Cost Details",
            credential_secret=SecretStr("rotated-billing-sp-secret"),
            version=after_check.version,
        ),
    )
    assert updated.name == "Contoso Azure EA Cost Details"
    assert updated.revision == 2
    assert updated.has_stored_secret is True
    # Old secret was retired and deleted from vault; only new secret remains
    assert initial_secret_uri in vault.deleted
    assert list(vault.values.values()) == ["rotated-billing-sp-secret"]

    # 4. Trigger connected run (defaults to previous complete month 2026-09) -> starts in "running"
    started_run = await service.start_billing_run(
        actor,
        created.id,
        BillingRunTriggerRequest(),
    )
    assert started_run.source_month == "2026-09"
    assert started_run.status == "running"
    assert started_run.acquisition_kind == "connected"
    assert fake_connector.seen_secrets[-1] == "rotated-billing-sp-secret"
    stored_running = await pricing_repo.get_billing_run(TENANT, started_run.id)
    assert stored_running is not None
    assert stored_running.operation_id == "op-cost-details-1"
    assert stored_running.poll_after == poll_time

    # 5. Poll the running run -> transitions to readyForReview and ingests parts
    completed_run = await service.poll_billing_run(actor, started_run.id)
    assert completed_run.status == "readyForReview"
    assert completed_run.source_manifest_id == "manifest-2026-09-v1"
    assert completed_run.eligible_row_count == 2
    assert len(completed_run.candidates) == 1
    assert completed_run.candidates[0].input_per_million == pytest.approx(1.75)
    assert completed_run.candidates[0].output_per_million == pytest.approx(7.00)
    assert any(issue.code == "connectorWarning" for issue in completed_run.issues)

    # Idempotent poll once already readyForReview returns the completed run
    polled_again = await service.poll_billing_run(actor, started_run.id)
    assert polled_again.status == "readyForReview"
    assert polled_again.id == completed_run.id

    # 6. Delete connection -> CAS check + deletes secret from vault
    with pytest.raises(ConflictError):
        await service.delete_billing_connection(actor, created.id, version="wrong-version")
    await service.delete_billing_connection(actor, created.id, version=updated.version)
    assert vault.values == {}
    with pytest.raises(NotFoundError):
        await service.get_billing_connection(actor, created.id)


# -- 6. FastAPI HTTP routes end-to-end verification ---------------------------------------------


def test_pricing_billing_http_routes_end_to_end(admin_client: TestClient) -> None:
    service: PricingService = admin_client.app.state.pricing_service
    fake_connector = _FakeBillingConnector(
        provider="aws",
        steps=[
            BillingConnectorStepResult(
                status="completed",
                parts=[
                    BillingConnectorPayloadPart(
                        name="cur-sep.csv",
                        content=(
                            b"line_item_usage_account_id,line_item_product_code,"
                            b"line_item_usage_type,line_item_operation,line_item_resource_id,"
                            b"line_item_usage_start_date,line_item_usage_amount,pricing_unit,"
                            b"line_item_line_item_type,line_item_currency_code,"
                            b"line_item_unblended_cost,product_region_code,product_model\n"
                            b"123456789012,AmazonBedrock,USE1-Claude3-5Sonnet-input-tokens,"
                            b"InvokeModel,arn:aws:bedrock:us-east-1::foundation-model/claude,"
                            b"2026-09-10T00:00:00Z,1000,1K tokens,Usage,USD,2.40,us-east-1,"
                            b"anthropic.claude-3-5-sonnet-20241022-v2:0\n"
                            b"123456789012,AmazonBedrock,USE1-Claude3-5Sonnet-output-tokens,"
                            b"InvokeModel,arn:aws:bedrock:us-east-1::foundation-model/claude,"
                            b"2026-09-10T00:00:00Z,500,1K tokens,Usage,USD,6.00,us-east-1,"
                            b"anthropic.claude-3-5-sonnet-20241022-v2:0\n"
                        ),
                    )
                ],
                source_scope="s3://contoso-cur-bucket/cur",
                source_manifest_id="aws-manifest-202609",
                total_read_bytes=512,
            )
        ],
    )
    service.register_billing_connector(fake_connector)

    # 1. Create AWS billing connection via POST /api/v1/pricing/billing/connections
    create_resp = admin_client.post(
        "/api/v1/pricing/billing/connections",
        json={
            "name": "AWS Bedrock CUR",
            "provider": "aws",
            "pricingCloud": "aws-bedrock",
            "awsRoleArn": "arn:aws:iam::123456789012:role/MosaicCurReader",
            "awsCurBucket": "contoso-cur-bucket",
            "credentialSecret": "aws-external-id-secret",
        },
    )
    assert create_resp.status_code == 201, create_resp.text
    conn_json = create_resp.json()
    conn_id = conn_json["id"]
    assert conn_json["hasStoredSecret"] is True
    assert "aws-external-id-secret" not in create_resp.text

    # 2. List connections via GET /api/v1/pricing/billing/connections
    list_resp = admin_client.get("/api/v1/pricing/billing/connections")
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 1

    # 3. Check connection via POST /api/v1/pricing/billing/connections/{id}/check
    check_resp = admin_client.post(f"/api/v1/pricing/billing/connections/{conn_id}/check")
    assert check_resp.status_code == 200, check_resp.text
    assert check_resp.json()["status"] == "ready"

    # 4. Trigger run via POST /api/v1/pricing/billing/connections/{id}/runs
    run_resp = admin_client.post(
        f"/api/v1/pricing/billing/connections/{conn_id}/runs",
        json={"sourceMonth": "2026-09"},
    )
    assert run_resp.status_code == 201, run_resp.text
    run_detail = run_resp.json()
    run_id = run_detail["id"]
    assert run_detail["status"] == "readyForReview"
    cand_id = run_detail["candidates"][0]["candidateId"]

    # 5. Update mappings via PATCH /api/v1/pricing/billing/runs/{id}/mappings
    patch_resp = admin_client.patch(
        f"/api/v1/pricing/billing/runs/{run_id}/mappings",
        json={
            "mappings": [
                {
                    "candidateId": cand_id,
                    "scope": "cloudModel",
                    "cloud": "aws-bedrock",
                    "confirmed": True,
                }
            ],
            "previewRevision": run_detail["previewRevision"],
            "version": run_detail["version"],
        },
    )
    assert patch_resp.status_code == 200, patch_resp.text
    patched_run = patch_resp.json()
    assert patched_run["previewRevision"] == run_detail["previewRevision"] + 1

    # 6. Publish run via POST /api/v1/pricing/billing/runs/{id}/publish
    pub_resp = admin_client.post(
        f"/api/v1/pricing/billing/runs/{run_id}/publish",
        json={
            "note": "Approved AWS Bedrock September rates",
            "previewRevision": patched_run["previewRevision"],
            "version": patched_run["version"],
        },
    )
    assert pub_resp.status_code == 200, pub_resp.text
    pub_json = pub_resp.json()
    assert pub_json["sourceMonth"] == "2026-09"
    assert pub_json["pricingCloud"] == "aws-bedrock"
    assert len(pub_json["priceVersionIds"]) == 1

    # 7. List publications via GET /api/v1/pricing/billing/publications
    pubs_resp = admin_client.get("/api/v1/pricing/billing/publications")
    assert pubs_resp.status_code == 200
    assert [item["id"] for item in pubs_resp.json()] == [pub_json["id"]]

    # 8. Verify AWS price line appears in GET /api/v1/pricing/prices?cloud=aws-bedrock
    aws_prices = admin_client.get("/api/v1/pricing/prices", params={"cloud": "aws-bedrock"}).json()
    assert len(aws_prices["lines"]) == 1
    aws_current = aws_prices["lines"][0]["current"]
    assert aws_current["origin"] == "billing"
    assert aws_current["inputPerMillion"] == pytest.approx(2.40)
    assert aws_current["outputPerMillion"] == pytest.approx(12.00)
    assert aws_current["sources"][0]["kind"] == "billingRun"
    assert aws_current["sources"][0]["url"] is None
    assert aws_current["sources"][0]["reference"].startswith(
        f"mosaic://pricing/billing-runs/{run_id}"
    )

    # 9. Delete connection via DELETE /api/v1/pricing/billing/connections/{id}
    latest_conn = admin_client.get(f"/api/v1/pricing/billing/connections/{conn_id}").json()
    del_resp = admin_client.delete(
        f"/api/v1/pricing/billing/connections/{conn_id}",
        params={"version": latest_conn["version"]},
    )
    assert del_resp.status_code == 204
    assert admin_client.get("/api/v1/pricing/billing/connections").json() == []
