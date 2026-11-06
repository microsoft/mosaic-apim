"""Billing-derived pricing foundation: contracts, publication visibility, CAS, and isolation."""

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any, cast

import pytest
from azure.cosmos.aio import CosmosClient
from mosaic_api.billing_pricing import (
    BillingAcquisitionRun,
    BillingCandidateMapping,
    BillingCandidateRate,
    BillingConnection,
    BillingConnectionCheckResult,
    BillingConnectionCreate,
    BillingConnectionUpdate,
    BillingConnector,
    BillingConnectorPayloadPart,
    BillingConnectorStepResult,
    BillingEvidenceChunk,
    BillingNormalizedEvidenceItem,
    BillingPublication,
    BillingPublishRequest,
    BillingReviewIssue,
    BillingRunTriggerRequest,
    BillingSchedule,
    billing_connection_view,
    billing_evidence_chunk_id,
    billing_evidence_ref,
    billing_publication_id,
    billing_publication_view,
    billing_run_detail_view,
    billing_run_summary_view,
    parse_source_month,
    previous_complete_month,
    validate_source_month,
)
from mosaic_api.domain import (
    AuditEvent,
    ModelEndpoint,
    ModelEndpointCapabilities,
    ModelProvider,
    new_id,
)
from mosaic_api.errors import ConflictError
from mosaic_api.observed import ObservedModelDeployment
from mosaic_api.pricing import PriceVersion
from mosaic_api.repositories import (
    CosmosPricingRepository,
    InMemoryGatewayRepository,
    InMemoryModelEndpointRepository,
    InMemoryPricingRepository,
    PricingRepository,
)
from mosaic_api.services.directory import Actor
from mosaic_api.services.pricing import PricingService, price_view
from pydantic import SecretStr
from pydantic import ValidationError as PydanticValidationError
from test_model_access_repositories import Container, Cosmos

TENANT = "tenant-billing"
OTHER_TENANT = "tenant-other"


class _QueryableContainer(Container):
    """Extends the shared transactional batch test container with partition-scoped query_items."""

    def query_items(
        self,
        *,
        query: str,
        parameters: list[dict[str, Any]],
        partition_key: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        params = {item["name"]: item["value"] for item in parameters}
        tenant_id = params.get("@tenantId", partition_key)
        entity_type = params.get("@entityType")
        connection_id = params.get("@connectionId")
        run_id = params.get("@runId")
        prefix = params.get("@prefix")
        matched: list[dict[str, Any]] = []
        for (doc_tenant, _), doc in list(self.items.items()):
            if tenant_id is not None and doc_tenant != tenant_id:
                continue
            if entity_type is not None and doc.get("entityType") != entity_type:
                continue
            if connection_id is not None and doc.get("connectionId") != connection_id:
                continue
            if run_id is not None and doc.get("runId") != run_id:
                continue
            if prefix is not None and not str(doc.get("id", "")).startswith(str(prefix)):
                continue
            matched.append(dict(doc))

        async def _iterate() -> AsyncIterator[dict[str, Any]]:
            for item in matched:
                yield item

        return _iterate()


class _QueryableCosmos(Cosmos):
    def __init__(self) -> None:
        self.containers = {
            name: _QueryableContainer() for name in ("desired", "audit", "sync", "observed")
        }


def _repository(backend: str) -> PricingRepository:
    if backend == "memory":
        return InMemoryPricingRepository()
    cosmos = _QueryableCosmos()
    return CosmosPricingRepository(
        cast(CosmosClient, cosmos), "db", "desired", "audit", owns_client=False
    )


def _audit(tenant_id: str = TENANT, action: str = "pricing.billingTest") -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id=tenant_id,
        action=action,
        resource_type="billingPricing",
        resource_id="resource-1",
        actor_object_id="admin-1",
    )


# -- source month and helper functions ---------------------------------------------------------


