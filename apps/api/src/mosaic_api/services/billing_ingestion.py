"""Shared billing ingestion, normalization, and quantity-weighted Decimal rate derivation."""

from __future__ import annotations

import csv
import io
import json
import re
import zlib
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import cache
from typing import Any

from mosaic_api.billing_pricing import (
    BillingCandidateMapping,
    BillingCandidateRate,
    BillingConnectorPayloadPart,
    BillingEvidenceChunk,
    BillingMappingConfidence,
    BillingMappingScope,
    BillingNormalizedEvidenceItem,
    BillingProvider,
    BillingReviewIssue,
    BillingSourceTemplateView,
    BillingUsageCategory,
    billing_evidence_chunk_id,
    parse_source_month,
)
from mosaic_api.domain import deterministic_id
from mosaic_api.errors import ValidationError
from mosaic_api.integrations.parquet_cur import PARQUET_MAGIC, read_parquet_table
from mosaic_api.pricing import (
    DeploymentFacts,
    PriceBook,
    PriceEntry,
    PriceMatch,
    canonical_deployment_type,
    is_provisioned,
    load_seed,
    normalize_region,
)

DEFAULT_MAX_UPLOAD_BYTES = 15 * 1024 * 1024
DEFAULT_MAX_DECOMPRESSED_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_ROWS = 100_000
EVIDENCE_CHUNK_SIZE = 100
MOSAIC_TEMPLATE_SCHEMA_VERSION = "mosaic_billing_v1"

_MILLION = Decimal("1000000")
_THOUSAND = Decimal("1000")
_ONE = Decimal("1")
_ZERO = Decimal("0")
_RATE_QUANT = Decimal("0.000001")
_COST_QUANT = Decimal("0.000001")
_MONTH_QUANT = Decimal("0.0001")

# Magic byte signatures for unsupported binary formats
_ZIP_XLSX_MAGIC = b"PK\x03\x04"
_OLE_XLS_MAGIC = b"\xd0\xcf\x11\xe0"
_PDF_MAGIC = b"%PDF-"
_GZIP_MAGIC = b"\x1f\x8b"

# AWS short region prefixes used in CUR UsageType strings
_AWS_USAGE_PREFIX_REGIONS: dict[str, str] = {
    "use1": "us-east-1",
    "use2": "us-east-2",
    "usw1": "us-west-1",
    "usw2": "us-west-2",
    "euw1": "eu-west-1",
    "euw2": "eu-west-2",
    "euw3": "eu-west-3",
    "euc1": "eu-central-1",
    "euc2": "eu-central-2",
    "eun1": "eu-north-1",
    "eus1": "eu-south-1",
    "eus2": "eu-south-2",
    "aps1": "ap-south-1",
    "aps2": "ap-south-2",
    "apse1": "ap-southeast-1",
    "apse2": "ap-southeast-2",
    "apse3": "ap-southeast-3",
    "apse4": "ap-southeast-4",
    "apn1": "ap-northeast-1",
    "apne1": "ap-northeast-1",
    "apn2": "ap-northeast-2",
    "apne2": "ap-northeast-2",
    "apn3": "ap-northeast-3",
    "apne3": "ap-northeast-3",
    "ape1": "ap-east-1",
    "cac1": "ca-central-1",
    "caw1": "ca-west-1",
    "sae1": "sa-east-1",
    "mes1": "me-south-1",
    "mec1": "me-central-1",
    "afs1": "af-south-1",
    "ilc1": "il-central-1",
    "ugw1": "us-gov-west-1",
    "uge1": "us-gov-east-1",
}

_AWS_FULL_REGION_PREFIX_RE = re.compile(
    r"^(?P<region>(?:us-(?:gov-)?(?:east|west)-\d+|ca-(?:central|west)-\d+|"
    r"eu-(?:west|central|north|south)-\d+|ap-(?:south|southeast|northeast|east)-\d+|"
    r"sa-east-\d+|me-(?:south|central)-\d+|af-south-\d+|il-central-\d+))-(?P<rest>.+)$",
    re.IGNORECASE,
)
_AWS_SHORT_REGION_PREFIX_RE = re.compile(
    r"^(?P<prefix>[A-Za-z]{2,4}\d)-(?P<rest>.+)$"
)

# Token unit normalization factors: raw_quantity * factor_to_tokens = total tokens
_TOKEN_UNIT_FACTORS: dict[str, Decimal] = {
    "1 token": _ONE,
    "token": _ONE,
    "tokens": _ONE,
    "input token": _ONE,
    "input tokens": _ONE,
    "output token": _ONE,
    "output tokens": _ONE,
    "1k tokens": _THOUSAND,
    "1k token": _THOUSAND,
    "1,000 tokens": _THOUSAND,
    "1000 tokens": _THOUSAND,
    "1000 token": _THOUSAND,
    "thousand tokens": _THOUSAND,
    "1m tokens": _MILLION,
    "1m token": _MILLION,
    "1,000,000 tokens": _MILLION,
    "1000000 tokens": _MILLION,
    "1000000 token": _MILLION,
    "million tokens": _MILLION,
    "1000k tokens": _MILLION,
}

_PTU_HOUR_UNIT_FACTORS: dict[str, Decimal] = {
    "1 hour": _ONE,
    "hour": _ONE,
    "hours": _ONE,
    "hr": _ONE,
    "hrs": _ONE,
    "ptu-hour": _ONE,
    "ptu-hours": _ONE,
    "ptu hour": _ONE,
    "ptu hours": _ONE,
    "ptuHours": _ONE,
    "ptuhours": _ONE,
    "1 ptu hour": _ONE,
    "1 ptu-hour": _ONE,
    "100 hours": Decimal("100"),
}

_MONTHLY_COMMITMENT_UNITS: frozenset[str] = frozenset(
    {"1 month", "month", "months", "commitment month", "1 commitment month"}
)

_UNSUPPORTED_CAPACITY_UNITS: frozenset[str] = frozenset(
    {
        "modelunit-hour",
        "modelunit-hours",
        "modelunit hour",
        "modelunit hours",
        "model-unit-hour",
        "model-unit-hours",
        "gsu",
        "gsu-hour",
        "gsu-hours",
        "gsu hour",
        "generative service unit",
        "generative service units",
    }
)

# GCP recurring commitment credit types vs one-off credits
_GCP_RECURRING_CREDIT_TYPES: frozenset[str] = frozenset(
    {
        "COMMITTED_USAGE_DISCOUNT",
        "COMMITTED_USAGE_DISCOUNT_DOLLAR_BASE",
        "SUBSCRIPTION_BENEFIT",
    }
)
_GCP_ONEOFF_CREDIT_TYPES: frozenset[str] = frozenset(
    {
        "PROMOTION",
        "FREE_TIER",
        "DISCOUNT",
        "RESELLER_MARGIN",
        "FEE_UTILIZATION_OFFSET",
        "SUSTAINED_USAGE_DISCOUNT",
    }
)


class BillingPayloadTooLargeError(ValidationError):
    """Raised when a billing upload or decompressed stream exceeds configured byte limits."""

    status_code = 413
    code = "payload_too_large"


@dataclass(frozen=True)
class _SeedMeterMatch:
    publisher: str | None
    model: str
    version: str | None
    deployment_type: str | None
    usage_category: BillingUsageCategory


@dataclass
class _NormalizedRowRecord:
    provider: BillingProvider
    source_scope: str | None = None
    source_account_id: str | None = None
    source_resource_id: str | None = None
    source_resource_name: str | None = None
    service_name: str | None = None
    meter_id: str | None = None
    sku_id: str | None = None
    sku_description: str | None = None
    publisher: str | None = None
    model: str | None = None
    model_version: str | None = None
    region: str | None = None
    deployment_type: str | None = None
    service_tier: str | None = None
    routing_mode: str | None = None
    usage_category: BillingUsageCategory = "inputTokens"
    raw_usage_unit: str | None = None
    raw_quantity: Decimal = _ZERO
    normalized_quantity: Decimal = _ZERO
    normalized_unit: str | None = None
    eligible_cost_usd: Decimal = _ZERO
    amortized_benefit_cost_usd: Decimal = _ZERO
    excluded_cost_usd: Decimal = _ZERO
    excluded_reason: str | None = None
    usage_date: date | None = None
    invoice_month: str | None = None
    currency: str = "USD"
    notes: list[str] = field(default_factory=list)
    unamortized_prepaid_blocker: bool = False
    unknown_unit_blocker: bool = False
    unsupported_capacity_blocker: bool = False


@dataclass
class BillingIngestionResult:
    provider: BillingProvider
    pricing_cloud: str
    source_month: str
    invoice_months: list[str]
    source_scope: str | None
    raw_row_count: int
    eligible_row_count: int
    excluded_row_count: int
    total_read_bytes: int
    total_eligible_cost_usd: float
    total_excluded_cost_usd: float
    evidence_items: list[BillingNormalizedEvidenceItem]
    candidates: list[BillingCandidateRate]
    mappings: list[BillingCandidateMapping]
    issues: list[BillingReviewIssue]


@cache
def _seed_meter_lookup() -> tuple[
    dict[tuple[str, str], _SeedMeterMatch],
    dict[str, _SeedMeterMatch],
    list[tuple[str, str | None, str | None]],
]:
    """Build lookup tables from the packaged Azure Retail Prices seed."""

    seed = load_seed()
    by_product_meter: dict[tuple[str, str], _SeedMeterMatch] = {}
    by_meter: dict[str, _SeedMeterMatch] = {}
    known_models: dict[str, tuple[str, str | None, str | None]] = {}

    for price in seed.prices:
        if price.model != "*":
            known_models.setdefault(
                price.model.casefold(), (price.model, price.publisher, price.version)
            )
            for alias in price.aliases:
                known_models.setdefault(
                    alias.casefold(), (price.model, price.publisher, price.version)
                )
        rm = price.retail_meters
        if rm is None:
            continue
        prod_key = rm.product.strip().casefold()
        for cat, meter_name in (
            ("inputTokens", rm.input),
            ("cachedInputTokens", rm.cached_input),
            ("outputTokens", rm.output),
            ("ptuHours", rm.provisioned),
        ):
            if not meter_name:
                continue
            m_key = meter_name.strip().casefold()
            match = _SeedMeterMatch(
                publisher=price.publisher,
                model=price.model,
                version=price.version,
                deployment_type=canonical_deployment_type(price.deployment_type),
                usage_category=cat,  # type: ignore[arg-type]
            )
            by_product_meter.setdefault((prod_key, m_key), match)
            by_meter.setdefault(m_key, match)

    # Order known models longest-first so e.g. gpt-4o-mini matches before gpt-4o
    sorted_models = sorted(
        known_models.values(), key=lambda item: len(item[0]), reverse=True
    )
    return by_product_meter, by_meter, sorted_models


def billing_source_templates() -> list[BillingSourceTemplateView]:
    """Return schema documentation, sample CSVs, and extraction queries for billing imports."""

    gcp_query = (
        "SELECT\n"
        "  billing_account_id,\n"
        "  project.id AS project_id,\n"
        "  service.description AS service_description,\n"
        "  sku.id AS sku_id,\n"
        "  sku.description AS sku_description,\n"
        "  usage_start_time,\n"
        "  usage_end_time,\n"
        "  usage.amount_in_pricing_units,\n"
        "  usage.pricing_unit,\n"
        "  cost,\n"
        "  cost_type,\n"
        "  currency,\n"
        "  invoice.month AS invoice_month,\n"
        "  location.region AS location_region,\n"
        "  resource.name AS resource_name,\n"
        "  price.effective_price AS price_effective_price,\n"
        "  TO_JSON_STRING(credits) AS credits\n"
        "FROM `PROJECT_ID.DATASET_ID.gcp_billing_export_resource_v1_BILLING_ACCOUNT_ID`\n"
        "WHERE usage_start_time >= @usage_start\n"
        "  AND usage_start_time < @usage_end"
    )
    return [
        BillingSourceTemplateView(
            provider="azure",
            title="Azure Cost Management Cost Details (AmortizedCost)",
            schema_version="1",
            supported_formats=[".csv", ".csv.gz"],
            required_columns=[
                "MeterCategory",
                "MeterName",
                "Product",
                "Quantity",
                "UnitOfMeasure",
                "CostInBillingCurrency",
                "BillingCurrencyCode",
                "Date",
            ],
            optional_columns=[
                "MeterSubCategory",
                "MeterId",
                "PartNumber",
                "EffectivePrice",
                "UnitPrice",
                "PayGPrice",
                "ChargeType",
                "PricingModel",
                "BenefitId",
                "BenefitName",
                "ResourceId",
                "ResourceLocation",
                "BillingPeriodStartDate",
                "AdditionalInfo",
            ],
            sample_csv=(
                "Date,MeterCategory,MeterName,Product,Quantity,UnitOfMeasure,"
                "EffectivePrice,CostInBillingCurrency,BillingCurrencyCode,ChargeType,"
                "PricingModel,ResourceId,ResourceLocation\n"
                "2026-09-15,Foundry Models,gpt 4o 1120 Inp glbl Tokens,Azure OpenAI,"
                "2000,1K Tokens,0.0018,3.60,USD,Usage,OnDemand,"
                "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-ai/"
                "providers/Microsoft.CognitiveServices/accounts/contoso-aoai,eastus2\n"
                "2026-09-15,Foundry Models,gpt 4o 1120 Outp glbl Tokens,Azure OpenAI,"
                "500,1K Tokens,0.0072,3.60,USD,Usage,OnDemand,"
                "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-ai/"
                "providers/Microsoft.CognitiveServices/accounts/contoso-aoai,eastus2\n"
            ),
            notes=[
                "Export the AmortizedCost dataset so reservation and savings plan usage "
                "includes amortized commitment cost.",
                "ActualCost exports that show $0 for prepaid reservation or savings plan "
                "usage are blocked from publishing.",
                "Only USD-denominated billing exports are accepted.",
            ],
        ),
        BillingSourceTemplateView(
            provider="aws",
            title="AWS Cost and Usage Report (CUR / CUR 2.0)",
            schema_version="1",
            supported_formats=[".csv", ".csv.gz", ".parquet"],
            required_columns=[
                "line_item_usage_account_id",
                "line_item_product_code",
                "line_item_usage_type",
                "line_item_usage_start_date",
                "line_item_usage_amount",
                "pricing_unit",
                "line_item_line_item_type",
                "line_item_currency_code",
                "line_item_unblended_cost",
            ],
            optional_columns=[
                "line_item_operation",
                "line_item_resource_id",
                "line_item_net_unblended_cost",
                "reservation_effective_cost",
                "savings_plan_savings_plan_effective_cost",
                "savings_plan_net_savings_plan_effective_cost",
                "product_region_code",
                "product_model",
            ],
            sample_csv=(
                "line_item_usage_account_id,line_item_product_code,line_item_usage_type,"
                "line_item_usage_start_date,line_item_usage_amount,pricing_unit,"
                "line_item_line_item_type,line_item_currency_code,line_item_unblended_cost,"
                "line_item_net_unblended_cost,product_region_code,product_model\n"
                "123456789012,AmazonBedrock,USE1-Claude3-5Sonnet-input-tokens,"
                "2026-09-10T00:00:00Z,1500,1K tokens,Usage,USD,4.50,3.75,us-east-1,"
                "anthropic.claude-3-5-sonnet-20241022-v2:0\n"
                "123456789012,AmazonBedrock,USE1-Claude3-5Sonnet-output-tokens,"
                "2026-09-10T00:00:00Z,300,1K tokens,Usage,USD,4.50,3.75,us-east-1,"
                "anthropic.claude-3-5-sonnet-20241022-v2:0\n"
            ),
            notes=[
                "Supports both legacy CUR slash headers (lineItem/UsageType) and CUR 2.0 "
                "snake_case columns.",
                "Preserves AWS region identifiers (such as us-east-1), service tiers, and "
                "cross-region routing.",
                "Bedrock Provisioned Throughput (ModelUnit-Hour) is recorded in evidence "
                "and flagged as an unsupported capacity unit.",
            ],
        ),
        BillingSourceTemplateView(
            provider="gcp",
            title="Google Cloud Billing BigQuery Export",
            schema_version="1",
            supported_formats=[".csv", ".csv.gz", ".json", ".jsonl", ".ndjson"],
            required_columns=[
                "billing_account_id",
                "service_description",
                "sku_description",
                "usage_start_time",
                "usage_amount_in_pricing_units",
                "usage_pricing_unit",
                "cost",
                "currency",
            ],
            optional_columns=[
                "project_id",
                "sku_id",
                "usage_end_time",
                "usage_amount",
                "usage_unit",
                "cost_type",
                "currency_conversion_rate",
                "invoice_month",
                "location_region",
                "resource_name",
                "resource_global_name",
                "price_effective_price",
                "credits",
            ],
            sample_csv=(
                "billing_account_id,project_id,service_description,sku_id,sku_description,"
                "usage_start_time,usage_amount_in_pricing_units,usage_pricing_unit,cost,"
                "cost_type,currency,invoice_month,location_region,credits\n"
                "012345-6789AB-CDEF01,contoso-ai,Vertex AI,SKU-GEMINI-IN,"
                "Gemini 1.5 Pro Input Tokens,2026-09-12T00:00:00Z,2000000,1 token,2.50,"
                "regular,USD,202609,us-central1,\"[{\"\"type\"\":\"\"COMMITTED_USAGE_DISCOUNT\"\","
                "\"\"amount\"\":-0.50}]\"\n"
                "012345-6789AB-CDEF01,contoso-ai,Vertex AI,SKU-GEMINI-OUT,"
                "Gemini 1.5 Pro Output Tokens,2026-09-12T00:00:00Z,500000,1 token,2.50,"
                "regular,USD,202609,us-central1,[]\n"
            ),
            extraction_query=gcp_query,
            notes=[
                "Observation window is filtered by usage_start_time; invoice.month is "
                "tracked separately to surface late billing adjustments.",
                "Recurring commitment credits (CUDs) reduce effective cost; one-off "
                "promotions and free-tier credits are excluded.",
            ],
        ),
        BillingSourceTemplateView(
            provider="mosaicTemplate",
            title="MOSAIC Billing CSV Template (mosaic_billing_v1)",
            schema_version=MOSAIC_TEMPLATE_SCHEMA_VERSION,
            supported_formats=[".csv", ".csv.gz"],
            required_columns=[
                "schema_version",
                "provider",
                "pricing_cloud",
                "source_month",
                "model",
                "usage_category",
                "usage_quantity",
                "usage_unit",
                "currency",
                "eligible_cost_usd",
            ],
            optional_columns=[
                "source_account_id",
                "source_resource_id",
                "source_resource_name",
                "publisher",
                "model_version",
                "deployment_type",
                "region",
                "service_tier",
                "routing_mode",
                "amortized_benefit_cost_usd",
                "excluded_cost_usd",
                "excluded_reason",
                "invoice_month",
            ],
            sample_csv=(
                "schema_version,provider,pricing_cloud,source_month,source_account_id,"
                "source_resource_id,source_resource_name,publisher,model,model_version,"
                "deployment_type,region,service_tier,routing_mode,usage_category,"
                "usage_quantity,usage_unit,currency,eligible_cost_usd,"
                "amortized_benefit_cost_usd,excluded_cost_usd,excluded_reason,invoice_month\n"
                "mosaic_billing_v1,azure,commercial,2026-09,sub-1,"
                "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/"
                "providers/Microsoft.CognitiveServices/accounts/contoso-aoai,contoso-aoai,"
                "OpenAI,gpt-4o,2024-11-20,GlobalStandard,eastus2,Standard,in-region,"
                "inputTokens,2000,1K Tokens,USD,3.60,0.00,0.00,,2026-09\n"
                "mosaic_billing_v1,azure,commercial,2026-09,sub-1,"
                "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/"
                "providers/Microsoft.CognitiveServices/accounts/contoso-aoai,contoso-aoai,"
                "OpenAI,gpt-4o,2024-11-20,GlobalStandard,eastus2,Standard,in-region,"
                "outputTokens,500,1K Tokens,USD,3.60,0.00,0.00,,2026-09\n"
            ),
            notes=[
                "Use schema_version=mosaic_billing_v1 to import normalized billing rows "
                "from any pre-processed source.",
            ],
        ),
    ]


