"""Google Cloud Billing BigQuery export connector for billing-derived model pricing."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import httpx
import jwt
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import ClientAuthenticationError

from mosaic_api.billing_pricing import (
    BillingAcquisitionRun,
    BillingConnection,
    BillingConnectionCheckResult,
    BillingConnectorPayloadPart,
    BillingConnectorStepResult,
    ConnectedBillingProvider,
    parse_source_month,
    previous_complete_month,
)
from mosaic_api.domain import utc_now
from mosaic_api.errors import UpstreamAuthorizationError, UpstreamError, ValidationError
from mosaic_api.integrations.mcp.credentials import scope_for_audience
from mosaic_api.pricing import DeploymentFacts, PriceBook
from mosaic_api.services.billing_ingestion import (
    DEFAULT_MAX_DECOMPRESSED_BYTES,
    DEFAULT_MAX_ROWS,
    DEFAULT_MAX_UPLOAD_BYTES,
    BillingIngestionResult,
    BillingPayloadTooLargeError,
    ingest_billing_bytes,
    ingest_billing_parts,
)

DEFAULT_GCP_MAX_BYTES_BILLED = 10_000_000_000  # 10 GB cap per query job
DEFAULT_GCP_PAGE_SIZE = 5_000
DEFAULT_GCP_POLL_SECONDS = 5

BIGQUERY_READONLY_SCOPE = "https://www.googleapis.com/auth/bigquery.readonly"
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

GCP_STS_TOKEN_URL = "https://sts.googleapis.com/v1/token"
GCP_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
GCP_IAM_CREDENTIALS_BASE_URL = "https://iamcredentials.googleapis.com/v1"
GCP_BIGQUERY_BASE_URL = "https://bigquery.googleapis.com/bigquery/v2"

OAUTH_TOKEN_EXCHANGE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
OAUTH_ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
OAUTH_JWT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:jwt"
OAUTH_JWT_BEARER_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"

REQUIRED_GCP_BILLING_EXPORT_COLUMNS: tuple[str, ...] = (
    "service",
    "sku",
    "usage",
    "cost",
    "currency",
    "usage_start_time",
)

_GCP_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_GCP_EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_ALLOWED_GOOGLE_AUTH_HOST_SUFFIXES: tuple[str, ...] = (
    "googleapis.com",
    ".googleapis.com",
)
_JWT_TOKEN_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")
_PEM_BLOCK_RE = re.compile(
    r"-----BEGIN [^-]+-----.*?-----END [^-]+-----",
    re.DOTALL,
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_NUMERIC_EPOCH_RE = re.compile(r"^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$")

TokenProvider = Callable[..., Awaitable[str]]
SubjectTokenProvider = Callable[[str], Awaitable[str]]


def _redact_sensitive_text(text: str, *secrets: str | None) -> str:
    """Remove tokens, JWT assertions, PEM keys, and explicit secret values from error text."""

    redacted = _PEM_BLOCK_RE.sub("[REDACTED_PRIVATE_KEY]", text)
    redacted = _JWT_TOKEN_RE.sub("[REDACTED_TOKEN]", redacted)
    redacted = _BEARER_RE.sub("Bearer [REDACTED_TOKEN]", redacted)
    for secret in secrets:
        if secret and secret.strip():
            trimmed = secret.strip()
            if len(trimmed) >= 4:
                redacted = redacted.replace(trimmed, "[REDACTED]")
    return redacted


def validate_gcp_identifier(value: str | None, *, label: str) -> str:
    """Validate a BigQuery project, dataset, table, or location identifier against injection."""

    cleaned = (value or "").strip()
    if not cleaned or not _GCP_IDENTIFIER_PATTERN.fullmatch(cleaned) or ".." in cleaned:
        raise ValidationError(
            f"GCP BigQuery {label} is missing or contains unsupported characters.",
            details={"field": label},
        )
    return cleaned


def _validate_google_endpoint_url(url: str, *, label: str) -> str:
    """Ensure a token or impersonation URL uses HTTPS on an official Google API host."""

    cleaned = url.strip()
    parts = urlsplit(cleaned)
    host = (parts.hostname or "").casefold()
    if parts.scheme.casefold() != "https" or not host:
        raise ValidationError(
            f"GCP {label} must use https:// on a googleapis.com host.",
            details={"field": label},
        )
    if not any(
        host == suffix or host.endswith(suffix)
        for suffix in _ALLOWED_GOOGLE_AUTH_HOST_SUFFIXES
    ):
        raise ValidationError(
            f"GCP {label} host {host!r} is not an allowed googleapis.com endpoint.",
            details={"field": label, "host": host},
        )
    return cleaned


def build_gcp_billing_export_query(
    project_id: str,
    dataset_id: str,
    table_id: str,
) -> str:
    """Build a parameterized BigQuery Standard SQL query for Cloud Billing detailed exports.

    - Strictly validates ``project_id``, ``dataset_id``, and ``table_id`` against
      ``^[A-Za-z0-9._:-]{1,128}$``.
    - Filters the observation window by ``usage_start_time >= TIMESTAMP(@usage_start)`` and
      ``usage_start_time < TIMESTAMP(@usage_end)`` (never by ``invoice.month``), while selecting
      ``invoice.month AS invoice_month`` so late billing adjustments are preserved.
    - Preserves ``credits`` as an array per usage line item via a correlated ``UNNEST(credits)``
      subquery so ``cost`` and ``usage.amount_in_pricing_units`` are never multiplied when a row
      carries multiple credit entries.
    """

    safe_project = validate_gcp_identifier(project_id, label="project ID")
    safe_dataset = validate_gcp_identifier(dataset_id, label="dataset ID")
    safe_table = validate_gcp_identifier(table_id, label="table ID")
    table_fqn = f"{safe_project}.{safe_dataset}.{safe_table}"

    return (
        "SELECT\n"
        "  billing_account_id,\n"
        "  project.id AS project_id,\n"
        "  service.description AS service_description,\n"
        "  sku.id AS sku_id,\n"
        "  sku.description AS sku_description,\n"
        "  location.region AS location_region,\n"
        "  resource.name AS resource_name,\n"
        "  resource.global_name AS resource_global_name,\n"
        "  usage_start_time,\n"
        "  usage_end_time,\n"
        "  usage.amount AS usage_amount,\n"
        "  usage.unit AS usage_unit,\n"
        "  usage.amount_in_pricing_units AS usage_amount_in_pricing_units,\n"
        "  usage.pricing_unit AS usage_pricing_unit,\n"
        "  cost,\n"
        "  cost_type,\n"
        "  currency,\n"
        "  currency_conversion_rate,\n"
        "  price.effective_price AS price_effective_price,\n"
        "  invoice.month AS invoice_month,\n"
        "  (SELECT ARRAY_AGG(STRUCT(c.id, c.name, c.full_name, c.type, c.amount)) "
        "FROM UNNEST(credits) AS c) AS credits\n"
        f"FROM `{table_fqn}`\n"
        "WHERE usage_start_time >= TIMESTAMP(@usage_start)\n"
        "  AND usage_start_time < TIMESTAMP(@usage_end)\n"
        "  AND (@billing_account_id IS NULL OR @billing_account_id = '' "
        "OR billing_account_id = @billing_account_id)\n"
        "  AND (\n"
        "    service.description IN ('Vertex AI', 'Gemini API', 'Cloud AI', 'Generative AI')\n"
        "    OR REGEXP_CONTAINS(\n"
        "      LOWER(COALESCE(sku.description, '')),\n"
        "      r'(vertex\\s*ai|gemini|claude|anthropic|llama|mistral|embedding"
        "|gecko|palm|gsu|partner)'\n"
        "    )\n"
        "  )"
    )


def deterministic_bigquery_job_id(
    tenant_id: str,
    run_id: str,
    source_month: str,
    *,
    connection_id: str | None = None,
) -> str:
    """Build a deterministic, restart-safe BigQuery jobId for a billing acquisition run."""

    parts = [tenant_id.strip(), run_id.strip(), source_month.strip()]
    if connection_id:
        parts.append(connection_id.strip())
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]
    month_token = re.sub(r"[^0-9]", "", source_month)[:6] or "month"
    return f"mosaic_bq_{month_token}_{digest}"


def inspect_bigquery_billing_schema(
    fields: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Return any missing required Cloud Billing export columns from a BigQuery table schema."""

    by_name: dict[str, Mapping[str, Any]] = {
        str(field.get("name") or "").strip().casefold(): field
        for field in fields
        if isinstance(field, Mapping) and str(field.get("name") or "").strip()
    }

    missing: list[str] = []
    for required_col in REQUIRED_GCP_BILLING_EXPORT_COLUMNS:
        if required_col in by_name:
            field_obj = by_name[required_col]
            subfields = field_obj.get("fields")
            if isinstance(subfields, list) and subfields:
                sub_names = {
                    str(sf.get("name") or "").strip().casefold()
                    for sf in subfields
                    if isinstance(sf, Mapping)
                }
                if required_col == "service" and "description" not in sub_names:
                    missing.append("service.description")
                elif required_col == "sku" and "description" not in sub_names:
                    missing.append("sku.description")
                elif required_col == "usage" and not (
                    {"amount_in_pricing_units", "amount"} & sub_names
                ):
                    missing.append("usage.amount_in_pricing_units")
            continue

        # Also accept pre-flattened views exposing the required billing columns
        if required_col == "service" and "service_description" in by_name:
            continue
        if required_col == "sku" and "sku_description" in by_name:
            continue
        if required_col == "usage" and (
            "usage_amount_in_pricing_units" in by_name or "usage_amount" in by_name
        ):
            continue

        missing.append(required_col)

    return missing


