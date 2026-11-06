"""Contracts and persistent state for billing-derived model pricing."""

import calendar
import re
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, Literal, Protocol, Self

from pydantic import Field, SecretStr, field_validator, model_validator

from mosaic_api.domain import Entity, MosaicModel, deterministic_id, utc_now
from mosaic_api.pricing import CLOUD_KEY, PriceOrigin, cloud_label, month_first
from mosaic_api.scheduling import IanaTimeZone, LocalTime

BillingProvider = Literal["azure", "aws", "gcp", "mosaicTemplate"]
ConnectedBillingProvider = Literal["azure", "aws", "gcp"]
BillingCostBasis = Literal["contractAmortized"]
BillingConnectionStatus = Literal["untested", "ready", "failed"]
BillingRunStatus = Literal[
    "queued",
    "running",
    "readyForReview",
    "published",
    "failed",
    "superseded",
]
BillingAcquisitionKind = Literal["upload", "connected", "scheduled"]
BillingUsageCategory = Literal[
    "inputTokens",
    "cachedInputTokens",
    "cacheWriteTokens",
    "outputTokens",
    "ptuHours",
    "monthlyCommitment",
    "unsupported",
    "excluded",
]
AzureBillingRetrievalMode = Literal["costDetailsApi", "exportManifest"]
BillingReviewIssueSeverity = Literal["blocker", "warning", "info"]
BillingMappingScope = Literal["deployment", "cloudModel", "excluded"]
BillingMappingConfidence = Literal["exact", "suggested", "ambiguous", "unmatched"]
PricingBasis = Literal["publicSeed", "customManual", "billingDerived", "mixed"]

SOURCE_MONTH_PATTERN = re.compile(r"^(?P<year>\d{4})-(?P<month>0[1-9]|1[0-2])$")
_AWS_ACCOUNT_ID_PATTERN = re.compile(r"^\d{12}$")
_AWS_ROLE_ARN_PATTERN = re.compile(
    r"^arn:aws(?:-cn|-us-gov)?:iam::(?P<account>\d{12}):role/\S+$"
)
_GCP_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_CLOUD_KEY_PATTERN = r"^[a-z0-9][a-z0-9-]{0,39}$"