def _check_unsupported_binary(data: bytes, file_name: str) -> None:
    lower_name = file_name.strip().casefold()
    if data.startswith(_ZIP_XLSX_MAGIC) or lower_name.endswith((".xlsx", ".zip")):
        raise ValidationError(
            "Excel workbooks (.xlsx) and ZIP archives are not supported. Export the detailed "
            "billing dataset as CSV (.csv, .csv.gz), JSON/NDJSON, or AWS CUR Parquet (.parquet).",
            details={"fileName": file_name, "reason": "unsupportedBinaryFormat"},
        )
    if data.startswith(_OLE_XLS_MAGIC) or lower_name.endswith(".xls"):
        raise ValidationError(
            "Legacy Excel workbooks (.xls) are not supported. Export the detailed billing "
            "dataset as CSV.",
            details={"fileName": file_name, "reason": "unsupportedBinaryFormat"},
        )
    if data.startswith(_PDF_MAGIC) or lower_name.endswith(".pdf"):
        raise ValidationError(
            "PDF invoices are not supported because they lack the meter- and token-level "
            "quantities required to derive unit rates.",
            details={"fileName": file_name, "reason": "unsupportedBinaryFormat"},
        )


def _decompress_gzip_bounded(data: bytes, *, max_decompressed_bytes: int, file_name: str) -> bytes:
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = bytearray()
    chunk_size = 65536
    try:
        for offset in range(0, len(data), chunk_size):
            piece = data[offset : offset + chunk_size]
            remaining = max_decompressed_bytes - len(out) + 1
            decompressed = decompressor.decompress(piece, remaining)
            out.extend(decompressed)
            if len(out) > max_decompressed_bytes or decompressor.unconsumed_tail:
                raise BillingPayloadTooLargeError(
                    f"Decompressed billing file {file_name!r} exceeds the maximum allowed size "
                    f"({max_decompressed_bytes:,} bytes).",
                    details={
                        "fileName": file_name,
                        "maxDecompressedBytes": max_decompressed_bytes,
                    },
                )
        tail = decompressor.flush()
        out.extend(tail)
        if len(out) > max_decompressed_bytes:
            raise BillingPayloadTooLargeError(
                f"Decompressed billing file {file_name!r} exceeds the maximum allowed size "
                f"({max_decompressed_bytes:,} bytes).",
                details={
                    "fileName": file_name,
                    "maxDecompressedBytes": max_decompressed_bytes,
                },
            )
    except BillingPayloadTooLargeError:
        raise
    except Exception as exc:
        raise ValidationError(
            f"Compressed billing file {file_name!r} could not be decompressed as valid GZIP.",
            details={"fileName": file_name},
        ) from exc
    return bytes(out)


def _parse_decimal(
    raw_value: Any,
    *,
    field_name: str,
    row_number: int,
    required: bool = False,
    default: Decimal = _ZERO,
) -> Decimal:
    if raw_value is None:
        if required:
            raise ValidationError(
                f"Row {row_number} is missing a required numeric value for {field_name}.",
                details={"row": row_number, "field": field_name},
            )
        return default
    if isinstance(raw_value, bool):
        raise ValidationError(
            f"Row {row_number} has an invalid boolean value for numeric field {field_name}.",
            details={"row": row_number, "field": field_name},
        )
    text = str(raw_value).strip()
    if not text:
        if required:
            raise ValidationError(
                f"Row {row_number} is missing a required numeric value for {field_name}.",
                details={"row": row_number, "field": field_name},
            )
        return default
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValidationError(
            f"Row {row_number} has an invalid numeric value in {field_name}: {text!r}.",
            details={"row": row_number, "field": field_name},
        ) from exc
    if not value.is_finite():
        raise ValidationError(
            f"Row {row_number} has a non-finite number ({text}) in {field_name}.",
            details={"row": row_number, "field": field_name},
        )
    return value


def _parse_usage_date(raw_value: Any) -> date | None:
    if raw_value is None:
        return None
    if isinstance(raw_value, datetime):
        return raw_value.date()
    if isinstance(raw_value, date):
        return raw_value
    text = str(raw_value).strip()
    if not text:
        return None
    # Handle ISO date or timestamp: 2026-09-15 or 2026-09-15T12:34:56Z or 2026-09-15 00:00:00 UTC
    iso_prefix = text[:10]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", iso_prefix):
        try:
            return date.fromisoformat(iso_prefix)
        except ValueError:
            pass
    # Handle MM/DD/YYYY (common in Azure EA/MCA CSV exports)
    mm_dd_yyyy = re.match(r"^(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>\d{4})", text)
    if mm_dd_yyyy:
        try:
            return date(
                int(mm_dd_yyyy.group("year")),
                int(mm_dd_yyyy.group("month")),
                int(mm_dd_yyyy.group("day")),
            )
        except ValueError:
            pass
    return None