def _format_bq_timestamp(raw_value: Any) -> str | None:
    if raw_value is None:
        return None
    text = str(raw_value).strip()
    if not text:
        return None
    if _NUMERIC_EPOCH_RE.fullmatch(text):
        try:
            epoch = float(text)
            dt = datetime.fromtimestamp(epoch, tz=UTC)
            return dt.isoformat().replace("+00:00", "Z")
        except (ValueError, OverflowError, OSError):
            return text
    return text


def _decode_bq_value(field_schema: Mapping[str, Any], raw_value: Any) -> Any:
    """Recursively decode a BigQuery ``f`` / ``v`` cell value using its ``TableFieldSchema``."""

    mode = str(field_schema.get("mode") or "NULLABLE").strip().upper()
    field_type = str(field_schema.get("type") or "STRING").strip().upper()

    if raw_value is None:
        return [] if mode == "REPEATED" else None

    if mode == "REPEATED":
        element_schema = {**field_schema, "mode": "NULLABLE"}
        if isinstance(raw_value, str):
            stripped = raw_value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                try:
                    parsed_list = json.loads(stripped)
                    if isinstance(parsed_list, list):
                        return [
                            _decode_bq_value(element_schema, item)
                            for item in parsed_list
                            if item is not None
                        ]
                except json.JSONDecodeError:
                    pass
        if isinstance(raw_value, list):
            decoded_items: list[Any] = []
            for item in raw_value:
                unwrapped = (
                    item["v"]
                    if isinstance(item, Mapping) and "v" in item and set(item.keys()) <= {"v"}
                    else item
                )
                if unwrapped is None:
                    continue
                decoded_items.append(_decode_bq_value(element_schema, unwrapped))
            return decoded_items
        return [_decode_bq_value(element_schema, raw_value)]

    if field_type in ("RECORD", "STRUCT"):
        subfields = field_schema.get("fields") or []
        if isinstance(raw_value, Mapping) and "f" in raw_value and isinstance(raw_value["f"], list):
            cells = raw_value["f"]
            record_out: dict[str, Any] = {}
            for idx, subfield in enumerate(subfields):
                if not isinstance(subfield, Mapping):
                    continue
                sub_name = str(subfield.get("name") or f"field_{idx}")
                cell = cells[idx] if idx < len(cells) else None
                cell_v = cell.get("v") if isinstance(cell, Mapping) and "v" in cell else cell
                record_out[sub_name] = _decode_bq_value(subfield, cell_v)
            return record_out
        if isinstance(raw_value, Mapping):
            record_out = {}
            sub_map = {
                str(sf.get("name")): sf
                for sf in subfields
                if isinstance(sf, Mapping) and sf.get("name")
            }
            for k, v in raw_value.items():
                sf = sub_map.get(str(k))
                record_out[str(k)] = _decode_bq_value(sf, v) if sf is not None else v
            return record_out
        return raw_value

    if field_type == "TIMESTAMP":
        return _format_bq_timestamp(raw_value)

    if field_type in ("BOOLEAN", "BOOL"):
        if isinstance(raw_value, bool):
            return raw_value
        return str(raw_value).strip().casefold() == "true"

    if field_type in ("INTEGER", "INT64"):
        if isinstance(raw_value, int) and not isinstance(raw_value, bool):
            return raw_value
        try:
            return int(str(raw_value).strip())
        except ValueError:
            return raw_value

    if field_type in ("FLOAT", "FLOAT64", "NUMERIC", "BIGNUMERIC"):
        if isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool):
            return raw_value
        text = str(raw_value).strip()
        if not text:
            return None
        try:
            # Verify numeric validity while preserving exact decimal string or number
            Decimal(text)
            return text
        except InvalidOperation:
            return raw_value

    return raw_value


def convert_bigquery_row_to_dict(
    schema_fields: Sequence[Mapping[str, Any]],
    row: Mapping[str, Any],
) -> dict[str, Any]:
    """Convert one BigQuery ``TableRow`` (``{"f": [{"v": ...}, ...]}``) into a normalized dict."""

    if "f" not in row or not isinstance(row.get("f"), list):
        # Already a dictionary representation
        out = dict(row)
    else:
        cells = row["f"]
        out = {}
        for idx, field_schema in enumerate(schema_fields):
            if not isinstance(field_schema, Mapping):
                continue
            name = str(field_schema.get("name") or f"col_{idx}")
            cell = cells[idx] if idx < len(cells) else None
            raw_v = cell.get("v") if isinstance(cell, Mapping) and "v" in cell else cell
            out[name] = _decode_bq_value(field_schema, raw_v)

    # Normalize credits items so each credit entry retains type, id, name, full_name, and amount
    raw_credits = out.get("credits")
    if isinstance(raw_credits, list):
        normalized_credits: list[dict[str, Any]] = []
        for entry in raw_credits:
            if not isinstance(entry, Mapping):
                continue
            credit_item = dict(entry)
            c_type = str(credit_item.get("type") or "").strip()
            if not c_type:
                inferred_name = str(
                    credit_item.get("name") or credit_item.get("full_name") or ""
                ).upper()
                if "COMMITTED" in inferred_name or "CUD" in inferred_name:
                    credit_item["type"] = "COMMITTED_USAGE_DISCOUNT"
                elif "SUBSCRIPTION" in inferred_name:
                    credit_item["type"] = "SUBSCRIPTION_BENEFIT"
                elif "PROMO" in inferred_name:
                    credit_item["type"] = "PROMOTION"
                elif "FREE" in inferred_name:
                    credit_item["type"] = "FREE_TIER"
            normalized_credits.append(credit_item)
        out["credits"] = normalized_credits

    return out


class BillingIngestionEngine:
    """Adapter facade over MOSAIC's shared billing ingestion and rate derivation pipeline."""

    def __init__(
        self,
        *,
        deployments: Mapping[str, DeploymentFacts] | None = None,
        book: PriceBook | None = None,
        today: date | None = None,
        max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
        max_decompressed_bytes: int = DEFAULT_MAX_DECOMPRESSED_BYTES,
        max_rows: int = DEFAULT_MAX_ROWS,
    ) -> None:
        self._deployments = deployments
        self._book = book
        self._today = today
        self._max_upload_bytes = max_upload_bytes
        self._max_decompressed_bytes = max_decompressed_bytes
        self._max_rows = max_rows

    def ingest_parts(
        self,
        parts: Sequence[BillingConnectorPayloadPart],
        *,
        provider: Literal["azure", "aws", "gcp", "mosaicTemplate"] = "gcp",
        pricing_cloud: str = "gcp-vertex",
        source_month: str,
        source_scope: str | None = None,
    ) -> BillingIngestionResult:
        return ingest_billing_parts(
            parts,
            provider=provider,
            pricing_cloud=pricing_cloud,
            source_month=source_month,
            source_scope=source_scope,
            deployments=self._deployments,
            book=self._book,
            today=self._today,
            max_upload_bytes=self._max_upload_bytes,
            max_decompressed_bytes=self._max_decompressed_bytes,
            max_rows=self._max_rows,
        )

    def ingest_bytes(
        self,
        content: bytes,
        *,
        file_name: str = "gcp-export.jsonl",
        provider: Literal["azure", "aws", "gcp", "mosaicTemplate"] = "gcp",
        pricing_cloud: str = "gcp-vertex",
        source_month: str,
        source_scope: str | None = None,
    ) -> BillingIngestionResult:
        return ingest_billing_bytes(
            content,
            file_name=file_name,
            provider=provider,
            pricing_cloud=pricing_cloud,
            source_month=source_month,
            source_scope=source_scope,
            deployments=self._deployments,
            book=self._book,
            today=self._today,
            max_upload_bytes=self._max_upload_bytes,
            max_decompressed_bytes=self._max_decompressed_bytes,
            max_rows=self._max_rows,
        )


