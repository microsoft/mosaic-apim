"""Tests for billing import normalization, quantity-weighted Decimal derivation, and upload APIs."""

from __future__ import annotations

import base64
import csv
import gzip
import io
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mosaic_api.billing_pricing import BillingCandidateMapping, BillingMappingsUpdateRequest
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from mosaic_api.domain import (
    AuditEvent,
    ModelEndpoint,
    ModelEndpointCapabilities,
    ModelProvider,
    new_id,
)
from mosaic_api.errors import ValidationError
from mosaic_api.integrations.parquet_cur import read_parquet_table, write_parquet_table
from mosaic_api.main import create_app
from mosaic_api.observed import ObservedModelDeployment
from mosaic_api.pricing import PriceCreate
from mosaic_api.repositories import (
    InMemoryGatewayRepository,
    InMemoryModelEndpointRepository,
    InMemoryPricingRepository,
)
from mosaic_api.services.billing_ingestion import (
    BillingPayloadTooLargeError,
    ingest_billing_bytes,
)
from mosaic_api.services.directory import Actor
from mosaic_api.services.pricing import PricingService

TENANT = "tenant-billing-imports"


def _clock() -> datetime:
    return datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def _actor(tenant_id: str = TENANT) -> Actor:
    return Actor(object_id="admin-1", tenant_id=tenant_id)


def _audit(tenant_id: str = TENANT) -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id=tenant_id,
        action="test.seed",
        resource_type="seed",
        resource_id="seed",
        actor_object_id="admin-1",
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


async def _build_service() -> tuple[
    PricingService,
    InMemoryPricingRepository,
    InMemoryModelEndpointRepository,
]:
    pricing_repo = InMemoryPricingRepository()
    endpoint_repo = InMemoryModelEndpointRepository()
    gateway_repo = InMemoryGatewayRepository()

    aoai_res_id = (
        "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-ai"
        "/providers/Microsoft.CognitiveServices/accounts/contoso-aoai"
    )
    await endpoint_repo.save_endpoint(
        ModelEndpoint(
            id="endpoint-aoai",
            tenant_id=TENANT,
            name="Contoso AOAI",
            provider=ModelProvider.AZURE_OPENAI,
            endpoint="https://contoso-aoai.openai.azure.com",
            azure_resource_id=aoai_res_id,
            account_name="contoso-aoai",
            capabilities=ModelEndpointCapabilities(location="eastus2"),
        ),
        _audit(),
    )
    await endpoint_repo.replace_observed_for_endpoint(
        TENANT,
        "endpoint-aoai",
        [
            ObservedModelDeployment(
                id="obs-chat",
                tenant_id=TENANT,
                endpoint_id="endpoint-aoai",
                snapshot_id="snap-1",
                deployment_name="chat",
                model_name="gpt-4o",
                model_version="2024-11-20",
                model_format="OpenAI",
                sku_name="GlobalStandard",
                sku_capacity=100,
            ),
            ObservedModelDeployment(
                id="obs-embed",
                tenant_id=TENANT,
                endpoint_id="endpoint-aoai",
                snapshot_id="snap-1",
                deployment_name="embed",
                model_name="text-embedding-3-large",
                model_version="1",
                model_format="OpenAI",
                sku_name="Standard",
                sku_capacity=100,
            ),
        ],
        "snap-1",
    )
    service = PricingService(
        pricing_repo,
        endpoint_repository=endpoint_repo,
        gateway_repository=gateway_repo,
        clock=_clock,
    )
    return service, pricing_repo, endpoint_repo


# -- 1. Quantity-weighted Decimal derivation across unequal quantities & unit scales ------------


async def test_quantity_weighted_decimal_derivation_across_scales() -> None:
    """Proves rates are quantity-weighted Decimal sums rather than simple averages of row prices."""

    service, _, _ = await _build_service()
    # Day 1: 100,000 raw tokens ("1 Token") = 0.1M tokens, cost $0.30 (row unit rate = $3.00/M)
    # Day 2: 900 thousand tokens ("1K Tokens") = 0.9M tokens, cost $1.35 (row unit rate = $1.50/M)
    # Day 3: 1 million tokens ("1M Tokens") = 1.0M tokens, cost $1.55 (row unit rate = $1.55/M)
    # Total input = 2.0M tokens, total input cost = $3.20 -> weighted rate = $1.60/M
    # (A simple average of row unit rates would have been (3.00 + 1.50 + 1.55) / 3 = 2.016667!)
    # Output:
    # Day 1: 50,000 "Tokens" = 0.05M, cost $0.50 ($10.00/M)
    # Day 2: 450 "1,000 Tokens" = 0.45M, cost $2.70 ($6.00/M)
    # Total output = 0.50M tokens, total output cost = $3.20 -> weighted rate = $6.40/M
    res_id = (
        "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-ai/"
        "providers/Microsoft.CognitiveServices/accounts/contoso-aoai"
    )
    csv_text = (
        "Date,MeterCategory,MeterName,Product,Quantity,UnitOfMeasure,EffectivePrice,"
        "CostInBillingCurrency,BillingCurrencyCode,ChargeType,PricingModel,"
        "ResourceId,ResourceLocation\n"
        f"2026-09-01,Foundry Models,gpt 4o 1120 Inp glbl Tokens,Azure OpenAI,100000,1 Token,"
        f"0.000003,0.30,USD,Usage,OnDemand,{res_id},East US 2\n"
        f"2026-09-02,Foundry Models,gpt 4o 1120 Inp glbl Tokens,Azure OpenAI,900,1K Tokens,"
        f"0.0015,1.35,USD,Usage,OnDemand,{res_id},East US 2\n"
        f"2026-09-03,Foundry Models,gpt 4o 1120 Inp glbl Tokens,Azure OpenAI,1,1M Tokens,"
        f"1.55,1.55,USD,Usage,OnDemand,{res_id},East US 2\n"
        f"2026-09-01,Foundry Models,gpt 4o 1120 Outp glbl Tokens,Azure OpenAI,50000,Tokens,"
        f"0.00001,0.50,USD,Usage,OnDemand,{res_id},East US 2\n"
        f'2026-09-02,Foundry Models,gpt 4o 1120 Outp glbl Tokens,Azure OpenAI,450,"1,000 Tokens",'
        f"0.006,2.70,USD,Usage,OnDemand,{res_id},East US 2\n"
    )

    detail = await service.import_billing_data(
        _actor(),
        provider="azure",
        pricing_cloud="commercial",
        source_month="2026-09",
        file_name="azure-sep.csv",
        content=csv_text.encode("utf-8"),
    )

    assert detail.status == "readyForReview"
    assert detail.raw_row_count == 5
    assert detail.eligible_row_count == 5
    assert detail.total_eligible_cost_usd == pytest.approx(6.40)
    assert len(detail.candidates) == 1

    cand = detail.candidates[0]
    assert cand.model == "gpt-4o"
    assert cand.version == "2024-11-20"
    assert cand.deployment_type == "GlobalStandard"
    assert cand.region == "eastus2"
    assert cand.input_tokens == pytest.approx(2_000_000.0)
    assert cand.output_tokens == pytest.approx(500_000.0)
    assert cand.input_cost_usd == pytest.approx(3.20)
    assert cand.output_cost_usd == pytest.approx(3.20)
    assert cand.input_per_million == pytest.approx(1.60)
    assert cand.output_per_million == pytest.approx(6.40)
    assert cand.publishable is True
    assert cand.mapping_confidence == "exact"
    assert cand.suggested_deployment_key == "endpoint-aoai/chat"
    assert cand.existing_origin == "seed"
    assert cand.existing_input_per_million == pytest.approx(2.50)
    assert cand.existing_output_per_million == pytest.approx(10.0)
    assert cand.conflicts_with_manual_override is False
    assert detail.mappings[0].scope == "deployment"
    assert detail.mappings[0].confirmed is True