def _normalize_invoice_month(raw_value: Any) -> str | None:
    if raw_value is None:
        return None
    text = str(raw_value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{6}", text):
        return f"{text[:4]}-{text[4:6]}"
    if re.fullmatch(r"\d{4}-\d{2}", text[:7]):
        return text[:7]
    parsed_date = _parse_usage_date(text)
    if parsed_date is not None:
        return f"{parsed_date.year:04d}-{parsed_date.month:02d}"
    return None


def _normalize_unit_scale(
    usage_category: BillingUsageCategory,
    raw_unit: str | None,
    raw_quantity: Decimal,
) -> tuple[Decimal, str | None, bool, bool]:
    """Return ``(normalized_quantity, normalized_unit, unknown_unit, unsupported_capacity)``."""

    if usage_category == "excluded":
        return _ZERO, None, False, False

    cleaned = re.sub(r"\s+", " ", (raw_unit or "").strip()).casefold()
    if cleaned in _UNSUPPORTED_CAPACITY_UNITS or usage_category == "unsupported":
        if cleaned in _UNSUPPORTED_CAPACITY_UNITS:
            return raw_quantity, raw_unit, False, True
        return raw_quantity, raw_unit, True, False

    if usage_category in (
        "inputTokens",
        "cachedInputTokens",
        "cacheWriteTokens",
        "outputTokens",
    ):
        factor = _TOKEN_UNIT_FACTORS.get(cleaned)
        if factor is None:
            return _ZERO, None, True, False
        return raw_quantity * factor, "tokens", False, False

    if usage_category == "ptuHours":
        factor = _PTU_HOUR_UNIT_FACTORS.get(cleaned)
        if factor is None:
            if cleaned in _UNSUPPORTED_CAPACITY_UNITS:
                return raw_quantity, raw_unit, False, True
            return _ZERO, None, True, False
        return raw_quantity * factor, "ptuHours", False, False

    if usage_category == "monthlyCommitment":
        if cleaned in _MONTHLY_COMMITMENT_UNITS:
            return raw_quantity, "months", False, False
        return _ZERO, None, True, False

    return _ZERO, None, True, False


def _is_embedding_model(model: str | None) -> bool:
    if not model:
        return False
    folded = model.casefold()
    return "embed" in folded


def _extract_azure_account_name(resource_id: str | None, resource_name: str | None) -> str | None:
    if resource_id:
        match = re.search(r"/accounts/([^/]+)", resource_id, re.IGNORECASE)
        if match:
            return match.group(1)
    if resource_name:
        return resource_name.strip() or None
    return None


def _infer_publisher(text: str, model: str | None = None) -> str | None:
    combined = f"{text} {model or ''}".casefold()
    if "anthropic" in combined or "claude" in combined:
        return "Anthropic"
    if "deepseek" in combined:
        return "DeepSeek"
    if any(k in combined for k in ("mistral", "ministral", "codestral", "pixtral")):
        return "Mistral AI"
    if "llama" in combined or "meta" in combined:
        return "Meta"
    if "cohere" in combined or "command-r" in combined:
        return "Cohere"
    if "gemini" in combined or "vertex" in combined or "palm" in combined or "gecko" in combined:
        return "Google"
    if "titan" in combined or "nova" in combined or "amazon." in combined:
        return "Amazon"
    if "phi-" in combined or "phi " in combined or "mai-" in combined:
        return "Microsoft"
    if (
        "openai" in combined
        or "gpt" in combined
        or "text-embedding" in combined
        or re.search(r"\bo[134](?:-|\b)", combined)
    ):
        return "OpenAI"
    return None


def _parse_azure_meter_and_model(
    *,
    product: str,
    meter_category: str,
    meter_subcategory: str,
    meter_name: str,
    unit_of_measure: str,
    additional_info: str,
) -> tuple[str | None, str | None, str | None, str | None, BillingUsageCategory]:
    """Resolve ``(publisher, model, version, deployment_type, usage_category)`` for an Azure row."""

    by_prod_meter, by_meter, known_models = _seed_meter_lookup()

    info_model: str | None = None
    info_version: str | None = None
    info_deployment_type: str | None = None
    info_publisher: str | None = None
    if additional_info.strip().startswith("{"):
        try:
            parsed_info = json.loads(additional_info)
            if isinstance(parsed_info, dict):
                lower_info = {str(k).casefold(): v for k, v in parsed_info.items()}
                raw_m = lower_info.get("modelname") or lower_info.get("model")
                if isinstance(raw_m, str) and raw_m.strip():
                    info_model = raw_m.strip()
                raw_v = lower_info.get("modelversion") or lower_info.get("version")
                if isinstance(raw_v, str) and raw_v.strip():
                    info_version = raw_v.strip()
                raw_dt = lower_info.get("deploymenttype") or lower_info.get("skuname")
                if isinstance(raw_dt, str) and raw_dt.strip():
                    info_deployment_type = canonical_deployment_type(raw_dt.strip())
                raw_pub = lower_info.get("publisher") or lower_info.get("modelpublisher")
                if isinstance(raw_pub, str) and raw_pub.strip():
                    info_publisher = raw_pub.strip()
        except Exception:
            pass

    prod_key = product.strip().casefold()
    meter_key = meter_name.strip().casefold()
    seed_match = by_prod_meter.get((prod_key, meter_key)) or by_meter.get(meter_key)
    if seed_match is not None:
        return (
            info_publisher or seed_match.publisher,
            info_model or seed_match.model,
            info_version or seed_match.version,
            info_deployment_type or seed_match.deployment_type,
            seed_match.usage_category,
        )

    combined = f"{product} {meter_subcategory} {meter_name}".strip()
    folded = combined.casefold()
    unit_folded = unit_of_measure.strip().casefold()

    # Determine usage category
    usage_category: BillingUsageCategory
    if "provisioned" in folded or re.search(r"\bptu\b", folded):
        if unit_folded in _MONTHLY_COMMITMENT_UNITS:
            usage_category = "monthlyCommitment"
        else:
            usage_category = "ptuHours"
    elif "cache" in folded and "write" in folded:
        usage_category = "cacheWriteTokens"
    elif "cached" in folded or "cchd" in folded or "cache read" in folded or "cache-read" in folded:
        usage_category = "cachedInputTokens"
    elif any(tok in folded for tok in ("outp", "outpt", "output", "completion")):
        usage_category = "outputTokens"
    elif any(tok in folded for tok in ("inp", "inpt", "input", "prompt", "embedding", "embed")):
        usage_category = "inputTokens"
    elif unit_folded in _MONTHLY_COMMITMENT_UNITS:
        usage_category = "monthlyCommitment"
    elif unit_folded in _PTU_HOUR_UNIT_FACTORS:
        usage_category = "ptuHours"
    else:
        usage_category = "inputTokens"

    # Determine deployment type
    deployment_type = info_deployment_type
    if deployment_type is None:
        for known_dt in (
            "GlobalProvisionedManaged",
            "DataZoneProvisionedManaged",
            "ProvisionedManaged",
            "GlobalBatch",
            "DataZoneBatch",
            "GlobalStandard",
            "DataZoneStandard",
        ):
            if known_dt.casefold() in folded.replace(" ", ""):
                deployment_type = known_dt
                break
    if deployment_type is None:
        is_global = "glbl" in folded or "global" in folded
        is_dz = (
            "dzone" in folded
            or "data zone" in folded
            or "datazone" in folded
            or re.search(r"\bdz\b", folded) is not None
        )
        is_batch = "batch" in folded
        if usage_category in ("ptuHours", "monthlyCommitment"):
            if is_global:
                deployment_type = "GlobalProvisionedManaged"
            elif is_dz:
                deployment_type = "DataZoneProvisionedManaged"
            else:
                deployment_type = "ProvisionedManaged"
        elif is_batch:
            deployment_type = "DataZoneBatch" if is_dz else "GlobalBatch"
        elif is_global:
            deployment_type = "GlobalStandard"
        elif is_dz:
            deployment_type = "DataZoneStandard"
        elif "regnl" in folded or "regional" in folded or "standard" in folded:
            deployment_type = "Standard"
        else:
            deployment_type = "GlobalStandard"

    # Determine model & version
    model = info_model
    version = info_version
    publisher = info_publisher

    if version is None:
        date_ver = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", combined)
        if date_ver:
            version = date_ver.group(1)
        else:
            short_ver = re.search(r"\b(1120|0806|0718|0513|0409|0125|1106|0613|0414)\b", combined)
            if short_ver:
                mmdd = short_ver.group(1)
                yr = "2025" if mmdd == "0414" else "2024"
                version = f"{yr}-{mmdd[:2]}-{mmdd[2:]}"

    if model is None:
        compact_combined = re.sub(r"[^a-z0-9.-]", " ", folded)
        for candidate_model, cand_pub, cand_ver in known_models:
            pattern_str = re.escape(candidate_model.casefold()).replace(r"\-", r"[\s-]*")
            if re.search(rf"(?<![a-z0-9]){pattern_str}(?![a-z0-9])", compact_combined):
                model = candidate_model
                publisher = publisher or cand_pub
                if (
                    version is None
                    and cand_ver is not None
                    and cand_ver.replace("-", "") in compact_combined
                ):
                    version = cand_ver
                break

    if model is None:
        if usage_category in ("ptuHours", "monthlyCommitment"):
            model = "*"
        else:
            # Extract leading model identifier from meter_name
            cleaned_meter = re.sub(
                r"\b(?:cached|cchd|cache|write|read|inp|inpt|input|outp|outpt|output|prompt|"
                r"completion|glbl|global|dzone|data\s*zone|dz|regnl|regional|standard|batch|"
                r"tokens|token|1k|1m|20\d{2}-\d{2}-\d{2}|1120|0806|0718|0414)\b",
                " ",
                meter_name,
                flags=re.IGNORECASE,
            )
            cleaned_meter = re.sub(r"\s+", "-", cleaned_meter.strip(" -_"))
            model = cleaned_meter or None

    publisher = publisher or _infer_publisher(f"{meter_category} {combined}", model)
    return publisher, model, version, deployment_type, usage_category


def _is_azure_model_service_row(
    *,
    meter_category: str,
    meter_subcategory: str,
    product: str,
    meter_name: str,
    resource_id: str,
) -> bool:
    combined = (
        f"{meter_category} {meter_subcategory} {product} {meter_name} {resource_id}".casefold()
    )
    if any(
        non_ai in combined
        for non_ai in (
            "virtual machines",
            "storage",
            "bandwidth",
            "azure monitor",
            "log analytics",
            "key vault",
            "api management",
            "virtual network",
            "container registry",
            "azure dns",
        )
    ):
        return False
    return any(
        marker in combined
        for marker in (
            "foundry models",
            "azure openai",
            "cognitive services",
            "microsoft.cognitiveservices",
            "openai",
            "deepseek",
            "mistral",
            "llama",
            "phi",
            "cohere",
            "claude",
            "gpt",
            "token",
            "provisioned",
            "ptu",
            "embedding",
        )
    )


def _parse_aws_bedrock_usage_type(
    usage_type: str,
    product_region_code: str | None,
    product_model: str | None,
    pricing_unit: str,
) -> tuple[
    str | None,
    str | None,
    str | None,
    str,
    str,
    BillingUsageCategory,
    bool,
]:
    """Parse AWS Bedrock CUR UsageType and return
    ``(publisher, model, region, service_tier, routing_mode, usage_category, is_unsupported_cap)``.
    """

    raw_ut = usage_type.strip()
    unit_folded = pricing_unit.strip().casefold()
    ut_folded = raw_ut.casefold()

    region: str | None = (product_region_code or "").strip() or None
    rest = raw_ut

    full_match = _AWS_FULL_REGION_PREFIX_RE.match(raw_ut)
    if full_match:
        region = region or full_match.group("region").casefold()
        rest = full_match.group("rest")
    else:
        short_match = _AWS_SHORT_REGION_PREFIX_RE.match(raw_ut)
        if short_match:
            prefix = short_match.group("prefix").casefold()
            if prefix in _AWS_USAGE_PREFIX_REGIONS:
                region = region or _AWS_USAGE_PREFIX_REGIONS[prefix]
                rest = short_match.group("rest")

    # Check for Bedrock Provisioned Throughput (ModelUnit-Hour)
    if (
        unit_folded in _UNSUPPORTED_CAPACITY_UNITS
        or "modelunit" in unit_folded.replace("-", "").replace(" ", "")
        or "provisionedthroughput" in ut_folded.replace("-", "")
        or "modelunit" in ut_folded.replace("-", "")
    ):
        cleaned_model = product_model.strip() if product_model and product_model.strip() else None
        if not cleaned_model:
            cleaned = re.sub(
                r"(?i)-?(?:provisioned-?throughput|model-?unit-?hours?|model-?unit|custom-?model)",
                "",
                rest,
            ).strip("-")
            cleaned_model = cleaned or "bedrock-provisioned"
        publisher = _infer_publisher(raw_ut, cleaned_model)
        return (
            publisher,
            cleaned_model,
            region,
            "ProvisionedThroughput",
            "in-region",
            "unsupported",
            True,
        )

    # Routing mode extraction
    routing_mode = "in-region"
    if re.search(r"-cross-region-global\b", rest, flags=re.IGNORECASE):
        routing_mode = "cross-region-global"
        rest = re.sub(r"-cross-region-global\b", "", rest, flags=re.IGNORECASE)
    elif re.search(r"-cross-region(?:-inference)?\b", rest, flags=re.IGNORECASE):
        routing_mode = "cross-region"
        rest = re.sub(r"-cross-region(?:-inference)?\b", "", rest, flags=re.IGNORECASE)

    # Service tier extraction
    service_tier = "Standard"
    tier_match = re.search(r"-(standard|priority|flex|batch)$", rest, flags=re.IGNORECASE)
    if tier_match:
        tier_word = tier_match.group(1).casefold()
        service_tier = tier_word.capitalize()
        rest = rest[: tier_match.start()]

    # Token category extraction
    usage_category: BillingUsageCategory = "inputTokens"
    token_patterns: list[tuple[str, BillingUsageCategory]] = [
        (
            r"-(?:cache-read-input-token-count|cache-read-input-tokens|cached-input-tokens)$",
            "cachedInputTokens",
        ),
        (r"-(?:cache-write-input-token-count|cache-write-input-tokens)$", "cacheWriteTokens"),
        (r"-(?:input-token-count|input-tokens)$", "inputTokens"),
        (r"-(?:output-token-count|output-tokens)$", "outputTokens"),
    ]
    matched_token_cat = False
    for pat, cat in token_patterns:
        m = re.search(pat, rest, flags=re.IGNORECASE)
        if m:
            usage_category = cat
            rest = rest[: m.start()]
            matched_token_cat = True
            break

    if not matched_token_cat:
        # Handle case where tier was placed before token-type or inside string
        for pat_inner, cat in (
            (r"cache-read-input-token(?:-count|s)?", "cachedInputTokens"),
            (r"cache-write-input-token(?:-count|s)?", "cacheWriteTokens"),
            (r"input-token(?:-count|s)?", "inputTokens"),
            (r"output-token(?:-count|s)?", "outputTokens"),
        ):
            m = re.search(pat_inner, rest, flags=re.IGNORECASE)
            if m:
                usage_category = cat
                before = rest[: m.start()].rstrip("-")
                after = rest[m.end() :].lstrip("-")
                if after.casefold() in ("standard", "priority", "flex", "batch"):
                    service_tier = after.capitalize()
                rest = before
                break

    # Strip trailing -mantle if present (from {region}-{model}-mantle-{token-type}-standard)
    rest = re.sub(r"-mantle$", "", rest, flags=re.IGNORECASE).strip("-")

    model = (product_model or "").strip() or rest or None
    publisher = _infer_publisher(raw_ut, model)
    return publisher, model, region, service_tier, routing_mode, usage_category, False


def _parse_gcp_sku_and_model(
    sku_description: str,
    service_description: str,
    pricing_unit: str,
    resource_name: str | None,
) -> tuple[str | None, str | None, str, BillingUsageCategory, bool]:
    """Parse GCP SKU description and return:
    ``(publisher, model, service_tier, usage_category, is_unsupported_capacity)``.
    """

    desc = sku_description.strip()
    folded = desc.casefold()
    unit_folded = pricing_unit.strip().casefold()

    if (
        unit_folded in _UNSUPPORTED_CAPACITY_UNITS
        or re.search(r"\bgsu\b", folded)
        or "generative service unit" in folded
        or "provisioned throughput" in folded
    ):
        cleaned_model = re.sub(
            r"(?i)\b(?:vertex\s*ai|provisioned\s*throughput|generative\s*service\s*units?|gsu)\b",
            " ",
            desc,
        )
        cleaned_model = re.sub(r"\s+", "-", cleaned_model.strip(" -_")) or "vertex-gsu"
        publisher = _infer_publisher(f"{service_description} {desc}", cleaned_model)
        return publisher, cleaned_model, "ProvisionedThroughput", "unsupported", True

    if "cache" in folded and ("write" in folded or "storage" in folded):
        usage_category: BillingUsageCategory = "cacheWriteTokens"
    elif "context caching" in folded or "cached" in folded or "cache read" in folded:
        usage_category = "cachedInputTokens"
    elif any(w in folded for w in ("output", "candidate", "completion", "response")):
        usage_category = "outputTokens"
    else:
        usage_category = "inputTokens"

    service_tier = "Batch" if "batch" in folded else "Standard"

    # Extract canonical model identifier from SKU description
    model: str | None = None
    known_gcp_models = [
        (r"gemini[\s-]*2\.5[\s-]*pro", "gemini-2.5-pro"),
        (r"gemini[\s-]*2\.5[\s-]*flash", "gemini-2.5-flash"),
        (r"gemini[\s-]*2\.0[\s-]*flash[\s-]*lite", "gemini-2.0-flash-lite"),
        (r"gemini[\s-]*2\.0[\s-]*flash", "gemini-2.0-flash"),
        (r"gemini[\s-]*1\.5[\s-]*pro", "gemini-1.5-pro"),
        (r"gemini[\s-]*1\.5[\s-]*flash", "gemini-1.5-flash"),
        (r"claude[\s-]*3[\s.-]*7[\s-]*sonnet", "claude-3-7-sonnet"),
        (r"claude[\s-]*3[\s.-]*5[\s-]*sonnet", "claude-3-5-sonnet"),
        (r"claude[\s-]*3[\s.-]*5[\s-]*haiku", "claude-3-5-haiku"),
        (r"claude[\s-]*3[\s-]*opus", "claude-3-opus"),
        (r"text[\s-]*embedding[\s-]*005", "text-embedding-005"),
        (r"text[\s-]*embedding[\s-]*004", "text-embedding-004"),
        (r"text[\s-]*multilingual[\s-]*embedding[\s-]*002", "text-multilingual-embedding-002"),
        (r"textembedding[\s-]*gecko", "textembedding-gecko"),
        (r"gemini[\s-]*embedding[\s-]*001", "gemini-embedding-001"),
        (r"llama[\s-]*3\.3[\s-]*70b", "llama-3.3-70b"),
        (r"mistral[\s-]*large", "mistral-large"),
    ]
    for pat, canonical in known_gcp_models:
        if re.search(pat, folded):
            model = canonical
            break

    if model is None and resource_name:
        m_res = re.search(r"/models/([A-Za-z0-9._-]+)", resource_name)
        if m_res:
            model = m_res.group(1)

    if model is None:
        cleaned = re.sub(
            r"(?i)\b(?:vertex\s*ai|google|context\s*caching|cached|cache\s*read|cache\s*write|"
            r"input|output|prompt|candidate|completion|text|tokens?|batch|standard|regional|global|"
            r"in\s+[a-z0-9-]+)\b",
            " ",
            desc,
        )
        cleaned = re.sub(r"\s+", "-", cleaned.strip(" -_:()")).casefold()
        model = cleaned or None

    publisher = _infer_publisher(f"{service_description} {desc}", model)
    return publisher, model, service_tier, usage_category, False


def _get_ci(row: Mapping[str, Any], *names: str) -> Any:
    """Case-insensitive / punctuation-tolerant field lookup in a parsed row dict."""

    for name in names:
        if name in row and row[name] is not None and str(row[name]).strip() != "":
            return row[name]
    folded_map = {str(k).strip().casefold(): v for k, v in row.items()}
    for name in names:
        val = folded_map.get(name.casefold())
        if val is not None and str(val).strip() != "":
            return val
    return None


def _get_nested_or_flat(row: Mapping[str, Any], dotted: str, underscored: str) -> Any:
    val = _get_ci(row, dotted, underscored)
    if val is not None:
        return val
    parts = dotted.split(".")
    curr: Any = row
    for part in parts:
        if isinstance(curr, Mapping):
            curr = _get_ci(curr, part)
        else:
            curr = None
            break
    return curr


def _read_csv_rows(
    text: str, *, max_rows: int, file_name: str
) -> tuple[list[str], list[dict[str, Any]]]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValidationError(
            f"Billing CSV {file_name!r} has no header row.",
            details={"fileName": file_name},
        )
    headers = [h.strip() for h in reader.fieldnames if h is not None and h.strip()]
    if not headers:
        raise ValidationError(
            f"Billing CSV {file_name!r} has no valid column names.",
            details={"fileName": file_name},
        )
    rows: list[dict[str, Any]] = []
    for idx, raw_row in enumerate(reader, start=1):
        if idx > max_rows:
            raise ValidationError(
                f"Billing CSV {file_name!r} exceeds the maximum allowed row count "
                f"({max_rows:,} rows). Filter the export to AI/model services for a single "
                "completed month.",
                details={"fileName": file_name, "maxRows": max_rows},
            )
        if raw_row.get(None):
            raise ValidationError(
                f"Row {idx} in {file_name!r} has more values than the CSV header.",
                details={"fileName": file_name, "row": idx},
            )
        cleaned_row = {
            k.strip(): (v.strip() if isinstance(v, str) else v)
            for k, v in raw_row.items()
            if k is not None
        }
        # Skip completely blank lines
        if not any(v not in (None, "") for v in cleaned_row.values()):
            continue
        rows.append(cleaned_row)
    if not rows:
        raise ValidationError(
            f"Billing dataset {file_name!r} contains headers but no data rows.",
            details={"fileName": file_name},
        )
    return headers, rows


def _read_json_rows(text: str, *, max_rows: int, file_name: str) -> list[dict[str, Any]]:
    stripped = text.strip()
    if not stripped:
        raise ValidationError(
            f"Billing JSON file {file_name!r} is empty.",
            details={"fileName": file_name},
        )
    rows: list[dict[str, Any]] = []
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValidationError(
                f"Invalid JSON in {file_name!r}: {exc.msg}.",
                details={"fileName": file_name},
            ) from exc
        if not isinstance(parsed, list):
            raise ValidationError(
                f"Expected a JSON array of billing rows in {file_name!r}.",
                details={"fileName": file_name},
            )
        if len(parsed) > max_rows:
            raise ValidationError(
                f"Billing JSON {file_name!r} exceeds the maximum allowed row count "
                f"({max_rows:,} rows).",
                details={"fileName": file_name, "maxRows": max_rows},
            )
        for idx, item in enumerate(parsed, start=1):
            if not isinstance(item, dict):
                raise ValidationError(
                    f"Row {idx} in {file_name!r} is not a JSON object.",
                    details={"fileName": file_name, "row": idx},
                )
            rows.append(item)
    else:
        for idx, line in enumerate(stripped.splitlines(), start=1):
            line_s = line.strip()
            if not line_s:
                continue
            if len(rows) + 1 > max_rows:
                raise ValidationError(
                    f"Billing NDJSON {file_name!r} exceeds the maximum allowed row count "
                    f"({max_rows:,} rows).",
                    details={"fileName": file_name, "maxRows": max_rows},
                )
            try:
                item = json.loads(line_s)
            except json.JSONDecodeError as exc:
                raise ValidationError(
                    f"Invalid JSON on line {idx} of {file_name!r}: {exc.msg}.",
                    details={"fileName": file_name, "row": idx},
                ) from exc
            if not isinstance(item, dict):
                raise ValidationError(
                    f"Line {idx} in {file_name!r} is not a JSON object.",
                    details={"fileName": file_name, "row": idx},
                )
            rows.append(item)
    if not rows:
        raise ValidationError(
            f"Billing JSON dataset {file_name!r} contains no rows.",
            details={"fileName": file_name},
        )
    return rows


def _enforce_usd_currency(currency_raw: Any, *, row_number: int) -> str:
    if currency_raw is None or not str(currency_raw).strip():
        raise ValidationError(
            f"Row {row_number} is missing a billing currency code; USD is required.",
            details={"code": "nonUsdCurrency", "row": row_number},
        )
    code = str(currency_raw).strip().upper()
    if code != "USD":
        raise ValidationError(
            f"MOSAIC accepts USD-denominated billing data only. Row {row_number} is denominated "
            f"in {code!r}; auxiliary currency conversion columns are never used.",
            details={"code": "nonUsdCurrency", "currency": code, "row": row_number},
        )
    return "USD"


def _normalize_azure_rows(
    headers: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
    *,
    source_month: str,
    source_scope: str | None,
) -> tuple[list[_NormalizedRowRecord], list[BillingReviewIssue]]:
    header_set = {h.strip().casefold() for h in headers}
    has_cost = bool(header_set & {"costinbillingcurrency", "cost", "pretaxcost"})
    has_qty = bool(header_set & {"quantity", "consumedquantity", "usagequantity"})
    has_unit = bool(header_set & {"unitofmeasure", "unit"})
    has_meter = bool(header_set & {"metername", "product", "productname", "metersubcategory"})
    has_currency = bool(header_set & {"billingcurrencycode", "billingcurrency", "currency"})

    if not (has_cost and has_qty and has_unit and has_meter and has_currency):
        missing: list[str] = []
        if not has_meter:
            missing.append("MeterName/Product")
        if not has_qty:
            missing.append("Quantity")
        if not has_unit:
            missing.append("UnitOfMeasure")
        if not has_cost:
            missing.append("CostInBillingCurrency")
        if not has_currency:
            missing.append("BillingCurrencyCode")
        raise ValidationError(
            "Unsupported Azure Cost Details schema: cost-only summaries are not accepted. "
            f"Missing required columns: {', '.join(missing)}.",
            details={"missingColumns": missing, "provider": "azure"},
        )

    month_start, month_end = parse_source_month(source_month)
    records: list[_NormalizedRowRecord] = []
    issues: list[BillingReviewIssue] = []
    tax_excluded_rows = 0
    outside_month_rows = 0

    for idx, row in enumerate(rows, start=1):
        currency = _enforce_usd_currency(
            _get_ci(row, "BillingCurrencyCode", "BillingCurrency", "Currency"),
            row_number=idx,
        )
        quantity = _parse_decimal(
            _get_ci(row, "Quantity", "ConsumedQuantity", "UsageQuantity"),
            field_name="Quantity",
            row_number=idx,
            required=True,
        )
        cost_raw = _get_ci(row, "CostInBillingCurrency", "Cost", "PreTaxCost")
        eff_price_raw = _get_ci(row, "EffectivePrice")
        eff_price = _parse_decimal(
            eff_price_raw,
            field_name="EffectivePrice",
            row_number=idx,
            required=False,
        )
        if cost_raw is not None:
            cost = _parse_decimal(
                cost_raw,
                field_name="CostInBillingCurrency",
                row_number=idx,
                required=True,
            )
        elif eff_price_raw is not None:
            cost = quantity * eff_price
        else:
            raise ValidationError(
                f"Row {idx} is missing CostInBillingCurrency.",
                details={"row": idx, "field": "CostInBillingCurrency"},
            )

        if _get_ci(row, "UnitPrice") is not None:
            _parse_decimal(_get_ci(row, "UnitPrice"), field_name="UnitPrice", row_number=idx)
        if _get_ci(row, "PayGPrice") is not None:
            _parse_decimal(_get_ci(row, "PayGPrice"), field_name="PayGPrice", row_number=idx)

        charge_type = str(_get_ci(row, "ChargeType") or "Usage").strip()
        charge_type_folded = charge_type.casefold()
        is_adjustment = charge_type_folded in {
            "tax",
            "roundingerror",
            "credit",
            "refund",
            "unusedreservation",
            "unusedsavingsplan",
            "purchase",
        }
        if quantity < _ZERO and not is_adjustment:
            raise ValidationError(
                f"Row {idx} has a negative quantity ({quantity}) on a non-refund charge type "
                f"({charge_type!r}).",
                details={"row": idx, "field": "Quantity"},
            )

        usage_date = _parse_usage_date(_get_ci(row, "Date", "UsageDateTime"))
        billing_period = _normalize_invoice_month(
            _get_ci(row, "BillingPeriodStartDate", "ServicePeriodStartDate")
        )
        meter_category = str(_get_ci(row, "MeterCategory", "ServiceName") or "").strip()
        meter_subcategory = str(_get_ci(row, "MeterSubCategory") or "").strip()
        meter_name = str(_get_ci(row, "MeterName") or "").strip()
        meter_id = str(_get_ci(row, "MeterId") or "").strip() or None
        product = str(_get_ci(row, "Product", "ProductName") or "").strip()
        part_number = str(_get_ci(row, "PartNumber", "ProductId", "SkuId") or "").strip() or None
        unit_of_measure = str(_get_ci(row, "UnitOfMeasure", "Unit") or "").strip()
        pricing_model = str(_get_ci(row, "PricingModel") or "OnDemand").strip()
        benefit_id = str(_get_ci(row, "BenefitId", "ReservationId") or "").strip() or None
        benefit_name = str(_get_ci(row, "BenefitName", "ReservationName") or "").strip() or None
        resource_id = (
            str(_get_ci(row, "ResourceId", "InstanceId", "InstanceName") or "").strip() or None
        )
        resource_name = str(_get_ci(row, "ResourceName") or "").strip() or None
        account_name = _extract_azure_account_name(resource_id, resource_name)
        location_raw = str(
            _get_ci(row, "ResourceLocation", "Location", "MeterRegion") or ""
        ).strip()
        region = normalize_region(location_raw) if location_raw.casefold() != "global" else None
        sub_id = str(_get_ci(row, "SubscriptionId", "BillingAccountId") or "").strip() or None
        additional_info = str(_get_ci(row, "AdditionalInfo", "Tags") or "").strip()

        if usage_date is not None and not (month_start <= usage_date <= month_end):
            outside_month_rows += 1
            records.append(
                _NormalizedRowRecord(
                    provider="azure",
                    source_scope=source_scope,
                    source_account_id=sub_id,
                    source_resource_id=resource_id,
                    source_resource_name=account_name,
                    service_name=meter_category or product or None,
                    meter_id=meter_id,
                    sku_id=part_number,
                    sku_description=meter_name or product or None,
                    region=region,
                    usage_category="excluded",
                    raw_usage_unit=unit_of_measure or None,
                    raw_quantity=quantity,
                    excluded_cost_usd=cost,
                    excluded_reason="outsideSourceMonth",
                    usage_date=usage_date,
                    invoice_month=billing_period,
                    currency=currency,
                )
            )
            continue

        if is_adjustment:
            if charge_type_folded == "tax":
                tax_excluded_rows += 1
            records.append(
                _NormalizedRowRecord(
                    provider="azure",
                    source_scope=source_scope,
                    source_account_id=sub_id,
                    source_resource_id=resource_id,
                    source_resource_name=account_name,
                    service_name=meter_category or product or None,
                    meter_id=meter_id,
                    sku_id=part_number,
                    sku_description=meter_name or product or None,
                    region=region,
                    usage_category="excluded",
                    raw_usage_unit=unit_of_measure or None,
                    raw_quantity=quantity,
                    excluded_cost_usd=cost,
                    excluded_reason=f"chargeType:{charge_type}",
                    usage_date=usage_date,
                    invoice_month=billing_period,
                    currency=currency,
                )
            )
            continue

        if not _is_azure_model_service_row(
            meter_category=meter_category,
            meter_subcategory=meter_subcategory,
            product=product,
            meter_name=meter_name,
            resource_id=resource_id or "",
        ):
            records.append(
                _NormalizedRowRecord(
                    provider="azure",
                    source_scope=source_scope,
                    source_account_id=sub_id,
                    source_resource_id=resource_id,
                    source_resource_name=account_name,
                    service_name=meter_category or product or None,
                    meter_id=meter_id,
                    sku_id=part_number,
                    sku_description=meter_name or product or None,
                    region=region,
                    usage_category="excluded",
                    raw_usage_unit=unit_of_measure or None,
                    raw_quantity=quantity,
                    excluded_cost_usd=cost,
                    excluded_reason="nonModelService",
                    usage_date=usage_date,
                    invoice_month=billing_period,
                    currency=currency,
                )
            )
            continue

        publisher, model, version, deployment_type, usage_category = _parse_azure_meter_and_model(
            product=product,
            meter_category=meter_category,
            meter_subcategory=meter_subcategory,
            meter_name=meter_name,
            unit_of_measure=unit_of_measure,
            additional_info=additional_info,
        )
        norm_qty, norm_unit, unknown_unit, unsupported_cap = _normalize_unit_scale(
            usage_category, unit_of_measure, quantity
        )
        if unknown_unit or unsupported_cap:
            usage_category = "unsupported"

        is_commitment = pricing_model.casefold() in {"reservation", "savingsplan"} or bool(
            benefit_id or benefit_name
        )
        unamortized_blocker = False
        eligible_cost = _ZERO
        amortized_cost = _ZERO
        if is_commitment:
            if cost == _ZERO and eff_price == _ZERO and quantity > _ZERO:
                unamortized_blocker = True
                issues.append(
                    BillingReviewIssue(
                        code="unamortizedPrepaidUsage",
                        severity="blocker",
                        message=(
                            f"Row {idx} ({meter_name or product}) shows $0.00 cost for "
                            f"{pricing_model} commitment usage. Export the AmortizedCost dataset "
                            "instead of ActualCost so prepaid capacity is not priced as free."
                        ),
                        model=model,
                        region=region,
                        source_resource_id=resource_id,
                        details={
                            "row": idx,
                            "pricingModel": pricing_model,
                            "benefitId": benefit_id,
                            "benefitName": benefit_name,
                        },
                    )
                )
            else:
                amortized_cost = cost
        else:
            eligible_cost = cost

        if unknown_unit:
            issues.append(
                BillingReviewIssue(
                    code="unknownUsageUnit",
                    severity="blocker",
                    message=(
                        f"Row {idx} uses unrecognized unit of measure {unit_of_measure!r} "
                        f"for {meter_name or model or 'model usage'}."
                    ),
                    model=model,
                    region=region,
                    source_resource_id=resource_id,
                    details={"row": idx, "unit": unit_of_measure},
                )
            )
        if unsupported_cap:
            issues.append(
                BillingReviewIssue(
                    code="unsupportedCapacityUnit",
                    severity="blocker",
                    message=(
                        f"Row {idx} uses unsupported provisioned capacity unit {unit_of_measure!r}."
                    ),
                    model=model,
                    region=region,
                    source_resource_id=resource_id,
                    details={"row": idx, "unit": unit_of_measure},
                )
            )

        records.append(
            _NormalizedRowRecord(
                provider="azure",
                source_scope=source_scope,
                source_account_id=sub_id,
                source_resource_id=resource_id,
                source_resource_name=account_name,
                service_name=meter_category or product or "Foundry Models",
                meter_id=meter_id,
                sku_id=part_number,
                sku_description=meter_name or product or None,
                publisher=publisher,
                model=model,
                model_version=version,
                region=region,
                deployment_type=deployment_type,
                service_tier=(
                    "Batch" if deployment_type and "Batch" in deployment_type else "Standard"
                ),
                routing_mode=(
                    "global"
                    if deployment_type and deployment_type.startswith("Global")
                    else "data-zone"
                    if deployment_type and deployment_type.startswith("DataZone")
                    else "in-region"
                ),
                usage_category=usage_category,
                raw_usage_unit=unit_of_measure or None,
                raw_quantity=quantity,
                normalized_quantity=norm_qty,
                normalized_unit=norm_unit,
                eligible_cost_usd=eligible_cost,
                amortized_benefit_cost_usd=amortized_cost,
                usage_date=usage_date,
                invoice_month=billing_period or source_month,
                currency=currency,
                unamortized_prepaid_blocker=unamortized_blocker,
                unknown_unit_blocker=unknown_unit,
                unsupported_capacity_blocker=unsupported_cap,
            )
        )

    if tax_excluded_rows > 0:
        issues.append(
            BillingReviewIssue(
                code="taxExcluded",
                severity="info",
                message=f"Excluded {tax_excluded_rows} tax row(s) from effective rate derivation.",
                details={"excludedTaxRows": tax_excluded_rows},
            )
        )
    if outside_month_rows > 0:
        issues.append(
            BillingReviewIssue(
                code="outsideSourceMonth",
                severity="warning",
                message=(
                    f"Excluded {outside_month_rows} row(s) whose usage date fell outside "
                    f"{source_month}."
                ),
                details={"excludedRows": outside_month_rows, "sourceMonth": source_month},
            )
        )
    return records, issues


def _normalize_aws_rows(
    headers: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
    *,
    source_month: str,
    source_scope: str | None,
) -> tuple[list[_NormalizedRowRecord], list[BillingReviewIssue]]:
    header_set = {h.strip().casefold() for h in headers}
    has_usage_qty = bool(header_set & {"lineitem/usageamount", "line_item_usage_amount"})
    has_unit = bool(header_set & {"pricing/unit", "pricing_unit"})
    has_usage_type = bool(
        header_set
        & {
            "lineitem/usagetype",
            "line_item_usage_type",
            "product/usagetype",
            "product_usagetype",
            "product/model",
            "product_model",
        }
    )
    has_cost = bool(
        header_set
        & {
            "lineitem/unblendedcost",
            "line_item_unblended_cost",
            "lineitem/netunblendedcost",
            "line_item_net_unblended_cost",
            "savingsplan/savingsplaneffectivecost",
            "savings_plan_savings_plan_effective_cost",
        }
    )
    has_currency = bool(
        header_set
        & {
            "lineitem/currencycode",
            "line_item_currency_code",
            "pricing/currency",
            "pricing_currency",
        }
    )
    if not (has_usage_qty and has_unit and has_usage_type and has_cost and has_currency):
        missing: list[str] = []
        if not has_usage_type:
            missing.append("line_item_usage_type")
        if not has_usage_qty:
            missing.append("line_item_usage_amount")
        if not has_unit:
            missing.append("pricing_unit")
        if not has_cost:
            missing.append("line_item_unblended_cost")
        if not has_currency:
            missing.append("line_item_currency_code")
        raise ValidationError(
            "Unsupported AWS CUR schema: Cost Explorer summaries without detailed line-item usage "
            f"types and pricing units are not accepted. Missing columns: {', '.join(missing)}.",
            details={"missingColumns": missing, "provider": "aws"},
        )

    month_start, month_end = parse_source_month(source_month)
    records: list[_NormalizedRowRecord] = []
    issues: list[BillingReviewIssue] = []
    outside_month_rows = 0

    for idx, row in enumerate(rows, start=1):
        currency = _enforce_usd_currency(
            _get_ci(
                row,
                "lineItem/CurrencyCode",
                "line_item_currency_code",
                "pricing/currency",
                "pricing_currency",
            ),
            row_number=idx,
        )
        account_id = str(
            _get_ci(row, "lineItem/UsageAccountId", "line_item_usage_account_id") or ""
        ).strip() or None
        product_code = str(
            _get_ci(row, "lineItem/ProductCode", "line_item_product_code") or ""
        ).strip()
        usage_type = str(
            _get_ci(
                row,
                "lineItem/UsageType",
                "line_item_usage_type",
                "product/usagetype",
                "product_usagetype",
            )
            or ""
        ).strip()
        operation = str(
            _get_ci(row, "lineItem/Operation", "line_item_operation") or ""
        ).strip() or None
        resource_id = str(
            _get_ci(row, "lineItem/ResourceId", "line_item_resource_id") or ""
        ).strip() or None
        usage_start = _parse_usage_date(
            _get_ci(row, "lineItem/UsageStartDate", "line_item_usage_start_date")
        )
        invoice_month = _normalize_invoice_month(
            _get_ci(row, "bill/BillingPeriodStartDate", "bill_billing_period_start_date")
        )
        quantity = _parse_decimal(
            _get_ci(row, "lineItem/UsageAmount", "line_item_usage_amount"),
            field_name="line_item_usage_amount",
            row_number=idx,
            required=True,
        )
        pricing_unit = str(_get_ci(row, "pricing/unit", "pricing_unit") or "").strip()
        line_item_type = str(
            _get_ci(row, "lineItem/LineItemType", "line_item_line_item_type") or "Usage"
        ).strip()
        lit_folded = line_item_type.casefold()

        unblended = _parse_decimal(
            _get_ci(row, "lineItem/UnblendedCost", "line_item_unblended_cost"),
            field_name="line_item_unblended_cost",
            row_number=idx,
            required=False,
        )
        net_unblended_raw = _get_ci(
            row, "lineItem/NetUnblendedCost", "line_item_net_unblended_cost"
        )
        net_unblended = (
            _parse_decimal(
                net_unblended_raw,
                field_name="line_item_net_unblended_cost",
                row_number=idx,
            )
            if net_unblended_raw is not None
            else None
        )
        res_eff_raw = _get_ci(
            row, "reservation/EffectiveCost", "reservation_effective_cost"
        )
        res_eff = (
            _parse_decimal(res_eff_raw, field_name="reservation_effective_cost", row_number=idx)
            if res_eff_raw is not None
            else None
        )
        sp_eff_raw = _get_ci(
            row,
            "savingsPlan/SavingsPlanEffectiveCost",
            "savings_plan_savings_plan_effective_cost",
        )
        sp_eff = (
            _parse_decimal(
                sp_eff_raw,
                field_name="savings_plan_savings_plan_effective_cost",
                row_number=idx,
            )
            if sp_eff_raw is not None
            else None
        )
        net_sp_eff_raw = _get_ci(
            row,
            "savingsPlan/NetSavingsPlanEffectiveCost",
            "savings_plan_net_savings_plan_effective_cost",
        )
        net_sp_eff = (
            _parse_decimal(
                net_sp_eff_raw,
                field_name="savings_plan_net_savings_plan_effective_cost",
                row_number=idx,
            )
            if net_sp_eff_raw is not None
            else None
        )

        product_region_code = str(
            _get_ci(
                row,
                "product/regionCode",
                "product_region_code",
                "product/region",
                "product_region",
            )
            or ""
        ).strip() or None
        product_model = str(
            _get_ci(
                row,
                "product/model",
                "product_model",
                "product/modelId",
                "product_model_id",
            )
            or ""
        ).strip() or None

        is_excluded_type = lit_folded in {
            "tax",
            "credit",
            "refund",
            "fee",
            "rifee",
            "savingsplanrecurringfee",
            "savingsplanupfrontfee",
            "savingsplannegation",
            "bundleddiscount",
            "edpdiscount",
            "privateratediscount",
        }
        if quantity < _ZERO and not is_excluded_type:
            raise ValidationError(
                f"Row {idx} has a negative usage amount ({quantity}) on line item type "
                f"{line_item_type!r}.",
                details={"row": idx, "field": "line_item_usage_amount"},
            )

        effective_row_cost = net_unblended if net_unblended is not None else unblended

        if usage_start is not None and not (month_start <= usage_start <= month_end):
            outside_month_rows += 1
            records.append(
                _NormalizedRowRecord(
                    provider="aws",
                    source_scope=source_scope,
                    source_account_id=account_id,
                    source_resource_id=resource_id,
                    service_name=product_code or None,
                    sku_description=usage_type or None,
                    region=product_region_code,
                    usage_category="excluded",
                    raw_usage_unit=pricing_unit or None,
                    raw_quantity=quantity,
                    excluded_cost_usd=effective_row_cost,
                    excluded_reason="outsideSourceMonth",
                    usage_date=usage_start,
                    invoice_month=invoice_month or source_month,
                    currency=currency,
                )
            )
            continue

        if is_excluded_type:
            excluded_amt = _ZERO if lit_folded == "savingsplannegation" else effective_row_cost
            records.append(
                _NormalizedRowRecord(
                    provider="aws",
                    source_scope=source_scope,
                    source_account_id=account_id,
                    source_resource_id=resource_id,
                    service_name=product_code or None,
                    sku_description=usage_type or None,
                    region=product_region_code,
                    usage_category="excluded",
                    raw_usage_unit=pricing_unit or None,
                    raw_quantity=quantity,
                    excluded_cost_usd=excluded_amt,
                    excluded_reason=f"lineItemType:{line_item_type}",
                    usage_date=usage_start,
                    invoice_month=invoice_month or source_month,
                    currency=currency,
                )
            )
            continue

        if product_code and "bedrock" not in product_code.casefold():
            records.append(
                _NormalizedRowRecord(
                    provider="aws",
                    source_scope=source_scope,
                    source_account_id=account_id,
                    source_resource_id=resource_id,
                    service_name=product_code,
                    sku_description=usage_type or None,
                    region=product_region_code,
                    usage_category="excluded",
                    raw_usage_unit=pricing_unit or None,
                    raw_quantity=quantity,
                    excluded_cost_usd=effective_row_cost,
                    excluded_reason="nonModelService",
                    usage_date=usage_start,
                    invoice_month=invoice_month or source_month,
                    currency=currency,
                )
            )
            continue

        (
            publisher,
            model,
            region,
            service_tier,
            routing_mode,
            usage_category,
            is_unsupported_cap,
        ) = _parse_aws_bedrock_usage_type(
            usage_type, product_region_code, product_model, pricing_unit
        )

        norm_qty, norm_unit, unknown_unit, unit_unsupported_cap = _normalize_unit_scale(
            usage_category, pricing_unit, quantity
        )
        unsupported_cap = is_unsupported_cap or unit_unsupported_cap
        if unknown_unit or unsupported_cap:
            usage_category = "unsupported"

        eligible_cost = _ZERO
        amortized_cost = _ZERO
        unamortized_blocker = False

        if lit_folded == "savingsplancoveredusage":
            chosen_sp = net_sp_eff if net_sp_eff is not None else sp_eff
            if chosen_sp is None or (chosen_sp == _ZERO and quantity > _ZERO):
                unamortized_blocker = True
                issues.append(
                    BillingReviewIssue(
                        code="unamortizedPrepaidUsage",
                        severity="blocker",
                        message=(
                            f"Row {idx} ({usage_type}) is SavingsPlanCoveredUsage without a "
                            "non-zero SavingsPlanEffectiveCost."
                        ),
                        model=model,
                        region=region,
                        source_resource_id=resource_id,
                        details={"row": idx, "lineItemType": line_item_type},
                    )
                )
            else:
                amortized_cost = chosen_sp
        elif lit_folded == "discountedusage":
            if res_eff is None or (res_eff == _ZERO and quantity > _ZERO):
                unamortized_blocker = True
                issues.append(
                    BillingReviewIssue(
                        code="unamortizedPrepaidUsage",
                        severity="blocker",
                        message=(
                            f"Row {idx} ({usage_type}) is DiscountedUsage without a "
                            "non-zero reservation/EffectiveCost."
                        ),
                        model=model,
                        region=region,
                        source_resource_id=resource_id,
                        details={"row": idx, "lineItemType": line_item_type},
                    )
                )
            else:
                amortized_cost = res_eff
        else:
            eligible_cost = effective_row_cost

        if unsupported_cap:
            issues.append(
                BillingReviewIssue(
                    code="unsupportedCapacityUnit",
                    severity="blocker",
                    message=(
                        f"Row {idx} ({usage_type}) uses AWS Bedrock Provisioned Throughput "
                        f"({pricing_unit!r}), which cannot be converted to Azure PTU-hours."
                    ),
                    model=model,
                    region=region,
                    source_resource_id=resource_id,
                    details={"row": idx, "unit": pricing_unit, "usageType": usage_type},
                )
            )
        elif unknown_unit:
            issues.append(
                BillingReviewIssue(
                    code="unknownUsageUnit",
                    severity="blocker",
                    message=(
                        f"Row {idx} ({usage_type}) uses unrecognized pricing unit {pricing_unit!r}."
                    ),
                    model=model,
                    region=region,
                    source_resource_id=resource_id,
                    details={"row": idx, "unit": pricing_unit},
                )
            )

        records.append(
            _NormalizedRowRecord(
                provider="aws",
                source_scope=source_scope,
                source_account_id=account_id,
                source_resource_id=resource_id,
                source_resource_name=operation,
                service_name=product_code or "AmazonBedrock",
                sku_description=usage_type or None,
                publisher=publisher,
                model=model,
                region=region,
                deployment_type="Batch" if service_tier == "Batch" else service_tier,
                service_tier=service_tier,
                routing_mode=routing_mode,
                usage_category=usage_category,
                raw_usage_unit=pricing_unit or None,
                raw_quantity=quantity,
                normalized_quantity=norm_qty,
                normalized_unit=norm_unit,
                eligible_cost_usd=eligible_cost,
                amortized_benefit_cost_usd=amortized_cost,
                usage_date=usage_start,
                invoice_month=invoice_month or source_month,
                currency=currency,
                unamortized_prepaid_blocker=unamortized_blocker,
                unknown_unit_blocker=unknown_unit,
                unsupported_capacity_blocker=unsupported_cap,
            )
        )

    if outside_month_rows > 0:
        issues.append(
            BillingReviewIssue(
                code="outsideSourceMonth",
                severity="warning",
                message=(
                    f"Excluded {outside_month_rows} CUR row(s) whose usage_start_date fell "
                    f"outside {source_month}."
                ),
                details={"excludedRows": outside_month_rows, "sourceMonth": source_month},
            )
        )
    return records, issues


def _parse_gcp_credits(
    row: Mapping[str, Any], *, row_number: int
) -> list[tuple[str, Decimal]]:
    """Extract ``(credit_type, signed_amount)`` pairs from nested or flattened GCP row fields."""

    raw_credits = _get_ci(row, "credits")
    parsed_list: list[tuple[str, Decimal]] = []

    if isinstance(raw_credits, list):
        for item in raw_credits:
            if isinstance(item, Mapping):
                c_type = str(_get_ci(item, "type", "name", "id") or "DISCOUNT").strip().upper()
                c_amt = _parse_decimal(
                    _get_ci(item, "amount"),
                    field_name="credits.amount",
                    row_number=row_number,
                    required=False,
                )
                if c_amt != _ZERO:
                    parsed_list.append((c_type, c_amt))
        return parsed_list

    if isinstance(raw_credits, str) and raw_credits.strip():
        text = raw_credits.strip()
        if text.startswith("["):
            try:
                loaded = json.loads(text)
                if isinstance(loaded, list):
                    for item in loaded:
                        if isinstance(item, Mapping):
                            c_type = (
                                str(_get_ci(item, "type", "name", "id") or "DISCOUNT")
                                .strip()
                                .upper()
                            )
                            c_amt = _parse_decimal(
                                _get_ci(item, "amount"),
                                field_name="credits.amount",
                                row_number=row_number,
                                required=False,
                            )
                            if c_amt != _ZERO:
                                parsed_list.append((c_type, c_amt))
                    return parsed_list
            except Exception as exc:
                raise ValidationError(
                    f"Row {row_number} has malformed JSON in credits column.",
                    details={"row": row_number, "field": "credits"},
                ) from exc

    flat_type = _get_nested_or_flat(row, "credits.type", "credits_type")
    flat_amt = _get_nested_or_flat(row, "credits.amount", "credits_amount")
    if flat_amt is not None:
        c_amt = _parse_decimal(
            flat_amt, field_name="credits.amount", row_number=row_number, required=False
        )
        if c_amt != _ZERO:
            c_type = str(flat_type or "DISCOUNT").strip().upper()
            parsed_list.append((c_type, c_amt))
    return parsed_list


def _normalize_gcp_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    source_month: str,
    source_scope: str | None,
) -> tuple[list[_NormalizedRowRecord], list[BillingReviewIssue]]:
    first_row = rows[0]
    has_sku = (
        _get_nested_or_flat(first_row, "sku.description", "sku_description") is not None
        or "sku" in first_row
        or "sku.description" in first_row
        or "sku_description" in first_row
    )
    has_cost = "cost" in {str(k).casefold() for k in first_row}
    has_qty = (
        _get_nested_or_flat(
            first_row, "usage.amount_in_pricing_units", "usage_amount_in_pricing_units"
        )
        is not None
        or _get_nested_or_flat(first_row, "usage.amount", "usage_amount") is not None
        or "usage" in first_row
    )
    if not (has_sku and has_cost and has_qty):
        raise ValidationError(
            "Unsupported GCP Cloud Billing export schema: rows must include sku.description, "
            "usage.amount_in_pricing_units (or usage.amount), usage.pricing_unit, cost, "
            "and currency.",
            details={"provider": "gcp"},
        )

    month_start, month_end = parse_source_month(source_month)
    records: list[_NormalizedRowRecord] = []
    issues: list[BillingReviewIssue] = []
    outside_month_rows = 0
    one_off_credit_total = _ZERO
    observed_invoice_months: set[str] = set()

    for idx, row in enumerate(rows, start=1):
        currency = _enforce_usd_currency(_get_ci(row, "currency"), row_number=idx)
        billing_account_id = str(_get_ci(row, "billing_account_id") or "").strip() or None
        project_id = str(
            _get_nested_or_flat(row, "project.id", "project_id") or ""
        ).strip() or None
        service_desc = str(
            _get_nested_or_flat(row, "service.description", "service_description") or ""
        ).strip()
        sku_id = str(_get_nested_or_flat(row, "sku.id", "sku_id") or "").strip() or None
        sku_desc = str(
            _get_nested_or_flat(row, "sku.description", "sku_description") or ""
        ).strip()
        usage_start = _parse_usage_date(_get_ci(row, "usage_start_time"))
        invoice_month = _normalize_invoice_month(
            _get_nested_or_flat(row, "invoice.month", "invoice_month")
        )
        if invoice_month:
            observed_invoice_months.add(invoice_month)

        qty_raw = _get_nested_or_flat(
            row, "usage.amount_in_pricing_units", "usage_amount_in_pricing_units"
        )
        unit_raw = _get_nested_or_flat(row, "usage.pricing_unit", "usage_pricing_unit")
        if qty_raw is None:
            qty_raw = _get_nested_or_flat(row, "usage.amount", "usage_amount")
        if unit_raw is None:
            unit_raw = _get_nested_or_flat(row, "usage.unit", "usage_unit")

        quantity = _parse_decimal(
            qty_raw,
            field_name="usage.amount_in_pricing_units",
            row_number=idx,
            required=True,
        )
        pricing_unit = str(unit_raw or "").strip()
        cost = _parse_decimal(
            _get_ci(row, "cost"),
            field_name="cost",
            row_number=idx,
            required=True,
        )
        if _get_nested_or_flat(row, "price.effective_price", "price_effective_price") is not None:
            _parse_decimal(
                _get_nested_or_flat(row, "price.effective_price", "price_effective_price"),
                field_name="price.effective_price",
                row_number=idx,
            )

        cost_type = str(_get_ci(row, "cost_type") or "regular").strip()
        cost_type_folded = cost_type.casefold()
        is_tax_or_adj = cost_type_folded in {"tax", "rounding_error", "roundingerror", "adjustment"}
        if quantity < _ZERO and not is_tax_or_adj:
            raise ValidationError(
                f"Row {idx} has a negative usage quantity ({quantity}) on cost_type {cost_type!r}.",
                details={"row": idx, "field": "usage.amount_in_pricing_units"},
            )

        region = str(
            _get_nested_or_flat(row, "location.region", "location_region")
            or _get_nested_or_flat(row, "location.location", "location_location")
            or ""
        ).strip() or None
        resource_name = str(
            _get_nested_or_flat(row, "resource.name", "resource_name")
            or _get_nested_or_flat(row, "resource.global_name", "resource_global_name")
            or ""
        ).strip() or None

        if usage_start is not None and not (month_start <= usage_start <= month_end):
            outside_month_rows += 1
            records.append(
                _NormalizedRowRecord(
                    provider="gcp",
                    source_scope=source_scope or billing_account_id,
                    source_account_id=project_id or billing_account_id,
                    source_resource_id=resource_name,
                    service_name=service_desc or None,
                    sku_id=sku_id,
                    sku_description=sku_desc or None,
                    region=region,
                    usage_category="excluded",
                    raw_usage_unit=pricing_unit or None,
                    raw_quantity=quantity,
                    excluded_cost_usd=cost,
                    excluded_reason="outsideSourceMonth",
                    usage_date=usage_start,
                    invoice_month=invoice_month or source_month,
                    currency=currency,
                )
            )
            continue

        if is_tax_or_adj:
            records.append(
                _NormalizedRowRecord(
                    provider="gcp",
                    source_scope=source_scope or billing_account_id,
                    source_account_id=project_id or billing_account_id,
                    source_resource_id=resource_name,
                    service_name=service_desc or None,
                    sku_id=sku_id,
                    sku_description=sku_desc or None,
                    region=region,
                    usage_category="excluded",
                    raw_usage_unit=pricing_unit or None,
                    raw_quantity=quantity,
                    excluded_cost_usd=cost,
                    excluded_reason=f"costType:{cost_type}",
                    usage_date=usage_start,
                    invoice_month=invoice_month or source_month,
                    currency=currency,
                )
            )
            continue

        combined_srv = f"{service_desc} {sku_desc}".casefold()
        if not any(
            ai_kw in combined_srv
            for ai_kw in (
                "vertex",
                "gemini",
                "claude",
                "anthropic",
                "llama",
                "mistral",
                "embedding",
                "gecko",
                "ai platform",
                "token",
                "gsu",
            )
        ):
            records.append(
                _NormalizedRowRecord(
                    provider="gcp",
                    source_scope=source_scope or billing_account_id,
                    source_account_id=project_id or billing_account_id,
                    source_resource_id=resource_name,
                    service_name=service_desc or None,
                    sku_id=sku_id,
                    sku_description=sku_desc or None,
                    region=region,
                    usage_category="excluded",
                    raw_usage_unit=pricing_unit or None,
                    raw_quantity=quantity,
                    excluded_cost_usd=cost,
                    excluded_reason="nonModelService",
                    usage_date=usage_start,
                    invoice_month=invoice_month or source_month,
                    currency=currency,
                )
            )
            continue

        publisher, model, service_tier, usage_category, is_unsupported_cap = (
            _parse_gcp_sku_and_model(sku_desc, service_desc, pricing_unit, resource_name)
        )
        norm_qty, norm_unit, unknown_unit, unit_unsupported_cap = _normalize_unit_scale(
            usage_category, pricing_unit, quantity
        )
        unsupported_cap = is_unsupported_cap or unit_unsupported_cap
        if unknown_unit or unsupported_cap:
            usage_category = "unsupported"

        credits_list = _parse_gcp_credits(row, row_number=idx)
        recurring_credit_dec = _ZERO
        excluded_credit_dec = _ZERO
        row_notes: list[str] = []
        for c_type, c_amt in credits_list:
            # Ensure credit amount carries negative sign when reducing cost
            signed_amt = -abs(c_amt) if c_amt > _ZERO else c_amt
            if c_type in _GCP_RECURRING_CREDIT_TYPES:
                recurring_credit_dec += signed_amt
            else:
                # Exclude one-off promotions, free tier, sustained usage discounts, etc.
                excluded_credit_dec += abs(signed_amt)
                one_off_credit_total += abs(signed_amt)
                row_notes.append(f"Excluded one-off credit {c_type} ({signed_amt})")

        if unsupported_cap:
            issues.append(
                BillingReviewIssue(
                    code="unsupportedCapacityUnit",
                    severity="blocker",
                    message=(
                        f"Row {idx} ({sku_desc}) uses Vertex AI Provisioned Throughput / GSU "
                        f"({pricing_unit!r}), which cannot be converted to Azure PTU-hours."
                    ),
                    model=model,
                    region=region,
                    source_resource_id=resource_name,
                    details={"row": idx, "unit": pricing_unit, "skuDescription": sku_desc},
                )
            )
        elif unknown_unit:
            issues.append(
                BillingReviewIssue(
                    code="unknownUsageUnit",
                    severity="blocker",
                    message=(
                        f"Row {idx} ({sku_desc}) uses unrecognized pricing unit {pricing_unit!r}."
                    ),
                    model=model,
                    region=region,
                    source_resource_id=resource_name,
                    details={"row": idx, "unit": pricing_unit},
                )
            )

        records.append(
            _NormalizedRowRecord(
                provider="gcp",
                source_scope=source_scope or billing_account_id,
                source_account_id=project_id or billing_account_id,
                source_resource_id=resource_name,
                source_resource_name=resource_name,
                service_name=service_desc or "Vertex AI",
                sku_id=sku_id,
                sku_description=sku_desc or None,
                publisher=publisher,
                model=model,
                region=region,
                deployment_type=service_tier,
                service_tier=service_tier,
                routing_mode="global" if (region or "").casefold() == "global" else "in-region",
                usage_category=usage_category,
                raw_usage_unit=pricing_unit or None,
                raw_quantity=quantity,
                normalized_quantity=norm_qty,
                normalized_unit=norm_unit,
                eligible_cost_usd=cost,
                amortized_benefit_cost_usd=recurring_credit_dec,
                excluded_cost_usd=excluded_credit_dec,
                excluded_reason="oneOffCredit" if excluded_credit_dec > _ZERO else None,
                usage_date=usage_start,
                invoice_month=invoice_month or source_month,
                currency=currency,
                notes=row_notes,
                unknown_unit_blocker=unknown_unit,
                unsupported_capacity_blocker=unsupported_cap,
            )
        )

    if one_off_credit_total > _ZERO:
        issues.append(
            BillingReviewIssue(
                code="oneOffCreditsExcluded",
                severity="info",
                message=(
                    f"Excluded ${float(one_off_credit_total.quantize(_COST_QUANT)):.4f} in one-off "
                    "promotional or free-tier credits so temporary credits do not depress "
                    "recurring unit rates."
                ),
                details={"excludedCreditsUsd": float(one_off_credit_total.quantize(_COST_QUANT))},
            )
        )

    if outside_month_rows > 0:
        issues.append(
            BillingReviewIssue(
                code="outsideSourceMonth",
                severity="warning",
                message=(
                    f"Excluded {outside_month_rows} GCP row(s) whose usage_start_time fell "
                    f"outside {source_month}."
                ),
                details={"excludedRows": outside_month_rows, "sourceMonth": source_month},
            )
        )

    lagged_months = sorted(m for m in observed_invoice_months if m != source_month)
    issues.append(
        BillingReviewIssue(
            code="late_adjustments_note",
            severity="warning" if lagged_months else "info",
            message=(
                "GCP Cloud Billing filters observation windows by usage_start_time while "
                "recording invoice.month separately; late adjustments can appear on a "
                "subsequent invoice month."
                + (
                    f" Observed invoice month(s) differing from {source_month}: "
                    f"{', '.join(lagged_months)}."
                    if lagged_months
                    else ""
                )
            ),
            details={
                "sourceMonth": source_month,
                "invoiceMonths": sorted(observed_invoice_months),
                "laggedInvoiceMonths": lagged_months,
            },
        )
    )
    return records, issues