def _clean_optional_str(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def previous_complete_month(today: date) -> str:
    """Return the most recent completed calendar month in ``YYYY-MM`` form relative to ``today``."""

    if today.month == 1:
        return f"{today.year - 1:04d}-12"
    return f"{today.year:04d}-{today.month - 1:02d}"


def parse_source_month(value: str) -> tuple[date, date]:
    """Parse ``YYYY-MM`` and return its inclusive ``(first_day, last_day)``."""

    cleaned = value.strip()
    match = SOURCE_MONTH_PATTERN.fullmatch(cleaned)
    if match is None:
        raise ValueError("Source month must use YYYY-MM format, such as 2026-09")
    year = int(match.group("year"))
    month = int(match.group("month"))
    if year < 2000 or year > 2100:
        raise ValueError("Source month year must be between 2000 and 2100")
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def validate_source_month(value: str | None, *, today: date) -> str:
    """Validate that ``value`` is a completed UTC calendar month before ``today``'s month."""

    resolved = previous_complete_month(today) if value is None else value.strip()
    _, last_day = parse_source_month(resolved)
    if last_day >= month_first(today):
        raise ValueError(
            f"Source month {resolved} is not yet complete; choose {previous_complete_month(today)} "
            "or an earlier completed calendar month"
        )
    return resolved


def billing_evidence_chunk_id(tenant_id: str, run_id: str, chunk_index: int) -> str:
    """Deterministic Cosmos document ID for one chunk of a billing run's normalized evidence."""

    if chunk_index < 0:
        raise ValueError("chunk_index must be non-negative")
    return deterministic_id("billingEvidenceChunk", tenant_id, run_id, str(chunk_index))


def billing_publication_id(tenant_id: str, run_id: str) -> str:
    """Deterministic publication ID for one billing acquisition run."""

    return deterministic_id("billingPublication", tenant_id, run_id)


def billing_evidence_ref(run_id: str, candidate_id: str | None = None) -> str:
    """Internal MOSAIC evidence URI for a billing run and optional candidate rate."""

    cleaned_run = run_id.strip()
    if not cleaned_run:
        raise ValueError("run_id is required")
    base = f"mosaic://pricing/billing-runs/{cleaned_run}"
    if candidate_id is not None and candidate_id.strip():
        return f"{base}#candidate={candidate_id.strip()}"
    return base


class BillingSchedule(MosaicModel):
    """Opt-in monthly draft refresh schedule for a connected billing source."""

    enabled: bool = False
    day_of_month: int = Field(default=3, ge=1, le=28)
    time_utc: LocalTime = "04:00"
    time_zone: IanaTimeZone = "UTC"
    last_triggered_source_month: str | None = None
    last_run_id: str | None = None
    last_run_at: datetime | None = None
    last_error: str | None = None

    @field_validator("last_triggered_source_month")
    @classmethod
    def _validate_last_month(cls, value: str | None) -> str | None:
        cleaned = _clean_optional_str(value)
        if cleaned is None:
            return None
        parse_source_month(cleaned)
        return cleaned


def _validate_provider_connection_fields(
    provider: ConnectedBillingProvider,
    *,
    azure_cloud: Literal["commercial", "government"] | None,
    azure_scope: str | None,
    azure_retrieval_mode: AzureBillingRetrievalMode | None,
    azure_export_manifest_url: str | None,
    aws_account_id: str | None,
    aws_role_arn: str | None,
    aws_cur_bucket: str | None,
    gcp_project_id: str | None,
    gcp_dataset_id: str | None,
    gcp_table_id: str | None,
) -> tuple[
    Literal["commercial", "government"] | None,
    AzureBillingRetrievalMode | None,
    str | None,
]:
    if provider == "azure":
        if not azure_scope:
            raise ValueError(
                "Azure billing connections require an Azure billing or subscription scope"
            )
        resolved_cloud: Literal["commercial", "government"] = azure_cloud or "commercial"
        resolved_mode: AzureBillingRetrievalMode = azure_retrieval_mode or "costDetailsApi"
        if resolved_mode == "exportManifest":
            if not azure_export_manifest_url:
                raise ValueError(
                    "Azure export-manifest retrieval requires an HTTPS export manifest URL"
                )
            if not azure_export_manifest_url.casefold().startswith("https://"):
                raise ValueError("Azure export manifest URL must use https://")
        elif azure_export_manifest_url and not azure_export_manifest_url.casefold().startswith(
            "https://"
        ):
            raise ValueError("Azure export manifest URL must use https://")
        return resolved_cloud, resolved_mode, aws_account_id

    if provider == "aws":
        if not aws_role_arn or not aws_cur_bucket:
            raise ValueError(
                "AWS billing connections require both an IAM role ARN and a CUR S3 bucket"
            )
        role_match = _AWS_ROLE_ARN_PATTERN.fullmatch(aws_role_arn)
        if role_match is None:
            raise ValueError(
                "Expected an AWS IAM role ARN of the form arn:aws:iam::<accountId>:role/<roleName>"
            )
        inferred_account = role_match.group("account")
        resolved_account = aws_account_id or inferred_account
        if not _AWS_ACCOUNT_ID_PATTERN.fullmatch(resolved_account):
            raise ValueError("AWS account ID must be a 12-digit account number")
        return azure_cloud, azure_retrieval_mode, resolved_account

    if provider == "gcp":
        if not gcp_project_id or not gcp_dataset_id or not gcp_table_id:
            raise ValueError(
                "GCP billing connections require a BigQuery project ID, dataset ID, and table ID"
            )
        for label, identifier in (
            ("project ID", gcp_project_id),
            ("dataset ID", gcp_dataset_id),
            ("table ID", gcp_table_id),
        ):
            if not _GCP_IDENTIFIER_PATTERN.fullmatch(identifier):
                raise ValueError(f"GCP BigQuery {label} contains unsupported characters")
        return azure_cloud, azure_retrieval_mode, aws_account_id

    raise ValueError(f"Unsupported connected billing provider: {provider}")


class BillingConnection(Entity):
    """Read-only cloud billing data connection configured by an administrator."""

    entity_type: Literal["billingConnection"] = "billingConnection"
    name: str = Field(min_length=1, max_length=120)
    provider: ConnectedBillingProvider
    pricing_cloud: str = Field(pattern=_CLOUD_KEY_PATTERN)
    # Azure settings
    azure_cloud: Literal["commercial", "government"] | None = None
    azure_connection_id: str | None = None
    azure_scope: str | None = None
    azure_retrieval_mode: AzureBillingRetrievalMode | None = None
    azure_export_manifest_url: str | None = None
    # AWS settings
    aws_account_id: str | None = None
    aws_role_arn: str | None = None
    aws_external_id: str | None = None
    aws_audience: str | None = None
    aws_region: str | None = None
    aws_cur_bucket: str | None = None
    aws_cur_prefix: str | None = None
    aws_export_name: str | None = None
    # GCP settings
    gcp_billing_account_id: str | None = None
    gcp_project_id: str | None = None
    gcp_dataset_id: str | None = None
    gcp_table_id: str | None = None
    gcp_location: str | None = None
    gcp_service_account_email: str | None = None
    gcp_workload_identity_audience: str | None = None
    gcp_max_bytes_billed: int | None = Field(default=None, ge=1_000_000, le=100_000_000_000)
    # Secret references (never serialized to view payloads)
    credential_secret_uri: str | None = Field(default=None, repr=False)
    retired_secret_uris: list[str] = Field(default_factory=list, repr=False)
    # Schedule and health state
    schedule: BillingSchedule = Field(default_factory=BillingSchedule)
    status: BillingConnectionStatus = "untested"
    status_message: str | None = None
    checked_at: datetime | None = None
    cost_notice_acknowledged: bool = True
    revision: int = Field(default=1, ge=1)
    updated_by: str | None = None

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Enter a connection name")
        return stripped

    @field_validator("pricing_cloud", mode="before")
    @classmethod
    def _clean_pricing_cloud(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        folded = value.strip().casefold()
        if not CLOUD_KEY.fullmatch(folded):
            raise ValueError(
                "A pricing cloud key is up to 40 lowercase letters, digits, and hyphens"
            )
        return folded

    @field_validator(
        "azure_connection_id",
        "azure_scope",
        "azure_export_manifest_url",
        "aws_account_id",
        "aws_role_arn",
        "aws_external_id",
        "aws_audience",
        "aws_region",
        "aws_cur_bucket",
        "aws_cur_prefix",
        "aws_export_name",
        "gcp_billing_account_id",
        "gcp_project_id",
        "gcp_dataset_id",
        "gcp_table_id",
        "gcp_location",
        "gcp_service_account_email",
        "gcp_workload_identity_audience",
        "credential_secret_uri",
        "status_message",
        "updated_by",
    )
    @classmethod
    def _strip_optional_fields(cls, value: str | None) -> str | None:
        return _clean_optional_str(value)

    @model_validator(mode="after")
    def _validate_provider_fields(self) -> Self:
        azure_cloud, azure_mode, aws_account = _validate_provider_connection_fields(
            self.provider,
            azure_cloud=self.azure_cloud,
            azure_scope=self.azure_scope,
            azure_retrieval_mode=self.azure_retrieval_mode,
            azure_export_manifest_url=self.azure_export_manifest_url,
            aws_account_id=self.aws_account_id,
            aws_role_arn=self.aws_role_arn,
            aws_cur_bucket=self.aws_cur_bucket,
            gcp_project_id=self.gcp_project_id,
            gcp_dataset_id=self.gcp_dataset_id,
            gcp_table_id=self.gcp_table_id,
        )
        self.azure_cloud = azure_cloud
        self.azure_retrieval_mode = azure_mode
        self.aws_account_id = aws_account
        return self


class BillingConnectionCreate(MosaicModel):
    """Administrator request to register a connected cloud billing source."""

    name: str = Field(min_length=1, max_length=120)
    provider: ConnectedBillingProvider
    pricing_cloud: str = Field(pattern=_CLOUD_KEY_PATTERN)
    # Azure settings
    azure_cloud: Literal["commercial", "government"] | None = None
    azure_connection_id: str | None = None
    azure_scope: str | None = None
    azure_retrieval_mode: AzureBillingRetrievalMode | None = None
    azure_export_manifest_url: str | None = None
    # AWS settings
    aws_account_id: str | None = None
    aws_role_arn: str | None = None
    aws_external_id: str | None = None
    aws_audience: str | None = None
    aws_region: str | None = None
    aws_cur_bucket: str | None = None
    aws_cur_prefix: str | None = None
    aws_export_name: str | None = None
    # GCP settings
    gcp_billing_account_id: str | None = None
    gcp_project_id: str | None = None
    gcp_dataset_id: str | None = None
    gcp_table_id: str | None = None
    gcp_location: str | None = None
    gcp_service_account_email: str | None = None
    gcp_workload_identity_audience: str | None = None
    gcp_max_bytes_billed: int | None = Field(default=None, ge=1_000_000, le=100_000_000_000)
    # Optional secret material or Key Vault reference
    credential_secret: SecretStr | None = Field(default=None, repr=False)
    credential_secret_uri: str | None = Field(default=None, repr=False)
    schedule: BillingSchedule = Field(default_factory=BillingSchedule)
    cost_notice_acknowledged: bool = True

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Enter a connection name")
        return stripped

    @field_validator("pricing_cloud", mode="before")
    @classmethod
    def _clean_pricing_cloud(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        folded = value.strip().casefold()
        if not CLOUD_KEY.fullmatch(folded):
            raise ValueError(
                "A pricing cloud key is up to 40 lowercase letters, digits, and hyphens"
            )
        return folded

    @field_validator(
        "azure_connection_id",
        "azure_scope",
        "azure_export_manifest_url",
        "aws_account_id",
        "aws_role_arn",
        "aws_external_id",
        "aws_audience",
        "aws_region",
        "aws_cur_bucket",
        "aws_cur_prefix",
        "aws_export_name",
        "gcp_billing_account_id",
        "gcp_project_id",
        "gcp_dataset_id",
        "gcp_table_id",
        "gcp_location",
        "gcp_service_account_email",
        "gcp_workload_identity_audience",
        "credential_secret_uri",
    )
    @classmethod
    def _strip_optional_fields(cls, value: str | None) -> str | None:
        return _clean_optional_str(value)

    @model_validator(mode="after")
    def _validate_create(self) -> Self:
        if not self.cost_notice_acknowledged:
            raise ValueError(
                "Acknowledge that billing queries and export reads can incur cloud provider charges"
            )
        azure_cloud, azure_mode, aws_account = _validate_provider_connection_fields(
            self.provider,
            azure_cloud=self.azure_cloud,
            azure_scope=self.azure_scope,
            azure_retrieval_mode=self.azure_retrieval_mode,
            azure_export_manifest_url=self.azure_export_manifest_url,
            aws_account_id=self.aws_account_id,
            aws_role_arn=self.aws_role_arn,
            aws_cur_bucket=self.aws_cur_bucket,
            gcp_project_id=self.gcp_project_id,
            gcp_dataset_id=self.gcp_dataset_id,
            gcp_table_id=self.gcp_table_id,
        )
        self.azure_cloud = azure_cloud
        self.azure_retrieval_mode = azure_mode
        self.aws_account_id = aws_account
        return self


class BillingConnectionUpdate(MosaicModel):
    """Partial update to a connected cloud billing source, guarded by ``version`` (ETag)."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    pricing_cloud: str | None = None
    # Azure settings
    azure_cloud: Literal["commercial", "government"] | None = None
    azure_connection_id: str | None = None
    azure_scope: str | None = None
    azure_retrieval_mode: AzureBillingRetrievalMode | None = None
    azure_export_manifest_url: str | None = None
    # AWS settings
    aws_account_id: str | None = None
    aws_role_arn: str | None = None
    aws_external_id: str | None = None
    aws_audience: str | None = None
    aws_region: str | None = None
    aws_cur_bucket: str | None = None
    aws_cur_prefix: str | None = None
    aws_export_name: str | None = None
    # GCP settings
    gcp_billing_account_id: str | None = None
    gcp_project_id: str | None = None
    gcp_dataset_id: str | None = None
    gcp_table_id: str | None = None
    gcp_location: str | None = None
    gcp_service_account_email: str | None = None
    gcp_workload_identity_audience: str | None = None
    gcp_max_bytes_billed: int | None = Field(default=None, ge=1_000_000, le=100_000_000_000)
    # Secret rotation/removal
    credential_secret: SecretStr | None = Field(default=None, repr=False)
    credential_secret_uri: str | None = Field(default=None, repr=False)
    clear_stored_secret: bool = False
    schedule: BillingSchedule | None = None
    cost_notice_acknowledged: bool | None = None
    version: str | None = Field(default=None, max_length=200)

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("Enter a connection name")
        return stripped

    @field_validator("pricing_cloud")
    @classmethod
    def _clean_pricing_cloud(cls, value: str | None) -> str | None:
        if value is None:
            return None
        folded = value.strip().casefold()
        if not CLOUD_KEY.fullmatch(folded):
            raise ValueError(
                "A pricing cloud key is up to 40 lowercase letters, digits, and hyphens"
            )
        return folded

    @field_validator(
        "azure_connection_id",
        "azure_scope",
        "azure_export_manifest_url",
        "aws_account_id",
        "aws_role_arn",
        "aws_external_id",
        "aws_audience",
        "aws_region",
        "aws_cur_bucket",
        "aws_cur_prefix",
        "aws_export_name",
        "gcp_billing_account_id",
        "gcp_project_id",
        "gcp_dataset_id",
        "gcp_table_id",
        "gcp_location",
        "gcp_service_account_email",
        "gcp_workload_identity_audience",
        "credential_secret_uri",
    )
    @classmethod
    def _strip_optional_fields(cls, value: str | None) -> str | None:
        return _clean_optional_str(value)


class BillingConnectionView(MosaicModel):
    """Administrator view of a billing connection. Never exposes secret values or vault URIs."""

    id: str
    name: str
    provider: ConnectedBillingProvider
    pricing_cloud: str
    pricing_cloud_label: str
    azure_cloud: Literal["commercial", "government"] | None = None
    azure_connection_id: str | None = None
    azure_scope: str | None = None
    azure_retrieval_mode: AzureBillingRetrievalMode | None = None
    azure_export_manifest_url: str | None = None
    aws_account_id: str | None = None
    aws_role_arn: str | None = None
    aws_external_id: str | None = None
    aws_audience: str | None = None
    aws_region: str | None = None
    aws_cur_bucket: str | None = None
    aws_cur_prefix: str | None = None
    aws_export_name: str | None = None
    gcp_billing_account_id: str | None = None
    gcp_project_id: str | None = None
    gcp_dataset_id: str | None = None
    gcp_table_id: str | None = None
    gcp_location: str | None = None
    gcp_service_account_email: str | None = None
    gcp_workload_identity_audience: str | None = None
    gcp_max_bytes_billed: int | None = None
    has_stored_secret: bool = False
    schedule: BillingSchedule
    status: BillingConnectionStatus
    status_message: str | None = None
    checked_at: datetime | None = None
    cost_notice_acknowledged: bool = True
    revision: int = 1
    created_at: datetime
    updated_at: datetime
    updated_by: str | None = None
    version: str | None = None


class BillingConnectionCheckResult(MosaicModel):
    """Outcome of verifying read-only access to a billing connection's dataset."""

    connection_id: str | None = None
    provider: ConnectedBillingProvider | None = None
    status: BillingConnectionStatus
    message: str
    checked_at: datetime = Field(default_factory=utc_now)
    scope_summary: str | None = None
    has_stored_secret: bool = False
    supported_source_months: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


def billing_connection_view(connection: BillingConnection) -> BillingConnectionView:
    """Convert a stored ``BillingConnection`` into its redacted administrator view."""

    return BillingConnectionView(
        id=connection.id,
        name=connection.name,
        provider=connection.provider,
        pricing_cloud=connection.pricing_cloud,
        pricing_cloud_label=cloud_label(connection.pricing_cloud),
        azure_cloud=connection.azure_cloud,
        azure_connection_id=connection.azure_connection_id,
        azure_scope=connection.azure_scope,
        azure_retrieval_mode=connection.azure_retrieval_mode,
        azure_export_manifest_url=connection.azure_export_manifest_url,
        aws_account_id=connection.aws_account_id,
        aws_role_arn=connection.aws_role_arn,
        aws_external_id=connection.aws_external_id,
        aws_audience=connection.aws_audience,
        aws_region=connection.aws_region,
        aws_cur_bucket=connection.aws_cur_bucket,
        aws_cur_prefix=connection.aws_cur_prefix,
        aws_export_name=connection.aws_export_name,
        gcp_billing_account_id=connection.gcp_billing_account_id,
        gcp_project_id=connection.gcp_project_id,
        gcp_dataset_id=connection.gcp_dataset_id,
        gcp_table_id=connection.gcp_table_id,
        gcp_location=connection.gcp_location,
        gcp_service_account_email=connection.gcp_service_account_email,
        gcp_workload_identity_audience=connection.gcp_workload_identity_audience,
        gcp_max_bytes_billed=connection.gcp_max_bytes_billed,
        has_stored_secret=bool(connection.credential_secret_uri),
        schedule=connection.schedule.model_copy(deep=True),
        status=connection.status,
        status_message=connection.status_message,
        checked_at=connection.checked_at,
        cost_notice_acknowledged=connection.cost_notice_acknowledged,
        revision=connection.revision,
        created_at=connection.created_at,
        updated_at=connection.updated_at,
        updated_by=connection.updated_by,
        version=connection.etag,
    )


class BillingNormalizedEvidenceItem(MosaicModel):
    """Normalized billing evidence retained to reproduce and review derived model rates."""

    line_key: str
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
    usage_category: BillingUsageCategory
    raw_usage_unit: str | None = None
    raw_quantity: float = 0.0
    normalized_quantity: float = 0.0
    normalized_unit: str | None = None
    eligible_cost_usd: float = 0.0
    amortized_benefit_cost_usd: float = 0.0
    excluded_cost_usd: float = 0.0
    excluded_reason: str | None = None
    row_count: int = Field(default=1, ge=1)
    usage_start: date | None = None
    usage_end: date | None = None
    invoice_month: str | None = None
    currency: str = "USD"
    notes: list[str] = Field(default_factory=list)


class BillingEvidenceChunk(Entity):
    """Bounded Cosmos chunk of normalized evidence items belonging to a billing acquisition run."""

    entity_type: Literal["billingEvidenceChunk"] = "billingEvidenceChunk"
    run_id: str
    chunk_index: int = Field(ge=0)
    items: list[BillingNormalizedEvidenceItem] = Field(default_factory=list)


class BillingReviewIssue(MosaicModel):
    """Diagnostic blocker, warning, or informational note surfaced during billing review."""

    code: str
    severity: BillingReviewIssueSeverity = "warning"
    message: str
    model: str | None = None
    region: str | None = None
    source_resource_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class BillingCandidateMapping(MosaicModel):
    """Administrator mapping decision for a derived candidate rate."""

    candidate_id: str
    scope: BillingMappingScope = "deployment"
    deployment_key: str | None = None
    cloud: str | None = None
    confirmed: bool = False
    replace_manual_override: bool = False

    @field_validator("deployment_key", "cloud")
    @classmethod
    def _clean_optional(cls, value: str | None) -> str | None:
        return _clean_optional_str(value)


class BillingCandidateRate(MosaicModel):
    """Quantity-weighted effective rate candidate derived from normalized billing evidence."""

    candidate_id: str
    provider: BillingProvider
    pricing_cloud: str
    source_scope: str | None = None
    source_account_id: str | None = None
    source_resource_id: str | None = None
    source_resource_name: str | None = None
    publisher: str | None = None
    model: str
    version: str | None = None
    deployment_type: str | None = None
    region: str | None = None
    service_tier: str | None = None
    routing_mode: str | None = None
    # Normalized quantities
    input_tokens: float | None = None
    cached_input_tokens: float | None = None
    cache_write_tokens: float | None = None
    output_tokens: float | None = None
    ptu_hours: float | None = None
    commitment_months: float | None = None
    # Eligible and excluded USD costs
    input_cost_usd: float | None = None
    cached_input_cost_usd: float | None = None
    cache_write_cost_usd: float | None = None
    output_cost_usd: float | None = None
    ptu_cost_usd: float | None = None
    monthly_commitment_cost_usd: float | None = None
    total_eligible_cost_usd: float = 0.0
    excluded_cost_usd: float = 0.0
    # Quantity-weighted effective rates in MOSAIC units
    input_per_million: float | None = None
    cached_input_per_million: float | None = None
    output_per_million: float | None = None
    ptu_hourly: float | None = None
    monthly_amount: float | None = None
    # Publishability and binding suggestion
    publishable: bool = True
    unpublishable_reason: str | None = None
    suggested_scope: BillingMappingScope = "cloudModel"
    suggested_deployment_key: str | None = None
    ambiguous_deployment_keys: list[str] = Field(default_factory=list)
    mapping_confidence: BillingMappingConfidence = "unmatched"
    # Existing applicable price comparison
    existing_price_id: str | None = None
    existing_line_id: str | None = None
    existing_origin: PriceOrigin | None = None
    existing_input_per_million: float | None = None
    existing_cached_input_per_million: float | None = None
    existing_output_per_million: float | None = None
    existing_ptu_hourly: float | None = None
    existing_monthly_amount: float | None = None
    conflicts_with_manual_override: bool = False
    warnings: list[str] = Field(default_factory=list)
    evidence_line_keys: list[str] = Field(default_factory=list)


class BillingAcquisitionRun(Entity):
    """One upload, connected retrieval, or scheduled draft acquisition of billing data."""

    entity_type: Literal["billingAcquisitionRun"] = "billingAcquisitionRun"
    connection_id: str | None = None
    connection_name: str | None = None
    provider: BillingProvider
    pricing_cloud: str = Field(pattern=_CLOUD_KEY_PATTERN)
    acquisition_kind: BillingAcquisitionKind
    source_month: str
    invoice_months: list[str] = Field(default_factory=list)
    cost_basis: BillingCostBasis = "contractAmortized"
    currency: str = "USD"
    derivation_version: str = "1"
    status: BillingRunStatus = "queued"
    status_message: str | None = None
    source_scope: str | None = None
    source_file_name: str | None = None
    source_manifest_id: str | None = None
    content_fingerprint: str | None = None
    duplicate_of_run_id: str | None = None
    operation_id: str | None = None
    operation_location: str | None = None
    continuation_token: str | None = None
    poll_after: datetime | None = None
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    published_at: datetime | None = None
    publication_id: str | None = None
    effective_from_default: date | None = None
    raw_row_count: int = Field(default=0, ge=0)
    eligible_row_count: int = Field(default=0, ge=0)
    excluded_row_count: int = Field(default=0, ge=0)
    total_read_bytes: int = Field(default=0, ge=0)
    total_eligible_cost_usd: float = 0.0
    total_excluded_cost_usd: float = 0.0
    evidence_chunk_count: int = Field(default=0, ge=0)
    evidence_item_count: int = Field(default=0, ge=0)
    candidates: list[BillingCandidateRate] = Field(default_factory=list)
    mappings: list[BillingCandidateMapping] = Field(default_factory=list)
    issues: list[BillingReviewIssue] = Field(default_factory=list)
    preview_revision: int = Field(default=1, ge=1)
    created_by: str | None = None
    published_by: str | None = None

    @field_validator("source_month")
    @classmethod
    def _check_source_month(cls, value: str) -> str:
        cleaned = value.strip()
        parse_source_month(cleaned)
        return cleaned


class BillingPublication(Entity):
    """Atomic visibility marker that activates prepared ``PriceVersion`` records for a run."""

    entity_type: Literal["billingPublication"] = "billingPublication"
    run_id: str
    connection_id: str | None = None
    provider: BillingProvider
    pricing_cloud: str = Field(pattern=_CLOUD_KEY_PATTERN)
    source_month: str
    effective_from: date
    backdated: bool = False
    price_version_ids: list[str] = Field(default_factory=list)
    affected_deployment_keys: list[str] = Field(default_factory=list)
    replaced_manual_override_ids: list[str] = Field(default_factory=list)
    note: str | None = None
    published_by: str
    published_at: datetime = Field(default_factory=utc_now)

    @field_validator("source_month")
    @classmethod
    def _check_source_month(cls, value: str) -> str:
        cleaned = value.strip()
        parse_source_month(cleaned)
        return cleaned


class BillingRunTriggerRequest(MosaicModel):
    """Administrator request to start an on-demand retrieval for a completed calendar month."""

    source_month: str | None = None
    force_rerun: bool = False

    @field_validator("source_month")
    @classmethod
    def _clean_source_month(cls, value: str | None) -> str | None:
        cleaned = _clean_optional_str(value)
        if cleaned is None:
            return None
        parse_source_month(cleaned)
        return cleaned


class BillingImportRequest(MosaicModel):
    """Administrator request to upload and normalize a billing dataset."""

    provider: BillingProvider
    pricing_cloud: str = Field(pattern=_CLOUD_KEY_PATTERN)
    source_month: str | None = None
    file_name: str = Field(default="upload.csv", min_length=1, max_length=260)
    source_scope: str | None = Field(default=None, max_length=500)
    content_base64: str | None = None
    content_text: str | None = None
    force: bool = False

    @field_validator("pricing_cloud", mode="before")
    @classmethod
    def _clean_pricing_cloud(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        folded = value.strip().casefold()
        if not CLOUD_KEY.fullmatch(folded):
            raise ValueError(
                "A pricing cloud key is up to 40 lowercase letters, digits, and hyphens"
            )
        return folded

    @field_validator("file_name")
    @classmethod
    def _clean_file_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Provide a file name")
        return stripped

    @field_validator("source_month")
    @classmethod
    def _clean_source_month(cls, value: str | None) -> str | None:
        cleaned = _clean_optional_str(value)
        if cleaned is None:
            return None
        parse_source_month(cleaned)
        return cleaned

    @field_validator("source_scope")
    @classmethod
    def _clean_source_scope(cls, value: str | None) -> str | None:
        return _clean_optional_str(value)


class BillingMappingsUpdateRequest(MosaicModel):
    """Administrator update to candidate mappings before publishing a billing run."""

    mappings: list[BillingCandidateMapping] = Field(default_factory=list, max_length=500)
    preview_revision: int | None = Field(default=None, ge=1)
    version: str | None = Field(default=None, max_length=200)


class BillingPublishRequest(MosaicModel):
    """Administrator approval to publish reviewed candidate rates into the shared price book."""

    effective_from: date | None = None
    acknowledge_backdate_impact: bool = False
    acknowledge_manual_override_replacement: bool = False
    note: str | None = Field(default=None, max_length=500)
    preview_revision: int | None = Field(default=None, ge=1)
    version: str | None = Field(default=None, max_length=200)

    @field_validator("note")
    @classmethod
    def _clean_note(cls, value: str | None) -> str | None:
        return _clean_optional_str(value)


class BillingRunSummaryView(MosaicModel):
    """Summary view of a billing acquisition run for history and status lists."""

    id: str
    connection_id: str | None = None
    connection_name: str | None = None
    provider: BillingProvider
    pricing_cloud: str
    pricing_cloud_label: str
    acquisition_kind: BillingAcquisitionKind
    source_month: str
    invoice_months: list[str] = Field(default_factory=list)
    cost_basis: BillingCostBasis = "contractAmortized"
    currency: str = "USD"
    derivation_version: str = "1"
    status: BillingRunStatus
    status_message: str | None = None
    source_scope: str | None = None
    source_file_name: str | None = None
    source_manifest_id: str | None = None
    content_fingerprint: str | None = None
    duplicate_of_run_id: str | None = None
    started_at: datetime
    completed_at: datetime | None = None
    published_at: datetime | None = None
    publication_id: str | None = None
    raw_row_count: int = 0
    eligible_row_count: int = 0
    excluded_row_count: int = 0
    total_read_bytes: int = 0
    total_eligible_cost_usd: float = 0.0
    total_excluded_cost_usd: float = 0.0
    evidence_chunk_count: int = 0
    evidence_item_count: int = 0
    candidate_count: int = 0
    publishable_candidate_count: int = 0
    blocker_count: int = 0
    warning_count: int = 0
    preview_revision: int = 1
    created_by: str | None = None
    published_by: str | None = None
    version: str | None = None


class BillingRunDetailView(BillingRunSummaryView):
    """Full review detail for a billing acquisition run, including candidates and evidence."""

    effective_from_default: date | None = None
    candidates: list[BillingCandidateRate] = Field(default_factory=list)
    mappings: list[BillingCandidateMapping] = Field(default_factory=list)
    issues: list[BillingReviewIssue] = Field(default_factory=list)
    evidence_items: list[BillingNormalizedEvidenceItem] = Field(default_factory=list)


class BillingPublicationView(MosaicModel):
    """Administrator view of a committed billing price publication."""

    id: str
    run_id: str
    connection_id: str | None = None
    provider: BillingProvider
    pricing_cloud: str
    pricing_cloud_label: str
    source_month: str
    effective_from: date
    backdated: bool = False
    price_version_ids: list[str] = Field(default_factory=list)
    affected_deployment_keys: list[str] = Field(default_factory=list)
    replaced_manual_override_ids: list[str] = Field(default_factory=list)
    note: str | None = None
    published_by: str
    published_at: datetime


class BillingSourceTemplateView(MosaicModel):
    """Schema documentation, sample CSV, or extraction query for an importable billing source."""

    provider: BillingProvider
    title: str
    schema_version: str = "1"
    supported_formats: list[str] = Field(default_factory=list)
    required_columns: list[str] = Field(default_factory=list)
    optional_columns: list[str] = Field(default_factory=list)
    sample_csv: str | None = None
    extraction_query: str | None = None
    notes: list[str] = Field(default_factory=list)


class BillingSchedulerStatusView(MosaicModel):
    """Hosting readiness and operational state for the monthly billing draft scheduler."""

    enabled: bool = True
    scheduled_workers_always_on: bool = False
    billing_scheduler_always_on: bool = False
    active_scheduled_connections: int = 0
    in_flight_runs: int = 0
    ready: bool = True
    warning: str | None = None
    warnings: list[str] = Field(default_factory=list)
    last_tick_at: datetime | None = None
    last_succeeded_at: datetime | None = None
    last_error: str | None = None


BillingSchedulerReadinessView = BillingSchedulerStatusView


def billing_run_summary_view(run: BillingAcquisitionRun) -> BillingRunSummaryView:
    """Build a summary view from a stored ``BillingAcquisitionRun``."""

    return BillingRunSummaryView(
        id=run.id,
        connection_id=run.connection_id,
        connection_name=run.connection_name,
        provider=run.provider,
        pricing_cloud=run.pricing_cloud,
        pricing_cloud_label=cloud_label(run.pricing_cloud),
        acquisition_kind=run.acquisition_kind,
        source_month=run.source_month,
        invoice_months=list(run.invoice_months),
        cost_basis=run.cost_basis,
        currency=run.currency,
        derivation_version=run.derivation_version,
        status=run.status,
        status_message=run.status_message,
        source_scope=run.source_scope,
        source_file_name=run.source_file_name,
        source_manifest_id=run.source_manifest_id,
        content_fingerprint=run.content_fingerprint,
        duplicate_of_run_id=run.duplicate_of_run_id,
        started_at=run.started_at,
        completed_at=run.completed_at,
        published_at=run.published_at,
        publication_id=run.publication_id,
        raw_row_count=run.raw_row_count,
        eligible_row_count=run.eligible_row_count,
        excluded_row_count=run.excluded_row_count,
        total_read_bytes=run.total_read_bytes,
        total_eligible_cost_usd=run.total_eligible_cost_usd,
        total_excluded_cost_usd=run.total_excluded_cost_usd,
        evidence_chunk_count=run.evidence_chunk_count,
        evidence_item_count=run.evidence_item_count,
        candidate_count=len(run.candidates),
        publishable_candidate_count=sum(1 for item in run.candidates if item.publishable),
        blocker_count=sum(1 for issue in run.issues if issue.severity == "blocker"),
        warning_count=sum(1 for issue in run.issues if issue.severity == "warning"),
        preview_revision=run.preview_revision,
        created_by=run.created_by,
        published_by=run.published_by,
        version=run.etag,
    )


def billing_run_detail_view(
    run: BillingAcquisitionRun,
    evidence_chunks: Sequence[BillingEvidenceChunk] = (),
) -> BillingRunDetailView:
    """Build a full review view from a stored ``BillingAcquisitionRun`` and its evidence chunks."""

    summary = billing_run_summary_view(run)
    ordered_chunks = sorted(evidence_chunks, key=lambda chunk: (chunk.chunk_index, chunk.id))
    evidence_items = [
        item.model_copy(deep=True)
        for chunk in ordered_chunks
        for item in chunk.items
    ]
    return BillingRunDetailView(
        **summary.model_dump(by_alias=False),
        effective_from_default=run.effective_from_default,
        candidates=[item.model_copy(deep=True) for item in run.candidates],
        mappings=[item.model_copy(deep=True) for item in run.mappings],
        issues=[item.model_copy(deep=True) for item in run.issues],
        evidence_items=evidence_items,
    )


def billing_publication_view(publication: BillingPublication) -> BillingPublicationView:
    """Build an administrator view of a ``BillingPublication``."""

    return BillingPublicationView(
        id=publication.id,
        run_id=publication.run_id,
        connection_id=publication.connection_id,
        provider=publication.provider,
        pricing_cloud=publication.pricing_cloud,
        pricing_cloud_label=cloud_label(publication.pricing_cloud),
        source_month=publication.source_month,
        effective_from=publication.effective_from,
        backdated=publication.backdated,
        price_version_ids=list(publication.price_version_ids),
        affected_deployment_keys=list(publication.affected_deployment_keys),
        replaced_manual_override_ids=list(publication.replaced_manual_override_ids),
        note=publication.note,
        published_by=publication.published_by,
        published_at=publication.published_at,
    )


class BillingConnectorPayloadPart(MosaicModel):
    """One downloaded partition or query result page returned by a connected billing source."""

    name: str
    content: bytes
    content_type: str | None = None


class BillingConnectorStepResult(MosaicModel):
    """Progress or completion result from one step of a connected billing acquisition."""

    status: Literal["running", "completed"]
    parts: list[BillingConnectorPayloadPart] = Field(default_factory=list)
    source_scope: str | None = None
    source_manifest_id: str | None = None
    operation_id: str | None = None
    operation_location: str | None = None
    continuation_token: str | None = None
    poll_after: datetime | None = None
    status_message: str | None = None
    total_read_bytes: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list)


class BillingConnector(Protocol):
    """Shared interface implemented by Azure, AWS, and GCP billing connectors."""

    provider: ConnectedBillingProvider

    async def check_access(
        self,
        connection: BillingConnection,
        *,
        secret_value: str | None = None,
    ) -> BillingConnectionCheckResult:
        """Verify read-only access to the configured billing dataset without mutating state."""
        ...

    async def start_or_poll_acquisition(
        self,
        connection: BillingConnection,
        run: BillingAcquisitionRun,
        *,
        secret_value: str | None = None,
    ) -> BillingConnectorStepResult:
        """Start or resume an asynchronous billing extraction for ``run.source_month``."""
        ...