@pytest.mark.parametrize("resource_id", ["unregistered-resource", None])
async def test_unmatched_billing_identity_requires_mapping_without_losing_evidence(
    resource_id: str | None,
) -> None:
    service, _, _ = await _build_service()
    content = (
        "Date,MeterCategory,MeterName,Product,Quantity,UnitOfMeasure,"
        "CostInBillingCurrency,BillingCurrencyCode,ResourceId,SubscriptionId,ResourceLocation\n"
        "2026-09-01,Foundry Models,gpt 4o 1120 Inp glbl Tokens,Azure OpenAI,1000,1K Tokens,"
        f"2.00,USD,{resource_id or ''},unregistered-account,East US 2\n"
        "2026-09-01,Foundry Models,gpt 4o 1120 Outp glbl Tokens,Azure OpenAI,100,1K Tokens,"
        f"0.80,USD,{resource_id or ''},unregistered-account,East US 2\n"
    ).encode()
    detail = await service.import_billing_data(
        _actor(),
        provider="azure",
        pricing_cloud="commercial",
        source_month="2026-09",
        file_name="unmatched.csv",
        content=content,
    )
    candidate = detail.candidates[0]
    assert candidate.publishable is True
    assert candidate.input_per_million == 2.0
    assert candidate.output_per_million == 8.0
    assert candidate.mapping_confidence == "unmatched"
    assert candidate.suggested_scope == "excluded"
    assert candidate.suggested_deployment_key is None
    assert detail.mappings[0].scope == "excluded"
    assert detail.mappings[0].confirmed is False
    assert detail.eligible_row_count == 2
    assert detail.total_eligible_cost_usd == pytest.approx(2.8)
    assert len(detail.evidence_items) == 2
    assert set(candidate.evidence_line_keys) == {e.line_key for e in detail.evidence_items}
    assert all(e.source_resource_id == resource_id for e in detail.evidence_items)
    assert all(e.source_account_id == "unregistered-account" for e in detail.evidence_items)

    with pytest.raises(ValidationError, match="At least one publishable candidate"):
        await service.publish_billing_run(_actor(), detail.id)

    updated = await service.update_billing_mappings(
        _actor(),
        detail.id,
        BillingMappingsUpdateRequest(
            mappings=[
                BillingCandidateMapping(
                    candidate_id=candidate.candidate_id,
                    scope="cloudModel",
                    cloud="commercial",
                    confirmed=True,
                )
            ]
        ),
    )
    assert updated.mappings[0].scope == "cloudModel"
    assert updated.mappings[0].confirmed is True
    publication = await service.publish_billing_run(_actor(), detail.id)
    assert publication is not None


# -- 2. Azure Cost Details CSV & .csv.gz, reservations, tax exclusion, manual conflict ----------