class GcpBillingConnector:
    """Read-only Google Cloud Billing BigQuery connector using Workload Identity or SA JWT."""

    provider: ConnectedBillingProvider = "gcp"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        credential: AsyncTokenCredential | None = None,
        subject_token_provider: SubjectTokenProvider | None = None,
        token_provider: TokenProvider | None = None,
        clock: Callable[[], datetime] = utc_now,
        timeout: float = 30.0,
        default_max_bytes_billed: int = DEFAULT_GCP_MAX_BYTES_BILLED,
        page_size: int = DEFAULT_GCP_PAGE_SIZE,
        poll_seconds: int = DEFAULT_GCP_POLL_SECONDS,
        max_result_rows: int = DEFAULT_MAX_ROWS,
        max_result_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0),
            follow_redirects=False,
        )
        self._owns_client = client is None
        self._credential = credential
        self._subject_token_provider = subject_token_provider
        self._token_provider = token_provider
        self._clock = clock
        self._default_max_bytes_billed = default_max_bytes_billed
        self._page_size = max(1, page_size)
        self._poll_seconds = max(1, poll_seconds)
        self._max_result_rows = max(1, max_result_rows)
        self._max_result_bytes = max(1024, max_result_bytes)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _resolve_table_coordinates(
        self, connection: BillingConnection
    ) -> tuple[str, str, str, str, int]:
        if connection.provider != "gcp":
            raise ValidationError(
                f"GcpBillingConnector requires provider='gcp', got {connection.provider!r}."
            )
        project_id = validate_gcp_identifier(connection.gcp_project_id, label="project ID")
        dataset_id = validate_gcp_identifier(connection.gcp_dataset_id, label="dataset ID")
        table_id = validate_gcp_identifier(connection.gcp_table_id, label="table ID")
        if connection.gcp_location is not None:
            validate_gcp_identifier(connection.gcp_location, label="location")
        if connection.gcp_billing_account_id is not None:
            validate_gcp_identifier(connection.gcp_billing_account_id, label="billing account ID")
        max_bytes = connection.gcp_max_bytes_billed or self._default_max_bytes_billed
        if max_bytes <= 0:
            max_bytes = self._default_max_bytes_billed
        scope = f"{project_id}.{dataset_id}.{table_id}"
        return project_id, dataset_id, table_id, scope, max_bytes

    async def _invoke_injected_token_provider(
        self,
        connection: BillingConnection,
        secret_value: str | None,
    ) -> str:
        assert self._token_provider is not None
        sig = inspect.signature(self._token_provider)
        params = [
            p
            for p in sig.parameters.values()
            if p.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        has_varargs = any(
            p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values()
        )
        if has_varargs or len(params) >= 2:
            token = await self._token_provider(connection, secret_value)
        elif len(params) == 1:
            token = await self._token_provider(connection)
        else:
            token = await self._token_provider()
        cleaned = str(token or "").strip()
        if not cleaned:
            raise UpstreamAuthorizationError(
                "GCP token provider returned an empty access token."
            )
        return cleaned

    async def _acquire_subject_token(self, audience: str) -> str:
        cleaned_audience = audience.strip()
        if not cleaned_audience:
            raise ValidationError("GCP Workload Identity Federation requires a non-empty audience.")
        if self._subject_token_provider is not None:
            token = await self._subject_token_provider(cleaned_audience)
            if not token or not token.strip():
                raise UpstreamAuthorizationError(
                    "MOSAIC could not acquire an OIDC subject token for GCP Workload Identity "
                    "Federation."
                )
            return token.strip()
        if self._credential is not None:
            scope = scope_for_audience(cleaned_audience)
            try:
                access_token = await self._credential.get_token(scope)
            except ClientAuthenticationError as exc:
                raise UpstreamAuthorizationError(
                    "MOSAIC could not acquire an Entra OIDC token for GCP Workload Identity "
                    "Federation.",
                    details={"audience": cleaned_audience},
                ) from exc
            except Exception as exc:
                raise UpstreamError(
                    "Failed to request an Entra OIDC token for GCP Workload Identity Federation.",
                    details={"audience": cleaned_audience},
                ) from exc
            if not access_token.token or not access_token.token.strip():
                raise UpstreamAuthorizationError(
                    "MOSAIC received an empty Entra OIDC token for GCP Workload Identity "
                    "Federation."
                )
            return access_token.token.strip()
        raise UpstreamAuthorizationError(
            "GCP Workload Identity Federation is configured, but no Entra credential or subject "
            "token provider is available."
        )

    async def _exchange_sts_token(
        self,
        *,
        audience: str,
        subject_token: str,
        subject_token_type: str = OAUTH_JWT_TOKEN_TYPE,
        token_url: str = GCP_STS_TOKEN_URL,
        scope: str = CLOUD_PLATFORM_SCOPE,
    ) -> str:
        validated_url = _validate_google_endpoint_url(token_url, label="STS token URL")
        form_data = {
            "grant_type": OAUTH_TOKEN_EXCHANGE_GRANT_TYPE,
            "audience": audience,
            "scope": scope,
            "requested_token_type": OAUTH_ACCESS_TOKEN_TYPE,
            "subject_token": subject_token,
            "subject_token_type": subject_token_type,
        }
        try:
            response = await self._client.post(
                validated_url,
                data=form_data,
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(
                "Could not reach Google Security Token Service (STS) for Workload Identity "
                "Federation."
            ) from exc

        if response.status_code != 200:
            err_msg = self._extract_oauth_error_message(response, subject_token)
            raise UpstreamAuthorizationError(
                f"Google Workload Identity Federation token exchange failed "
                f"(HTTP {response.status_code}): {err_msg}",
                details={"httpStatus": response.status_code, "stage": "stsTokenExchange"},
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamError(
                "Google Security Token Service (STS) returned a non-JSON response."
            ) from exc

        access_token = str(payload.get("access_token") or "").strip()
        if not access_token:
            raise UpstreamAuthorizationError(
                "Google Security Token Service (STS) response did not include an access_token."
            )
        return access_token

    async def _impersonate_service_account(
        self,
        *,
        federated_token: str,
        service_account_email: str,
        impersonation_url: str | None = None,
    ) -> str:
        email = service_account_email.strip()
        if not _GCP_EMAIL_PATTERN.fullmatch(email):
            raise ValidationError(
                f"GCP service account email {email!r} is not a valid email address.",
                details={"field": "gcp_service_account_email"},
            )
        if impersonation_url:
            target_url = _validate_google_endpoint_url(
                impersonation_url, label="service account impersonation URL"
            )
        else:
            encoded_email = quote(email, safe="@._+-")
            target_url = (
                f"{GCP_IAM_CREDENTIALS_BASE_URL}/projects/-/serviceAccounts/"
                f"{encoded_email}:generateAccessToken"
            )

        try:
            response = await self._client.post(
                target_url,
                headers={
                    "Authorization": f"Bearer {federated_token}",
                    "Accept": "application/json",
                },
                json={
                    "scope": [BIGQUERY_READONLY_SCOPE],
                    "lifetime": "3600s",
                },
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(
                f"Could not reach Google IAM Credentials API to impersonate {email}."
            ) from exc

        if response.status_code != 200:
            err_msg = self._extract_google_api_error_message(response, federated_token)
            raise UpstreamAuthorizationError(
                f"Google service account impersonation failed for {email} "
                f"(HTTP {response.status_code}): {err_msg}. Ensure the federated identity holds "
                "roles/iam.workloadIdentityUser (and roles/iam.serviceAccountTokenCreator) on the "
                "service account.",
                details={
                    "httpStatus": response.status_code,
                    "serviceAccountEmail": email,
                    "stage": "serviceAccountImpersonation",
                },
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamError(
                "Google IAM Credentials API returned a non-JSON response."
            ) from exc

        access_token = str(
            payload.get("accessToken") or payload.get("access_token") or ""
        ).strip()
        if not access_token:
            raise UpstreamAuthorizationError(
                f"Google IAM Credentials API did not return an accessToken for {email}."
            )
        return access_token

    async def _exchange_service_account_jwt(
        self,
        credential_data: Mapping[str, Any],
        *,
        impersonate_email: str | None = None,
    ) -> str:
        client_email = str(credential_data.get("client_email") or "").strip()
        private_key = str(credential_data.get("private_key") or "").strip()
        private_key_id = str(credential_data.get("private_key_id") or "").strip() or None
        token_uri = str(credential_data.get("token_uri") or GCP_OAUTH_TOKEN_URL).strip()

        if not client_email or not _GCP_EMAIL_PATTERN.fullmatch(client_email):
            raise ValidationError(
                "GCP service account JSON credential is missing a valid client_email."
            )
        if not private_key or "PRIVATE KEY" not in private_key:
            raise ValidationError(
                "GCP service account JSON credential is missing a valid PEM private_key."
            )
        validated_token_uri = _validate_google_endpoint_url(token_uri, label="OAuth token URI")

        now_ts = int(self._clock().timestamp())
        needs_impersonation = bool(
            impersonate_email and impersonate_email.strip().casefold() != client_email.casefold()
        )
        scope = (
            f"{CLOUD_PLATFORM_SCOPE} {BIGQUERY_READONLY_SCOPE}"
            if needs_impersonation
            else BIGQUERY_READONLY_SCOPE
        )
        claims: dict[str, Any] = {
            "iss": client_email,
            "sub": client_email,
            "scope": scope,
            "aud": validated_token_uri,
            "iat": now_ts,
            "exp": now_ts + 3600,
        }
        jwt_headers: dict[str, Any] | None = (
            {"kid": private_key_id} if private_key_id else None
        )
        try:
            assertion = jwt.encode(
                claims,
                private_key,
                algorithm="RS256",
                headers=jwt_headers,
            )
        except Exception as exc:
            raise ValidationError(
                "Failed to sign OAuth2 JWT assertion with the stored GCP service account key."
            ) from exc

        try:
            response = await self._client.post(
                validated_token_uri,
                data={
                    "grant_type": OAUTH_JWT_BEARER_GRANT_TYPE,
                    "assertion": assertion,
                },
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(
                "Could not reach Google OAuth2 token endpoint for service account authentication."
            ) from exc

        if response.status_code != 200:
            err_msg = self._extract_oauth_error_message(response, private_key, assertion)
            raise UpstreamAuthorizationError(
                f"Google OAuth2 service account token exchange failed "
                f"(HTTP {response.status_code}): {err_msg}",
                details={"httpStatus": response.status_code, "stage": "serviceAccountJwtExchange"},
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamError(
                "Google OAuth2 token endpoint returned a non-JSON response."
            ) from exc

        access_token = str(payload.get("access_token") or "").strip()
        if not access_token:
            raise UpstreamAuthorizationError(
                "Google OAuth2 token endpoint did not return an access_token."
            )

        if needs_impersonation and impersonate_email:
            return await self._impersonate_service_account(
                federated_token=access_token,
                service_account_email=impersonate_email,
            )
        return access_token

    async def _exchange_external_account_json(
        self,
        connection: BillingConnection,
        credential_data: Mapping[str, Any],
    ) -> str:
        audience = str(
            credential_data.get("audience") or connection.gcp_workload_identity_audience or ""
        ).strip()
        if not audience:
            raise ValidationError(
                "GCP external_account credential JSON is missing a Workload Identity audience."
            )
        token_url = str(credential_data.get("token_url") or GCP_STS_TOKEN_URL).strip()
        subject_token_type = str(
            credential_data.get("subject_token_type") or OAUTH_JWT_TOKEN_TYPE
        ).strip()
        impersonation_url = (
            str(credential_data.get("service_account_impersonation_url") or "").strip() or None
        )

        embedded_subject_token = str(credential_data.get("subject_token") or "").strip()
        if embedded_subject_token:
            subject_token = embedded_subject_token
        else:
            subject_token = await self._acquire_subject_token(audience)

        needs_impersonation = bool(connection.gcp_service_account_email or impersonation_url)
        sts_scope = CLOUD_PLATFORM_SCOPE if needs_impersonation else BIGQUERY_READONLY_SCOPE
        sts_token = await self._exchange_sts_token(
            audience=audience,
            subject_token=subject_token,
            subject_token_type=subject_token_type,
            token_url=token_url,
            scope=sts_scope,
        )

        if needs_impersonation:
            sa_email = (
                connection.gcp_service_account_email
                or "external-service-account@system.gserviceaccount.com"
            )
            if impersonation_url and not connection.gcp_service_account_email:
                match = re.search(
                    r"/serviceAccounts/([^/:]+):generateAccessToken",
                    impersonation_url,
                )
                if match:
                    sa_email = match.group(1)
            return await self._impersonate_service_account(
                federated_token=sts_token,
                service_account_email=sa_email,
                impersonation_url=impersonation_url,
            )
        return sts_token

    async def _acquire_access_token(
        self,
        connection: BillingConnection,
        *,
        secret_value: str | None = None,
    ) -> str:
        """Acquire a short-lived BigQuery read-only OAuth2 access token."""

        if self._token_provider is not None:
            return await self._invoke_injected_token_provider(connection, secret_value)

        if secret_value is not None and secret_value.strip():
            trimmed_secret = secret_value.strip()
            if trimmed_secret.startswith("{"):
                try:
                    parsed_secret = json.loads(trimmed_secret)
                except json.JSONDecodeError as exc:
                    raise ValidationError(
                        "Stored GCP credential secret is not valid JSON."
                    ) from exc
                if not isinstance(parsed_secret, Mapping):
                    raise ValidationError(
                        "Stored GCP credential secret must be a JSON object."
                    )
                cred_type = str(parsed_secret.get("type") or "").strip().casefold()
                if cred_type == "service_account" or (
                    "private_key" in parsed_secret and "client_email" in parsed_secret
                ):
                    return await self._exchange_service_account_jwt(
                        parsed_secret,
                        impersonate_email=connection.gcp_service_account_email,
                    )
                if cred_type == "external_account" or "audience" in parsed_secret:
                    return await self._exchange_external_account_json(connection, parsed_secret)
                raise ValidationError(
                    "Unsupported GCP credential JSON type; expected 'service_account' or "
                    "'external_account'."
                )

            # Non-JSON secret with Workload Identity audience acts as the OIDC subject token
            if connection.gcp_workload_identity_audience:
                sts_scope = (
                    CLOUD_PLATFORM_SCOPE
                    if connection.gcp_service_account_email
                    else BIGQUERY_READONLY_SCOPE
                )
                sts_token = await self._exchange_sts_token(
                    audience=connection.gcp_workload_identity_audience,
                    subject_token=trimmed_secret,
                    scope=sts_scope,
                )
                if connection.gcp_service_account_email:
                    return await self._impersonate_service_account(
                        federated_token=sts_token,
                        service_account_email=connection.gcp_service_account_email,
                    )
                return sts_token

            raise ValidationError(
                "GCP billing credential secret must be a service_account or external_account JSON "
                "document when gcp_workload_identity_audience is not configured."
            )

        if connection.gcp_workload_identity_audience:
            subject_token = await self._acquire_subject_token(
                connection.gcp_workload_identity_audience
            )
            sts_scope = (
                CLOUD_PLATFORM_SCOPE
                if connection.gcp_service_account_email
                else BIGQUERY_READONLY_SCOPE
            )
            sts_token = await self._exchange_sts_token(
                audience=connection.gcp_workload_identity_audience,
                subject_token=subject_token,
                scope=sts_scope,
            )
            if connection.gcp_service_account_email:
                return await self._impersonate_service_account(
                    federated_token=sts_token,
                    service_account_email=connection.gcp_service_account_email,
                )
            return sts_token

        raise UpstreamAuthorizationError(
            "GCP billing connection requires either gcp_workload_identity_audience for Workload "
            "Identity Federation or a stored service_account / external_account credential."
        )

    @staticmethod
    def _extract_oauth_error_message(
        response: httpx.Response,
        *secrets: str | None,
    ) -> str:
        try:
            body = response.json()
            if isinstance(body, Mapping):
                desc = body.get("error_description") or body.get("error")
                if isinstance(desc, Mapping):
                    desc = desc.get("message") or desc.get("status")
                if desc:
                    return _redact_sensitive_text(str(desc), *secrets)
        except ValueError:
            pass
        text = response.text.strip()[:300] or f"HTTP {response.status_code}"
        return _redact_sensitive_text(text, *secrets)

    @staticmethod
    def _extract_google_api_error(
        response: httpx.Response,
        *secrets: str | None,
    ) -> tuple[str, str | None, list[dict[str, str]]]:
        """Return ``(message, primary_reason, errors_list)`` from a Google JSON API error."""

        try:
            body = response.json()
        except ValueError:
            raw = _redact_sensitive_text(
                response.text.strip()[:300] or f"HTTP {response.status_code}",
                *secrets,
            )
            return raw, None, []

        if not isinstance(body, Mapping):
            return f"HTTP {response.status_code}", None, []

        err_obj = body.get("error")
        if isinstance(err_obj, Mapping):
            msg = _redact_sensitive_text(
                str(err_obj.get("message") or f"HTTP {response.status_code}"),
                *secrets,
            )
            raw_errors = err_obj.get("errors")
            parsed_errors: list[dict[str, str]] = []
            primary_reason: str | None = None
            if isinstance(raw_errors, list):
                for item in raw_errors:
                    if isinstance(item, Mapping):
                        reason = str(item.get("reason") or "").strip()
                        item_msg = _redact_sensitive_text(
                            str(item.get("message") or ""), *secrets
                        )
                        if reason and primary_reason is None:
                            primary_reason = reason
                        parsed_errors.append({"reason": reason, "message": item_msg})
            return msg, primary_reason, parsed_errors

        return f"HTTP {response.status_code}", None, []

    def _extract_google_api_error_message(
        self,
        response: httpx.Response,
        *secrets: str | None,
    ) -> str:
        msg, _, _ = self._extract_google_api_error(response, *secrets)
        return msg

    @staticmethod
    def _build_query_job_payload(
        *,
        project_id: str,
        dataset_id: str,
        table_id: str,
        source_month: str,
        billing_account_id: str | None,
        max_bytes_billed: int,
        job_id: str | None = None,
        location: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        month_start, month_end = parse_source_month(source_month)
        next_month_start = month_end + timedelta(days=1)
        usage_start_iso = f"{month_start.isoformat()}T00:00:00Z"
        usage_end_iso = f"{next_month_start.isoformat()}T00:00:00Z"
        query_sql = build_gcp_billing_export_query(project_id, dataset_id, table_id)

        payload: dict[str, Any] = {
            "configuration": {
                "dryRun": dry_run,
                "query": {
                    "query": query_sql,
                    "useLegacySql": False,
                    "maximumBytesBilled": str(max_bytes_billed),
                    "parameterMode": "NAMED",
                    "queryParameters": [
                        {
                            "name": "usage_start",
                            "parameterType": {"type": "STRING"},
                            "parameterValue": {"value": usage_start_iso},
                        },
                        {
                            "name": "usage_end",
                            "parameterType": {"type": "STRING"},
                            "parameterValue": {"value": usage_end_iso},
                        },
                        {
                            "name": "billing_account_id",
                            "parameterType": {"type": "STRING"},
                            "parameterValue": {"value": (billing_account_id or "").strip()},
                        },
                    ],
                },
            }
        }
        if job_id or location:
            job_ref: dict[str, Any] = {"projectId": project_id}
            if job_id:
                job_ref["jobId"] = job_id
            if location:
                job_ref["location"] = location
            payload["jobReference"] = job_ref
        return payload

    @staticmethod
    def _extract_job_errors(
        payload: Mapping[str, Any],
        *secrets: str | None,
    ) -> list[dict[str, str]]:
        """Extract error entries from a BigQuery Job or GetQueryResults response."""

        collected: list[dict[str, str]] = []
        top_errors = payload.get("errors")
        if isinstance(top_errors, list):
            for item in top_errors:
                if isinstance(item, Mapping):
                    collected.append(
                        {
                            "reason": str(item.get("reason") or "").strip(),
                            "message": _redact_sensitive_text(
                                str(item.get("message") or ""), *secrets
                            ),
                        }
                    )
        status_obj = payload.get("status")
        if isinstance(status_obj, Mapping):
            err_res = status_obj.get("errorResult")
            if isinstance(err_res, Mapping):
                collected.append(
                    {
                        "reason": str(err_res.get("reason") or "").strip(),
                        "message": _redact_sensitive_text(
                            str(err_res.get("message") or ""), *secrets
                        ),
                    }
                )
            status_errors = status_obj.get("errors")
            if isinstance(status_errors, list):
                for item in status_errors:
                    if isinstance(item, Mapping):
                        collected.append(
                            {
                                "reason": str(item.get("reason") or "").strip(),
                                "message": _redact_sensitive_text(
                                    str(item.get("message") or ""), *secrets
                                ),
                            }
                        )
        return collected

    @staticmethod
    def _is_bytes_billed_exceeded(
        reason: str | None,
        message: str,
        errors: Sequence[Mapping[str, str]] = (),
    ) -> bool:
        if reason == "bytesBilledLimitExceeded":
            return True
        folded_msg = message.casefold()
        if "bytesbilledlimitexceeded" in folded_msg or "maximumbytesbilled" in folded_msg or (
            "bytes billed" in folded_msg and "limit" in folded_msg
        ):
            return True
        for err in errors:
            r = str(err.get("reason") or "").strip()
            m = str(err.get("message") or "").casefold()
            if (
                r == "bytesBilledLimitExceeded"
                or "bytesbilledlimitexceeded" in m
                or "maximumbytesbilled" in m
                or ("bytes billed" in m and "limit" in m)
            ):
                return True
        return False

    def _raise_for_bigquery_job_errors(
        self,
        errors: Sequence[Mapping[str, str]],
        *,
        project_id: str,
        dataset_id: str,
        table_id: str,
        max_bytes_billed: int,
        job_id: str,
    ) -> None:
        if not errors:
            return
        primary = errors[0]
        reason = primary.get("reason") or "queryError"
        message = primary.get("message") or "BigQuery query execution failed."
        scope = f"{project_id}.{dataset_id}.{table_id}"
        if self._is_bytes_billed_exceeded(reason, message, errors):
            raise ValidationError(
                f"BigQuery query for {scope} exceeded maximumBytesBilled "
                f"({max_bytes_billed:,} bytes): {message} "
                "Narrow the billing export view or increase gcp_max_bytes_billed.",
                details={
                    "projectId": project_id,
                    "datasetId": dataset_id,
                    "tableId": table_id,
                    "jobId": job_id,
                    "maximumBytesBilled": max_bytes_billed,
                    "reason": "bytesBilledLimitExceeded",
                },
            )
        if reason in ("accessDenied", "forbidden", "unauthorized"):
            raise UpstreamAuthorizationError(
                f"BigQuery denied access while running billing query {job_id} on {scope}: "
                f"{message}. Grant roles/bigquery.jobUser on project {project_id} and "
                f"roles/bigquery.dataViewer on {scope}.",
                details={
                    "projectId": project_id,
                    "datasetId": dataset_id,
                    "tableId": table_id,
                    "jobId": job_id,
                    "reason": reason,
                    "requiredRoles": ["roles/bigquery.jobUser", "roles/bigquery.dataViewer"],
                },
            )
        raise UpstreamError(
            f"BigQuery billing query {job_id} failed ({reason}): {message}",
            details={
                "projectId": project_id,
                "datasetId": dataset_id,
                "tableId": table_id,
                "jobId": job_id,
                "reason": reason,
            },
        )

    async def check_access(
        self,
        connection: BillingConnection,
        *,
        secret_value: str | None = None,
    ) -> BillingConnectionCheckResult:
        """Verify authentication, BigQuery table schema, and dry-run query execution."""

        checked_at = self._clock()
        has_secret = bool(
            connection.credential_secret_uri or (secret_value and secret_value.strip())
        )

        try:
            project_id, dataset_id, table_id, scope, max_bytes = self._resolve_table_coordinates(
                connection
            )
        except ValidationError as exc:
            return BillingConnectionCheckResult(
                connection_id=connection.id,
                provider="gcp",
                status="failed",
                message=exc.message,
                checked_at=checked_at,
                has_stored_secret=has_secret,
                details=dict(exc.details),
            )

        try:
            access_token = await self._acquire_access_token(
                connection,
                secret_value=secret_value,
            )
        except (UpstreamAuthorizationError, UpstreamError, ValidationError) as exc:
            return BillingConnectionCheckResult(
                connection_id=connection.id,
                provider="gcp",
                status="failed",
                message=exc.message,
                checked_at=checked_at,
                scope_summary=scope,
                has_stored_secret=has_secret,
                details=dict(exc.details),
            )

        auth_headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }

        # 1. Verify dataset/table read access and inspect table schema
        table_url = (
            f"{GCP_BIGQUERY_BASE_URL}/projects/{quote(project_id, safe='')}"
            f"/datasets/{quote(dataset_id, safe='')}"
            f"/tables/{quote(table_id, safe='')}"
        )
        try:
            table_resp = await self._client.get(table_url, headers=auth_headers)
        except httpx.HTTPError:
            return BillingConnectionCheckResult(
                connection_id=connection.id,
                provider="gcp",
                status="failed",
                message=f"Could not reach Google BigQuery API to inspect table {scope}.",
                checked_at=checked_at,
                scope_summary=scope,
                has_stored_secret=has_secret,
            )

        if table_resp.status_code in (401, 403):
            err_msg = self._extract_google_api_error_message(
                table_resp, access_token, secret_value
            )
            return BillingConnectionCheckResult(
                connection_id=connection.id,
                provider="gcp",
                status="failed",
                message=(
                    f"BigQuery denied access to billing export table {scope} "
                    f"(HTTP {table_resp.status_code}): {err_msg}. Grant minimal read-only IAM "
                    f"roles: roles/bigquery.dataViewer on {scope} and roles/bigquery.jobUser on "
                    f"project {project_id}."
                ),
                checked_at=checked_at,
                scope_summary=scope,
                has_stored_secret=has_secret,
                details={
                    "projectId": project_id,
                    "datasetId": dataset_id,
                    "tableId": table_id,
                    "httpStatus": table_resp.status_code,
                    "requiredRoles": ["roles/bigquery.dataViewer", "roles/bigquery.jobUser"],
                },
            )

        if table_resp.status_code == 404:
            return BillingConnectionCheckResult(
                connection_id=connection.id,
                provider="gcp",
                status="failed",
                message=(
                    f"BigQuery billing export table {scope} was not found (HTTP 404). Verify "
                    "the project ID, dataset ID, and Cloud Billing export table name."
                ),
                checked_at=checked_at,
                scope_summary=scope,
                has_stored_secret=has_secret,
                details={
                    "projectId": project_id,
                    "datasetId": dataset_id,
                    "tableId": table_id,
                    "httpStatus": 404,
                },
            )

        if table_resp.status_code != 200:
            err_msg = self._extract_google_api_error_message(
                table_resp, access_token, secret_value
            )
            return BillingConnectionCheckResult(
                connection_id=connection.id,
                provider="gcp",
                status="failed",
                message=(
                    f"BigQuery returned HTTP {table_resp.status_code} when inspecting {scope}: "
                    f"{err_msg}"
                ),
                checked_at=checked_at,
                scope_summary=scope,
                has_stored_secret=has_secret,
                details={"httpStatus": table_resp.status_code},
            )

        try:
            table_data = table_resp.json()
        except ValueError:
            return BillingConnectionCheckResult(
                connection_id=connection.id,
                provider="gcp",
                status="failed",
                message="BigQuery returned a non-JSON table metadata response.",
                checked_at=checked_at,
                scope_summary=scope,
                has_stored_secret=has_secret,
            )

        schema_obj = table_data.get("schema") if isinstance(table_data, Mapping) else None
        fields = schema_obj.get("fields") if isinstance(schema_obj, Mapping) else None
        if not isinstance(fields, list) or not fields:
            return BillingConnectionCheckResult(
                connection_id=connection.id,
                provider="gcp",
                status="failed",
                message=(
                    f"BigQuery table {scope} does not expose a readable schema. Point the "
                    "connection at a Cloud Billing detailed or standard BigQuery export table "
                    "containing service, sku, usage, cost, currency, and usage_start_time."
                ),
                checked_at=checked_at,
                scope_summary=scope,
                has_stored_secret=has_secret,
                details={
                    "missingColumns": list(REQUIRED_GCP_BILLING_EXPORT_COLUMNS),
                },
            )

        missing_cols = inspect_bigquery_billing_schema(fields)
        if missing_cols:
            missing_list = ", ".join(missing_cols)
            return BillingConnectionCheckResult(
                connection_id=connection.id,
                provider="gcp",
                status="failed",
                message=(
                    f"BigQuery table {scope} is not a valid Cloud Billing export table; "
                    f"missing required billing export columns: {missing_list}."
                ),
                checked_at=checked_at,
                scope_summary=scope,
                has_stored_secret=has_secret,
                details={
                    "projectId": project_id,
                    "datasetId": dataset_id,
                    "tableId": table_id,
                    "missingColumns": missing_cols,
                },
            )

        # 2. Verify query job creation permission (roles/bigquery.jobUser) via dryRun=True
        sample_month = previous_complete_month(checked_at.date())
        dry_run_payload = self._build_query_job_payload(
            project_id=project_id,
            dataset_id=dataset_id,
            table_id=table_id,
            source_month=sample_month,
            billing_account_id=connection.gcp_billing_account_id,
            max_bytes_billed=max_bytes,
            location=connection.gcp_location,
            dry_run=True,
        )
        jobs_url = f"{GCP_BIGQUERY_BASE_URL}/projects/{quote(project_id, safe='')}/jobs"
        try:
            dry_resp = await self._client.post(
                jobs_url,
                headers=auth_headers,
                json=dry_run_payload,
            )
        except httpx.HTTPError:
            return BillingConnectionCheckResult(
                connection_id=connection.id,
                provider="gcp",
                status="failed",
                message=f"Could not reach Google BigQuery Jobs API for project {project_id}.",
                checked_at=checked_at,
                scope_summary=scope,
                has_stored_secret=has_secret,
            )

        if dry_resp.status_code in (401, 403):
            err_msg = self._extract_google_api_error_message(
                dry_resp, access_token, secret_value
            )
            return BillingConnectionCheckResult(
                connection_id=connection.id,
                provider="gcp",
                status="failed",
                message=(
                    f"BigQuery denied dry-run query execution in project {project_id} "
                    f"(HTTP {dry_resp.status_code}): {err_msg}. Grant roles/bigquery.jobUser on "
                    f"project {project_id} and roles/bigquery.dataViewer on {scope}."
                ),
                checked_at=checked_at,
                scope_summary=scope,
                has_stored_secret=has_secret,
                details={
                    "projectId": project_id,
                    "datasetId": dataset_id,
                    "tableId": table_id,
                    "httpStatus": dry_resp.status_code,
                    "requiredRoles": ["roles/bigquery.jobUser", "roles/bigquery.dataViewer"],
                },
            )

        if dry_resp.status_code != 200:
            err_msg, reason, parsed_errors = self._extract_google_api_error(
                dry_resp, access_token, secret_value
            )
            if self._is_bytes_billed_exceeded(reason, err_msg, parsed_errors):
                return BillingConnectionCheckResult(
                    connection_id=connection.id,
                    provider="gcp",
                    status="failed",
                    message=(
                        f"BigQuery dry-run query for {scope} exceeds maximumBytesBilled "
                        f"({max_bytes:,} bytes): {err_msg}"
                    ),
                    checked_at=checked_at,
                    scope_summary=scope,
                    has_stored_secret=has_secret,
                    details={
                        "maximumBytesBilled": max_bytes,
                        "reason": "bytesBilledLimitExceeded",
                    },
                )
            return BillingConnectionCheckResult(
                connection_id=connection.id,
                provider="gcp",
                status="failed",
                message=(
                    f"BigQuery dry-run query validation failed (HTTP {dry_resp.status_code}): "
                    f"{err_msg}"
                ),
                checked_at=checked_at,
                scope_summary=scope,
                has_stored_secret=has_secret,
                details={"httpStatus": dry_resp.status_code, "reason": reason},
            )

        estimated_bytes = 0
        try:
            dry_json = dry_resp.json()
        except ValueError:
            dry_json = {}

        if isinstance(dry_json, Mapping):
            job_errors = self._extract_job_errors(dry_json, access_token, secret_value)
            if job_errors:
                first_err = job_errors[0]
                return BillingConnectionCheckResult(
                    connection_id=connection.id,
                    provider="gcp",
                    status="failed",
                    message=(
                        f"BigQuery dry-run query reported an error "
                        f"({first_err.get('reason') or 'error'}): {first_err.get('message')}"
                    ),
                    checked_at=checked_at,
                    scope_summary=scope,
                    has_stored_secret=has_secret,
                    details={"reason": first_err.get("reason")},
                )
            stats = dry_json.get("statistics")
            if isinstance(stats, Mapping):
                raw_est = stats.get("totalBytesProcessed")
                if raw_est is None and isinstance(stats.get("query"), Mapping):
                    raw_est = stats["query"].get("totalBytesProcessed")
                if raw_est is not None:
                    try:
                        estimated_bytes = int(str(raw_est))
                    except ValueError:
                        estimated_bytes = 0

        warnings: list[str] = []
        if estimated_bytes > max_bytes:
            return BillingConnectionCheckResult(
                connection_id=connection.id,
                provider="gcp",
                status="failed",
                message=(
                    f"BigQuery dry-run estimated {estimated_bytes:,} bytes processed for {scope}, "
                    f"which exceeds maximumBytesBilled ({max_bytes:,} bytes)."
                ),
                checked_at=checked_at,
                scope_summary=scope,
                has_stored_secret=has_secret,
                details={
                    "estimatedBytesProcessed": estimated_bytes,
                    "maximumBytesBilled": max_bytes,
                    "reason": "bytesBilledLimitExceeded",
                },
            )

        table_location = (
            str(table_data.get("location") or connection.gcp_location or "").strip() or None
            if isinstance(table_data, Mapping)
            else connection.gcp_location
        )
        return BillingConnectionCheckResult(
            connection_id=connection.id,
            provider="gcp",
            status="ready",
            message=(
                f"Verified read-only BigQuery billing export access and schema for {scope}."
            ),
            checked_at=checked_at,
            scope_summary=scope,
            has_stored_secret=has_secret,
            supported_source_months=[sample_month],
            warnings=warnings,
            details={
                "projectId": project_id,
                "datasetId": dataset_id,
                "tableId": table_id,
                "location": table_location,
                "maximumBytesBilled": max_bytes,
                "estimatedBytesProcessed": estimated_bytes,
            },
        )

    async def start_or_poll_acquisition(
        self,
        connection: BillingConnection,
        run: BillingAcquisitionRun,
        *,
        secret_value: str | None = None,
    ) -> BillingConnectorStepResult:
        """Submit or poll a bounded, parameterized BigQuery billing query and paginate results."""

        project_id, dataset_id, table_id, scope, max_bytes = self._resolve_table_coordinates(
            connection
        )
        access_token = await self._acquire_access_token(
            connection,
            secret_value=secret_value,
        )
        auth_headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        location = run.operation_location or connection.gcp_location
        job_id = (
            run.operation_id.strip()
            if run.operation_id and run.operation_id.strip()
            else deterministic_bigquery_job_id(
                run.tenant_id,
                run.id,
                run.source_month,
                connection_id=connection.id,
            )
        )

        initial_page_payload: Mapping[str, Any] | None = None

        if not (run.operation_id and run.operation_id.strip()):
            job_payload = self._build_query_job_payload(
                project_id=project_id,
                dataset_id=dataset_id,
                table_id=table_id,
                source_month=run.source_month,
                billing_account_id=connection.gcp_billing_account_id,
                max_bytes_billed=max_bytes,
                job_id=job_id,
                location=location,
                dry_run=False,
            )
            jobs_url = f"{GCP_BIGQUERY_BASE_URL}/projects/{quote(project_id, safe='')}/jobs"
            try:
                submit_resp = await self._client.post(
                    jobs_url,
                    headers=auth_headers,
                    json=job_payload,
                )
            except httpx.HTTPError as exc:
                raise UpstreamError(
                    f"Could not reach Google BigQuery Jobs API to submit query {job_id}."
                ) from exc

            # 409 Conflict means the deterministic jobId was already submitted on an earlier
            # interrupted attempt; proceed to polling it safely.
            if submit_resp.status_code != 409:
                if submit_resp.status_code in (401, 403):
                    err_msg = self._extract_google_api_error_message(
                        submit_resp, access_token, secret_value
                    )
                    raise UpstreamAuthorizationError(
                        f"BigQuery denied query job submission in project {project_id} "
                        f"(HTTP {submit_resp.status_code}): {err_msg}. Grant "
                        f"roles/bigquery.jobUser on project {project_id} and "
                        f"roles/bigquery.dataViewer on {scope}.",
                        details={
                            "projectId": project_id,
                            "datasetId": dataset_id,
                            "tableId": table_id,
                            "jobId": job_id,
                            "httpStatus": submit_resp.status_code,
                            "requiredRoles": [
                                "roles/bigquery.jobUser",
                                "roles/bigquery.dataViewer",
                            ],
                        },
                    )
                if submit_resp.status_code not in (200, 201):
                    err_msg, reason, parsed_errors = self._extract_google_api_error(
                        submit_resp, access_token, secret_value
                    )
                    if self._is_bytes_billed_exceeded(reason, err_msg, parsed_errors):
                        raise ValidationError(
                            f"BigQuery query for {scope} exceeded maximumBytesBilled "
                            f"({max_bytes:,} bytes): {err_msg}",
                            details={
                                "projectId": project_id,
                                "datasetId": dataset_id,
                                "tableId": table_id,
                                "jobId": job_id,
                                "maximumBytesBilled": max_bytes,
                                "reason": "bytesBilledLimitExceeded",
                            },
                        )
                    raise UpstreamError(
                        f"BigQuery rejected query job {job_id} "
                        f"(HTTP {submit_resp.status_code}): {err_msg}",
                        details={
                            "projectId": project_id,
                            "jobId": job_id,
                            "httpStatus": submit_resp.status_code,
                            "reason": reason,
                        },
                    )

                try:
                    submit_json = submit_resp.json()
                except ValueError as exc:
                    raise UpstreamError(
                        "BigQuery returned a non-JSON response when submitting query job."
                    ) from exc

                if isinstance(submit_json, Mapping):
                    job_ref = submit_json.get("jobReference")
                    if isinstance(job_ref, Mapping):
                        returned_job_id = str(job_ref.get("jobId") or "").strip()
                        if returned_job_id:
                            job_id = returned_job_id
                        returned_loc = str(job_ref.get("location") or "").strip()
                        if returned_loc:
                            location = returned_loc

                    job_errors = self._extract_job_errors(
                        submit_json, access_token, secret_value
                    )
                    self._raise_for_bigquery_job_errors(
                        job_errors,
                        project_id=project_id,
                        dataset_id=dataset_id,
                        table_id=table_id,
                        max_bytes_billed=max_bytes,
                        job_id=job_id,
                    )

                    job_complete = submit_json.get("jobComplete")
                    status_obj = submit_json.get("status")
                    state = (
                        str(status_obj.get("state") or "").strip().upper()
                        if isinstance(status_obj, Mapping)
                        else ""
                    )

                    if job_complete is False or state in ("PENDING", "RUNNING"):
                        return BillingConnectorStepResult(
                            status="running",
                            source_scope=scope,
                            source_manifest_id=job_id,
                            operation_id=job_id,
                            operation_location=location,
                            poll_after=self._clock() + timedelta(seconds=self._poll_seconds),
                            status_message=(
                                f"BigQuery billing export query {job_id} is running for "
                                f"{run.source_month}."
                            ),
                        )

                    if job_complete is True and (
                        "rows" in submit_json or "schema" in submit_json
                    ):
                        initial_page_payload = submit_json
                    elif state != "DONE" and job_complete is not True:
                        return BillingConnectorStepResult(
                            status="running",
                            source_scope=scope,
                            source_manifest_id=job_id,
                            operation_id=job_id,
                            operation_location=location,
                            poll_after=self._clock() + timedelta(seconds=self._poll_seconds),
                            status_message=(
                                f"BigQuery billing export query {job_id} submitted for "
                                f"{run.source_month}."
                            ),
                        )

        return await self._poll_and_paginate_results(
            project_id=project_id,
            dataset_id=dataset_id,
            table_id=table_id,
            scope=scope,
            job_id=job_id,
            location=location,
            source_month=run.source_month,
            max_bytes_billed=max_bytes,
            auth_headers=auth_headers,
            access_token=access_token,
            secret_value=secret_value,
            initial_page_token=run.continuation_token,
            initial_page_payload=initial_page_payload,
        )

    async def _fetch_query_results_page(
        self,
        *,
        project_id: str,
        dataset_id: str,
        table_id: str,
        scope: str,
        job_id: str,
        location: str | None,
        max_bytes_billed: int,
        auth_headers: Mapping[str, str],
        access_token: str,
        secret_value: str | None,
        page_token: str | None = None,
    ) -> Mapping[str, Any]:
        queries_url = (
            f"{GCP_BIGQUERY_BASE_URL}/projects/{quote(project_id, safe='')}"
            f"/queries/{quote(job_id, safe='')}"
        )
        params: dict[str, Any] = {"maxResults": str(self._page_size)}
        if location:
            params["location"] = location
        if page_token:
            params["pageToken"] = page_token

        try:
            resp = await self._client.get(
                queries_url,
                headers=auth_headers,
                params=params,
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(
                f"Could not reach Google BigQuery API while polling query {job_id}."
            ) from exc

        if resp.status_code in (401, 403):
            err_msg = self._extract_google_api_error_message(resp, access_token, secret_value)
            raise UpstreamAuthorizationError(
                f"BigQuery denied access while reading results for query {job_id} on {scope} "
                f"(HTTP {resp.status_code}): {err_msg}. Grant roles/bigquery.jobUser on project "
                f"{project_id} and roles/bigquery.dataViewer on {scope}.",
                details={
                    "projectId": project_id,
                    "datasetId": dataset_id,
                    "tableId": table_id,
                    "jobId": job_id,
                    "httpStatus": resp.status_code,
                    "requiredRoles": ["roles/bigquery.jobUser", "roles/bigquery.dataViewer"],
                },
            )

        if resp.status_code != 200:
            err_msg, reason, parsed_errors = self._extract_google_api_error(
                resp, access_token, secret_value
            )
            if self._is_bytes_billed_exceeded(reason, err_msg, parsed_errors):
                raise ValidationError(
                    f"BigQuery query {job_id} for {scope} exceeded maximumBytesBilled "
                    f"({max_bytes_billed:,} bytes): {err_msg} "
                    "Narrow the billing export view or increase gcp_max_bytes_billed.",
                    details={
                        "projectId": project_id,
                        "datasetId": dataset_id,
                        "tableId": table_id,
                        "jobId": job_id,
                        "maximumBytesBilled": max_bytes_billed,
                        "reason": "bytesBilledLimitExceeded",
                    },
                )
            raise UpstreamError(
                f"BigQuery failed while fetching query results for {job_id} "
                f"(HTTP {resp.status_code}): {err_msg}",
                details={
                    "projectId": project_id,
                    "jobId": job_id,
                    "httpStatus": resp.status_code,
                    "reason": reason,
                },
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise UpstreamError(
                f"BigQuery returned a non-JSON query results payload for {job_id}."
            ) from exc

        if not isinstance(data, Mapping):
            raise UpstreamError(
                f"BigQuery returned an unexpected query results structure for {job_id}."
            )
        return data

    async def _poll_and_paginate_results(
        self,
        *,
        project_id: str,
        dataset_id: str,
        table_id: str,
        scope: str,
        job_id: str,
        location: str | None,
        source_month: str,
        max_bytes_billed: int,
        auth_headers: Mapping[str, str],
        access_token: str,
        secret_value: str | None,
        initial_page_token: str | None = None,
        initial_page_payload: Mapping[str, Any] | None = None,
    ) -> BillingConnectorStepResult:
        page_payload = (
            initial_page_payload
            if initial_page_payload is not None
            else await self._fetch_query_results_page(
                project_id=project_id,
                dataset_id=dataset_id,
                table_id=table_id,
                scope=scope,
                job_id=job_id,
                location=location,
                max_bytes_billed=max_bytes_billed,
                auth_headers=auth_headers,
                access_token=access_token,
                secret_value=secret_value,
                page_token=initial_page_token,
            )
        )

        job_errors = self._extract_job_errors(page_payload, access_token, secret_value)
        self._raise_for_bigquery_job_errors(
            job_errors,
            project_id=project_id,
            dataset_id=dataset_id,
            table_id=table_id,
            max_bytes_billed=max_bytes_billed,
            job_id=job_id,
        )

        job_complete = page_payload.get("jobComplete")
        status_obj = page_payload.get("status")
        state = (
            str(status_obj.get("state") or "").strip().upper()
            if isinstance(status_obj, Mapping)
            else ""
        )
        if job_complete is False or state in ("PENDING", "RUNNING"):
            return BillingConnectorStepResult(
                status="running",
                source_scope=scope,
                source_manifest_id=job_id,
                operation_id=job_id,
                operation_location=location,
                continuation_token=initial_page_token,
                poll_after=self._clock() + timedelta(seconds=self._poll_seconds),
                status_message=(
                    f"BigQuery billing export query {job_id} is still running for {source_month}."
                ),
            )

        schema_fields: list[Mapping[str, Any]] = []
        schema_obj = page_payload.get("schema")
        if isinstance(schema_obj, Mapping) and isinstance(schema_obj.get("fields"), list):
            schema_fields = [
                f for f in schema_obj["fields"] if isinstance(f, Mapping)
            ]

        ndjson_chunks: list[bytes] = []
        total_rows = 0
        total_bytes = 0
        seen_tokens: set[str] = set()
        current_payload = page_payload

        while True:
            if not schema_fields:
                curr_schema = current_payload.get("schema")
                if isinstance(curr_schema, Mapping) and isinstance(curr_schema.get("fields"), list):
                    schema_fields = [
                        f for f in curr_schema["fields"] if isinstance(f, Mapping)
                    ]

            raw_rows = current_payload.get("rows")
            if isinstance(raw_rows, list):
                for raw_row in raw_rows:
                    if not isinstance(raw_row, Mapping):
                        continue
                    total_rows += 1
                    if total_rows > self._max_result_rows:
                        raise ValidationError(
                            f"BigQuery billing query {job_id} returned more than the maximum "
                            f"allowed row count ({self._max_result_rows:,} rows).",
                            details={
                                "jobId": job_id,
                                "maxRows": self._max_result_rows,
                            },
                        )
                    row_dict = convert_bigquery_row_to_dict(schema_fields, raw_row)
                    line_bytes = (
                        json.dumps(row_dict, separators=(",", ":"), sort_keys=True) + "\n"
                    ).encode("utf-8")
                    total_bytes += len(line_bytes)
                    if total_bytes > self._max_result_bytes:
                        raise BillingPayloadTooLargeError(
                            f"BigQuery billing query {job_id} result payload exceeds the maximum "
                            f"allowed size ({self._max_result_bytes:,} bytes).",
                            details={
                                "jobId": job_id,
                                "maxBytes": self._max_result_bytes,
                            },
                        )
                    ndjson_chunks.append(line_bytes)

            next_token_raw = current_payload.get("pageToken")
            next_token = str(next_token_raw).strip() if next_token_raw else None
            if not next_token:
                break
            if next_token in seen_tokens:
                raise UpstreamError(
                    f"BigQuery returned a repeating pageToken while paginating query {job_id}.",
                    details={"jobId": job_id},
                )
            seen_tokens.add(next_token)

            current_payload = await self._fetch_query_results_page(
                project_id=project_id,
                dataset_id=dataset_id,
                table_id=table_id,
                scope=scope,
                job_id=job_id,
                location=location,
                max_bytes_billed=max_bytes_billed,
                auth_headers=auth_headers,
                access_token=access_token,
                secret_value=secret_value,
                page_token=next_token,
            )
            page_errors = self._extract_job_errors(current_payload, access_token, secret_value)
            self._raise_for_bigquery_job_errors(
                page_errors,
                project_id=project_id,
                dataset_id=dataset_id,
                table_id=table_id,
                max_bytes_billed=max_bytes_billed,
                job_id=job_id,
            )

        content_bytes = b"".join(ndjson_chunks)
        warnings: list[str] = []
        parts: list[BillingConnectorPayloadPart] = []
        if content_bytes:
            parts.append(
                BillingConnectorPayloadPart(
                    name=f"gcp-{source_month}.jsonl",
                    content=content_bytes,
                    content_type="application/x-ndjson",
                )
            )
        else:
            warnings.append(
                f"BigQuery query {job_id} completed with 0 matching AI model billing rows for "
                f"{source_month}."
            )

        return BillingConnectorStepResult(
            status="completed",
            parts=parts,
            source_scope=scope,
            source_manifest_id=job_id,
            operation_id=job_id,
            operation_location=location,
            continuation_token=None,
            status_message=(
                f"Retrieved {total_rows:,} Cloud Billing export row(s) from {scope} "
                f"(job {job_id})."
            ),
            total_read_bytes=total_bytes,
            warnings=warnings,
        )