def test_source_month_validation_and_helpers() -> None:
    assert previous_complete_month(date(2026, 1, 15)) == "2025-12"
    assert previous_complete_month(date(2026, 10, 28)) == "2026-09"
    assert parse_source_month("2024-02") == (date(2024, 2, 1), date(2024, 2, 29))
    assert validate_source_month(None, today=date(2026, 10, 28)) == "2026-09"
    assert validate_source_month("2026-08", today=date(2026, 10, 28)) == "2026-08"

    with pytest.raises(ValueError, match="not yet complete"):
        validate_source_month("2026-10", today=date(2026, 10, 28))
    with pytest.raises(ValueError, match="not yet complete"):
        validate_source_month("2026-11", today=date(2026, 10, 28))
    with pytest.raises(ValueError, match="YYYY-MM"):
        parse_source_month("2026-13")

    chunk_id = billing_evidence_chunk_id(TENANT, "run_1", 0)
    assert chunk_id == billing_evidence_chunk_id(TENANT, "run_1", 0)
    assert chunk_id != billing_evidence_chunk_id(TENANT, "run_1", 1)
    assert chunk_id != billing_evidence_chunk_id(OTHER_TENANT, "run_1", 0)

    pub_id = billing_publication_id(TENANT, "run_1")
    assert pub_id == billing_publication_id(TENANT, "run_1")
    assert pub_id != billing_publication_id(OTHER_TENANT, "run_1")

    assert billing_evidence_ref("run_1") == "mosaic://pricing/billing-runs/run_1"
    assert (
        billing_evidence_ref("run_1", "cand_42")
        == "mosaic://pricing/billing-runs/run_1#candidate=cand_42"
    )


# -- connection validation and secret redaction ------------------------------------------------


def test_billing_connection_provider_validation_and_secret_redaction() -> None:
    azure_conn = BillingConnection(
        id="conn_azure",
        tenant_id=TENANT,
        name="  Contoso EA  ",
        provider="azure",
        pricing_cloud="Commercial",
        azure_scope="/providers/Microsoft.Billing/billingAccounts/12345",
        credential_secret_uri="https://contoso-kv.vault.azure.net/secrets/billing-sp",
        retired_secret_uris=["https://contoso-kv.vault.azure.net/secrets/old-sp"],
        etag="etag-conn-1",
    )
    assert azure_conn.name == "Contoso EA"
    assert azure_conn.pricing_cloud == "commercial"
    assert azure_conn.azure_cloud == "commercial"
    assert azure_conn.azure_retrieval_mode == "costDetailsApi"
    assert "contoso-kv" not in repr(azure_conn)

    view = billing_connection_view(azure_conn)
    dumped = view.model_dump(by_alias=True)
    assert view.has_stored_secret is True
    assert view.version == "etag-conn-1"
    assert "credentialSecretUri" not in dumped
    assert "retiredSecretUris" not in dumped

    aws_create = BillingConnectionCreate.model_validate(
        {
            "name": "AWS Bedrock CUR",
            "provider": "aws",
            "pricingCloud": "aws-commercial",
            "awsRoleArn": "arn:aws:iam::123456789012:role/MosaicCurReader",
            "awsCurBucket": "contoso-cur-bucket",
            "credentialSecret": SecretStr("external-secret"),
        }
    )
    assert aws_create.aws_account_id == "123456789012"
    assert "external-secret" not in repr(aws_create)

    gcp_create = BillingConnectionCreate.model_validate(
        {
            "name": "GCP Vertex Billing",
            "provider": "gcp",
            "pricingCloud": "gcp-vertex",
            "gcpProjectId": "contoso-finops",
            "gcpDatasetId": "billing_export",
            "gcpTableId": "gcp_billing_export_resource_v1_012345",
            "schedule": {
                "enabled": True,
                "dayOfMonth": 4,
                "timeUtc": "05:30",
                "timeZone": "America/New_York",
            },
        }
    )
    assert gcp_create.schedule.enabled is True
    assert gcp_create.schedule.day_of_month == 4

    with pytest.raises(PydanticValidationError, match="Azure billing or subscription scope"):
        BillingConnectionCreate.model_validate(
            {
                "name": "Missing Azure Scope",
                "provider": "azure",
                "pricingCloud": "commercial",
            }
        )

    with pytest.raises(PydanticValidationError, match="export manifest URL"):
        BillingConnectionCreate.model_validate(
            {
                "name": "Missing Manifest URL",
                "provider": "azure",
                "pricingCloud": "commercial",
                "azureScope": "/subscriptions/00000000-0000-0000-0000-000000000000",
                "azureRetrievalMode": "exportManifest",
            }
        )

    with pytest.raises(PydanticValidationError, match="IAM role ARN and a CUR S3 bucket"):
        BillingConnectionCreate.model_validate(
            {
                "name": "Missing AWS Bucket",
                "provider": "aws",
                "pricingCloud": "aws",
                "awsRoleArn": "arn:aws:iam::123456789012:role/Reader",
            }
        )

    with pytest.raises(PydanticValidationError, match="BigQuery project ID"):
        BillingConnectionCreate.model_validate(
            {
                "name": "Missing GCP Table",
                "provider": "gcp",
                "pricingCloud": "gcp",
                "gcpProjectId": "proj",
                "gcpDatasetId": "dataset",
            }
        )

    with pytest.raises(PydanticValidationError, match="incur cloud provider charges"):
        BillingConnectionCreate.model_validate(
            {
                "name": "Unacknowledged Notice",
                "provider": "azure",
                "pricingCloud": "commercial",
                "azureScope": "/subscriptions/00000000-0000-0000-0000-000000000000",
                "costNoticeAcknowledged": False,
            }
        )

    update = BillingConnectionUpdate.model_validate(
        {"name": " Updated Name ", "pricingCloud": "Government", "version": "etag-1"}
    )
    assert update.name == "Updated Name"
    assert update.pricing_cloud == "government"