async def test_azure_cost_details_gzip_amortized_vs_unamortized_and_manual_conflict() -> None:
    service, _, endpoint_repo = await _build_service()
    actor = _actor()

    # Add an administrator manual override for endpoint-aoai/chat so conflict detection triggers
    await service.add_price(
        actor,
        PriceCreate(
            cloud="commercial",
            publisher="OpenAI",
            model="gpt-4o",
            version="2024-11-20",
            deployment_type="GlobalStandard",
            deployment="endpoint-aoai/chat",
            input_per_million=2.10,
            output_per_million=8.40,
            effective_from=date(2026, 9, 1),
            source_url="https://contoso.example/manual",
            note="Manual override",
        ),
    )

    # Add two ambiguous deployments of gpt-4o-mini on the same endpoint to test ambiguity detection
    await endpoint_repo.replace_observed_for_endpoint(
        TENANT,
        "endpoint-aoai",
        [
            ObservedModelDeployment(
                id="obs-chat",
                tenant_id=TENANT,
                endpoint_id="endpoint-aoai",
                snapshot_id="snap-2",
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
                endpoint_id="endpoint-aoai",
                snapshot_id="snap-2",
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
                endpoint_id="endpoint-aoai",
                snapshot_id="snap-2",
                deployment_name="mini-b",
                model_name="gpt-4o-mini",
                model_version="2024-07-18",
                model_format="OpenAI",
                sku_name="GlobalStandard",
                sku_capacity=50,
            ),
            ObservedModelDeployment(
                id="obs-ptu",
                tenant_id=TENANT,
                endpoint_id="endpoint-aoai",
                snapshot_id="snap-2",
                deployment_name="ptu-main",
                model_name="gpt-4o",
                model_version="2024-11-20",
                model_format="OpenAI",
                sku_name="GlobalProvisionedManaged",
                sku_capacity=50,
            ),
        ],
        "snap-2",
    )

    res_id = (
        "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-ai/"
        "providers/Microsoft.CognitiveServices/accounts/contoso-aoai"
    )
    # 1) Test unamortized $0 ActualCost reservation row is blocked!
    unamortized_csv = (
        "Date,MeterCategory,MeterName,Product,Quantity,UnitOfMeasure,EffectivePrice,"
        "CostInBillingCurrency,BillingCurrencyCode,ChargeType,PricingModel,BenefitId,"
        "ResourceId,ResourceLocation\n"
        "2026-09-10,Foundry Models,Provisioned Managed Global Unit,Azure OpenAI,1200,1 Hour,0.00,"
        "0.00,USD,Usage,Reservation,/providers/Microsoft.Capacity/reservationOrders/ro-1,"
        f"{res_id},eastus2\n"
    )
    blocked_run = await service.import_billing_data(
        actor,
        provider="azure",
        pricing_cloud="commercial",
        source_month="2026-09",
        file_name="actual-cost.csv.gz",
        content=gzip.compress(unamortized_csv.encode("utf-8")),
    )
    assert blocked_run.blocker_count >= 1
    assert any(issue.code == "unamortizedPrepaidUsage" for issue in blocked_run.issues)
    assert len(blocked_run.candidates) == 1
    assert blocked_run.candidates[0].publishable is False
    assert blocked_run.candidates[0].ptu_hourly == pytest.approx(0.0)

    # 2) Test AmortizedCost export with amortized PTU reservation + token rows + tax exclusion
    amortized_csv = (
        "Date,MeterCategory,MeterName,Product,Quantity,UnitOfMeasure,EffectivePrice,"
        "CostInBillingCurrency,BillingCurrencyCode,ChargeType,PricingModel,BenefitId,"
        "ResourceId,ResourceLocation\n"
        "2026-09-10,Foundry Models,Provisioned Managed Global Unit,Azure OpenAI,1200,1 Hour,0.75,"
        "900.00,USD,Usage,Reservation,/providers/Microsoft.Capacity/reservationOrders/ro-1,"
        f"{res_id},eastus2\n"
        "2026-09-10,Foundry Models,gpt 4o 1120 Inp glbl Tokens,Azure OpenAI,1000,1K Tokens,0.002,"
        f"2.00,USD,Usage,OnDemand,,{res_id},eastus2\n"
        "2026-09-10,Foundry Models,gpt 4o 1120 Outp glbl Tokens,Azure OpenAI,500,1K Tokens,0.008,"
        f"4.00,USD,Usage,OnDemand,,{res_id},eastus2\n"
        "2026-09-10,Foundry Models,gpt 4o mini 0718 Inp glbl Tokens,Azure OpenAI,2000,1K Tokens,"
        f"0.00012,0.24,USD,Usage,OnDemand,,{res_id},eastus2\n"
        "2026-09-10,Foundry Models,gpt 4o mini 0718 Outp glbl Tokens,Azure OpenAI,1000,1K Tokens,"
        f"0.00048,0.48,USD,Usage,OnDemand,,{res_id},eastus2\n"
        "2026-09-10,Foundry Models,Sales Tax,Azure OpenAI,1,1 Unit,45.00,"
        f"45.00,USD,Tax,OnDemand,,{res_id},eastus2\n"
    )
    amortized_run = await service.import_billing_data(
        actor,
        provider="azure",
        pricing_cloud="commercial",
        source_month="2026-09",
        file_name="amortized-cost.csv.gz",
        content=gzip.compress(amortized_csv.encode("utf-8")),
    )
    assert amortized_run.blocker_count == 0
    assert amortized_run.excluded_row_count == 1
    assert amortized_run.total_excluded_cost_usd == pytest.approx(45.0)

    by_model = {(c.model, c.deployment_type): c for c in amortized_run.candidates}
    ptu_cand = by_model[("*", "GlobalProvisionedManaged")]
    assert ptu_cand.publishable is True
    assert ptu_cand.ptu_hours == pytest.approx(1200.0)
    assert ptu_cand.ptu_hourly == pytest.approx(0.75)
    assert ptu_cand.mapping_confidence == "exact"
    assert ptu_cand.suggested_deployment_key == "endpoint-aoai/ptu-main"

    gpt4o_cand = by_model[("gpt-4o", "GlobalStandard")]
    assert gpt4o_cand.conflicts_with_manual_override is True
    assert gpt4o_cand.existing_origin == "admin"

    mini_cand = by_model[("gpt-4o-mini", "GlobalStandard")]
    assert mini_cand.mapping_confidence == "ambiguous"
    assert mini_cand.ambiguous_deployment_keys == [
        "endpoint-aoai/mini-a",
        "endpoint-aoai/mini-b",
    ]


# -- 3. AWS CUR legacy & CUR 2.0 CSV and Parquet parsing ----------------------------------------


