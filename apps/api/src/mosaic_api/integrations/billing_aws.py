"""Read-only AWS CUR / CUR 2.0 S3 billing connector using STS OIDC and pure-Python SigV4."""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx
from azure.core.credentials_async import AsyncTokenCredential

from mosaic_api.billing_pricing import (
    BillingAcquisitionRun,
    BillingConnection,
    BillingConnectionCheckResult,
    BillingConnector,
    BillingConnectorPayloadPart,
    BillingConnectorStepResult,
    BillingProvider,
    ConnectedBillingProvider,
    parse_source_month,
)
from mosaic_api.domain import utc_now
from mosaic_api.errors import (
    DomainError,
    UpstreamAuthorizationError,
    UpstreamError,
    UpstreamNotFoundError,
    ValidationError,
)
from mosaic_api.integrations.mcp.credentials import EntraTokenProvider
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

DEFAULT_AWS_REGION = "us-east-1"
DEFAULT_AWS_OIDC_AUDIENCE = "api://AzureADTokenExchange"
DEFAULT_STS_SESSION_NAME = "mosaic-billing-cur-reader"
DEFAULT_STS_DURATION_SECONDS = 3600
DEFAULT_MAX_REPORT_KEYS = 50
MAX_LIST_PAGES = 5

COST_EXPLORER_UNSUPPORTED_NOTE = (
    "MOSAIC reads detailed AWS Cost and Usage Report (CUR / CUR 2.0) exports from S3 rather "
    "than AWS Cost Explorer (ce:GetCostAndUsage) because Cost Explorer blends mixed usage "
    "units (N/A) and cannot provide an authoritative per-token rate denominator."
)

_BUCKET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_REGION_NAME_RE = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-\d+$")
_BILLING_PERIOD_RANGE_RE = re.compile(
    r"(?<!\d)(?P<y1>20\d{2})(?P<m1>0[1-9]|1[0-2])01(?:T\d{6}(?:\.\d+)?Z)?"
    r"-(?P<y2>20\d{2})(?P<m2>0[1-9]|1[0-2])01(?:T\d{6}(?:\.\d+)?Z)?(?!\d)"
)
_HIVE_BILLING_PERIOD_RE = re.compile(
    r"billing_period=(?P<year>20\d{2})-(?P<month>0[1-9]|1[0-2])",
    re.IGNORECASE,
)
_SLASH_MONTH_RE = re.compile(r"(?:^|/)(?P<year>20\d{2})-(?P<month>0[1-9]|1[0-2])(?:/|$)")
_AWS_ACCESS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_BEDROCK_API_KEY_RE = re.compile(r"\bABSK[A-Za-z0-9+/=_-]{8,}\b")
_SECRET_QUERY_RE = re.compile(
    r"(?i)(WebIdentityToken|X-Amz-Security-Token|X-Amz-Signature|SecretAccessKey|SessionToken)"
    r"=([^&\s\"']+)"
)
_SUPPORTED_REPORT_EXTENSIONS: tuple[str, ...] = (".csv", ".csv.gz", ".gz", ".parquet")


@dataclass(frozen=True)
class AwsSessionCredentials:
    """Short-lived AWS STS credentials. Secret fields are never included in repr or str."""

    access_key_id: str
    secret_access_key: str
    session_token: str | None = None
    expiration: datetime | None = None
    assumed_role_arn: str | None = None

    def __repr__(self) -> str:
        return (
            f"AwsSessionCredentials(access_key_id='***', secret_access_key='***', "
            f"has_session_token={self.session_token is not None}, "
            f"assumed_role_arn={self.assumed_role_arn!r})"
        )

    def __str__(self) -> str:
        return self.__repr__()


@dataclass(frozen=True)
class S3ListedObject:
    """Metadata for one object returned by S3 ListObjectsV2."""

    key: str
    last_modified: str = ""
    size: int = 0
    etag: str | None = None


@dataclass(frozen=True)
class CurManifestDescriptor:
    """Normalized metadata from an AWS CUR or CUR 2.0 manifest JSON document."""

    manifest_key: str
    assembly_id: str
    source_month: str
    currency: str
    report_keys: list[str]
    columns: list[str]
    compression: str | None = None
    content_type: str | None = None