def test_connector_protocol_and_run_views() -> None:
    class _FakeConnector:
        provider: Any = "azure"

        async def check_access(
            self,
            connection: BillingConnection,
            *,
            secret_value: str | None = None,
        ) -> BillingConnectionCheckResult:
            return BillingConnectionCheckResult(
                connection_id=connection.id,
                provider=connection.provider,
                status="ready",
                message="Verified Cost Details access",
                has_stored_secret=secret_value is not None,
                supported_source_months=["2026-09"],
            )

        async def start_or_poll_acquisition(
            self,
            connection: BillingConnection,
            run: BillingAcquisitionRun,
            *,
            secret_value: str | None = None,
        ) -> BillingConnectorStepResult:
            return BillingConnectorStepResult(
                status="completed",
                parts=[BillingConnectorPayloadPart(name="part-0.csv", content=b"col1,col2\n")],
                source_scope=connection.azure_scope,
                total_read_bytes=10,
            )

    connector: BillingConnector = _FakeConnector()
    assert connector.provider == "azure"

    candidate = BillingCandidateRate(
        candidate_id="cand_1",
        provider="azure",
        pricing_cloud="commercial",
        publisher="OpenAI",
        model="gpt-4o",
        version="2024-11-20",
        deployment_type="GlobalStandard",
        region="eastus2",
        input_tokens=2_000_000,
        output_tokens=500_000,
        input_cost_usd=3.6,
        output_cost_usd=3.6,
        total_eligible_cost_usd=7.2,
        input_per_million=1.8,
        output_per_million=7.2,
        publishable=True,
        suggested_scope="deployment",
        suggested_deployment_key="endpoint-1/chat",
        mapping_confidence="exact",
        evidence_line_keys=["line_1"],
    )
    run = BillingAcquisitionRun(
        id="run_1",
        tenant_id=TENANT,
        connection_id="conn_azure",
        connection_name="Contoso EA",
        provider="azure",
        pricing_cloud="commercial",
        acquisition_kind="connected",
        source_month="2026-09",
        status="readyForReview",
        raw_row_count=10,
        eligible_row_count=8,
        excluded_row_count=2,
        total_eligible_cost_usd=7.2,
        total_excluded_cost_usd=0.5,
        evidence_chunk_count=1,
        evidence_item_count=1,
        candidates=[candidate],
        mappings=[
            BillingCandidateMapping(
                candidate_id="cand_1",
                scope="deployment",
                deployment_key="endpoint-1/chat",
                confirmed=True,
            )
        ],
        issues=[
            BillingReviewIssue(
                code="tax_excluded",
                severity="info",
                message="Excluded 2 tax rows",
            )
        ],
        etag="etag-run-1",
    )
    chunk = BillingEvidenceChunk(
        id=billing_evidence_chunk_id(TENANT, run.id, 0),
        tenant_id=TENANT,
        run_id=run.id,
        chunk_index=0,
        items=[
            BillingNormalizedEvidenceItem(
                line_key="line_1",
                provider="azure",
                usage_category="inputTokens",
                raw_quantity=2000.0,
                raw_usage_unit="1K Tokens",
                normalized_quantity=2_000_000.0,
                normalized_unit="tokens",
                eligible_cost_usd=3.6,
            )
        ],
    )
    summary = billing_run_summary_view(run)
    assert summary.candidate_count == 1
    assert summary.publishable_candidate_count == 1
    assert summary.blocker_count == 0
    assert summary.version == "etag-run-1"

    detail = billing_run_detail_view(run, [chunk])
    assert len(detail.evidence_items) == 1
    assert detail.evidence_items[0].line_key == "line_1"

    trigger = BillingRunTriggerRequest(source_month="2026-09")
    assert trigger.source_month == "2026-09"
    publish_req = BillingPublishRequest(note="  Approved September rates  ")
    assert publish_req.note == "Approved September rates"