def test_aws_cur_csv_and_parquet_bedrock_dimensions() -> None:
    claude_arn = "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-5-sonnet"
    rows: list[dict[str, Any]] = [
        # Standard in-region input & output with NetUnblendedCost and cache read/write
        {
            "line_item_usage_account_id": "123456789012",
            "line_item_product_code": "AmazonBedrock",
            "line_item_usage_type": "USE1-Claude3-5Sonnet-input-tokens",
            "line_item_operation": "InvokeModel",
            "line_item_resource_id": claude_arn,
            "line_item_usage_start_date": "2026-09-05T00:00:00Z",
            "line_item_usage_amount": 2000.0,
            "pricing_unit": "1K tokens",
            "line_item_line_item_type": "Usage",
            "line_item_currency_code": "USD",
            "line_item_unblended_cost": 6.00,
            "line_item_net_unblended_cost": 4.80,
            "product_region_code": "us-east-1",
            "product_model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        },
        {
            "line_item_usage_account_id": "123456789012",
            "line_item_product_code": "AmazonBedrock",
            "line_item_usage_type": "USE1-Claude3-5Sonnet-output-tokens",
            "line_item_operation": "InvokeModel",
            "line_item_resource_id": claude_arn,
            "line_item_usage_start_date": "2026-09-05T00:00:00Z",
            "line_item_usage_amount": 500.0,
            "pricing_unit": "1K tokens",
            "line_item_line_item_type": "Usage",
            "line_item_currency_code": "USD",
            "line_item_unblended_cost": 7.50,
            "line_item_net_unblended_cost": 6.00,
            "product_region_code": "us-east-1",
            "product_model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        },
        {
            "line_item_usage_account_id": "123456789012",
            "line_item_product_code": "AmazonBedrock",
            "line_item_usage_type": "USE1-Claude3-5Sonnet-cache-read-input-token-count",
            "line_item_operation": "InvokeModel",
            "line_item_resource_id": claude_arn,
            "line_item_usage_start_date": "2026-09-05T00:00:00Z",
            "line_item_usage_amount": 1000.0,
            "pricing_unit": "1K tokens",
            "line_item_line_item_type": "Usage",
            "line_item_currency_code": "USD",
            "line_item_unblended_cost": 0.30,
            "line_item_net_unblended_cost": 0.24,
            "product_region_code": "us-east-1",
            "product_model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        },
        {
            "line_item_usage_account_id": "123456789012",
            "line_item_product_code": "AmazonBedrock",
            "line_item_usage_type": "USE1-Claude3-5Sonnet-cache-write-input-token-count",
            "line_item_operation": "InvokeModel",
            "line_item_resource_id": claude_arn,
            "line_item_usage_start_date": "2026-09-05T00:00:00Z",
            "line_item_usage_amount": 500.0,
            "pricing_unit": "1K tokens",
            "line_item_line_item_type": "Usage",
            "line_item_currency_code": "USD",
            "line_item_unblended_cost": 1.875,
            "line_item_net_unblended_cost": 1.50,
            "product_region_code": "us-east-1",
            "product_model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        },
        # Cross-region global routing + SavingsPlanCoveredUsage
        {
            "line_item_usage_account_id": "123456789012",
            "line_item_product_code": "AmazonBedrock",
            "line_item_usage_type": "us-west-2-claude-3-5-sonnet-input-tokens-cross-region-global",
            "line_item_operation": "InvokeModel",
            "line_item_resource_id": "arn:aws:bedrock:us-west-2::inference-profile/global.claude",
            "line_item_usage_start_date": "2026-09-06T00:00:00Z",
            "line_item_usage_amount": 1000.0,
            "pricing_unit": "1K tokens",
            "line_item_line_item_type": "SavingsPlanCoveredUsage",
            "line_item_currency_code": "USD",
            "line_item_unblended_cost": 3.00,
            "savings_plan_savings_plan_effective_cost": 2.20,
            "savings_plan_net_savings_plan_effective_cost": 2.10,
            "product_region_code": "us-west-2",
            "product_model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        },
        {
            "line_item_usage_account_id": "123456789012",
            "line_item_product_code": "AmazonBedrock",
            "line_item_usage_type": "us-west-2-claude-3-5-sonnet-output-tokens-cross-region-global",
            "line_item_operation": "InvokeModel",
            "line_item_resource_id": "arn:aws:bedrock:us-west-2::inference-profile/global.claude",
            "line_item_usage_start_date": "2026-09-06T00:00:00Z",
            "line_item_usage_amount": 200.0,
            "pricing_unit": "1K tokens",
            "line_item_line_item_type": "SavingsPlanCoveredUsage",
            "line_item_currency_code": "USD",
            "line_item_unblended_cost": 3.00,
            "savings_plan_savings_plan_effective_cost": 2.20,
            "savings_plan_net_savings_plan_effective_cost": 2.10,
            "product_region_code": "us-west-2",
            "product_model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        },
        # Priority tier + Mantle pattern
        {
            "line_item_usage_account_id": "123456789012",
            "line_item_product_code": "AmazonBedrock",
            "line_item_usage_type": "us-east-1-deepseek-r1-mantle-input-tokens-standard",
            "line_item_operation": "InvokeModel",
            "line_item_resource_id": "arn:aws:bedrock:us-east-1::foundation-model/deepseek.r1",
            "line_item_usage_start_date": "2026-09-07T00:00:00Z",
            "line_item_usage_amount": 1000.0,
            "pricing_unit": "1K tokens",
            "line_item_line_item_type": "Usage",
            "line_item_currency_code": "USD",
            "line_item_unblended_cost": 1.35,
            "product_region_code": "us-east-1",
        },
        {
            "line_item_usage_account_id": "123456789012",
            "line_item_product_code": "AmazonBedrock",
            "line_item_usage_type": "us-east-1-deepseek-r1-mantle-output-tokens-standard",
            "line_item_operation": "InvokeModel",
            "line_item_resource_id": "arn:aws:bedrock:us-east-1::foundation-model/deepseek.r1",
            "line_item_usage_start_date": "2026-09-07T00:00:00Z",
            "line_item_usage_amount": 500.0,
            "pricing_unit": "1K tokens",
            "line_item_line_item_type": "Usage",
            "line_item_currency_code": "USD",
            "line_item_unblended_cost": 2.70,
            "product_region_code": "us-east-1",
        },
        # Unsupported Bedrock Provisioned Throughput (ModelUnit-Hour)
        {
            "line_item_usage_account_id": "123456789012",
            "line_item_product_code": "AmazonBedrock",
            "line_item_usage_type": "USE1-ProvisionedThroughput-ModelUnit-Hour",
            "line_item_operation": "CreateProvisionedModelThroughput",
            "line_item_resource_id": "arn:aws:bedrock:us-east-1:123456789012:provisioned-model/abc",
            "line_item_usage_start_date": "2026-09-07T00:00:00Z",
            "line_item_usage_amount": 24.0,
            "pricing_unit": "ModelUnit-Hour",
            "line_item_line_item_type": "Usage",
            "line_item_currency_code": "USD",
            "line_item_unblended_cost": 500.0,
            "product_region_code": "us-east-1",
            "product_model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        },
    ]

    # Write to Parquet with dictionary encoding + GZIP compression and verify round-trip
    parquet_bytes = write_parquet_table(rows, compression="GZIP", use_dictionary=True)
    decoded_rows = read_parquet_table(parquet_bytes)
    assert len(decoded_rows) == len(rows)

    result = ingest_billing_bytes(
        parquet_bytes,
        file_name="cur2-export.parquet",
        provider="aws",
        pricing_cloud="aws-bedrock",
        source_month="2026-09",
    )

    assert result.raw_row_count == 9
    assert result.eligible_row_count == 9
    # Preserve AWS region hyphens (us-east-1, us-west-2)
    regions = {c.region for c in result.candidates}
    assert "us-east-1" in regions
    assert "us-west-2" in regions

    # Find standard in-region Claude candidate
    in_region_claude = next(
        c
        for c in result.candidates
        if c.model == "anthropic.claude-3-5-sonnet-20241022-v2:0"
        and c.region == "us-east-1"
        and c.routing_mode == "in-region"
        and c.publishable is True
    )
    assert in_region_claude.input_per_million == pytest.approx(2.40)  # 4.80 / 2.0M
    assert in_region_claude.output_per_million == pytest.approx(12.00)  # 6.00 / 0.5M
    assert in_region_claude.cached_input_per_million == pytest.approx(0.24)  # 0.24 / 1.0M
    assert in_region_claude.cache_write_tokens == pytest.approx(500_000.0)
    assert in_region_claude.cache_write_cost_usd == pytest.approx(1.50)
    assert any("cached prompt tokens" in w for w in in_region_claude.warnings)

    # Cross-region-global Claude candidate uses NetSavingsPlanEffectiveCost (2.10)
    cr_claude = next(
        c
        for c in result.candidates
        if c.region == "us-west-2" and c.routing_mode == "cross-region-global"
    )
    assert cr_claude.input_per_million == pytest.approx(2.10)  # 2.10 / 1.0M
    assert cr_claude.output_per_million == pytest.approx(10.50)  # 2.10 / 0.2M
    assert cr_claude.publishable is True

    # DeepSeek-R1 mantle candidate stripped "-mantle" cleanly
    deepseek = next(c for c in result.candidates if c.model == "deepseek-r1")
    assert deepseek.publisher == "DeepSeek"
    assert deepseek.input_per_million == pytest.approx(1.35)
    assert deepseek.output_per_million == pytest.approx(5.40)

    # Provisioned Throughput ModelUnit-Hour candidate is flagged unpublishable
    prov = next(c for c in result.candidates if not c.publishable)
    assert prov.service_tier == "ProvisionedThroughput"
    assert any(i.code == "unsupportedCapacityUnit" for i in result.issues)


def _aws_usage_rows() -> list[dict[str, Any]]:
    return [
        {
            "line_item_usage_account_id": "123456789012",
            "line_item_product_code": "AmazonBedrock",
            "line_item_usage_type": f"USE1-Claude3-5Sonnet-{category}-tokens",
            "line_item_usage_amount": quantity,
            "pricing_unit": "1K tokens",
            "line_item_currency_code": "USD",
            "line_item_usage_start_date": "2026-09-05T00:00:00Z",
        }
        for category, quantity in [("input", 1000), ("output", 100)]
    ]


def _aws_csv(rows: list[dict[str, Any]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(dict.fromkeys(k for row in rows for k in row)))
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode()


@pytest.mark.parametrize("missing", [None, "", " "])
@pytest.mark.parametrize("parquet", [False, True], ids=["csv", "parquet"])
def test_aws_missing_cash_cost_is_not_a_zero_rate(missing: str | None, parquet: bool) -> None:
    rows = _aws_usage_rows()
    for row in rows:
        row.update(line_item_unblended_cost=missing, line_item_net_unblended_cost=missing)
    with pytest.raises(ValidationError, match=r"required numeric value.*line_item_unblended_cost"):
        ingest_billing_bytes(
            write_parquet_table(rows) if parquet else _aws_csv(rows),
            file_name="missing-cost.parquet" if parquet else "missing-cost.csv",
            provider="aws",
            pricing_cloud="aws-bedrock",
            source_month="2026-09",
        )


@pytest.mark.parametrize(
    ("unblended", "net", "expected_cost"),
    [
        (None, "1.80", 1.8),
        (" ", "1.80", 1.8),
        ("3", "", 3.0),
        ("0", None, 0.0),
        (None, "0", 0.0),
        ("3", "0", 0.0),
    ],
)
def test_aws_net_only_cost_and_explicit_zero(
    unblended: str | None, net: str | None, expected_cost: float,
) -> None:
    rows = _aws_usage_rows()
    for row in rows:
        row["line_item_net_unblended_cost"] = net
        if unblended is not None:
            row["line_item_unblended_cost"] = unblended
    result = ingest_billing_bytes(
        _aws_csv(rows),
        file_name="cash-cost.csv",
        provider="aws",
        pricing_cloud="aws-bedrock",
        source_month="2026-09",
    )
    candidate = result.candidates[0]
    assert candidate.publishable is True
    assert candidate.input_per_million == expected_cost
    assert candidate.output_per_million == pytest.approx(expected_cost * 10)
    assert result.total_eligible_cost_usd == pytest.approx(expected_cost * 2)
    assert all(e.eligible_cost_usd == expected_cost for e in result.evidence_items)


@pytest.mark.parametrize(
    ("line_item_type", "effective_field"),
    [
        ("DiscountedUsage", "reservation_effective_cost"),
        ("SavingsPlanCoveredUsage", "savings_plan_savings_plan_effective_cost"),
        ("SavingsPlanCoveredUsage", "savings_plan_net_savings_plan_effective_cost"),
    ],
)
@pytest.mark.parametrize("effective_cost", [None, "", "0", "2"])
def test_aws_prepaid_usage_requires_nonzero_amortized_cost(
    line_item_type: str, effective_field: str, effective_cost: str | None,
) -> None:
    rows = _aws_usage_rows()
    for row in rows:
        row.update(line_item_line_item_type=line_item_type)
        row[effective_field] = effective_cost
    result = ingest_billing_bytes(
        _aws_csv(rows),
        file_name="prepaid.csv",
        provider="aws",
        pricing_cloud="aws-bedrock",
        source_month="2026-09",
    )
    candidate = result.candidates[0]
    if effective_cost == "2":
        assert candidate.publishable is True
        assert candidate.input_per_million == 2.0
        assert candidate.output_per_million == 20.0
        assert all(e.amortized_benefit_cost_usd == 2.0 for e in result.evidence_items)
        assert not any(i.code == "unamortizedPrepaidUsage" for i in result.issues)
    else:
        assert candidate.publishable is False
        assert all(m.scope == "excluded" for m in result.mappings)
        assert any(
            i.code == "unamortizedPrepaidUsage" and i.severity == "blocker"
            for i in result.issues
        )


@pytest.mark.parametrize("compression_level", [0, 9])
def test_gzip_members_preserve_full_weighted_evidence_and_share_size_limit(
    compression_level: int,
) -> None:
    rows = _aws_usage_rows() + _aws_usage_rows()
    rows[0]["identity_line_item_id"] = "x" * 70_000
    for row, cost, quantity in zip(
        rows, ["3", "1.5", "4", "2"], [1000, 100, 2000, 200], strict=True,
    ):
        row.update(line_item_unblended_cost=cost, line_item_usage_amount=quantity)
    content = _aws_csv(rows)
    lines = content.splitlines(keepends=True)
    members = gzip.compress(
        b"".join(lines[:3]), compresslevel=compression_level,
    ) + gzip.compress(b"".join(lines[3:]), compresslevel=compression_level)
    full = ingest_billing_bytes(
        content,
        file_name="full.csv",
        provider="aws",
        pricing_cloud="aws-bedrock",
        source_month="2026-09",
    )
    combined = ingest_billing_bytes(
        members,
        file_name="members.csv.gz",
        provider="aws",
        pricing_cloud="aws-bedrock",
        source_month="2026-09",
        max_decompressed_bytes=len(content),
    )
    assert combined.raw_row_count == full.raw_row_count == 4
    assert combined.evidence_items == full.evidence_items
    assert combined.candidates == full.candidates
    assert combined.total_eligible_cost_usd == full.total_eligible_cost_usd == 10.5
    assert combined.candidates[0].input_per_million == pytest.approx(2.333333)
    assert combined.candidates[0].output_per_million == pytest.approx(11.666667)

    with pytest.raises(BillingPayloadTooLargeError, match="Decompressed billing file"):
        ingest_billing_bytes(
            members,
            file_name="members.csv.gz",
            provider="aws",
            pricing_cloud="aws-bedrock",
            source_month="2026-09",
            max_decompressed_bytes=len(content) - 1,
        )


@pytest.mark.parametrize("truncated_bytes", [1, 4, 8])
def test_gzip_truncated_trailer_is_rejected(truncated_bytes: int) -> None:
    rows = _aws_usage_rows()
    for row in rows:
        row["line_item_unblended_cost"] = "2"
    content = gzip.compress(_aws_csv(rows))
    with pytest.raises(ValidationError, match="valid GZIP"):
        ingest_billing_bytes(
            content[:-truncated_bytes],
            file_name="truncated.csv.gz",
            provider="aws",
            pricing_cloud="aws-bedrock",
            source_month="2026-09",
        )


@pytest.mark.parametrize("trailing", [b"garbage", b"\x1f", gzip.compress(b"")[:-1]])
def test_gzip_trailing_garbage_or_incomplete_member_is_rejected(trailing: bytes) -> None:
    rows = _aws_usage_rows()
    for row in rows:
        row["line_item_unblended_cost"] = "2"
    with pytest.raises(ValidationError, match="valid GZIP"):
        ingest_billing_bytes(
            gzip.compress(_aws_csv(rows)) + trailing,
            file_name="trailing.csv.gz",
            provider="aws",
            pricing_cloud="aws-bedrock",
            source_month="2026-09",
        )


@pytest.mark.parametrize(
    "fixture_base64",
    [
        # PyArrow 26.0.0: float32 cost=[10, 20], nullable=False/True, uncompressed,
        # PLAIN encoding, DATA_PAGE V1, no statistics or stored Arrow schema.
        "UEFSMRUAFRAVECwVBBUAFQYVBhwAAAAAACBBAACgQRUEGSw1ABgGc2NoZW1hFQIAFQglABgEY29zdAAW"
        "BBkcGRwmABwVCBklBgAZGARjb3N0FQAWBBY2FjYmCEkcFQAVABUCAAAAFjYWBCYIFjYAKCBwYXJxdWV0"
        "LWNwcC1hcnJvdyB2ZXJzaW9uIDI2LjAuMBkcHAAAAHgAAABQQVIx",
        "UEFSMRUAFRwVHCwVBBUAFQYVBhwAAAACAAAABAEAACBBAACgQRUEGSw1ABgGc2NoZW1hFQIAFQglAhgE"
        "Y29zdAAWBBkcGRwmABwVCBklBgAZGARjb3N0FQAWBBZCFkImCEkcFQAVABUCADwpBhkmAAQAAAAWQhYE"
        "JggWQgAoIHBhcnF1ZXQtY3BwLWFycm93IHZlcnNpb24gMjYuMC4wGRwcAAAAgAAAAFBBUjE=",
    ],
    ids=["required", "optional"],
)
def test_independent_parquet_float_schema(fixture_base64: str) -> None:
    assert read_parquet_table(base64.b64decode(fixture_base64)) == [
        {"cost": 10.0},
        {"cost": 20.0},
    ]


# -- 4. GCP BigQuery CSV & NDJSON parsing, credits, and invoice.month lag -----------------------


def test_gcp_bigquery_ndjson_and_csv_credits_and_invoice_month() -> None:
    ndjson_lines = [
        # Gemini 1.5 Pro Input: 2M tokens, negotiated cost $3.00, CUD credit -$0.60,
        # plus one-off PROMOTION credit -$0.40 (excluded so effective cost is $2.40 -> $1.20/M)
        json.dumps(
            {
                "billing_account_id": "012345-6789AB-CDEF01",
                "project": {"id": "contoso-vertex"},
                "service": {"description": "Vertex AI"},
                "sku": {"id": "SKU-1", "description": "Gemini 1.5 Pro Input Tokens"},
                "usage_start_time": "2026-09-20T10:00:00Z",
                "usage": {
                    "amount": 2000000,
                    "unit": "tokens",
                    "amount_in_pricing_units": 2000000,
                    "pricing_unit": "1 token",
                },
                "cost": 3.00,
                "cost_type": "regular",
                "currency": "USD",
                "currency_conversion_rate": 1.0,
                "invoice": {"month": "202609"},
                "location": {"region": "us-central1"},
                "price": {"effective_price": 0.0000015},
                "credits": [
                    {
                        "id": "cud-1",
                        "name": "Committed Usage Discount",
                        "type": "COMMITTED_USAGE_DISCOUNT",
                        "amount": -0.60,
                    },
                    {
                        "id": "promo-1",
                        "name": "Trial Promotion",
                        "type": "PROMOTION",
                        "amount": -0.40,
                    },
                ],
            }
        ),
        # Gemini 1.5 Pro Output on a late adjustment in invoice.month 202610 for usage in 2026-09
        json.dumps(
            {
                "billing_account_id": "012345-6789AB-CDEF01",
                "project": {"id": "contoso-vertex"},
                "service": {"description": "Vertex AI"},
                "sku": {"id": "SKU-2", "description": "Gemini 1.5 Pro Output Tokens"},
                "usage_start_time": "2026-09-30T23:00:00Z",
                "usage": {
                    "amount_in_pricing_units": 500,
                    "pricing_unit": "1K tokens",
                },
                "cost": 2.50,
                "cost_type": "regular",
                "currency": "USD",
                "invoice": {"month": "202610"},
                "location": {"region": "us-central1"},
                "credits": [
                    {
                        "type": "SUBSCRIPTION_BENEFIT",
                        "amount": -0.50,
                    }
                ],
            }
        ),
        # Input-only embedding model in GCP (text-embedding-004) -> publishable without output
        json.dumps(
            {
                "billing_account_id": "012345-6789AB-CDEF01",
                "project": {"id": "contoso-vertex"},
                "service": {"description": "Vertex AI"},
                "sku": {"id": "SKU-3", "description": "Vertex AI Text Embedding 004 Input Tokens"},
                "usage_start_time": "2026-09-15T00:00:00Z",
                "usage": {
                    "amount_in_pricing_units": 4000000,
                    "pricing_unit": "1 token",
                },
                "cost": 0.10,
                "cost_type": "regular",
                "currency": "USD",
                "invoice": {"month": "202609"},
                "location": {"region": "us-central1"},
                "credits": [],
            }
        ),
        # Row outside source_month (2026-08) -> excluded
        json.dumps(
            {
                "billing_account_id": "012345-6789AB-CDEF01",
                "project": {"id": "contoso-vertex"},
                "service": {"description": "Vertex AI"},
                "sku": {"id": "SKU-1", "description": "Gemini 1.5 Pro Input Tokens"},
                "usage_start_time": "2026-08-31T23:00:00Z",
                "usage": {
                    "amount_in_pricing_units": 1000000,
                    "pricing_unit": "1 token",
                },
                "cost": 1.50,
                "cost_type": "regular",
                "currency": "USD",
                "invoice": {"month": "202609"},
                "location": {"region": "us-central1"},
            }
        ),
    ]

    result = ingest_billing_bytes(
        "\n".join(ndjson_lines).encode("utf-8"),
        file_name="gcp-export.ndjson",
        provider="gcp",
        pricing_cloud="gcp-vertex",
        source_month="2026-09",
    )

    assert result.invoice_months == ["2026-09", "2026-10"]
    assert result.eligible_row_count == 3
    assert result.excluded_row_count == 1
    assert any(i.code == "late_adjustments_note" and i.severity == "warning" for i in result.issues)
    assert any(i.code == "oneOffCreditsExcluded" for i in result.issues)
    assert any(i.code == "outsideSourceMonth" for i in result.issues)

    gemini = next(c for c in result.candidates if c.model == "gemini-1.5-pro")
    assert gemini.region == "us-central1"  # Hyphen preserved!
    # Input cost = 3.00 - 0.60 (CUD) = 2.40 over 2M tokens -> $1.20/M (promo -0.40 excluded!)
    assert gemini.input_cost_usd == pytest.approx(2.40)
    assert gemini.input_per_million == pytest.approx(1.20)
    # Output cost = 2.50 - 0.50 (SUBSCRIPTION_BENEFIT) = 2.00 over 0.5M tokens -> $4.00/M
    assert gemini.output_cost_usd == pytest.approx(2.00)
    assert gemini.output_per_million == pytest.approx(4.00)
    assert gemini.excluded_cost_usd == pytest.approx(0.40)
    assert gemini.publishable is True

    embed = next(c for c in result.candidates if c.model == "text-embedding-004")
    assert embed.input_per_million == pytest.approx(0.025)
    assert embed.output_per_million is None
    assert embed.publishable is True


# -- 5. Partial candidate (missing output tokens on generative model) & MOSAIC template --------


def test_partial_generative_candidate_kept_unpublishable_and_mosaic_template_roundtrip() -> None:
    # Generative model with input tokens only -> publishable=False with explicit reason
    template_csv = (
        "schema_version,provider,pricing_cloud,source_month,source_account_id,"
        "source_resource_id,source_resource_name,publisher,model,model_version,"
        "deployment_type,region,service_tier,routing_mode,usage_category,"
        "usage_quantity,usage_unit,currency,eligible_cost_usd,"
        "amortized_benefit_cost_usd,excluded_cost_usd,excluded_reason,invoice_month\n"
        "mosaic_billing_v1,azure,commercial,2026-09,sub-1,res-1,aoai-1,"
        "OpenAI,gpt-4o,2024-11-20,GlobalStandard,eastus2,Standard,in-region,"
        "inputTokens,1000,1K Tokens,USD,2.00,0.00,0.00,,2026-09\n"
        "mosaic_billing_v1,azure,commercial,2026-09,sub-1,res-1,aoai-1,"
        "OpenAI,text-embedding-3-large,1,Standard,eastus2,Standard,in-region,"
        "inputTokens,1000,1K Tokens,USD,0.12,0.00,0.00,,2026-09\n"
    )
    result = ingest_billing_bytes(
        template_csv.encode("utf-8"),
        file_name="template.csv",
        provider="mosaicTemplate",
        pricing_cloud="commercial",
        source_month="2026-09",
    )
    by_model = {c.model: c for c in result.candidates}

    gpt4o = by_model["gpt-4o"]
    assert gpt4o.input_per_million == pytest.approx(2.00)
    assert gpt4o.output_per_million is None
    assert gpt4o.publishable is False
    assert gpt4o.unpublishable_reason is not None
    assert "No output token usage was observed" in gpt4o.unpublishable_reason

    embedding = by_model["text-embedding-3-large"]
    assert embedding.input_per_million == pytest.approx(0.12)
    assert embedding.output_per_million is None
    assert embedding.publishable is True


# -- 6. Strict USD-only enforcement, binary rejection, and validation bounds -------------------


def test_strict_usd_only_and_invalid_inputs_rejected() -> None:
    # Non-USD rejected even when costInUsd is present
    non_usd_azure = (
        "Date,MeterCategory,MeterName,Product,Quantity,UnitOfMeasure,"
        "CostInBillingCurrency,CostInUsd,BillingCurrencyCode\n"
        "2026-09-10,Foundry Models,gpt 4o 1120 Inp glbl Tokens,Azure OpenAI,100,1K Tokens,"
        "1.80,2.00,EUR\n"
    )
    with pytest.raises(ValidationError, match="USD-denominated") as exc_info:
        ingest_billing_bytes(
            non_usd_azure.encode("utf-8"),
            file_name="eur.csv",
            provider="azure",
            pricing_cloud="commercial",
            source_month="2026-09",
        )
    assert exc_info.value.details.get("code") == "nonUsdCurrency"

    # Mixed USD + GBP rejected even with currency_conversion_rate
    mixed_gcp = "\n".join(
        [
            json.dumps(
                {
                    "service": {"description": "Vertex AI"},
                    "sku": {"description": "Gemini 1.5 Pro Input Tokens"},
                    "usage_start_time": "2026-09-10T00:00:00Z",
                    "usage": {"amount_in_pricing_units": 1000, "pricing_unit": "1 token"},
                    "cost": 1.0,
                    "currency": "USD",
                    "currency_conversion_rate": 1.0,
                }
            ),
            json.dumps(
                {
                    "service": {"description": "Vertex AI"},
                    "sku": {"description": "Gemini 1.5 Pro Output Tokens"},
                    "usage_start_time": "2026-09-10T00:00:00Z",
                    "usage": {"amount_in_pricing_units": 1000, "pricing_unit": "1 token"},
                    "cost": 2.0,
                    "currency": "GBP",
                    "currency_conversion_rate": 0.78,
                }
            ),
        ]
    )
    with pytest.raises(ValidationError, match="GBP"):
        ingest_billing_bytes(
            mixed_gcp.encode("utf-8"),
            file_name="mixed.ndjson",
            provider="gcp",
            pricing_cloud="gcp-vertex",
            source_month="2026-09",
        )

    # Reject XLSX, XLS, PDF
    with pytest.raises(ValidationError, match="Excel workbooks"):
        ingest_billing_bytes(
            b"PK\x03\x04fake-xlsx-bytes",
            file_name="bill.xlsx",
            provider="azure",
            pricing_cloud="commercial",
            source_month="2026-09",
        )
    with pytest.raises(ValidationError, match="Legacy Excel"):
        ingest_billing_bytes(
            b"\xd0\xcf\x11\xe0fake-xls-bytes",
            file_name="bill.xls",
            provider="azure",
            pricing_cloud="commercial",
            source_month="2026-09",
        )
    with pytest.raises(ValidationError, match="PDF invoices"):
        ingest_billing_bytes(
            b"%PDF-1.7 fake invoice",
            file_name="invoice.pdf",
            provider="azure",
            pricing_cloud="commercial",
            source_month="2026-09",
        )

    # Reject cost-only summary CSV missing Quantity / UnitOfMeasure
    cost_only = "ServiceName,CostInBillingCurrency,BillingCurrencyCode\nAzure OpenAI,150.00,USD\n"
    with pytest.raises(ValidationError, match="cost-only summaries"):
        ingest_billing_bytes(
            cost_only.encode("utf-8"),
            file_name="summary.csv",
            provider="azure",
            pricing_cloud="commercial",
            source_month="2026-09",
        )

    # Reject NaN / Inf and negative quantities
    nan_csv = (
        "Date,MeterCategory,MeterName,Product,Quantity,UnitOfMeasure,"
        "CostInBillingCurrency,BillingCurrencyCode\n"
        "2026-09-10,Foundry Models,gpt 4o 1120 Inp glbl Tokens,Azure OpenAI,NaN,1K Tokens,"
        "1.80,USD\n"
    )
    with pytest.raises(ValidationError, match="non-finite"):
        ingest_billing_bytes(
            nan_csv.encode("utf-8"),
            file_name="nan.csv",
            provider="azure",
            pricing_cloud="commercial",
            source_month="2026-09",
        )

    neg_qty_csv = (
        "Date,MeterCategory,MeterName,Product,Quantity,UnitOfMeasure,"
        "CostInBillingCurrency,BillingCurrencyCode,ChargeType\n"
        "2026-09-10,Foundry Models,gpt 4o 1120 Inp glbl Tokens,Azure OpenAI,-10,1K Tokens,"
        "1.80,USD,Usage\n"
    )
    with pytest.raises(ValidationError, match="negative quantity"):
        ingest_billing_bytes(
            neg_qty_csv.encode("utf-8"),
            file_name="neg.csv",
            provider="azure",
            pricing_cloud="commercial",
            source_month="2026-09",
        )

    # Reject oversized raw upload, gzip decompression bomb, and max row limit
    with pytest.raises(BillingPayloadTooLargeError, match="exceeds the maximum allowed size"):
        ingest_billing_bytes(
            b"x" * 200,
            file_name="big.csv",
            provider="azure",
            pricing_cloud="commercial",
            source_month="2026-09",
            max_upload_bytes=100,
        )

    compressed_bomb = gzip.compress(b"a" * 5000)
    with pytest.raises(BillingPayloadTooLargeError, match="Decompressed billing file"):
        ingest_billing_bytes(
            compressed_bomb,
            file_name="bomb.csv.gz",
            provider="azure",
            pricing_cloud="commercial",
            source_month="2026-09",
            max_decompressed_bytes=1000,
        )

    many_rows_csv = (
        "Date,MeterCategory,MeterName,Product,Quantity,UnitOfMeasure,"
        "CostInBillingCurrency,BillingCurrencyCode\n"
        + (
            "2026-09-10,Foundry Models,gpt 4o 1120 Inp glbl Tokens,Azure OpenAI,1,1K Tokens,"
            "0.002,USD\n"
            * 5
        )
    )
    with pytest.raises(ValidationError, match="maximum allowed row count"):
        ingest_billing_bytes(
            many_rows_csv.encode("utf-8"),
            file_name="many.csv",
            provider="azure",
            pricing_cloud="commercial",
            source_month="2026-09",
            max_rows=3,
        )

    # Unknown unit and zero denominator produce blocker issues and publishable=False
    unknown_unit_and_zero_csv = (
        "Date,MeterCategory,MeterName,Product,Quantity,UnitOfMeasure,"
        "CostInBillingCurrency,BillingCurrencyCode\n"
        "2026-09-10,Foundry Models,gpt 4o 1120 Inp glbl Tokens,Azure OpenAI,100,Requests,"
        "2.00,USD\n"
        "2026-09-10,Foundry Models,Phi-4 Inp glbl Tokens,Azure Phi Models,0,1K Tokens,"
        "2.00,USD\n"
    )
    res_issues = ingest_billing_bytes(
        unknown_unit_and_zero_csv.encode("utf-8"),
        file_name="issues.csv",
        provider="azure",
        pricing_cloud="commercial",
        source_month="2026-09",
    )
    issue_codes = {i.code for i in res_issues.issues}
    assert "unknownUsageUnit" in issue_codes
    assert "zeroQuantityDenominator" in issue_codes
    assert all(not c.publishable for c in res_issues.candidates)


# -- 7. Duplicate fingerprint detection, superseding drafts, and API routes --------------------


def test_billing_routes_require_admin() -> None:
    with TestClient(create_app(_settings(["User"]))) as user_client:
        assert user_client.get("/api/v1/pricing/billing/templates").status_code == 403
        assert (
            user_client.post(
                "/api/v1/pricing/billing/imports",
                json={
                    "provider": "azure",
                    "pricingCloud": "commercial",
                    "sourceMonth": "2026-09",
                    "contentText": "a,b\n1,2\n",
                },
            ).status_code
            == 403
        )
        assert user_client.get("/api/v1/pricing/billing/runs").status_code == 403
        assert user_client.get("/api/v1/pricing/billing/runs/run-1").status_code == 403


def test_duplicate_fingerprint_detection_and_api_routes(
    admin_client: TestClient,
) -> None:
    # 1. GET /api/v1/pricing/billing/templates returns all 4 documented templates
    templates_resp = admin_client.get("/api/v1/pricing/billing/templates")
    assert templates_resp.status_code == 200, templates_resp.text
    templates = templates_resp.json()
    assert [t["provider"] for t in templates] == ["azure", "aws", "gcp", "mosaicTemplate"]
    gcp_tmpl = next(t for t in templates if t["provider"] == "gcp")
    assert "usage_start_time" in gcp_tmpl["extractionQuery"]

    # Every sample_csv in templates round-trips cleanly through POST /api/v1/pricing/billing/imports
    mosaic_tmpl = next(t for t in templates if t["provider"] == "mosaicTemplate")
    sample_csv: str = mosaic_tmpl["sampleCsv"]

    first_resp = admin_client.post(
        "/api/v1/pricing/billing/imports",
        json={
            "provider": "mosaicTemplate",
            "pricingCloud": "commercial",
            "sourceMonth": "2026-09",
            "fileName": "mosaic-sep.csv",
            "contentBase64": base64.b64encode(sample_csv.encode("utf-8")).decode("ascii"),
        },
    )
    assert first_resp.status_code == 201, first_resp.text
    first_run = first_resp.json()
    assert first_run["status"] == "readyForReview"
    assert first_run["duplicateOfRunId"] is None
    assert first_run["publishableCandidateCount"] == 1

    # 2. Re-uploading identical content without force returns duplicateOfRunId pointing to first_run
    dup_resp = admin_client.post(
        "/api/v1/pricing/billing/imports",
        params={
            "provider": "mosaicTemplate",
            "pricingCloud": "commercial",
            "sourceMonth": "2026-09",
            "fileName": "mosaic-sep.csv",
        },
        content=sample_csv.encode("utf-8"),
        headers={"content-type": "text/csv"},
    )
    assert dup_resp.status_code == 201, dup_resp.text
    dup_run = dup_resp.json()
    assert dup_run["id"] == first_run["id"]
    assert dup_run["duplicateOfRunId"] == first_run["id"]

    runs_after_dup = admin_client.get("/api/v1/pricing/billing/runs").json()
    assert len(runs_after_dup) == 1

    # 3. Re-uploading with force=True creates a new draft and marks the old draft superseded
    forced_resp = admin_client.post(
        "/api/v1/pricing/billing/imports",
        params={
            "provider": "mosaicTemplate",
            "pricingCloud": "commercial",
            "sourceMonth": "2026-09",
            "fileName": "mosaic-sep-v2.csv",
            "force": "true",
        },
        content=sample_csv.encode("utf-8"),
        headers={"content-type": "text/csv"},
    )
    assert forced_resp.status_code == 201, forced_resp.text
    forced_run = forced_resp.json()
    assert forced_run["id"] != first_run["id"]
    assert forced_run["duplicateOfRunId"] == first_run["id"]

    first_detail = admin_client.get(f"/api/v1/pricing/billing/runs/{first_run['id']}").json()
    assert first_detail["status"] == "superseded"

    forced_detail = admin_client.get(f"/api/v1/pricing/billing/runs/{forced_run['id']}").json()
    assert forced_detail["status"] == "readyForReview"
    assert len(forced_detail["evidenceItems"]) == 2