def _normalize_mosaic_template_rows(
    headers: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
    *,
    source_month: str,
    source_scope: str | None,
) -> tuple[list[_NormalizedRowRecord], list[BillingReviewIssue]]:
    required = {
        "schema_version",
        "provider",
        "pricing_cloud",
        "source_month",
        "model",
        "usage_category",
        "usage_quantity",
        "usage_unit",
        "currency",
        "eligible_cost_usd",
    }
    header_set = {h.strip().casefold() for h in headers}
    missing = sorted(required - header_set)
    if missing:
        raise ValidationError(
            f"Unsupported MOSAIC billing template: missing required column(s): "
            f"{', '.join(missing)}.",
            details={"missingColumns": missing, "provider": "mosaicTemplate"},
        )

    valid_categories: set[str] = {
        "inputTokens",
        "cachedInputTokens",
        "cacheWriteTokens",
        "outputTokens",
        "ptuHours",
        "monthlyCommitment",
        "unsupported",
        "excluded",
    }
    records: list[_NormalizedRowRecord] = []
    issues: list[BillingReviewIssue] = []

    for idx, row in enumerate(rows, start=1):
        schema_ver = str(_get_ci(row, "schema_version") or "").strip()
        if schema_ver != MOSAIC_TEMPLATE_SCHEMA_VERSION:
            raise ValidationError(
                f"Row {idx} has unsupported schema_version {schema_ver!r}; "
                f"expected {MOSAIC_TEMPLATE_SCHEMA_VERSION!r}.",
                details={"row": idx, "schemaVersion": schema_ver},
            )
        currency = _enforce_usd_currency(_get_ci(row, "currency"), row_number=idx)
        row_month = str(_get_ci(row, "source_month") or "").strip()
        if row_month != source_month:
            raise ValidationError(
                f"Row {idx} has source_month {row_month!r}, which does not match target month "
                f"{source_month!r}.",
                details={"row": idx, "rowSourceMonth": row_month, "sourceMonth": source_month},
            )
        raw_cat = str(_get_ci(row, "usage_category") or "").strip()
        if raw_cat not in valid_categories:
            raise ValidationError(
                f"Row {idx} has unsupported usage_category {raw_cat!r}.",
                details={"row": idx, "usageCategory": raw_cat},
            )
        usage_category: BillingUsageCategory = raw_cat  # type: ignore[assignment]

        quantity = _parse_decimal(
            _get_ci(row, "usage_quantity"),
            field_name="usage_quantity",
            row_number=idx,
            required=True,
        )
        if quantity < _ZERO and usage_category != "excluded":
            raise ValidationError(
                f"Row {idx} has a negative usage_quantity ({quantity}).",
                details={"row": idx, "field": "usage_quantity"},
            )
        usage_unit = str(_get_ci(row, "usage_unit") or "").strip()
        eligible_cost = _parse_decimal(
            _get_ci(row, "eligible_cost_usd"),
            field_name="eligible_cost_usd",
            row_number=idx,
            required=True,
        )
        amortized_cost = _parse_decimal(
            _get_ci(row, "amortized_benefit_cost_usd"),
            field_name="amortized_benefit_cost_usd",
            row_number=idx,
            required=False,
        )
        excluded_cost = _parse_decimal(
            _get_ci(row, "excluded_cost_usd"),
            field_name="excluded_cost_usd",
            row_number=idx,
            required=False,
        )
        excluded_reason = str(_get_ci(row, "excluded_reason") or "").strip() or None
        row_provider_raw = str(_get_ci(row, "provider") or "mosaicTemplate").strip().casefold()
        model = str(_get_ci(row, "model") or "").strip() or None
        model_version = str(_get_ci(row, "model_version") or "").strip() or None
        publisher = str(_get_ci(row, "publisher") or "").strip() or _infer_publisher("", model)
        deployment_type = canonical_deployment_type(
            str(_get_ci(row, "deployment_type") or "").strip() or None
        )
        raw_region = str(_get_ci(row, "region") or "").strip() or None
        region = (
            normalize_region(raw_region)
            if row_provider_raw == "azure" and raw_region
            else raw_region
        )
        service_tier = str(_get_ci(row, "service_tier") or "").strip() or None
        routing_mode = str(_get_ci(row, "routing_mode") or "").strip() or None
        source_account_id = str(_get_ci(row, "source_account_id") or "").strip() or None
        source_resource_id = str(_get_ci(row, "source_resource_id") or "").strip() or None
        source_resource_name = str(_get_ci(row, "source_resource_name") or "").strip() or None
        invoice_month = (
            _normalize_invoice_month(_get_ci(row, "invoice_month")) or source_month
        )

        norm_qty, norm_unit, unknown_unit, unsupported_cap = _normalize_unit_scale(
            usage_category, usage_unit, quantity
        )
        if unknown_unit or unsupported_cap:
            usage_category = "unsupported"
        if unsupported_cap:
            issues.append(
                BillingReviewIssue(
                    code="unsupportedCapacityUnit",
                    severity="blocker",
                    message=f"Row {idx} uses unsupported capacity unit {usage_unit!r}.",
                    model=model,
                    region=region,
                    source_resource_id=source_resource_id,
                    details={"row": idx, "unit": usage_unit},
                )
            )
        elif unknown_unit:
            issues.append(
                BillingReviewIssue(
                    code="unknownUsageUnit",
                    severity="blocker",
                    message=f"Row {idx} uses unrecognized usage_unit {usage_unit!r}.",
                    model=model,
                    region=region,
                    source_resource_id=source_resource_id,
                    details={"row": idx, "unit": usage_unit},
                )
            )

        records.append(
            _NormalizedRowRecord(
                provider="mosaicTemplate",
                source_scope=source_scope,
                source_account_id=source_account_id,
                source_resource_id=source_resource_id,
                source_resource_name=source_resource_name,
                publisher=publisher,
                model=model,
                model_version=model_version,
                region=region,
                deployment_type=deployment_type,
                service_tier=service_tier,
                routing_mode=routing_mode,
                usage_category=usage_category,
                raw_usage_unit=usage_unit or None,
                raw_quantity=quantity,
                normalized_quantity=norm_qty,
                normalized_unit=norm_unit,
                eligible_cost_usd=eligible_cost,
                amortized_benefit_cost_usd=amortized_cost,
                excluded_cost_usd=excluded_cost,
                excluded_reason=excluded_reason,
                invoice_month=invoice_month,
                currency=currency,
                unknown_unit_blocker=unknown_unit,
                unsupported_capacity_blocker=unsupported_cap,
            )
        )

    return records, issues