class BillingIngestionEngine:
    """Convenience facade over the shared MOSAIC billing ingestion pipeline."""

    def ingest_parts(
        self,
        parts: Iterable[BillingConnectorPayloadPart],
        *,
        provider: BillingProvider = "aws",
        pricing_cloud: str = "aws-bedrock",
        source_month: str,
        source_scope: str | None = None,
        deployments: Mapping[str, DeploymentFacts] | None = None,
        book: PriceBook | None = None,
        today: date | None = None,
        max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
        max_decompressed_bytes: int = DEFAULT_MAX_DECOMPRESSED_BYTES,
        max_rows: int = DEFAULT_MAX_ROWS,
    ) -> BillingIngestionResult:
        return ingest_billing_parts(
            parts,
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

    def ingest_bytes(
        self,
        content: bytes,
        *,
        file_name: str,
        provider: BillingProvider = "aws",
        pricing_cloud: str = "aws-bedrock",
        source_month: str,
        source_scope: str | None = None,
        deployments: Mapping[str, DeploymentFacts] | None = None,
        book: PriceBook | None = None,
        today: date | None = None,
        max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
        max_decompressed_bytes: int = DEFAULT_MAX_DECOMPRESSED_BYTES,
        max_rows: int = DEFAULT_MAX_ROWS,
    ) -> BillingIngestionResult:
        return ingest_billing_bytes(
            content,
            file_name=file_name,
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


def redact_aws_secrets(text: str, *explicit_secrets: str | None) -> str:
    """Remove AWS access keys, secret keys, session tokens, and OIDC tokens from text."""

    redacted = text
    for secret in explicit_secrets:
        if secret and len(secret.strip()) >= 4:
            redacted = redacted.replace(secret.strip(), "[REDACTED]")
            encoded = quote(secret.strip(), safe="")
            if encoded != secret.strip():
                redacted = redacted.replace(encoded, "[REDACTED]")
    redacted = _AWS_ACCESS_KEY_RE.sub("[REDACTED_AWS_KEY]", redacted)
    redacted = _BEDROCK_API_KEY_RE.sub("[REDACTED_BEDROCK_KEY]", redacted)
    redacted = _SECRET_QUERY_RE.sub(r"\1=[REDACTED]", redacted)
    return redacted


def _uri_encode_path(path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    segments = path.split("/")
    return "/".join(quote(unquote(seg), safe="-_.~") for seg in segments)


def _canonical_query_string(params: Mapping[str, str] | Iterable[tuple[str, str]] | None) -> str:
    if not params:
        return ""
    items = list(params.items()) if isinstance(params, Mapping) else list(params)
    encoded_pairs = [
        (quote(str(k), safe="-_.~"), quote(str(v), safe="-_.~"))
        for k, v in items
    ]
    encoded_pairs.sort(key=lambda pair: (pair[0], pair[1]))
    return "&".join(f"{k}={v}" for k, v in encoded_pairs)


def _hmac_sha256(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def derive_sigv4_signing_key(
    secret_access_key: str,
    date_stamp: str,
    region: str,
    service: str = "s3",
) -> bytes:
    """Derive the AWS Signature Version 4 signing key for ``date_stamp``/``region``/``service``."""

    k_date = _hmac_sha256(f"AWS4{secret_access_key}".encode(), date_stamp)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    return _hmac_sha256(k_service, "aws4_request")


def sign_aws_sigv4_headers(
    *,
    method: str,
    url: str,
    region: str,
    credentials: AwsSessionCredentials,
    timestamp: datetime,
    service: str = "s3",
    params: Mapping[str, str] | None = None,
    body: bytes = b"",
    extra_headers: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build AWS Signature Version 4 headers (``AWS4-HMAC-SHA256``) for an HTTP request."""

    parsed = urlsplit(url)
    host = parsed.netloc
    if not host:
        raise ValidationError("Cannot sign an AWS request without a target host.")

    utc_ts = timestamp.astimezone(UTC)
    amz_date = utc_ts.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = utc_ts.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()

    canonical_uri = _uri_encode_path(parsed.path or "/")
    if params is not None:
        canonical_query = _canonical_query_string(params)
    elif parsed.query:
        raw_pairs: list[tuple[str, str]] = []
        for part in parsed.query.split("&"):
            if not part:
                continue
            k, _, v = part.partition("=")
            raw_pairs.append((unquote(k), unquote(v)))
        canonical_query = _canonical_query_string(raw_pairs)
    else:
        canonical_query = ""

    headers_to_sign: dict[str, str] = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if credentials.session_token:
        headers_to_sign["x-amz-security-token"] = credentials.session_token
    if extra_headers:
        for k, v in extra_headers.items():
            headers_to_sign[k.strip().lower()] = v.strip()

    sorted_header_names = sorted(headers_to_sign)
    canonical_headers = "".join(
        f"{name}:{headers_to_sign[name]}\n" for name in sorted_header_names
    )
    signed_headers = ";".join(sorted_header_names)

    canonical_request = (
        f"{method.upper()}\n"
        f"{canonical_uri}\n"
        f"{canonical_query}\n"
        f"{canonical_headers}\n"
        f"{signed_headers}\n"
        f"{payload_hash}"
    )
    canonical_request_hash = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n"
        f"{amz_date}\n"
        f"{credential_scope}\n"
        f"{canonical_request_hash}"
    )
    signing_key = derive_sigv4_signing_key(
        credentials.secret_access_key,
        date_stamp,
        region,
        service=service,
    )
    signature = hmac.new(
        signing_key,
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    authorization = (
        f"AWS4-HMAC-SHA256 Credential={credentials.access_key_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    request_headers: dict[str, str] = {
        "Authorization": authorization,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
    }
    if credentials.session_token:
        request_headers["x-amz-security-token"] = credentials.session_token
    if extra_headers:
        for k, v in extra_headers.items():
            request_headers[k] = v
    return request_headers


def _resolve_aws_region(connection: BillingConnection) -> str:
    raw_region = (connection.aws_region or "").strip().casefold()
    if not raw_region:
        role_arn = (connection.aws_role_arn or "").strip().casefold()
        if role_arn.startswith("arn:aws-us-gov:"):
            return "us-gov-west-1"
        if role_arn.startswith("arn:aws-cn:"):
            return "cn-north-1"
        return DEFAULT_AWS_REGION
    if not _REGION_NAME_RE.fullmatch(raw_region):
        raise ValidationError(
            f"AWS region {connection.aws_region!r} is not a valid AWS region identifier.",
            details={"awsRegion": connection.aws_region},
        )
    return raw_region


def _aws_dns_suffix(region: str, role_arn: str | None = None) -> str:
    if region.startswith("cn-") or (role_arn and role_arn.startswith("arn:aws-cn:")):
        return "amazonaws.com.cn"
    return "amazonaws.com"


def _validate_cur_bucket(bucket: str | None) -> str:
    cleaned = (bucket or "").strip()
    if not cleaned:
        raise ValidationError("AWS billing connections require a CUR S3 bucket name.")
    if ".." in cleaned or not _BUCKET_NAME_RE.fullmatch(cleaned):
        raise ValidationError(
            f"AWS CUR S3 bucket {bucket!r} is not a valid S3 bucket name.",
            details={"bucket": bucket},
        )
    return cleaned


def _normalize_cur_prefix(prefix: str | None) -> str:
    if prefix is None:
        return ""
    cleaned = prefix.strip()
    if not cleaned or cleaned == "/":
        return ""
    if "\\" in cleaned or "\x00" in cleaned:
        raise ValidationError(
            "AWS CUR S3 prefix contains unsupported path separator or control characters."
        )
    segments = [seg for seg in cleaned.strip("/").split("/") if seg]
    if any(seg in (".", "..") for seg in segments):
        raise ValidationError("AWS CUR S3 prefix must not contain path traversal segments ('..').")
    return "/".join(segments)


def validate_scoped_s3_key(
    raw_key: str,
    *,
    expected_bucket: str,
    normalized_prefix: str,
) -> str:
    """Validate that ``raw_key`` stays within ``expected_bucket`` and ``normalized_prefix``."""

    candidate = raw_key.strip()
    if not candidate:
        raise ValidationError("CUR manifest contained an empty S3 object key.")

    if candidate.casefold().startswith("s3://"):
        without_scheme = candidate[5:]
        bucket_part, sep, key_part = without_scheme.partition("/")
        if not sep or not key_part:
            raise ValidationError(
                "CUR manifest S3 URI is missing an object key.",
                details={"key": raw_key},
            )
        if bucket_part.strip().casefold() != expected_bucket.casefold():
            raise ValidationError(
                f"CUR manifest references bucket {bucket_part!r}, which does not match "
                f"the configured bucket {expected_bucket!r}.",
                details={"expectedBucket": expected_bucket, "actualBucket": bucket_part},
            )
        candidate = key_part

    decoded = unquote(candidate)
    for text_check in (candidate, decoded):
        if "\\" in text_check or "\x00" in text_check:
            raise ValidationError(
                "CUR S3 object key contains forbidden path characters.",
                details={"key": raw_key},
            )
        raw_segments = text_check.strip("/").split("/")
        if any(seg in ("", ".", "..") or ".." in seg for seg in raw_segments):
            raise ValidationError(
                "CUR S3 object key contains path traversal ('..') or empty path segments.",
                details={"key": raw_key},
            )

    cleaned_key = "/".join(candidate.strip("/").split("/"))
    if normalized_prefix:
        if cleaned_key != normalized_prefix and not cleaned_key.startswith(
            f"{normalized_prefix}/"
        ):
            raise ValidationError(
                f"CUR S3 object key {cleaned_key!r} is outside the configured prefix "
                f"{normalized_prefix!r}.",
                details={"key": cleaned_key, "configuredPrefix": normalized_prefix},
            )
    return cleaned_key


def _reject_bedrock_runtime_key(secret_value: str) -> None:
    stripped = secret_value.strip()
    if not stripped:
        return
    if (
        stripped.startswith("ABSK")
        or stripped.casefold().startswith("bedrock-api-key")
        or _BEDROCK_API_KEY_RE.search(stripped)
    ):
        raise ValidationError(
            "A Bedrock model runtime API key cannot be used for AWS billing retrieval. "
            "Configure an IAM role ARN with STS OIDC web identity federation instead.",
            details={"reason": "bedrockRuntimeKeyRejected"},
        )
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return
        if isinstance(parsed, Mapping):
            lower_keys = {str(k).casefold() for k in parsed}
            if lower_keys & {"bedrockapikey", "modelapikey", "apikey", "bedrock_api_key"}:
                raise ValidationError(
                    "A Bedrock model runtime API key cannot be used for AWS billing retrieval. "
                    "Configure an IAM role ARN with STS OIDC web identity federation instead.",
                    details={"reason": "bedrockRuntimeKeyRejected"},
                )


def _extract_source_month_from_string(text: str) -> str | None:
    range_match = _BILLING_PERIOD_RANGE_RE.search(text)
    if range_match:
        return f"{range_match.group('y1')}-{range_match.group('m1')}"
    hive_match = _HIVE_BILLING_PERIOD_RE.search(text)
    if hive_match:
        return f"{hive_match.group('year')}-{hive_match.group('month')}"
    slash_match = _SLASH_MONTH_RE.search(text)
    if slash_match:
        return f"{slash_match.group('year')}-{slash_match.group('month')}"
    return None


def _extract_source_month_from_manifest(
    manifest_key: str,
    payload: Mapping[str, Any],
) -> str | None:
    bp = payload.get("billingPeriod")
    if isinstance(bp, Mapping):
        start_raw = str(bp.get("start") or bp.get("startDate") or "").strip()
        if re.match(r"^20\d{2}(?:0[1-9]|1[0-2])01", start_raw):
            return f"{start_raw[:4]}-{start_raw[4:6]}"
        if re.match(r"^20\d{2}-(?:0[1-9]|1[0-2])", start_raw):
            return start_raw[:7]
    elif isinstance(bp, str) and bp.strip():
        from_bp = _extract_source_month_from_string(bp.strip())
        if from_bp:
            return from_bp
        if re.match(r"^20\d{2}-(?:0[1-9]|1[0-2])$", bp.strip()):
            return bp.strip()
    return _extract_source_month_from_string(manifest_key)


def _strip_xml_namespaces(elem: ET.Element) -> None:
    for item in elem.iter():
        if "}" in item.tag:
            item.tag = item.tag.split("}", 1)[1]


def _parse_s3_error(response: httpx.Response) -> tuple[str, str]:
    text = response.text.strip()
    if text.startswith("<"):
        try:
            root = ET.fromstring(text)
            _strip_xml_namespaces(root)
            code = (root.findtext(".//Code") or "").strip()
            msg = (root.findtext(".//Message") or "").strip()
            if code or msg:
                return code or f"HTTP{response.status_code}", msg or "AWS request failed"
        except ET.ParseError:
            pass
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, Mapping):
                err = parsed.get("Error") or parsed.get("error") or parsed
                if isinstance(err, Mapping):
                    code = str(err.get("Code") or err.get("code") or "").strip()
                    msg = str(err.get("Message") or err.get("message") or "").strip()
                    if code or msg:
                        return code or f"HTTP{response.status_code}", msg or "AWS request failed"
        except json.JSONDecodeError:
            pass
    return f"HTTP{response.status_code}", text[:240] or f"HTTP {response.status_code}"


def _is_expired_token_response(status_code: int, error_code: str, error_message: str) -> bool:
    if status_code not in (400, 401, 403):
        return False
    combined = f"{error_code} {error_message}".casefold()
    return any(
        token in combined
        for token in (
            "expiredtoken",
            "expiredtokenexception",
            "requestexpired",
            "tokenrefreshrequired",
            "the provided token has expired",
        )
    )


def _s3_permission_guidance(bucket: str, prefix: str, role_arn: str) -> str:
    object_arn = f"arn:aws:s3:::{bucket}/{prefix}/*" if prefix else f"arn:aws:s3:::{bucket}/*"
    prefix_cond = f" (with s3:prefix condition for '{prefix}/*')" if prefix else ""
    return (
        f"Verify that IAM role {role_arn} trusts MOSAIC's OIDC identity for "
        f"sts:AssumeRoleWithWebIdentity and grants read-only S3 permissions: "
        f"s3:ListBucket on arn:aws:s3:::{bucket}{prefix_cond} and "
        f"s3:GetObject on {object_arn}."
    )


def _sts_permission_guidance(connection: BillingConnection) -> str:
    role_arn = connection.aws_role_arn or "<aws-role-arn>"
    audience = connection.aws_audience or DEFAULT_AWS_OIDC_AUDIENCE
    bucket = connection.aws_cur_bucket or "<cur-bucket>"
    prefix = _normalize_cur_prefix(connection.aws_cur_prefix)
    return (
        f"AWS STS refused AssumeRoleWithWebIdentity for {role_arn}. Grant "
        f"sts:AssumeRoleWithWebIdentity in the role trust policy for audience {audience!r}, "
        f"plus s3:ListBucket on arn:aws:s3:::{bucket} and "
        f"s3:GetObject on arn:aws:s3:::{bucket}/{prefix + '/' if prefix else ''}*."
    )


def _classify_manifest_key(
    key: str,
    *,
    source_month: str,
    export_name: str | None = None,
) -> tuple[bool, bool, str]:
    """Return ``(matches_month, is_top_level_pointer, assembly_segment)`` for a manifest key.

    In AWS CUR and CUR 2.0:
    - Top-level pointer manifest:
      ``{prefix}/{export}/20260901-20261001/{export}-Manifest.json`` or
      ``{prefix}/{export}/metadata/20260901-20261001/{export}-Manifest.json`` or
      ``{prefix}/{export}/metadata/BILLING_PERIOD=2026-09/{export}-Manifest.json``
    - Assembly/revision manifest:
      ``{prefix}/{export}/20260901-20261001/{assemblyId}/{export}-Manifest.json``
    """

    if not key.casefold().endswith("manifest.json"):
        return False, False, ""

    inferred_month = _extract_source_month_from_string(key)
    if inferred_month != source_month:
        return False, False, ""

    if export_name:
        exp_folded = export_name.strip().casefold()
        segments_folded = [seg.casefold() for seg in key.strip("/").split("/")]
        file_folded = segments_folded[-1] if segments_folded else ""
        if exp_folded not in segments_folded and not file_folded.startswith(exp_folded):
            return False, False, ""

    segments = [seg for seg in key.strip("/").split("/") if seg]
    period_idx = -1
    for idx, seg in enumerate(segments[:-1]):
        if _extract_source_month_from_string(seg) == source_month:
            period_idx = idx
            break

    if period_idx == -1:
        return True, True, ""

    trailing_dirs = segments[period_idx + 1 : -1]
    if not trailing_dirs:
        return True, True, ""
    return True, False, "/".join(trailing_dirs)


class AwsBillingConnector(BillingConnector):
    """Connected AWS billing adapter for CUR / CUR 2.0 exports stored in Amazon S3."""

    provider: ConnectedBillingProvider = "aws"

    def __init__(
        self,
        *,
        azure_credential: AsyncTokenCredential | None = None,
        client: httpx.AsyncClient | None = None,
        web_identity_token_provider: (
            Callable[..., Awaitable[str] | str] | None
        ) = None,
        session_credentials_provider: (
            Callable[..., Awaitable[AwsSessionCredentials] | AwsSessionCredentials] | None
        ) = None,
        clock: Callable[[], datetime] = utc_now,
        max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
        max_report_keys: int = DEFAULT_MAX_REPORT_KEYS,
        sts_endpoint: str | None = None,
        s3_endpoint: str | None = None,
        use_path_style: bool = False,
        timeout: float = 30.0,
    ) -> None:
        self._azure_credential = azure_credential
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0),
            follow_redirects=False,
        )
        self._owns_client = client is None
        self._web_identity_token_provider = web_identity_token_provider
        self._session_credentials_provider = session_credentials_provider
        self._clock = clock
        self._max_upload_bytes = max_upload_bytes
        self._max_report_keys = max_report_keys
        self._sts_endpoint = sts_endpoint.rstrip("/") if sts_endpoint else None
        self._s3_endpoint = s3_endpoint.rstrip("/") if s3_endpoint else None
        self._use_path_style = use_path_style

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _resolve_web_identity_token(
        self,
        connection: BillingConnection,
        *,
        secret_value: str | None,
    ) -> str:
        audience = (connection.aws_audience or "").strip() or DEFAULT_AWS_OIDC_AUDIENCE

        if secret_value is not None and secret_value.strip():
            _reject_bedrock_runtime_key(secret_value)
            stripped = secret_value.strip()
            if stripped.startswith("{"):
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise ValidationError(
                        "Stored AWS billing credential JSON is malformed."
                    ) from exc
                if isinstance(parsed, Mapping):
                    for key_name in (
                        "webIdentityToken",
                        "web_identity_token",
                        "oidcToken",
                        "oidc_token",
                        "token",
                    ):
                        tok_val = parsed.get(key_name)
                        if isinstance(tok_val, str) and tok_val.strip():
                            _reject_bedrock_runtime_key(tok_val)
                            return tok_val.strip()
            return stripped

        if self._web_identity_token_provider is not None:
            sig = inspect.signature(self._web_identity_token_provider)
            if len(sig.parameters) >= 2:
                maybe_token = self._web_identity_token_provider(connection, audience)
            else:
                maybe_token = self._web_identity_token_provider(audience)
            token = await maybe_token if inspect.isawaitable(maybe_token) else maybe_token
            if not isinstance(token, str) or not token.strip():
                raise UpstreamAuthorizationError(
                    "Configured web identity token provider returned an empty OIDC token.",
                    details={"audience": audience},
                )
            _reject_bedrock_runtime_key(token)
            return token.strip()

        if self._azure_credential is not None:
            raw_token = await EntraTokenProvider(self._azure_credential).token_for(audience)
            _reject_bedrock_runtime_key(raw_token)
            return raw_token.strip()

        raise UpstreamAuthorizationError(
            f"No OIDC web identity token source is configured for AWS role "
            f"{connection.aws_role_arn}. Configure federated managed-identity token exchange "
            f"for audience {audience!r} or provide a web identity credential reference.",
            details={"audience": audience, "roleArn": connection.aws_role_arn},
        )

    async def _assume_role_credentials(
        self,
        connection: BillingConnection,
        *,
        secret_value: str | None,
        refresh: bool = False,
    ) -> AwsSessionCredentials:
        if secret_value is not None and secret_value.strip():
            _reject_bedrock_runtime_key(secret_value)

        if self._session_credentials_provider is not None:
            sig = inspect.signature(self._session_credentials_provider)
            if "refresh" in sig.parameters:
                maybe_creds = self._session_credentials_provider(
                    connection, secret_value, refresh=refresh
                )
            elif len(sig.parameters) >= 2:
                maybe_creds = self._session_credentials_provider(connection, secret_value)
            else:
                maybe_creds = self._session_credentials_provider(connection)
            creds = await maybe_creds if inspect.isawaitable(maybe_creds) else maybe_creds
            if not creds.access_key_id or not creds.secret_access_key:
                raise UpstreamAuthorizationError(
                    "AWS session credential provider returned incomplete credentials."
                )
            return creds

        if connection.aws_external_id:
            raise ValidationError(
                "aws_external_id is not supported by AssumeRoleWithWebIdentity. Remove the "
                "external ID for OIDC federation; external IDs require signed STS AssumeRole, "
                "which this connector's built-in authentication does not perform.",
                details={
                    "field": "aws_external_id",
                    "reason": "externalIdUnsupportedForWebIdentity",
                },
            )

        # Support JSON session credentials in secret_value only if explicitly structured as STS keys
        if secret_value is not None and secret_value.strip().startswith("{"):
            try:
                parsed_secret = json.loads(secret_value.strip())
            except json.JSONDecodeError as exc:
                raise ValidationError("Stored AWS billing secret is not valid JSON.") from exc
            if isinstance(parsed_secret, Mapping):
                ak = parsed_secret.get("accessKeyId") or parsed_secret.get("access_key_id")
                sk = parsed_secret.get("secretAccessKey") or parsed_secret.get("secret_access_key")
                st = parsed_secret.get("sessionToken") or parsed_secret.get("session_token")
                if isinstance(ak, str) and ak.strip() and isinstance(sk, str) and sk.strip():
                    _reject_bedrock_runtime_key(ak)
                    return AwsSessionCredentials(
                        access_key_id=ak.strip(),
                        secret_access_key=sk.strip(),
                        session_token=st.strip() if isinstance(st, str) and st.strip() else None,
                        assumed_role_arn=connection.aws_role_arn,
                    )

        web_token = await self._resolve_web_identity_token(
            connection, secret_value=secret_value
        )
        region = _resolve_aws_region(connection)
        role_arn = (connection.aws_role_arn or "").strip()
        if not role_arn:
            raise ValidationError("AWS billing connections require an IAM role ARN.")

        dns_suffix = _aws_dns_suffix(region, role_arn)
        sts_url = self._sts_endpoint or f"https://sts.{region}.{dns_suffix}/"
        if not sts_url.endswith("/"):
            sts_url = f"{sts_url}/"

        form_data: dict[str, str] = {
            "Action": "AssumeRoleWithWebIdentity",
            "Version": "2011-06-15",
            "RoleArn": role_arn,
            "RoleSessionName": DEFAULT_STS_SESSION_NAME,
            "WebIdentityToken": web_token,
            "DurationSeconds": str(DEFAULT_STS_DURATION_SECONDS),
        }
        try:
            response = await self._client.post(
                sts_url,
                data=form_data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/xml, application/json",
                },
            )
        except httpx.HTTPError as exc:
            safe_reason = redact_aws_secrets(str(exc), secret_value, web_token)
            raise UpstreamError(
                f"MOSAIC could not reach AWS STS ({sts_url}) to assume role {role_arn}.",
                details={"roleArn": role_arn, "reason": safe_reason},
            ) from exc

        if response.status_code >= 400:
            err_code, err_msg = _parse_s3_error(response)
            safe_err_msg = redact_aws_secrets(err_msg, secret_value, web_token)
            guidance = _sts_permission_guidance(connection)
            raise UpstreamAuthorizationError(
                f"{guidance} (AWS STS {err_code}: {safe_err_msg})",
                details={
                    "status": response.status_code,
                    "awsErrorCode": err_code,
                    "roleArn": role_arn,
                    "requiredPermissions": [
                        "sts:AssumeRoleWithWebIdentity",
                        "s3:ListBucket",
                        "s3:GetObject",
                    ],
                },
            )

        return self._parse_sts_credentials_response(
            response.text,
            role_arn=role_arn,
            secret_value=secret_value,
            web_token=web_token,
        )

    @staticmethod
    def _parse_sts_credentials_response(
        raw_body: str,
        *,
        role_arn: str,
        secret_value: str | None = None,
        web_token: str | None = None,
    ) -> AwsSessionCredentials:
        stripped = raw_body.strip()
        access_key_id = ""
        secret_access_key = ""
        session_token: str | None = None
        assumed_arn: str | None = None

        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise UpstreamError("AWS STS returned malformed JSON.") from exc
            if isinstance(parsed, Mapping):
                root_obj = (
                    parsed.get("AssumeRoleWithWebIdentityResponse")
                    or parsed.get("AssumeRoleWithWebIdentityResult")
                    or parsed
                )
                if isinstance(root_obj, Mapping) and "AssumeRoleWithWebIdentityResult" in root_obj:
                    root_obj = root_obj["AssumeRoleWithWebIdentityResult"]
                creds_obj = (
                    root_obj.get("Credentials") or root_obj.get("credentials")
                    if isinstance(root_obj, Mapping)
                    else None
                )
                if isinstance(creds_obj, Mapping):
                    access_key_id = str(
                        creds_obj.get("AccessKeyId") or creds_obj.get("accessKeyId") or ""
                    ).strip()
                    secret_access_key = str(
                        creds_obj.get("SecretAccessKey") or creds_obj.get("secretAccessKey") or ""
                    ).strip()
                    raw_tok = creds_obj.get("SessionToken") or creds_obj.get("sessionToken")
                    session_token = str(raw_tok).strip() if raw_tok else None
        elif stripped.startswith("<"):
            try:
                root = ET.fromstring(stripped)
                _strip_xml_namespaces(root)
                access_key_id = (root.findtext(".//Credentials/AccessKeyId") or "").strip()
                secret_access_key = (
                    root.findtext(".//Credentials/SecretAccessKey") or ""
                ).strip()
                raw_tok_xml = (root.findtext(".//Credentials/SessionToken") or "").strip()
                session_token = raw_tok_xml or None
                assumed_arn = (root.findtext(".//AssumedRoleUser/Arn") or "").strip() or None
            except ET.ParseError as exc:
                safe_preview = redact_aws_secrets(stripped[:160], secret_value, web_token)
                raise UpstreamError(
                    "AWS STS returned an unreadable XML response.",
                    details={"responsePreview": safe_preview},
                ) from exc

        if not access_key_id or not secret_access_key or not session_token:
            raise UpstreamError(
                "AWS STS AssumeRoleWithWebIdentity response did not include temporary "
                "session credentials (AccessKeyId, SecretAccessKey, SessionToken).",
                details={"roleArn": role_arn},
            )

        return AwsSessionCredentials(
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            session_token=session_token,
            assumed_role_arn=assumed_arn or role_arn,
        )

    def _build_s3_url(
        self, *, bucket: str, region: str, role_arn: str | None, key: str = ""
    ) -> str:
        dns_suffix = _aws_dns_suffix(region, role_arn)
        encoded_key = (
            "/".join(quote(seg, safe="-_.~") for seg in key.strip("/").split("/"))
            if key
            else ""
        )

        if self._s3_endpoint:
            base = self._s3_endpoint.rstrip("/")
            if encoded_key:
                return f"{base}/{bucket}/{encoded_key}"
            return f"{base}/{bucket}"

        use_path = self._use_path_style or ("." in bucket)
        if use_path:
            if encoded_key:
                return f"https://s3.{region}.{dns_suffix}/{bucket}/{encoded_key}"
            return f"https://s3.{region}.{dns_suffix}/{bucket}"

        if encoded_key:
            return f"https://{bucket}.s3.{region}.{dns_suffix}/{encoded_key}"
        return f"https://{bucket}.s3.{region}.{dns_suffix}/"

    async def _s3_get(
        self,
        connection: BillingConnection,
        *,
        bucket: str,
        region: str,
        credentials: AwsSessionCredentials,
        secret_value: str | None,
        key: str = "",
        params: Mapping[str, str] | None = None,
        allow_refresh: bool = True,
    ) -> tuple[httpx.Response, AwsSessionCredentials]:
        url = self._build_s3_url(
            bucket=bucket,
            region=region,
            role_arn=connection.aws_role_arn,
            key=key,
        )
        headers = sign_aws_sigv4_headers(
            method="GET",
            url=url,
            region=region,
            credentials=credentials,
            timestamp=self._clock(),
            service="s3",
            params=params,
        )
        try:
            response = await self._client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            safe_reason = redact_aws_secrets(
                str(exc),
                secret_value,
                credentials.access_key_id,
                credentials.secret_access_key,
                credentials.session_token,
            )
            raise UpstreamError(
                f"MOSAIC could not reach Amazon S3 bucket {bucket!r} in {region}.",
                details={"bucket": bucket, "region": region, "reason": safe_reason},
            ) from exc

        if response.status_code >= 400 and allow_refresh:
            err_code, err_msg = _parse_s3_error(response)
            if _is_expired_token_response(response.status_code, err_code, err_msg):
                refreshed = await self._assume_role_credentials(
                    connection,
                    secret_value=secret_value,
                    refresh=True,
                )
                return await self._s3_get(
                    connection,
                    bucket=bucket,
                    region=region,
                    credentials=refreshed,
                    secret_value=secret_value,
                    key=key,
                    params=params,
                    allow_refresh=False,
                )

        return response, credentials

    async def _list_s3_objects(
        self,
        connection: BillingConnection,
        *,
        bucket: str,
        region: str,
        prefix: str,
        credentials: AwsSessionCredentials,
        secret_value: str | None,
        max_keys: int = 200,
        max_pages: int = MAX_LIST_PAGES,
    ) -> tuple[list[S3ListedObject], AwsSessionCredentials]:
        objects: list[S3ListedObject] = []
        continuation_token: str | None = None
        current_creds = credentials

        for _ in range(max_pages):
            params: dict[str, str] = {
                "list-type": "2",
                "max-keys": str(max_keys),
            }
            if prefix:
                params["prefix"] = prefix
            if continuation_token:
                params["continuation-token"] = continuation_token

            response, current_creds = await self._s3_get(
                connection,
                bucket=bucket,
                region=region,
                credentials=current_creds,
                secret_value=secret_value,
                key="",
                params=params,
            )
            if response.status_code in (401, 403):
                err_code, err_msg = _parse_s3_error(response)
                safe_msg = redact_aws_secrets(
                    err_msg,
                    secret_value,
                    current_creds.access_key_id,
                    current_creds.secret_access_key,
                    current_creds.session_token,
                )
                guidance = _s3_permission_guidance(
                    bucket, prefix, connection.aws_role_arn or "<aws-role-arn>"
                )
                raise UpstreamAuthorizationError(
                    f"{guidance} (S3 {err_code}: {safe_msg})",
                    details={
                        "status": response.status_code,
                        "awsErrorCode": err_code,
                        "bucket": bucket,
                        "prefix": prefix,
                        "requiredPermissions": [
                            "sts:AssumeRoleWithWebIdentity",
                            "s3:ListBucket",
                            "s3:GetObject",
                        ],
                    },
                )
            if response.status_code == 404:
                raise UpstreamNotFoundError(
                    f"Amazon S3 bucket {bucket!r} was not found in region {region!r}.",
                    details={"bucket": bucket, "region": region},
                )
            if response.status_code >= 400:
                err_code, err_msg = _parse_s3_error(response)
                safe_msg = redact_aws_secrets(
                    err_msg,
                    secret_value,
                    current_creds.access_key_id,
                    current_creds.secret_access_key,
                    current_creds.session_token,
                )
                raise UpstreamError(
                    f"Amazon S3 ListObjectsV2 failed on bucket {bucket!r} "
                    f"({err_code}: {safe_msg}).",
                    details={"status": response.status_code, "awsErrorCode": err_code},
                )

            page_objs, next_token = self._parse_list_objects_v2(
                response.text,
                expected_bucket=bucket,
                normalized_prefix=prefix,
            )
            objects.extend(page_objs)
            if not next_token:
                break
            continuation_token = next_token

        return objects, current_creds

    @staticmethod
    def _parse_list_objects_v2(
        raw_text: str,
        *,
        expected_bucket: str,
        normalized_prefix: str,
    ) -> tuple[list[S3ListedObject], str | None]:
        stripped = raw_text.strip()
        items: list[S3ListedObject] = []
        next_token: str | None = None

        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise UpstreamError("Amazon S3 returned malformed JSON list output.") from exc
            if isinstance(parsed, Mapping):
                contents = parsed.get("Contents") or parsed.get("contents") or []
                if isinstance(contents, list):
                    for entry in contents:
                        if not isinstance(entry, Mapping):
                            continue
                        raw_k = str(entry.get("Key") or entry.get("key") or "").strip()
                        if not raw_k:
                            continue
                        valid_k = validate_scoped_s3_key(
                            raw_k,
                            expected_bucket=expected_bucket,
                            normalized_prefix=normalized_prefix,
                        )
                        items.append(
                            S3ListedObject(
                                key=valid_k,
                                last_modified=str(
                                    entry.get("LastModified") or entry.get("lastModified") or ""
                                ).strip(),
                                size=int(entry.get("Size") or entry.get("size") or 0),
                                etag=str(entry.get("ETag") or entry.get("etag") or "").strip('"')
                                or None,
                            )
                        )
                if parsed.get("IsTruncated") or parsed.get("isTruncated"):
                    nt = parsed.get("NextContinuationToken") or parsed.get("nextContinuationToken")
                    next_token = str(nt).strip() if nt else None
            return items, next_token

        try:
            root = ET.fromstring(stripped)
            _strip_xml_namespaces(root)
        except ET.ParseError as exc:
            raise UpstreamError("Amazon S3 returned unreadable ListObjectsV2 XML.") from exc

        for content_el in root.findall(".//Contents"):
            raw_key = (content_el.findtext("Key") or "").strip()
            if not raw_key:
                continue
            valid_key = validate_scoped_s3_key(
                raw_key,
                expected_bucket=expected_bucket,
                normalized_prefix=normalized_prefix,
            )
            last_mod = (content_el.findtext("LastModified") or "").strip()
            size_raw = (content_el.findtext("Size") or "0").strip()
            try:
                size_int = int(size_raw)
            except ValueError:
                size_int = 0
            etag_raw = (content_el.findtext("ETag") or "").strip().strip('"') or None
            items.append(
                S3ListedObject(
                    key=valid_key,
                    last_modified=last_mod,
                    size=size_int,
                    etag=etag_raw,
                )
            )

        is_trunc = (root.findtext(".//IsTruncated") or "").strip().casefold() == "true"
        if is_trunc:
            next_token = (root.findtext(".//NextContinuationToken") or "").strip() or None
        return items, next_token

    async def _fetch_and_parse_manifest(
        self,
        connection: BillingConnection,
        *,
        bucket: str,
        region: str,
        normalized_prefix: str,
        manifest_key: str,
        source_month: str,
        credentials: AwsSessionCredentials,
        secret_value: str | None,
        fallback_assembly_id: str | None = None,
    ) -> tuple[CurManifestDescriptor, int, AwsSessionCredentials]:
        scoped_manifest_key = validate_scoped_s3_key(
            manifest_key,
            expected_bucket=bucket,
            normalized_prefix=normalized_prefix,
        )
        response, updated_creds = await self._s3_get(
            connection,
            bucket=bucket,
            region=region,
            credentials=credentials,
            secret_value=secret_value,
            key=scoped_manifest_key,
        )
        if response.status_code in (401, 403):
            err_code, err_msg = _parse_s3_error(response)
            safe_msg = redact_aws_secrets(
                err_msg,
                secret_value,
                updated_creds.access_key_id,
                updated_creds.secret_access_key,
                updated_creds.session_token,
            )
            guidance = _s3_permission_guidance(
                bucket, normalized_prefix, connection.aws_role_arn or "<aws-role-arn>"
            )
            raise UpstreamAuthorizationError(
                f"{guidance} (S3 {err_code}: {safe_msg})",
                details={
                    "status": response.status_code,
                    "awsErrorCode": err_code,
                    "bucket": bucket,
                    "manifestKey": scoped_manifest_key,
                    "requiredPermissions": [
                        "sts:AssumeRoleWithWebIdentity",
                        "s3:ListBucket",
                        "s3:GetObject",
                    ],
                },
            )
        if response.status_code == 404:
            raise UpstreamNotFoundError(
                f"AWS CUR manifest {scoped_manifest_key!r} was not found in bucket {bucket!r}.",
                details={"bucket": bucket, "manifestKey": scoped_manifest_key},
            )
        if response.status_code >= 400:
            err_code, err_msg = _parse_s3_error(response)
            raise UpstreamError(
                f"Failed to read AWS CUR manifest {scoped_manifest_key!r} ({err_code}).",
                details={"status": response.status_code, "manifestKey": scoped_manifest_key},
            )

        manifest_bytes = response.content
        try:
            payload = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError(
                f"AWS CUR manifest {scoped_manifest_key!r} is not valid JSON.",
                details={"manifestKey": scoped_manifest_key},
            ) from exc

        if not isinstance(payload, Mapping):
            raise ValidationError(
                f"AWS CUR manifest {scoped_manifest_key!r} must be a JSON object.",
                details={"manifestKey": scoped_manifest_key},
            )

        manifest_bucket = str(payload.get("bucket") or "").strip()
        if manifest_bucket and manifest_bucket.casefold() != bucket.casefold():
            raise ValidationError(
                f"AWS CUR manifest {scoped_manifest_key!r} declares bucket {manifest_bucket!r}, "
                f"which does not match configured bucket {bucket!r}.",
                details={"expectedBucket": bucket, "manifestBucket": manifest_bucket},
            )

        raw_assembly = (
            payload.get("assemblyId")
            or payload.get("manifestId")
            or payload.get("executionId")
            or payload.get("exportId")
            or fallback_assembly_id
        )
        assembly_id = (
            str(raw_assembly).strip()
            if raw_assembly is not None and str(raw_assembly).strip()
            else hashlib.sha256(manifest_bytes).hexdigest()[:16]
        )

        # If this top-level pointer manifest delegates to an assembly manifest without inline
        # reportKeys, follow the assembly manifest key once.
        raw_report_keys = (
            payload.get("reportKeys")
            or payload.get("dataFiles")
            or payload.get("files")
        )
        pointer_manifest_key = payload.get("manifestKey") or payload.get("assemblyManifestKey")
        if (
            not raw_report_keys
            and isinstance(pointer_manifest_key, str)
            and pointer_manifest_key.strip()
        ):
            delegated_desc, delegated_bytes, updated_creds = await self._fetch_and_parse_manifest(
                connection,
                bucket=bucket,
                region=region,
                normalized_prefix=normalized_prefix,
                manifest_key=pointer_manifest_key.strip(),
                source_month=source_month,
                credentials=updated_creds,
                secret_value=secret_value,
                fallback_assembly_id=assembly_id,
            )
            return delegated_desc, len(manifest_bytes) + delegated_bytes, updated_creds

        if not isinstance(raw_report_keys, list) or not raw_report_keys:
            raise ValidationError(
                f"AWS CUR manifest {scoped_manifest_key!r} contains no reportKeys.",
                details={"manifestKey": scoped_manifest_key, "assemblyId": assembly_id},
            )
        if len(raw_report_keys) > self._max_report_keys:
            raise ValidationError(
                f"AWS CUR manifest {scoped_manifest_key!r} lists {len(raw_report_keys)} "
                f"report files, exceeding the maximum allowed ({self._max_report_keys}).",
                details={
                    "manifestKey": scoped_manifest_key,
                    "reportKeyCount": len(raw_report_keys),
                    "maxReportKeys": self._max_report_keys,
                },
            )

        validated_keys: list[str] = []
        seen_keys: set[str] = set()
        for entry in raw_report_keys:
            raw_rk = (
                str(entry.get("key") or entry.get("path") or "")
                if isinstance(entry, Mapping)
                else str(entry or "")
            ).strip()
            scoped_rk = validate_scoped_s3_key(
                raw_rk,
                expected_bucket=bucket,
                normalized_prefix=normalized_prefix,
            )
            lower_rk = scoped_rk.casefold()
            if not lower_rk.endswith(_SUPPORTED_REPORT_EXTENSIONS):
                raise ValidationError(
                    f"CUR report object {scoped_rk!r} has an unsupported file format; "
                    f"expected .csv, .csv.gz, or .parquet.",
                    details={"reportKey": scoped_rk},
                )
            if scoped_rk not in seen_keys:
                seen_keys.add(scoped_rk)
                validated_keys.append(scoped_rk)

        currency = str(
            payload.get("currencyCode") or payload.get("currency") or "USD"
        ).strip().upper()
        if currency != "USD":
            raise ValidationError(
                f"MOSAIC accepts USD-denominated AWS CUR exports only; manifest "
                f"{scoped_manifest_key!r} is denominated in {currency!r}.",
                details={"code": "nonUsdCurrency", "currency": currency},
            )

        raw_columns = payload.get("columns") or []
        columns: list[str] = []
        if isinstance(raw_columns, list):
            for col in raw_columns:
                if isinstance(col, Mapping):
                    cat = str(col.get("category") or "").strip()
                    name = str(col.get("name") or "").strip()
                    columns.append(f"{cat}/{name}" if cat and name else name or cat)
                elif isinstance(col, str) and col.strip():
                    columns.append(col.strip())

        detected_month = (
            _extract_source_month_from_manifest(scoped_manifest_key, payload) or source_month
        )
        compression = str(payload.get("compression") or "").strip() or None
        content_type = str(payload.get("contentType") or "").strip() or None

        return (
            CurManifestDescriptor(
                manifest_key=scoped_manifest_key,
                assembly_id=assembly_id,
                source_month=detected_month,
                currency=currency,
                report_keys=validated_keys,
                columns=columns,
                compression=compression,
                content_type=content_type,
            ),
            len(manifest_bytes),
            updated_creds,
        )

    async def _resolve_authoritative_manifest(
        self,
        connection: BillingConnection,
        *,
        bucket: str,
        region: str,
        normalized_prefix: str,
        source_month: str,
        credentials: AwsSessionCredentials,
        secret_value: str | None,
    ) -> tuple[CurManifestDescriptor, int, AwsSessionCredentials]:
        """Locate the single authoritative CUR / CUR 2.0 manifest for ``source_month``."""

        listed_objects, current_creds = await self._list_s3_objects(
            connection,
            bucket=bucket,
            region=region,
            prefix=normalized_prefix,
            credentials=credentials,
            secret_value=secret_value,
        )

        top_level_candidates: list[S3ListedObject] = []
        assembly_candidates: list[ tuple[S3ListedObject, str] ] = []

        for obj in listed_objects:
            matches, is_top_level, assembly_seg = _classify_manifest_key(
                obj.key,
                source_month=source_month,
                export_name=connection.aws_export_name,
            )
            if not matches:
                continue
            if is_top_level:
                top_level_candidates.append(obj)
            else:
                assembly_candidates.append((obj, assembly_seg))

        if top_level_candidates:
            # Prefer the most recently modified top-level pointer manifest
            chosen_top = max(
                top_level_candidates,
                key=lambda item: (item.last_modified, item.key),
            )
            return await self._fetch_and_parse_manifest(
                connection,
                bucket=bucket,
                region=region,
                normalized_prefix=normalized_prefix,
                manifest_key=chosen_top.key,
                source_month=source_month,
                credentials=current_creds,
                secret_value=secret_value,
            )

        if assembly_candidates:
            # Select ONLY the single latest assembly manifest so reruns are never double-counted
            chosen_asm_obj, chosen_asm_seg = max(
                assembly_candidates,
                key=lambda pair: (pair[0].last_modified, pair[1], pair[0].key),
            )
            return await self._fetch_and_parse_manifest(
                connection,
                bucket=bucket,
                region=region,
                normalized_prefix=normalized_prefix,
                manifest_key=chosen_asm_obj.key,
                source_month=source_month,
                credentials=current_creds,
                secret_value=secret_value,
                fallback_assembly_id=chosen_asm_seg.split("/")[0],
            )

        # Fallback: if ListObjectsV2 returned no manifest keys for source_month, probe standard
        # CUR / CUR 2.0 manifest paths directly if aws_export_name is known.
        start_date, end_date = parse_source_month(source_month)
        next_year = end_date.year + 1 if end_date.month == 12 else end_date.year
        next_month = 1 if end_date.month == 12 else end_date.month + 1
        period_range = (
            f"{start_date.year:04d}{start_date.month:02d}01-"
            f"{next_year:04d}{next_month:02d}01"
        )
        export_name = (connection.aws_export_name or "cur-export").strip()
        base_prefix = f"{normalized_prefix}/" if normalized_prefix else ""

        probe_keys = [
            f"{base_prefix}{export_name}/metadata/{period_range}/{export_name}-Manifest.json",
            f"{base_prefix}{export_name}/{period_range}/{export_name}-Manifest.json",
            f"{base_prefix}{period_range}/{export_name}-Manifest.json",
            f"{base_prefix}metadata/{period_range}/{export_name}-Manifest.json",
        ]
        for probe_key in probe_keys:
            try:
                return await self._fetch_and_parse_manifest(
                    connection,
                    bucket=bucket,
                    region=region,
                    normalized_prefix=normalized_prefix,
                    manifest_key=probe_key,
                    source_month=source_month,
                    credentials=current_creds,
                    secret_value=secret_value,
                )
            except UpstreamNotFoundError:
                continue

        scope_str = f"s3://{bucket}/{normalized_prefix}" if normalized_prefix else f"s3://{bucket}"
        raise UpstreamNotFoundError(
            f"No AWS CUR or CUR 2.0 manifest was found for {source_month} under {scope_str}.",
            details={
                "sourceMonth": source_month,
                "bucket": bucket,
                "prefix": normalized_prefix,
                "costExplorerNote": COST_EXPLORER_UNSUPPORTED_NOTE,
            },
        )

    async def check_access(
        self,
        connection: BillingConnection,
        *,
        secret_value: str | None = None,
    ) -> BillingConnectionCheckResult:
        """Verify STS role assumption and read-only S3 access to the configured CUR prefix."""

        checked_at = self._clock()
        if connection.provider != "aws":
            raise ValidationError(
                f"AwsBillingConnector cannot check a {connection.provider!r} connection."
            )

        bucket = _validate_cur_bucket(connection.aws_cur_bucket)
        normalized_prefix = _normalize_cur_prefix(connection.aws_cur_prefix)
        region = _resolve_aws_region(connection)
        scope_summary = (
            f"s3://{bucket}/{normalized_prefix}" if normalized_prefix else f"s3://{bucket}"
        )

        try:
            credentials = await self._assume_role_credentials(
                connection,
                secret_value=secret_value,
            )
            listed_objects, _ = await self._list_s3_objects(
                connection,
                bucket=bucket,
                region=region,
                prefix=normalized_prefix,
                credentials=credentials,
                secret_value=secret_value,
                max_keys=100,
                max_pages=2,
            )
        except ValidationError as exc:
            safe_msg = redact_aws_secrets(exc.message, secret_value)
            return BillingConnectionCheckResult(
                connection_id=connection.id,
                provider="aws",
                status="failed",
                message=safe_msg,
                checked_at=checked_at,
                scope_summary=scope_summary,
                has_stored_secret=bool(connection.credential_secret_uri or secret_value),
                warnings=[COST_EXPLORER_UNSUPPORTED_NOTE],
                details={
                    "bucket": bucket,
                    "prefix": normalized_prefix,
                    "region": region,
                    "costExplorerNote": COST_EXPLORER_UNSUPPORTED_NOTE,
                    **exc.details,
                },
            )
        except DomainError as exc:
            safe_msg = redact_aws_secrets(exc.message, secret_value)
            return BillingConnectionCheckResult(
                connection_id=connection.id,
                provider="aws",
                status="failed",
                message=safe_msg,
                checked_at=checked_at,
                scope_summary=scope_summary,
                has_stored_secret=bool(connection.credential_secret_uri or secret_value),
                warnings=[COST_EXPLORER_UNSUPPORTED_NOTE],
                details={
                    "bucket": bucket,
                    "prefix": normalized_prefix,
                    "region": region,
                    "roleArn": connection.aws_role_arn,
                    "costExplorerNote": COST_EXPLORER_UNSUPPORTED_NOTE,
                    **exc.details,
                },
            )

        discovered_months: set[str] = set()
        manifest_keys: list[str] = []
        for obj in listed_objects:
            if not obj.key.casefold().endswith("manifest.json"):
                continue
            if connection.aws_export_name:
                exp_folded = connection.aws_export_name.strip().casefold()
                if exp_folded not in obj.key.casefold():
                    continue
            manifest_keys.append(obj.key)
            month = _extract_source_month_from_string(obj.key)
            if month:
                discovered_months.add(month)

        sorted_months = sorted(discovered_months, reverse=True)
        warnings: list[str] = [COST_EXPLORER_UNSUPPORTED_NOTE]
        if not manifest_keys:
            warnings.insert(
                0,
                f"No CUR / CUR 2.0 manifest (*Manifest.json) was found under {scope_summary} yet. "
                "Verify that AWS Data Exports or Cost and Usage Reports has delivered at least "
                "one billing period export to this prefix.",
            )
            message = (
                f"Verified STS role assumption and S3 ListBucket access on {scope_summary}, "
                "but no CUR manifest files were found under the configured prefix yet."
            )
        else:
            message = (
                f"Verified STS role assumption and read-only S3 CUR access on {scope_summary} "
                f"({len(manifest_keys)} manifest file(s) across {len(sorted_months)} month(s))."
            )

        return BillingConnectionCheckResult(
            connection_id=connection.id,
            provider="aws",
            status="ready",
            message=message,
            checked_at=checked_at,
            scope_summary=scope_summary,
            has_stored_secret=bool(connection.credential_secret_uri or secret_value),
            supported_source_months=sorted_months,
            warnings=warnings,
            details={
                "bucket": bucket,
                "prefix": normalized_prefix,
                "region": region,
                "roleArn": connection.aws_role_arn,
                "manifestCount": len(manifest_keys),
                "costExplorerNote": COST_EXPLORER_UNSUPPORTED_NOTE,
            },
        )

    async def start_or_poll_acquisition(
        self,
        connection: BillingConnection,
        run: BillingAcquisitionRun,
        *,
        secret_value: str | None = None,
    ) -> BillingConnectorStepResult:
        """Resolve the authoritative CUR manifest for ``run.source_month`` and download parts."""

        if connection.provider != "aws":
            raise ValidationError(
                f"AwsBillingConnector cannot run acquisition for provider {connection.provider!r}."
            )
        parse_source_month(run.source_month)

        bucket = _validate_cur_bucket(connection.aws_cur_bucket)
        normalized_prefix = _normalize_cur_prefix(connection.aws_cur_prefix)
        region = _resolve_aws_region(connection)
        scope_summary = (
            f"s3://{bucket}/{normalized_prefix}" if normalized_prefix else f"s3://{bucket}"
        )

        credentials = await self._assume_role_credentials(
            connection,
            secret_value=secret_value,
        )
        manifest, _manifest_bytes_len, credentials = await self._resolve_authoritative_manifest(
            connection,
            bucket=bucket,
            region=region,
            normalized_prefix=normalized_prefix,
            source_month=run.source_month,
            credentials=credentials,
            secret_value=secret_value,
        )

        parts: list[BillingConnectorPayloadPart] = []
        total_data_bytes = 0

        for report_key in manifest.report_keys:
            scoped_key = validate_scoped_s3_key(
                report_key,
                expected_bucket=bucket,
                normalized_prefix=normalized_prefix,
            )
            response, credentials = await self._s3_get(
                connection,
                bucket=bucket,
                region=region,
                credentials=credentials,
                secret_value=secret_value,
                key=scoped_key,
            )
            if response.status_code in (401, 403):
                err_code, err_msg = _parse_s3_error(response)
                safe_msg = redact_aws_secrets(
                    err_msg,
                    secret_value,
                    credentials.access_key_id,
                    credentials.secret_access_key,
                    credentials.session_token,
                )
                guidance = _s3_permission_guidance(
                    bucket, normalized_prefix, connection.aws_role_arn or "<aws-role-arn>"
                )
                raise UpstreamAuthorizationError(
                    f"{guidance} (S3 {err_code}: {safe_msg})",
                    details={
                        "status": response.status_code,
                        "awsErrorCode": err_code,
                        "bucket": bucket,
                        "reportKey": scoped_key,
                    },
                )
            if response.status_code == 404:
                raise UpstreamNotFoundError(
                    f"AWS CUR report object {scoped_key!r} listed in manifest "
                    f"{manifest.assembly_id!r} was not found in bucket {bucket!r}.",
                    details={
                        "bucket": bucket,
                        "reportKey": scoped_key,
                        "assemblyId": manifest.assembly_id,
                    },
                )
            if response.status_code >= 400:
                err_code, err_msg = _parse_s3_error(response)
                raise UpstreamError(
                    f"Failed to download AWS CUR report object {scoped_key!r} ({err_code}).",
                    details={"status": response.status_code, "reportKey": scoped_key},
                )

            content_bytes = response.content
            total_data_bytes += len(content_bytes)
            if total_data_bytes > self._max_upload_bytes:
                raise BillingPayloadTooLargeError(
                    f"AWS CUR export objects for {run.source_month} ({total_data_bytes:,} bytes) "
                    f"exceed the maximum allowed download size ({self._max_upload_bytes:,} bytes).",
                    details={
                        "reportKey": scoped_key,
                        "totalBytes": total_data_bytes,
                        "maxUploadBytes": self._max_upload_bytes,
                    },
                )

            file_name = scoped_key.rsplit("/", 1)[-1] or scoped_key
            lower_fn = file_name.casefold()
            if lower_fn.endswith(".gz") and not lower_fn.endswith(".csv.gz"):
                file_name = f"{file_name[:-3]}.csv.gz"

            content_type = response.headers.get("content-type") or manifest.content_type
            parts.append(
                BillingConnectorPayloadPart(
                    name=file_name,
                    content=content_bytes,
                    content_type=content_type,
                )
            )

        warnings: list[str] = []
        if manifest.source_month != run.source_month:
            warnings.append(
                f"CUR manifest billingPeriod reported {manifest.source_month}, while acquisition "
                f"requested {run.source_month}."
            )

        return BillingConnectorStepResult(
            status="completed",
            parts=parts,
            source_scope=scope_summary,
            source_manifest_id=manifest.assembly_id,
            operation_id=manifest.assembly_id,
            status_message=(
                f"Downloaded {len(parts)} CUR object(s) from {scope_summary} "
                f"(manifest assembly {manifest.assembly_id})."
            ),
            total_read_bytes=total_data_bytes,
            warnings=warnings,
        )


__all__ = [
    "COST_EXPLORER_UNSUPPORTED_NOTE",
    "DEFAULT_AWS_OIDC_AUDIENCE",
    "DEFAULT_AWS_REGION",
    "DEFAULT_MAX_REPORT_KEYS",
    "AwsBillingConnector",
    "AwsSessionCredentials",
    "BillingIngestionEngine",
    "CurManifestDescriptor",
    "S3ListedObject",
    "derive_sigv4_signing_key",
    "redact_aws_secrets",
    "sign_aws_sigv4_headers",
    "validate_scoped_s3_key",
]