# -- atomic visibility and pricing service integration -----------------------------------------


@pytest.mark.parametrize("backend", ["memory", "cosmos"])
async def test_prepared_price_versions_stay_invisible_until_publication_commits(
    backend: str,
) -> None:
    repository = _repository(backend)
    endpoints = InMemoryModelEndpointRepository()
    gateways = InMemoryGatewayRepository()
    await endpoints.save_endpoint(
        ModelEndpoint(
            id="endpoint-1",
            tenant_id=TENANT,
            name="Contoso AOAI",
            provider=ModelProvider.AZURE_OPENAI,
            endpoint="https://contoso.openai.azure.com",
            capabilities=ModelEndpointCapabilities(location="eastus2"),
        ),
        _audit(),
    )
    await endpoints.replace_observed_for_endpoint(
        TENANT,
        "endpoint-1",
        [
            ObservedModelDeployment(
                id="obs-chat",
                tenant_id=TENANT,
                endpoint_id="endpoint-1",
                snapshot_id="snap-1",
                deployment_name="chat",
                model_name="gpt-4o",
                model_version="2024-11-20",
                model_format="OpenAI",
                sku_name="GlobalStandard",
                sku_capacity=100,
            )
        ],
        "snap-1",
    )
    service = PricingService(
        repository,
        endpoint_repository=endpoints,
        gateway_repository=gateways,
        clock=lambda: datetime(2026, 10, 15, 12, tzinfo=UTC),
    )
    actor = Actor(object_id="admin-1", tenant_id=TENANT)

    run = await repository.create_billing_run(
        BillingAcquisitionRun(
            id="run_sep",
            tenant_id=TENANT,
            provider="azure",
            pricing_cloud="commercial",
            acquisition_kind="upload",
            source_month="2026-09",
            status="readyForReview",
        ),
        _audit(action="pricing.billingRunCreated"),
    )
    assert run.etag is not None

    pub_id = billing_publication_id(TENANT, run.id)
    prepared = PriceVersion(
        id="price_billing_sep",
        tenant_id=TENANT,
        origin="billing",
        cloud="commercial",
        publisher="OpenAI",
        model="gpt-4o",
        version="2024-11-20",
        deployment_type="GlobalStandard",
        deployment="endpoint-1/chat",
        input_per_million=1.75,
        output_per_million=7.0,
        effective_from=date(2026, 10, 1),
        source_url=None,
        note="September billing rate",
        recorded_by="admin-1",
        publication_id=pub_id,
        billing_run_id=run.id,
        billing_provider="azure",
        source_month="2026-09",
        source_title="Azure Cost Details (2026-09)",
        source_reference=billing_evidence_ref(run.id, "cand_1"),
    )

    # Idempotent preparation before committing the publication marker.
    await repository.create_prepared_price_versions([prepared])
    await repository.create_prepared_price_versions([prepared])

    # Before BillingPublication is committed, PricingService.book() must NOT expose prepared rates.
    book_before = await service.book(TENANT)
    facts = (await service.deployments(TENANT))["endpoint-1/chat"]
    match_before = book_before.match(facts, date(2026, 10, 15))
    assert hasattr(match_before, "entry")
    assert match_before.entry.origin == "seed"
    assert match_before.entry.input_per_million == pytest.approx(2.5)

    overview_before = await service.overview(actor)
    assert overview_before.pricing_basis == "publicSeed"
    assert overview_before.pending_billing_reviews == 1
    assert overview_before.last_billing_published_at is None

    # A stale run ETag fails the transactional publication batch and keeps the rate invisible.
    publication = BillingPublication(
        id=pub_id,
        tenant_id=TENANT,
        run_id=run.id,
        provider="azure",
        pricing_cloud="commercial",
        source_month="2026-09",
        effective_from=date(2026, 10, 1),
        price_version_ids=[prepared.id],
        affected_deployment_keys=["endpoint-1/chat"],
        published_by="admin-1",
        published_at=datetime(2026, 10, 15, 12, tzinfo=UTC),
    )
    stale_run = run.model_copy(update={"status": "published", "etag": "stale-etag"})
    with pytest.raises(ConflictError):
        await repository.create_billing_publication(
            publication,
            stale_run,
            _audit(action="pricing.billingPublished"),
        )
    assert await repository.get_billing_publication(TENANT, pub_id) is None

    # Committing the publication marker atomically activates the prepared PriceVersion.
    published_run = run.model_copy(
        update={
            "status": "published",
            "publication_id": pub_id,
            "published_at": datetime(2026, 10, 15, 12, tzinfo=UTC),
            "published_by": "admin-1",
        }
    )
    saved_pub = await repository.create_billing_publication(
        publication,
        published_run,
        _audit(action="pricing.billingPublished"),
    )
    assert billing_publication_view(saved_pub).source_month == "2026-09"

    book_after = await service.book(TENANT)
    match_after = book_after.match(facts, date(2026, 10, 15))
    assert hasattr(match_after, "entry")
    assert match_after.entry.id == prepared.id
    assert match_after.entry.origin == "billing"
    assert match_after.entry.input_per_million == pytest.approx(1.75)

    view = price_view(match_after.entry, "current")
    assert view.origin == "billing"
    assert view.publication_id == pub_id
    assert view.billing_run_id == run.id
    assert view.source_month == "2026-09"
    assert view.billing_provider == "azure"
    assert view.sources[0].kind == "billingRun"
    assert view.sources[0].url is None
    assert view.sources[0].reference == billing_evidence_ref(run.id, "cand_1")

    overview_after = await service.overview(actor)
    assert overview_after.pricing_basis == "billingDerived"
    assert overview_after.pending_billing_reviews == 0
    assert overview_after.last_billing_source_month == "2026-09"