def _match_deployment_and_existing_price(
    candidate: BillingCandidateRate,
    *,
    deployments: Mapping[str, DeploymentFacts],
    book: PriceBook,
    today: date,
) -> tuple[BillingCandidateRate, list[BillingReviewIssue]]:
    """Resolve conservative deployment binding suggestions and compare against existing prices."""

    issues: list[BillingReviewIssue] = []
    cand_is_prov = (
        candidate.ptu_hourly is not None
        or candidate.monthly_amount is not None
        or is_provisioned(candidate.deployment_type)
    )
    cand_model_folded = candidate.model.casefold()
    cand_account = _extract_azure_account_name(
        candidate.source_resource_id, candidate.source_resource_name
    )

    # Find deployments in this cloud that serve the candidate's model and kind
    compatible: list[DeploymentFacts] = []
    resource_matched: list[DeploymentFacts] = []

    for facts in deployments.values():
        if facts.provisioned != cand_is_prov:
            continue
        if facts.cloud and facts.cloud.casefold() != candidate.pricing_cloud.casefold():
            continue
        if candidate.model != "*" and (
            not facts.model or facts.model.casefold() != cand_model_folded
        ):
            continue
        if (
            candidate.version
            and facts.version
            and candidate.version.casefold() != facts.version.casefold()
        ):
            continue
        if (
            candidate.deployment_type
            and facts.deployment_type
            and canonical_deployment_type(candidate.deployment_type)
            != canonical_deployment_type(facts.deployment_type)
        ):
            continue
        if (
            candidate.region
            and facts.region
            and candidate.region.casefold() != facts.region.casefold()
        ):
            continue

        compatible.append(facts)

        # Check explicit resource / account identity match
        res_match = False
        if candidate.source_resource_id and facts.azure_resource_id:
            src_res = candidate.source_resource_id.rstrip("/").casefold()
            dep_res = facts.azure_resource_id.rstrip("/").casefold()
            if src_res == dep_res or src_res.startswith(dep_res + "/"):
                res_match = True
        if not res_match and cand_account and facts.account_name:
            if cand_account.casefold() == facts.account_name.casefold():
                res_match = True
        if not res_match and candidate.source_resource_id:
            if candidate.source_resource_id.casefold() == facts.key.casefold():
                res_match = True
        if not res_match and candidate.source_resource_name:
            if candidate.source_resource_name.casefold() in {
                facts.deployment_name.casefold(),
                facts.endpoint_name.casefold(),
                facts.key.casefold(),
            }:
                res_match = True
        if res_match:
            resource_matched.append(facts)

    suggested_scope: BillingMappingScope = "cloudModel" if candidate.publishable else "excluded"
    suggested_deployment_key: str | None = None
    ambiguous_keys: list[str] = []
    confidence: BillingMappingConfidence = "unmatched"
    matched_facts_for_price: DeploymentFacts | None = None

    if len(resource_matched) == 1:
        only = resource_matched[0]
        confidence = "exact"
        suggested_scope = "deployment"
        suggested_deployment_key = only.key
        matched_facts_for_price = only
    elif len(resource_matched) > 1:
        confidence = "ambiguous"
        suggested_scope = "deployment"
        ambiguous_keys = sorted(item.key for item in resource_matched)
        matched_facts_for_price = resource_matched[0]
        warn_msg = (
            f"Multiple deployments on the same resource match {candidate.model}: "
            f"{', '.join(ambiguous_keys)}. Confirm which deployment this rate applies to."
        )
        candidate.warnings.append(warn_msg)
        issues.append(
            BillingReviewIssue(
                code="ambiguousDeploymentMapping",
                severity="warning",
                message=warn_msg,
                model=candidate.model,
                region=candidate.region,
                source_resource_id=candidate.source_resource_id,
                details={"ambiguousDeploymentKeys": ambiguous_keys},
            )
        )
    elif not candidate.source_resource_id and not cand_account:
        if len(compatible) == 1:
            confidence = "suggested"
            suggested_scope = "deployment"
            suggested_deployment_key = compatible[0].key
            matched_facts_for_price = compatible[0]
        elif len(compatible) > 1:
            confidence = "ambiguous"
            suggested_scope = "cloudModel"
            ambiguous_keys = sorted(item.key for item in compatible)
            matched_facts_for_price = compatible[0]
            warn_msg = (
                f"Multiple deployments in {candidate.pricing_cloud} match {candidate.model}: "
                f"{', '.join(ambiguous_keys)}."
            )
            candidate.warnings.append(warn_msg)
            issues.append(
                BillingReviewIssue(
                    code="ambiguousDeploymentMapping",
                    severity="warning",
                    message=warn_msg,
                    model=candidate.model,
                    region=candidate.region,
                    details={"ambiguousDeploymentKeys": ambiguous_keys},
                )
            )
    else:
        # Candidate names a specific resource/account that didn't match any registered endpoint's
        # resource/account. Check if compatible endpoints exist without resource identity.
        unanchored = [
            f for f in compatible if not f.azure_resource_id and not f.account_name
        ]
        if len(unanchored) == 1 and len(compatible) == 1:
            confidence = "suggested"
            suggested_scope = "deployment"
            suggested_deployment_key = unanchored[0].key
            matched_facts_for_price = unanchored[0]

    # Compare against existing applicable price in PriceBook
    existing_entry: PriceEntry | None = None
    if matched_facts_for_price is not None:
        match = book.match(matched_facts_for_price, today)
        if isinstance(match, PriceMatch):
            existing_entry = match.entry

    if existing_entry is None:
        probe_region = (
            normalize_region(candidate.region)
            if candidate.provider == "azure"
            else candidate.region
        )
        probe_type = candidate.deployment_type or (
            "ProvisionedManaged" if cand_is_prov else "GlobalStandard"
        )
        probe_facts = DeploymentFacts(
            key=suggested_deployment_key or "__probe__/default",
            endpoint_id="__probe__",
            endpoint_name="Probe",
            deployment_name="default",
            provider=candidate.provider,
            cloud=candidate.pricing_cloud,
            cloud_source="override",
            detected_cloud=candidate.pricing_cloud,
            region=probe_region,
            publisher=candidate.publisher,
            model=candidate.model if candidate.model != "*" else "gpt-4o",
            version=candidate.version,
            deployment_type=probe_type,
            deployment_type_source="admin",
            capacity=100 if cand_is_prov else None,
        )
        probe_match = book.match(probe_facts, today)
        if isinstance(probe_match, PriceMatch):
            existing_entry = probe_match.entry

    conflicts_manual = existing_entry is not None and existing_entry.origin == "admin"
    if conflicts_manual and existing_entry is not None:
        conflict_msg = (
            f"Candidate rate for {candidate.model} overlaps an administrator manual price "
            f"({existing_entry.id}); replacing it requires explicit confirmation."
        )
        candidate.warnings.append(conflict_msg)
        issues.append(
            BillingReviewIssue(
                code="manualOverrideConflict",
                severity="warning",
                message=conflict_msg,
                model=candidate.model,
                region=candidate.region,
                source_resource_id=candidate.source_resource_id,
                details={"existingPriceId": existing_entry.id},
            )
        )

    updated = candidate.model_copy(
        update={
            "suggested_scope": suggested_scope if candidate.publishable else "excluded",
            "suggested_deployment_key": suggested_deployment_key,
            "ambiguous_deployment_keys": ambiguous_keys,
            "mapping_confidence": confidence,
            "existing_price_id": existing_entry.id if existing_entry else None,
            "existing_line_id": existing_entry.line_id if existing_entry else None,
            "existing_origin": existing_entry.origin if existing_entry else None,
            "existing_input_per_million": (
                existing_entry.input_per_million if existing_entry else None
            ),
            "existing_cached_input_per_million": (
                existing_entry.cached_input_per_million if existing_entry else None
            ),
            "existing_output_per_million": (
                existing_entry.output_per_million if existing_entry else None
            ),
            "existing_ptu_hourly": existing_entry.ptu_hourly if existing_entry else None,
            "existing_monthly_amount": (
                existing_entry.monthly_amount if existing_entry else None
            ),
            "conflicts_with_manual_override": conflicts_manual,
        }
    )
    return updated, issues


def _aggregate_evidence_and_derive_candidates(
    records: Sequence[_NormalizedRowRecord],
    *,
    provider: BillingProvider,
    pricing_cloud: str,
    source_month: str,
    deployments: Mapping[str, DeploymentFacts],
    book: PriceBook,
    today: date,
) -> tuple[
    list[BillingNormalizedEvidenceItem],
    list[BillingCandidateRate],
    list[BillingCandidateMapping],
    list[BillingReviewIssue],
    int,
    int,
    Decimal,
    Decimal,
    list[str],
]:
    issues: list[BillingReviewIssue] = []
    eligible_row_count = 0
    excluded_row_count = 0
    total_eligible_cost = _ZERO
    total_excluded_cost = _ZERO
    invoice_months: set[str] = set()

    # Group normalized evidence items by deterministic key
    evidence_groups: dict[str, BillingNormalizedEvidenceItem] = {}
    evidence_decimals: dict[str, tuple[Decimal, Decimal, Decimal, Decimal, Decimal]] = {}

    # Group candidate accumulators by distinct resource/model/version/region/tier/routing/kind
    @dataclass
    class _CandidateAcc:
        provider: BillingProvider
        pricing_cloud: str
        source_scope: str | None
        source_account_id: str | None
        source_resource_id: str | None
        source_resource_name: str | None
        publisher: str | None
        model: str
        version: str | None
        deployment_type: str | None
        region: str | None
        service_tier: str | None
        routing_mode: str | None
        kind: str  # "tokens" | "provisioned" | "unsupported"
        qty_by_cat: dict[BillingUsageCategory, Decimal] = field(
            default_factory=lambda: defaultdict(lambda: _ZERO)
        )
        cost_by_cat: dict[BillingUsageCategory, Decimal] = field(
            default_factory=lambda: defaultdict(lambda: _ZERO)
        )
        rows_by_cat: dict[BillingUsageCategory, int] = field(
            default_factory=lambda: defaultdict(int)
        )
        excluded_cost: Decimal = _ZERO
        evidence_keys: list[str] = field(default_factory=list)
        unamortized_blocker: bool = False
        unknown_unit_blocker: bool = False
        unsupported_cap_blocker: bool = False

    cand_groups: dict[tuple[str, ...], _CandidateAcc] = {}

    for rec in records:
        if rec.invoice_month:
            invoice_months.add(rec.invoice_month)

        effective_line_cost = rec.eligible_cost_usd + rec.amortized_benefit_cost_usd
        if rec.usage_category == "excluded":
            excluded_row_count += 1
            total_excluded_cost += rec.excluded_cost_usd
        else:
            eligible_row_count += 1
            total_eligible_cost += effective_line_cost
            total_excluded_cost += rec.excluded_cost_usd

        line_key = deterministic_id(
            "evline",
            rec.provider,
            rec.source_account_id or "",
            rec.source_resource_id or "",
            rec.service_name or "",
            rec.meter_id or "",
            rec.sku_id or "",
            rec.sku_description or "",
            rec.publisher or "",
            rec.model or "",
            rec.model_version or "",
            rec.region or "",
            rec.deployment_type or "",
            rec.service_tier or "",
            rec.routing_mode or "",
            rec.usage_category,
            rec.raw_usage_unit or "",
            rec.excluded_reason or "",
            rec.invoice_month or "",
        )

        if line_key not in evidence_groups:
            evidence_groups[line_key] = BillingNormalizedEvidenceItem(
                line_key=line_key,
                provider=rec.provider,
                source_scope=rec.source_scope,
                source_account_id=rec.source_account_id,
                source_resource_id=rec.source_resource_id,
                source_resource_name=rec.source_resource_name,
                service_name=rec.service_name,
                meter_id=rec.meter_id,
                sku_id=rec.sku_id,
                sku_description=rec.sku_description,
                publisher=rec.publisher,
                model=rec.model,
                model_version=rec.model_version,
                region=rec.region,
                deployment_type=rec.deployment_type,
                service_tier=rec.service_tier,
                routing_mode=rec.routing_mode,
                usage_category=rec.usage_category,
                raw_usage_unit=rec.raw_usage_unit,
                normalized_unit=rec.normalized_unit,
                excluded_reason=rec.excluded_reason,
                row_count=1,
                usage_start=rec.usage_date,
                usage_end=rec.usage_date,
                invoice_month=rec.invoice_month,
                currency=rec.currency,
                notes=list(rec.notes),
            )
            evidence_decimals[line_key] = (
                rec.raw_quantity,
                rec.normalized_quantity,
                rec.eligible_cost_usd,
                rec.amortized_benefit_cost_usd,
                rec.excluded_cost_usd,
            )
        else:
            ev = evidence_groups[line_key]
            ev.row_count += 1
            if rec.usage_date is not None:
                if ev.usage_start is None or rec.usage_date < ev.usage_start:
                    ev.usage_start = rec.usage_date
                if ev.usage_end is None or rec.usage_date > ev.usage_end:
                    ev.usage_end = rec.usage_date
            for note in rec.notes:
                if note not in ev.notes:
                    ev.notes.append(note)
            rq, nq, ec, ac, xc = evidence_decimals[line_key]
            evidence_decimals[line_key] = (
                rq + rec.raw_quantity,
                nq + rec.normalized_quantity,
                ec + rec.eligible_cost_usd,
                ac + rec.amortized_benefit_cost_usd,
                xc + rec.excluded_cost_usd,
            )

        if rec.usage_category == "excluded" or not rec.model:
            continue

        cand_kind = (
            "provisioned"
            if rec.usage_category in ("ptuHours", "monthlyCommitment")
            else "unsupported"
            if rec.usage_category == "unsupported"
            else "tokens"
        )
        group_key: tuple[str, ...] = (
            str(rec.provider),
            pricing_cloud,
            (rec.source_account_id or "").casefold(),
            (rec.source_resource_id or "").casefold(),
            (rec.publisher or "").casefold(),
            rec.model.casefold(),
            (rec.model_version or "").casefold(),
            (rec.deployment_type or "").casefold(),
            (rec.region or "").casefold(),
            (rec.service_tier or "").casefold(),
            (rec.routing_mode or "").casefold(),
            cand_kind,
        )
        acc = cand_groups.get(group_key)
        if acc is None:
            acc = _CandidateAcc(
                provider=rec.provider,
                pricing_cloud=pricing_cloud,
                source_scope=rec.source_scope,
                source_account_id=rec.source_account_id,
                source_resource_id=rec.source_resource_id,
                source_resource_name=rec.source_resource_name,
                publisher=rec.publisher,
                model=rec.model,
                version=rec.model_version,
                deployment_type=rec.deployment_type,
                region=rec.region,
                service_tier=rec.service_tier,
                routing_mode=rec.routing_mode,
                kind=cand_kind,
            )
            cand_groups[group_key] = acc

        acc.qty_by_cat[rec.usage_category] += rec.normalized_quantity
        acc.cost_by_cat[rec.usage_category] += effective_line_cost
        acc.rows_by_cat[rec.usage_category] += 1
        acc.excluded_cost += rec.excluded_cost_usd
        if line_key not in acc.evidence_keys:
            acc.evidence_keys.append(line_key)
        acc.unamortized_blocker = acc.unamortized_blocker or rec.unamortized_prepaid_blocker
        acc.unknown_unit_blocker = acc.unknown_unit_blocker or rec.unknown_unit_blocker
        acc.unsupported_cap_blocker = (
            acc.unsupported_cap_blocker or rec.unsupported_capacity_blocker
        )

    # Finalize Decimal totals onto evidence items
    evidence_items: list[BillingNormalizedEvidenceItem] = []
    for line_key, ev in evidence_groups.items():
        rq, nq, ec, ac, xc = evidence_decimals[line_key]
        ev.raw_quantity = float(rq.quantize(_COST_QUANT, rounding=ROUND_HALF_UP))
        ev.normalized_quantity = float(nq.quantize(_COST_QUANT, rounding=ROUND_HALF_UP))
        ev.eligible_cost_usd = float(ec.quantize(_COST_QUANT, rounding=ROUND_HALF_UP))
        ev.amortized_benefit_cost_usd = float(ac.quantize(_COST_QUANT, rounding=ROUND_HALF_UP))
        ev.excluded_cost_usd = float(xc.quantize(_COST_QUANT, rounding=ROUND_HALF_UP))
        evidence_items.append(ev)

    candidates: list[BillingCandidateRate] = []
    mappings: list[BillingCandidateMapping] = []

    for group_key in sorted(cand_groups):
        acc = cand_groups[group_key]
        candidate_id = deterministic_id("cand", source_month, *group_key)

        in_tok = acc.qty_by_cat.get("inputTokens", _ZERO)
        in_cost = acc.cost_by_cat.get("inputTokens", _ZERO)
        in_rows = acc.rows_by_cat.get("inputTokens", 0)

        cached_tok = acc.qty_by_cat.get("cachedInputTokens", _ZERO)
        cached_cost = acc.cost_by_cat.get("cachedInputTokens", _ZERO)
        cached_rows = acc.rows_by_cat.get("cachedInputTokens", 0)

        cw_tok = acc.qty_by_cat.get("cacheWriteTokens", _ZERO)
        cw_cost = acc.cost_by_cat.get("cacheWriteTokens", _ZERO)
        cw_rows = acc.rows_by_cat.get("cacheWriteTokens", 0)

        out_tok = acc.qty_by_cat.get("outputTokens", _ZERO)
        out_cost = acc.cost_by_cat.get("outputTokens", _ZERO)
        out_rows = acc.rows_by_cat.get("outputTokens", 0)

        ptu_hrs = acc.qty_by_cat.get("ptuHours", _ZERO)
        ptu_cost = acc.cost_by_cat.get("ptuHours", _ZERO)
        ptu_rows = acc.rows_by_cat.get("ptuHours", 0)

        comm_mos = acc.qty_by_cat.get("monthlyCommitment", _ZERO)
        comm_cost = acc.cost_by_cat.get("monthlyCommitment", _ZERO)
        comm_rows = acc.rows_by_cat.get("monthlyCommitment", 0)

        unsup_cost = acc.cost_by_cat.get("unsupported", _ZERO)

        total_cand_cost = (
            in_cost + cached_cost + cw_cost + out_cost + ptu_cost + comm_cost + unsup_cost
        )

        input_per_million: float | None = None
        cached_input_per_million: float | None = None
        output_per_million: float | None = None
        ptu_hourly: float | None = None
        monthly_amount: float | None = None

        publishable = True
        unpublishable_reasons: list[str] = []
        warnings: list[str] = []

        if acc.unsupported_cap_blocker:
            publishable = False
            unpublishable_reasons.append(
                "Provisioned capacity unit is not supported by MOSAIC's PTU or monthly "
                "commitment model."
            )
        if acc.unknown_unit_blocker:
            publishable = False
            unpublishable_reasons.append(
                "Usage unit of measure could not be normalized to a supported token or PTU scale."
            )
        if acc.unamortized_blocker:
            publishable = False
            unpublishable_reasons.append(
                "Prepaid reservation or savings plan usage has $0 cash cost without amortized "
                "commitment cost."
            )

        # Check zero denominators where rows/cost exist
        for cat_label, rows_n, qty_dec in (
            ("input tokens", in_rows, in_tok),
            ("cached input tokens", cached_rows, cached_tok),
            ("output tokens", out_rows, out_tok),
            ("PTU hours", ptu_rows, ptu_hrs),
            ("commitment months", comm_rows, comm_mos),
        ):
            if rows_n > 0 and qty_dec <= _ZERO:
                publishable = False
                reason = f"Observed {cat_label} rows had a zero quantity denominator."
                unpublishable_reasons.append(reason)
                issues.append(
                    BillingReviewIssue(
                        code="zeroQuantityDenominator",
                        severity="blocker",
                        message=f"{acc.model}: {reason}",
                        model=acc.model,
                        region=acc.region,
                        source_resource_id=acc.source_resource_id,
                    )
                )

        if in_tok > _ZERO:
            rate_dec = (in_cost / (in_tok / _MILLION)).quantize(_RATE_QUANT, rounding=ROUND_HALF_UP)
            input_per_million = float(rate_dec)
        if cached_tok > _ZERO:
            rate_dec = (cached_cost / (cached_tok / _MILLION)).quantize(
                _RATE_QUANT, rounding=ROUND_HALF_UP
            )
            cached_input_per_million = float(rate_dec)
        if out_tok > _ZERO:
            rate_dec = (out_cost / (out_tok / _MILLION)).quantize(
                _RATE_QUANT, rounding=ROUND_HALF_UP
            )
            output_per_million = float(rate_dec)
        if ptu_hrs > _ZERO:
            rate_dec = (ptu_cost / ptu_hrs).quantize(_RATE_QUANT, rounding=ROUND_HALF_UP)
            ptu_hourly = float(rate_dec)
        if comm_mos > _ZERO:
            rate_dec = (comm_cost / comm_mos).quantize(_MONTH_QUANT, rounding=ROUND_HALF_UP)
            monthly_amount = float(rate_dec)

        if cached_rows > 0 or cw_rows > 0:
            cache_warn = (
                "Gateway telemetry does not separate cached prompt tokens from standard prompt "
                "tokens; cached input rates are recorded for reference and cache write costs "
                "are retained as evidence only."
            )
            warnings.append(cache_warn)
            issues.append(
                BillingReviewIssue(
                    code="cacheTokensObserved",
                    severity="warning",
                    message=f"{acc.model}: {cache_warn}",
                    model=acc.model,
                    region=acc.region,
                    source_resource_id=acc.source_resource_id,
                )
            )

        if acc.kind == "tokens":
            if input_per_million is None:
                publishable = False
                unpublishable_reasons.append(
                    "No standard input token usage was observed in the billing period; "
                    "MOSAIC requires an input token price."
                )
            elif output_per_million is None and not _is_embedding_model(acc.model):
                publishable = False
                partial_reason = (
                    "No output token usage was observed in the billing period; MOSAIC keeps "
                    "the existing price rather than replacing it with a partial rate."
                )
                unpublishable_reasons.append(partial_reason)
                issues.append(
                    BillingReviewIssue(
                        code="missingOutputTokens",
                        severity="warning",
                        message=f"{acc.model}: {partial_reason}",
                        model=acc.model,
                        region=acc.region,
                        source_resource_id=acc.source_resource_id,
                    )
                )
        elif acc.kind == "provisioned":
            if ptu_hourly is None and monthly_amount is None:
                publishable = False
                unpublishable_reasons.append(
                    "Provisioned capacity rows did not yield a valid PTU hourly or monthly rate."
                )
            elif monthly_amount is not None and not acc.source_resource_id:
                publishable = False
                unpublishable_reasons.append(
                    "A monthly commitment amount must be attributable to a specific "
                    "resource/deployment."
                )
        else:
            publishable = False
            if not unpublishable_reasons:
                unpublishable_reasons.append("Candidate uses an unsupported billing dimension.")

        if acc.routing_mode and acc.routing_mode not in ("in-region", "global", "data-zone"):
            warnings.append(f"Observed routing mode: {acc.routing_mode}.")
        if acc.service_tier and acc.service_tier not in ("Standard",):
            warnings.append(f"Observed service tier: {acc.service_tier}.")

        raw_candidate = BillingCandidateRate(
            candidate_id=candidate_id,
            provider=acc.provider,
            pricing_cloud=acc.pricing_cloud,
            source_scope=acc.source_scope,
            source_account_id=acc.source_account_id,
            source_resource_id=acc.source_resource_id,
            source_resource_name=acc.source_resource_name,
            publisher=acc.publisher,
            model=acc.model,
            version=acc.version,
            deployment_type=acc.deployment_type,
            region=acc.region,
            service_tier=acc.service_tier,
            routing_mode=acc.routing_mode,
            input_tokens=float(in_tok) if in_rows > 0 else None,
            cached_input_tokens=float(cached_tok) if cached_rows > 0 else None,
            cache_write_tokens=float(cw_tok) if cw_rows > 0 else None,
            output_tokens=float(out_tok) if out_rows > 0 else None,
            ptu_hours=float(ptu_hrs) if ptu_rows > 0 else None,
            commitment_months=float(comm_mos) if comm_rows > 0 else None,
            input_cost_usd=(
                float(in_cost.quantize(_COST_QUANT, rounding=ROUND_HALF_UP))
                if in_rows > 0
                else None
            ),
            cached_input_cost_usd=(
                float(cached_cost.quantize(_COST_QUANT, rounding=ROUND_HALF_UP))
                if cached_rows > 0
                else None
            ),
            cache_write_cost_usd=(
                float(cw_cost.quantize(_COST_QUANT, rounding=ROUND_HALF_UP))
                if cw_rows > 0
                else None
            ),
            output_cost_usd=(
                float(out_cost.quantize(_COST_QUANT, rounding=ROUND_HALF_UP))
                if out_rows > 0
                else None
            ),
            ptu_cost_usd=(
                float(ptu_cost.quantize(_COST_QUANT, rounding=ROUND_HALF_UP))
                if ptu_rows > 0
                else None
            ),
            monthly_commitment_cost_usd=(
                float(comm_cost.quantize(_COST_QUANT, rounding=ROUND_HALF_UP))
                if comm_rows > 0
                else None
            ),
            total_eligible_cost_usd=float(
                total_cand_cost.quantize(_COST_QUANT, rounding=ROUND_HALF_UP)
            ),
            excluded_cost_usd=float(
                acc.excluded_cost.quantize(_COST_QUANT, rounding=ROUND_HALF_UP)
            ),
            input_per_million=input_per_million,
            cached_input_per_million=cached_input_per_million,
            output_per_million=output_per_million,
            ptu_hourly=ptu_hourly,
            monthly_amount=monthly_amount,
            publishable=publishable,
            unpublishable_reason=" ".join(unpublishable_reasons) if unpublishable_reasons else None,
            warnings=warnings,
            evidence_line_keys=acc.evidence_keys,
        )

        matched_cand, match_issues = _match_deployment_and_existing_price(
            raw_candidate,
            deployments=deployments,
            book=book,
            today=today,
        )
        issues.extend(match_issues)
        candidates.append(matched_cand)

        mappings.append(
            BillingCandidateMapping(
                candidate_id=matched_cand.candidate_id,
                scope=matched_cand.suggested_scope,
                deployment_key=matched_cand.suggested_deployment_key,
                cloud=matched_cand.pricing_cloud,
                confirmed=(
                    matched_cand.publishable
                    and matched_cand.mapping_confidence == "exact"
                    and not matched_cand.conflicts_with_manual_override
                ),
                replace_manual_override=False,
            )
        )

    return (
        evidence_items,
        candidates,
        mappings,
        issues,
        eligible_row_count,
        excluded_row_count,
        total_eligible_cost,
        total_excluded_cost,
        sorted(invoice_months),
    )