# -- repository CAS and tenant isolation -------------------------------------------------------


@pytest.mark.parametrize("backend", ["memory", "cosmos"])
async def test_billing_repository_cas_and_tenant_isolation(backend: str) -> None:
    repository = _repository(backend)

    conn = BillingConnection(
        id="conn_1",
        tenant_id=TENANT,
        name="Azure Commercial Billing",
        provider="azure",
        pricing_cloud="commercial",
        azure_scope="/subscriptions/00000000-0000-0000-0000-000000000000",
        schedule=BillingSchedule(enabled=True, day_of_month=5),
    )
    created_conn = await repository.create_billing_connection(
        conn, _audit(action="pricing.billingConnectionCreated")
    )
    assert created_conn.etag is not None

    with pytest.raises(ConflictError):
        await repository.create_billing_connection(
            conn, _audit(action="pricing.billingConnectionCreated")
        )

    # Other tenant cannot see or modify TENANT's connection.
    assert await repository.list_billing_connections(OTHER_TENANT) == []
    assert await repository.get_billing_connection(OTHER_TENANT, conn.id) is None
    with pytest.raises(ConflictError):
        await repository.save_billing_connection(
            created_conn.model_copy(update={"tenant_id": OTHER_TENANT}),
            _audit(tenant_id=OTHER_TENANT),
        )

    # Updating with current ETag succeeds; stale ETag fails with ConflictError.
    updated_conn = await repository.save_billing_connection(
        created_conn.model_copy(update={"name": "Azure EA Billing", "revision": 2}),
        _audit(action="pricing.billingConnectionUpdated"),
    )
    assert updated_conn.name == "Azure EA Billing"
    assert updated_conn.etag != created_conn.etag

    with pytest.raises(ConflictError):
        await repository.save_billing_connection(
            created_conn.model_copy(update={"name": "Stale Overwrite"}),
            _audit(action="pricing.billingConnectionUpdated"),
        )
    with pytest.raises(ConflictError):
        await repository.delete_billing_connection(
            created_conn,
            _audit(action="pricing.billingConnectionDeleted"),
        )

    # Runs and evidence chunks are isolated by tenant and guarded by CAS.
    run = await repository.create_billing_run(
        BillingAcquisitionRun(
            id="run_1",
            tenant_id=TENANT,
            connection_id=conn.id,
            provider="azure",
            pricing_cloud="commercial",
            acquisition_kind="connected",
            source_month="2026-09",
        ),
        _audit(action="pricing.billingRunCreated"),
    )
    assert run.etag is not None
    assert len(await repository.list_billing_runs(TENANT, connection_id=conn.id)) == 1
    assert await repository.list_billing_runs(OTHER_TENANT) == []
    assert await repository.get_billing_run(OTHER_TENANT, run.id) is None

    updated_run = await repository.save_billing_run(
        run.model_copy(update={"status": "running"}),
        _audit(action="pricing.billingRunUpdated"),
    )
    with pytest.raises(ConflictError):
        await repository.save_billing_run(
            run.model_copy(update={"status": "failed"}),
            _audit(action="pricing.billingRunUpdated"),
        )
    assert updated_run.status == "running"

    chunk_0 = BillingEvidenceChunk(
        id=billing_evidence_chunk_id(TENANT, run.id, 0),
        tenant_id=TENANT,
        run_id=run.id,
        chunk_index=0,
        items=[
            BillingNormalizedEvidenceItem(
                line_key="k0",
                provider="azure",
                usage_category="inputTokens",
                eligible_cost_usd=1.0,
            )
        ],
    )
    chunk_1 = BillingEvidenceChunk(
        id=billing_evidence_chunk_id(TENANT, run.id, 1),
        tenant_id=TENANT,
        run_id=run.id,
        chunk_index=1,
        items=[
            BillingNormalizedEvidenceItem(
                line_key="k1",
                provider="azure",
                usage_category="outputTokens",
                eligible_cost_usd=2.0,
            )
        ],
    )
    await repository.save_billing_evidence_chunks(TENANT, run.id, [chunk_1, chunk_0])
    chunks = await repository.list_billing_evidence_chunks(TENANT, run.id)
    assert [item.chunk_index for item in chunks] == [0, 1]
    assert await repository.list_billing_evidence_chunks(OTHER_TENANT, run.id) == []

    # Replacing with a smaller chunk set removes stale chunks.
    await repository.save_billing_evidence_chunks(TENANT, run.id, [chunk_0])
    remaining = await repository.list_billing_evidence_chunks(TENANT, run.id)
    assert [item.chunk_index for item in remaining] == [0]

    # Deleting connection with current ETag succeeds.
    await repository.delete_billing_connection(
        updated_conn,
        _audit(action="pricing.billingConnectionDeleted"),
    )
    assert await repository.get_billing_connection(TENANT, conn.id) is None