def chunk_evidence_items(
    tenant_id: str,
    run_id: str,
    items: Sequence[BillingNormalizedEvidenceItem],
    *,
    chunk_size: int = EVIDENCE_CHUNK_SIZE,
) -> list[BillingEvidenceChunk]:
    """Split normalized evidence items into bounded Cosmos documents."""

    chunks: list[BillingEvidenceChunk] = []
    for chunk_idx, offset in enumerate(range(0, len(items), chunk_size)):
        batch = list(items[offset : offset + chunk_size])
        chunks.append(
            BillingEvidenceChunk(
                id=billing_evidence_chunk_id(tenant_id, run_id, chunk_idx),
                tenant_id=tenant_id,
                run_id=run_id,
                chunk_index=chunk_idx,
                items=batch,
            )
        )
    return chunks


def ingest_billing_parts(
    parts: Iterable[BillingConnectorPayloadPart],
    *,
    provider: BillingProvider,
    pricing_cloud: str,
    source_month: str,
    source_scope: str | None = None,
    deployments: Mapping[str, DeploymentFacts] | None = None,
    book: PriceBook | None = None,
    today: date | None = None,
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    max_decompressed_bytes: int = DEFAULT_MAX_DECOMPRESSED_BYTES,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> BillingIngestionResult:
    """Normalize one or more billing payload parts through the shared derivation pipeline."""

    parts_list = list(parts)
    if not parts_list:
        raise ValidationError("No billing payload parts were provided.")

    total_raw_bytes = 0
    total_decompressed_bytes = 0
    total_raw_rows = 0
    all_records: list[_NormalizedRowRecord] = []
    all_issues: list[BillingReviewIssue] = []

    for part in parts_list:
        file_name = part.name or "upload.csv"
        content = part.content
        if not content:
            raise ValidationError(
                f"Uploaded billing file {file_name!r} is empty.",
                details={"fileName": file_name},
            )
        total_raw_bytes += len(content)
        if total_raw_bytes > max_upload_bytes:
            raise BillingPayloadTooLargeError(
                f"Billing upload ({total_raw_bytes:,} bytes) exceeds the maximum allowed size "
                f"({max_upload_bytes:,} bytes).",
                details={"fileName": file_name, "maxUploadBytes": max_upload_bytes},
            )

        _check_unsupported_binary(content, file_name)

        lower_name = file_name.strip().casefold()
        is_gzip = content.startswith(_GZIP_MAGIC) or lower_name.endswith(".gz")
        if is_gzip:
            remaining_decomp = max_decompressed_bytes - total_decompressed_bytes
            if remaining_decomp <= 0:
                raise BillingPayloadTooLargeError(
                    "Decompressed billing payload exceeds the maximum allowed size.",
                    details={"maxDecompressedBytes": max_decompressed_bytes},
                )
            payload_bytes = _decompress_gzip_bounded(
                content,
                max_decompressed_bytes=remaining_decomp,
                file_name=file_name,
            )
            total_decompressed_bytes += len(payload_bytes)
            inner_name = file_name[:-3] if lower_name.endswith(".gz") else file_name
            _check_unsupported_binary(payload_bytes, inner_name)
        else:
            payload_bytes = content
            total_decompressed_bytes += len(payload_bytes)
            if total_decompressed_bytes > max_decompressed_bytes:
                raise BillingPayloadTooLargeError(
                    "Billing payload exceeds the maximum allowed decompressed size.",
                    details={"maxDecompressedBytes": max_decompressed_bytes},
                )

        remaining_rows = max_rows - total_raw_rows
        if remaining_rows <= 0:
            raise ValidationError(
                f"Billing dataset exceeds the maximum allowed row count ({max_rows:,} rows).",
                details={"maxRows": max_rows},
            )

        is_parquet = payload_bytes.startswith(PARQUET_MAGIC) or lower_name.endswith(".parquet")
        if is_parquet:
            if provider != "aws":
                raise ValidationError(
                    f"Parquet (.parquet) files are supported for AWS CUR exports only, "
                    f"not {provider!r}.",
                    details={"fileName": file_name, "provider": provider},
                )
            parquet_rows = read_parquet_table(
                payload_bytes,
                max_rows=remaining_rows,
                max_decompressed_bytes=max_decompressed_bytes,
            )
            if not parquet_rows:
                raise ValidationError(
                    f"Parquet billing file {file_name!r} contains no rows.",
                    details={"fileName": file_name},
                )
            parquet_headers = list({k for row in parquet_rows for k in row})
            total_raw_rows += len(parquet_rows)
            part_records, part_issues = _normalize_aws_rows(
                parquet_headers,
                parquet_rows,
                source_month=source_month,
                source_scope=source_scope,
            )
            all_records.extend(part_records)
            all_issues.extend(part_issues)
            continue

        try:
            text = payload_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValidationError(
                f"Billing file {file_name!r} is not valid UTF-8 text.",
                details={"fileName": file_name},
            ) from exc

        if not text.strip():
            raise ValidationError(
                f"Billing file {file_name!r} is empty.",
                details={"fileName": file_name},
            )

        effective_name = lower_name[:-3] if lower_name.endswith(".gz") else lower_name
        is_json = effective_name.endswith(
            (".json", ".jsonl", ".ndjson")
        ) or text.lstrip().startswith(("{", "["))
        if is_json:
            if provider != "gcp":
                raise ValidationError(
                    f"JSON / NDJSON billing exports are supported for GCP BigQuery exports only, "
                    f"not {provider!r}.",
                    details={"fileName": file_name, "provider": provider},
                )
            json_rows = _read_json_rows(text, max_rows=remaining_rows, file_name=file_name)
            total_raw_rows += len(json_rows)
            part_records, part_issues = _normalize_gcp_rows(
                json_rows,
                source_month=source_month,
                source_scope=source_scope,
            )
            all_records.extend(part_records)
            all_issues.extend(part_issues)
            continue

        csv_headers, csv_rows = _read_csv_rows(text, max_rows=remaining_rows, file_name=file_name)
        total_raw_rows += len(csv_rows)

        if provider == "azure":
            part_records, part_issues = _normalize_azure_rows(
                csv_headers,
                csv_rows,
                source_month=source_month,
                source_scope=source_scope,
            )
        elif provider == "aws":
            part_records, part_issues = _normalize_aws_rows(
                csv_headers,
                csv_rows,
                source_month=source_month,
                source_scope=source_scope,
            )
        elif provider == "gcp":
            part_records, part_issues = _normalize_gcp_rows(
                csv_rows,
                source_month=source_month,
                source_scope=source_scope,
            )
        elif provider == "mosaicTemplate":
            part_records, part_issues = _normalize_mosaic_template_rows(
                csv_headers,
                csv_rows,
                source_month=source_month,
                source_scope=source_scope,
            )
        else:
            raise ValidationError(f"Unsupported billing provider: {provider!r}")

        all_records.extend(part_records)
        all_issues.extend(part_issues)

    resolved_book = book or PriceBook([])
    resolved_deployments = deployments or {}
    resolved_today = today or date.today()

    (
        evidence_items,
        candidates,
        mappings,
        agg_issues,
        eligible_count,
        excluded_count,
        total_eligible_dec,
        total_excluded_dec,
        invoice_months,
    ) = _aggregate_evidence_and_derive_candidates(
        all_records,
        provider=provider,
        pricing_cloud=pricing_cloud,
        source_month=source_month,
        deployments=resolved_deployments,
        book=resolved_book,
        today=resolved_today,
    )

    combined_issues = [*all_issues, *agg_issues]
    if not candidates:
        combined_issues.append(
            BillingReviewIssue(
                code="noEligibleModelUsage",
                severity="warning",
                message=f"No eligible AI model usage rows were found for {source_month}.",
            )
        )

    return BillingIngestionResult(
        provider=provider,
        pricing_cloud=pricing_cloud,
        source_month=source_month,
        invoice_months=invoice_months,
        source_scope=source_scope,
        raw_row_count=total_raw_rows,
        eligible_row_count=eligible_count,
        excluded_row_count=excluded_count,
        total_read_bytes=total_raw_bytes,
        total_eligible_cost_usd=float(
            total_eligible_dec.quantize(_COST_QUANT, rounding=ROUND_HALF_UP)
        ),
        total_excluded_cost_usd=float(
            total_excluded_dec.quantize(_COST_QUANT, rounding=ROUND_HALF_UP)
        ),
        evidence_items=evidence_items,
        candidates=candidates,
        mappings=mappings,
        issues=combined_issues,
    )


def ingest_billing_bytes(
    content: bytes,
    *,
    file_name: str,
    provider: BillingProvider,
    pricing_cloud: str,
    source_month: str,
    source_scope: str | None = None,
    deployments: Mapping[str, DeploymentFacts] | None = None,
    book: PriceBook | None = None,
    today: date | None = None,
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    max_decompressed_bytes: int = DEFAULT_MAX_DECOMPRESSED_BYTES,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> BillingIngestionResult:
    """Normalize a single uploaded billing file and derive candidate rates."""

    return ingest_billing_parts(
        [BillingConnectorPayloadPart(name=file_name, content=content)],
        provider=provider,
        pricing_cloud=pricing_cloud,
        source_month=source_month,
        source_scope=source_scope,
        deployments=deployments,
        book=book,
        today=today,
        max_upload_bytes=max_upload_bytes,
        max_decompressed_bytes=max_decompressed_bytes,
        max_rows=max_rows,
    )
