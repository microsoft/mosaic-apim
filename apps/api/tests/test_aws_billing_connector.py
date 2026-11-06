"""Isolated tests for the AWS CUR / CUR 2.0 S3 billing connector and SigV4 client."""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self
from urllib.parse import parse_qs

import httpx
import pytest
from azure.core.credentials import AccessToken
from mosaic_api.billing_pricing import (
    BillingAcquisitionRun,
    BillingConnection,
    billing_connection_view,
)
from mosaic_api.errors import UpstreamAuthorizationError, ValidationError
from mosaic_api.integrations.billing_aws import (
    COST_EXPLORER_UNSUPPORTED_NOTE,
    DEFAULT_AWS_OIDC_AUDIENCE,
    AwsBillingConnector,
    AwsSessionCredentials,
    BillingIngestionEngine,
    redact_aws_secrets,
    sign_aws_sigv4_headers,
    validate_scoped_s3_key,
)
from mosaic_api.integrations.parquet_cur import write_parquet_table
from mosaic_api.services.billing_ingestion import BillingPayloadTooLargeError

TENANT = "tenant-aws-billing"
FIXED_NOW = datetime(2026, 10, 15, 12, 30, 0, tzinfo=UTC)


def _clock() -> datetime:
    return FIXED_NOW


class _FakeEntraCredential:
    """AsyncTokenCredential test double recording requested Entra scopes."""

    def __init__(self, token: str = "entra-oidc-jwt-secret-value") -> None:
        self.token = token
        self.requested_scopes: list[tuple[str, ...]] = []

    async def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        _ = kwargs
        self.requested_scopes.append(scopes)
        return AccessToken(self.token, int(FIXED_NOW.timestamp()) + 3600)

    async def close(self) -> None:
        return None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        await self.close()


def _aws_connection(
    *,
    role_arn: str = "arn:aws:iam::123456789012:role/MosaicCurReader",
    bucket: str = "contoso-cur-exports",
    prefix: str | None = "cur/bedrock-exports",
    export_name: str | None = "bedrock-cur",
    region: str | None = "us-east-1",
    external_id: str | None = None,
    audience: str | None = None,
) -> BillingConnection:
    return BillingConnection(
        id="conn-aws-1",
        tenant_id=TENANT,
        name="Contoso AWS Bedrock CUR",
        provider="aws",
        pricing_cloud="aws-bedrock",
        aws_role_arn=role_arn,
        aws_cur_bucket=bucket,
        aws_cur_prefix=prefix,
        aws_export_name=export_name,
        aws_region=region,
        aws_external_id=external_id,
        aws_audience=audience,
        credential_secret_uri="https://contoso-kv.vault.azure.net/secrets/aws-oidc",
    )


def _aws_run(source_month: str = "2026-09") -> BillingAcquisitionRun:
    return BillingAcquisitionRun(
        id="run-aws-2026-09",
        tenant_id=TENANT,
        connection_id="conn-aws-1",
        connection_name="Contoso AWS Bedrock CUR",
        provider="aws",
        pricing_cloud="aws-bedrock",
        acquisition_kind="connected",
        source_month=source_month,
        status="running",
    )


def _sts_xml_response(
    *,
    access_key_id: str = "ASIAIOSFODNN7EXAMPLE",
    secret_access_key: str = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    session_token: str = "AQoDYXdzEJr1EXAMPLETOKEN99999",
    role_arn: str = (
        "arn:aws:sts::123456789012:assumed-role/MosaicCurReader/mosaic-billing-cur-reader"
    ),
) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<AssumeRoleWithWebIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">'
        "  <AssumeRoleWithWebIdentityResult>"
        "    <Credentials>"
        f"      <AccessKeyId>{access_key_id}</AccessKeyId>"
        f"      <SecretAccessKey>{secret_access_key}</SecretAccessKey>"
        f"      <SessionToken>{session_token}</SessionToken>"
        "      <Expiration>2026-10-15T13:30:00Z</Expiration>"
        "    </Credentials>"
        "    <AssumedRoleUser>"
        f"      <Arn>{role_arn}</Arn>"
        "    </AssumedRoleUser>"
        "  </AssumeRoleWithWebIdentityResult>"
        "</AssumeRoleWithWebIdentityResponse>"
    )


def _list_objects_xml(entries: list[tuple[str, str, int]]) -> str:
    items_xml = "".join(
        f"<Contents>"
        f"<Key>{key}</Key>"
        f"<LastModified>{last_modified}</LastModified>"
        f"<Size>{size}</Size>"
        f'<ETag>"etag-{idx}"</ETag>'
        f"</Contents>"
        for idx, (key, last_modified, size) in enumerate(entries, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        "  <Name>contoso-cur-exports</Name>"
        "  <IsTruncated>false</IsTruncated>"
        f"  {items_xml}"
        "</ListBucketResult>"
    )


# -- 1. STS AssumeRoleWithWebIdentity (with & without ExternalId) and SigV4 signing ------------


@pytest.mark.parametrize("external_id", [None, "mosaic-tenant-ext-98765"])
async def test_sts_assume_role_with_web_identity_and_sigv4_headers(
    external_id: str | None,
) -> None:
    sts_requests: list[httpx.Request] = []
    s3_requests: list[httpx.Request] = []
    entra_cred = _FakeEntraCredential("federated-jwt-token-xyz123")

    manifest_key = (
        "cur/bedrock-exports/bedrock-cur/metadata/20260901-20261001/bedrock-cur-Manifest.json"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "sts.us-east-1.amazonaws.com":
            sts_requests.append(request)
            return httpx.Response(200, text=_sts_xml_response())
        s3_requests.append(request)
        return httpx.Response(
            200,
            text=_list_objects_xml([(manifest_key, "2026-10-02T04:00:00Z", 512)]),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = AwsBillingConnector(
        azure_credential=entra_cred,
        client=client,
        clock=_clock,
    )
    conn = _aws_connection(external_id=external_id, audience=None)

    check = await connector.check_access(conn)
    assert check.status == "ready"
    assert check.supported_source_months == ["2026-09"]

    # Verify Entra OIDC audience defaulted to api://AzureADTokenExchange/.default
    assert entra_cred.requested_scopes == [(f"{DEFAULT_AWS_OIDC_AUDIENCE}/.default",)]

    # Verify STS AssumeRoleWithWebIdentity form payload
    assert len(sts_requests) == 1
    sts_req = sts_requests[0]
    assert sts_req.method == "POST"
    assert "federated-jwt-token-xyz123" not in str(sts_req.url)
    sts_form = parse_qs(sts_req.content.decode("utf-8"))
    assert sts_form["Action"] == ["AssumeRoleWithWebIdentity"]
    assert sts_form["Version"] == ["2011-06-15"]
    assert sts_form["RoleArn"] == ["arn:aws:iam::123456789012:role/MosaicCurReader"]
    assert sts_form["WebIdentityToken"] == ["federated-jwt-token-xyz123"]
    if external_id is not None:
        assert sts_form["ExternalId"] == [external_id]
    else:
        assert "ExternalId" not in sts_form

    # Verify S3 SigV4 headers
    assert len(s3_requests) == 1
    s3_req = s3_requests[0]
    assert s3_req.url.host == "contoso-cur-exports.s3.us-east-1.amazonaws.com"
    assert s3_req.url.params["list-type"] == "2"
    assert s3_req.url.params["prefix"] == "cur/bedrock-exports"

    auth_header = s3_req.headers["Authorization"]
    assert auth_header.startswith(
        "AWS4-HMAC-SHA256 Credential=ASIAIOSFODNN7EXAMPLE/20261015/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;x-amz-content-sha256;x-amz-date;x-amz-security-token, Signature="
    )
    assert s3_req.headers["x-amz-date"] == "20261015T123000Z"
    assert s3_req.headers["x-amz-security-token"] == "AQoDYXdzEJr1EXAMPLETOKEN99999"
    assert s3_req.headers["x-amz-content-sha256"] == hashlib.sha256(b"").hexdigest()

    # Verify deterministic SigV4 signature matches direct sign_aws_sigv4_headers calculation
    expected_headers = sign_aws_sigv4_headers(
        method="GET",
        url="https://contoso-cur-exports.s3.us-east-1.amazonaws.com/",
        region="us-east-1",
        credentials=AwsSessionCredentials(
            access_key_id="ASIAIOSFODNN7EXAMPLE",
            secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            session_token="AQoDYXdzEJr1EXAMPLETOKEN99999",
        ),
        timestamp=FIXED_NOW,
        params=dict(s3_req.url.params),
    )
    assert auth_header == expected_headers["Authorization"]


# -- 2. check_access success, Cost Explorer warning, S3 403, and STS trust failure -------------


async def test_check_access_warnings_and_permission_failures() -> None:
    # 2a. Empty S3 prefix (no manifest yet) still succeeds with warning + Cost Explorer note
    def empty_s3_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host.startswith("sts."):
            return httpx.Response(200, text=_sts_xml_response())
        return httpx.Response(200, text=_list_objects_xml([]))

    empty_connector = AwsBillingConnector(
        client=httpx.AsyncClient(transport=httpx.MockTransport(empty_s3_handler)),
        web_identity_token_provider=lambda _aud: "oidc-tok-1",
        clock=_clock,
    )
    empty_res = await empty_connector.check_access(_aws_connection())
    assert empty_res.status == "ready"
    assert empty_res.supported_source_months == []
    assert any("No CUR / CUR 2.0 manifest" in w for w in empty_res.warnings)
    assert any(
        "Cost Explorer" in w and "ce:GetCostAndUsage" in w and "N/A" in w
        for w in empty_res.warnings
    )
    assert empty_res.details["costExplorerNote"] == COST_EXPLORER_UNSUPPORTED_NOTE

    # 2b. S3 403 AccessDenied returns status="failed" with actionable IAM policy guidance
    def s3_denied_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host.startswith("sts."):
            return httpx.Response(200, text=_sts_xml_response())
        return httpx.Response(
            403,
            text=(
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<Error><Code>AccessDenied</Code>"
                "<Message>User is not authorized to perform: s3:ListBucket</Message></Error>"
            ),
        )

    s3_denied_connector = AwsBillingConnector(
        client=httpx.AsyncClient(transport=httpx.MockTransport(s3_denied_handler)),
        web_identity_token_provider=lambda _aud: "oidc-tok-2",
        clock=_clock,
    )
    s3_denied_res = await s3_denied_connector.check_access(_aws_connection())
    assert s3_denied_res.status == "failed"
    assert "sts:AssumeRoleWithWebIdentity" in s3_denied_res.message
    assert "s3:ListBucket" in s3_denied_res.message
    assert "arn:aws:s3:::contoso-cur-exports" in s3_denied_res.message
    assert "s3:GetObject" in s3_denied_res.message
    assert "arn:aws:s3:::contoso-cur-exports/cur/bedrock-exports/*" in s3_denied_res.message

    # 2c. STS 403 AccessDenied trust failure returns status="failed" with STS guidance
    def sts_denied_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            text=(
                '<ErrorResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">'
                "  <Error>"
                "    <Code>AccessDenied</Code>"
                "    <Message>Not authorized to perform sts:AssumeRoleWithWebIdentity "
                "with token secret-oidc-value-999</Message>"
                "  </Error>"
                "</ErrorResponse>"
            ),
        )

    sts_denied_connector = AwsBillingConnector(
        client=httpx.AsyncClient(transport=httpx.MockTransport(sts_denied_handler)),
        clock=_clock,
    )
    sts_denied_res = await sts_denied_connector.check_access(
        _aws_connection(external_id="ext-123"),
        secret_value="secret-oidc-value-999",
    )
    assert sts_denied_res.status == "failed"
    assert "sts:AssumeRoleWithWebIdentity" in sts_denied_res.message
    assert "sts:ExternalId" in sts_denied_res.message
    assert "secret-oidc-value-999" not in sts_denied_res.message
    assert "secret-oidc-value-999" not in json.dumps(sts_denied_res.details)


# -- 3. Multi-revision CUR manifest selection (preventing double-counting) ---------------------


@pytest.mark.parametrize("include_top_level_pointer", [True, False])
async def test_multi_revision_cur_manifest_selects_only_latest_assembly(
    include_top_level_pointer: bool,
) -> None:
    """When S3 has assembly-old and assembly-new, ONLY assembly-new's reportKeys are downloaded."""

    old_manifest_key = (
        "cur/bedrock-exports/bedrock-cur/20260901-20261001/"
        "assembly-old/bedrock-cur-Manifest.json"
    )
    new_manifest_key = (
        "cur/bedrock-exports/bedrock-cur/20260901-20261001/"
        "assembly-new/bedrock-cur-Manifest.json"
    )
    top_pointer_key = (
        "cur/bedrock-exports/bedrock-cur/20260901-20261001/bedrock-cur-Manifest.json"
    )
    old_part_key = (
        "cur/bedrock-exports/bedrock-cur/20260901-20261001/assembly-old/part-00001.csv.gz"
    )
    new_part_key = (
        "cur/bedrock-exports/bedrock-cur/20260901-20261001/assembly-new/part-00001.csv.gz"
    )

    old_manifest_json = json.dumps(
        {
            "assemblyId": "assembly-old",
            "account": "123456789012",
            "billingPeriod": {
                "start": "20260901T000000.000Z",
                "end": "20261001T000000.000Z",
            },
            "bucket": "contoso-cur-exports",
            "currencyCode": "USD",
            "reportKeys": [old_part_key],
        }
    )
    new_manifest_json = json.dumps(
        {
            "assemblyId": "assembly-new",
            "account": "123456789012",
            "billingPeriod": {
                "start": "20260901T000000.000Z",
                "end": "20261001T000000.000Z",
            },
            "bucket": "contoso-cur-exports",
            "currencyCode": "USD",
            "reportKeys": [new_part_key],
        }
    )

    new_csv_bytes = gzip.compress(
        b"line_item_usage_account_id,line_item_product_code,line_item_usage_type,"
        b"line_item_usage_start_date,line_item_usage_amount,pricing_unit,"
        b"line_item_line_item_type,line_item_currency_code,line_item_unblended_cost,"
        b"product_region_code,product_model\n"
        b"123456789012,AmazonBedrock,USE1-Claude3-5Sonnet-input-tokens,"
        b"2026-09-10T00:00:00Z,1000,1K tokens,Usage,USD,3.00,us-east-1,"
        b"anthropic.claude-3-5-sonnet-20241022-v2:0\n"
        b"123456789012,AmazonBedrock,USE1-Claude3-5Sonnet-output-tokens,"
        b"2026-09-10T00:00:00Z,200,1K tokens,Usage,USD,3.00,us-east-1,"
        b"anthropic.claude-3-5-sonnet-20241022-v2:0\n"
    )

    listed_entries = [
        (old_manifest_key, "2026-10-01T01:00:00Z", len(old_manifest_json)),
        (old_part_key, "2026-10-01T01:00:00Z", 500),
        (new_manifest_key, "2026-10-02T06:00:00Z", len(new_manifest_json)),
        (new_part_key, "2026-10-02T06:00:00Z", len(new_csv_bytes)),
    ]
    if include_top_level_pointer:
        listed_entries.append(
            (top_pointer_key, "2026-10-02T06:00:05Z", len(new_manifest_json))
        )

    downloaded_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host.startswith("sts."):
            return httpx.Response(200, text=_sts_xml_response())
        if request.url.params.get("list-type") == "2":
            return httpx.Response(200, text=_list_objects_xml(listed_entries))

        path = request.url.path.lstrip("/")
        downloaded_paths.append(path)
        if path in (top_pointer_key, new_manifest_key):
            return httpx.Response(200, text=new_manifest_json)
        if path == old_manifest_key:
            pytest.fail("Connector must not read the superseded assembly-old manifest")
        if path == old_part_key:
            pytest.fail("Connector must not download assembly-old reportKeys")
        if path == new_part_key:
            return httpx.Response(
                200,
                content=new_csv_bytes,
                headers={"content-type": "application/gzip"},
            )
        return httpx.Response(404)

    connector = AwsBillingConnector(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        web_identity_token_provider=lambda _aud: "oidc-jwt",
        clock=_clock,
    )
    step = await connector.start_or_poll_acquisition(_aws_connection(), _aws_run("2026-09"))

    assert step.status == "completed"
    assert step.source_manifest_id == "assembly-new"
    assert step.source_scope == "s3://contoso-cur-exports/cur/bedrock-exports"
    assert len(step.parts) == 1
    assert step.parts[0].name == "part-00001.csv.gz"
    assert step.total_read_bytes == len(new_csv_bytes)
    assert old_part_key not in downloaded_paths
    assert new_part_key in downloaded_paths

    # Verify ingestion produces non-doubled token quantities (1M input, 0.2M output)
    ingested = BillingIngestionEngine().ingest_parts(
        step.parts,
        provider="aws",
        pricing_cloud="aws-bedrock",
        source_month="2026-09",
        source_scope=step.source_scope,
    )
    assert len(ingested.candidates) == 1
    cand = ingested.candidates[0]
    assert cand.input_tokens == pytest.approx(1_000_000.0)
    assert cand.output_tokens == pytest.approx(200_000.0)
    assert cand.input_per_million == pytest.approx(3.00)
    assert cand.output_per_million == pytest.approx(15.00)


# -- 4. End-to-end multi-part .csv.gz + .parquet CUR 2.0 download & Bedrock rate derivation ----


async def test_multipart_csvgz_and_parquet_cur2_bedrock_derivation_and_token_refresh() -> None:
    """Downloads a .csv.gz partition and a .parquet partition, refreshes expired STS credentials
    mid-transfer, and derives Bedrock rates across cache, cross-region, and Savings Plans."""

    manifest_key = (
        "cur/bedrock-exports/bedrock-cur/metadata/BILLING_PERIOD=2026-09/"
        "bedrock-cur-Manifest.json"
    )
    part1_key = (
        "cur/bedrock-exports/bedrock-cur/data/BILLING_PERIOD=2026-09/part-00001.csv.gz"
    )
    part2_key = (
        "cur/bedrock-exports/bedrock-cur/data/BILLING_PERIOD=2026-09/part-00002.snappy.parquet"
    )

    claude_arn = "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-5-sonnet"
    part1_csv = (
        "line_item_usage_account_id,line_item_product_code,line_item_usage_type,"
        "line_item_operation,line_item_resource_id,line_item_usage_start_date,"
        "line_item_usage_amount,pricing_unit,line_item_line_item_type,line_item_currency_code,"
        "line_item_unblended_cost,line_item_net_unblended_cost,product_region_code,product_model\n"
        f"123456789012,AmazonBedrock,USE1-Claude3-5Sonnet-input-tokens,InvokeModel,{claude_arn},"
        "2026-09-12T00:00:00Z,2000,1K tokens,Usage,USD,6.00,4.80,us-east-1,"
        "anthropic.claude-3-5-sonnet-20241022-v2:0\n"
        f"123456789012,AmazonBedrock,USE1-Claude3-5Sonnet-output-tokens,InvokeModel,{claude_arn},"
        "2026-09-12T00:00:00Z,500,1K tokens,Usage,USD,7.50,6.00,us-east-1,"
        "anthropic.claude-3-5-sonnet-20241022-v2:0\n"
        f"123456789012,AmazonBedrock,USE1-Claude3-5Sonnet-cache-read-input-token-count,"
        f"InvokeModel,{claude_arn},2026-09-12T00:00:00Z,1000,1K tokens,Usage,USD,0.30,0.24,"
        "us-east-1,anthropic.claude-3-5-sonnet-20241022-v2:0\n"
        f"123456789012,AmazonBedrock,USE1-Claude3-5Sonnet-cache-write-input-token-count,"
        f"InvokeModel,{claude_arn},2026-09-12T00:00:00Z,400,1K tokens,Usage,USD,1.50,1.20,"
        "us-east-1,anthropic.claude-3-5-sonnet-20241022-v2:0\n"
    )
    part1_bytes = gzip.compress(part1_csv.encode("utf-8"))

    # Part 2 (.parquet): cross-region inference profile covered by an AWS Savings Plan
    cross_region_arn = (
        "arn:aws:bedrock:us-east-1:123456789012:inference-profile/"
        "us.anthropic.claude-3-5-sonnet-20241022-v2:0"
    )
    parquet_rows: list[dict[str, Any]] = [
        {
            "line_item_usage_account_id": "123456789012",
            "line_item_product_code": "AmazonBedrock",
            "line_item_usage_type": (
                "us-east-1-claude-3-5-sonnet-input-tokens-cross-region-inference"
            ),
            "line_item_operation": "InvokeModel",
            "line_item_resource_id": cross_region_arn,
            "line_item_usage_start_date": "2026-09-18T00:00:00Z",
            "line_item_usage_amount": 1000.0,
            "pricing_unit": "1K tokens",
            "line_item_line_item_type": "SavingsPlanCoveredUsage",
            "line_item_currency_code": "USD",
            "line_item_unblended_cost": 3.00,
            "savings_plan_savings_plan_effective_cost": 2.10,
            "product_region_code": "us-east-1",
            "product_model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        },
        {
            "line_item_usage_account_id": "123456789012",
            "line_item_product_code": "AmazonBedrock",
            "line_item_usage_type": (
                "us-east-1-claude-3-5-sonnet-output-tokens-cross-region-inference"
            ),
            "line_item_operation": "InvokeModel",
            "line_item_resource_id": cross_region_arn,
            "line_item_usage_start_date": "2026-09-18T00:00:00Z",
            "line_item_usage_amount": 250.0,
            "pricing_unit": "1K tokens",
            "line_item_line_item_type": "SavingsPlanCoveredUsage",
            "line_item_currency_code": "USD",
            "line_item_unblended_cost": 3.75,
            "savings_plan_savings_plan_effective_cost": 2.625,
            "product_region_code": "us-east-1",
            "product_model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        },
    ]
    part2_bytes = write_parquet_table(parquet_rows, compression="GZIP", use_dictionary=True)

    manifest_json = json.dumps(
        {
            "manifestId": "cur2-manifest-202609-rev2",
            "billingPeriod": "2026-09",
            "currencyCode": "USD",
            "reportKeys": [part1_key, part2_key],
            "columns": [
                {"category": "lineItem", "name": "UsageType"},
                {"category": "lineItem", "name": "UsageAmount"},
            ],
        }
    )

    sts_call_count = 0
    part2_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sts_call_count, part2_attempts
        if request.url.host.startswith("sts."):
            sts_call_count += 1
            return httpx.Response(
                200,
                text=_sts_xml_response(
                    access_key_id=f"ASIAEXAMPLEKEY00000{sts_call_count}",
                    session_token=f"session-token-{sts_call_count}",
                ),
            )
        if request.url.params.get("list-type") == "2":
            return httpx.Response(
                200,
                text=_list_objects_xml(
                    [(manifest_key, "2026-10-03T00:00:00Z", len(manifest_json))]
                ),
            )

        path = request.url.path.lstrip("/")
        if path == manifest_key:
            return httpx.Response(200, text=manifest_json)
        if path == part1_key:
            return httpx.Response(200, content=part1_bytes)
        if path == part2_key:
            part2_attempts += 1
            if part2_attempts == 1:
                # Simulate mid-transfer token expiration on first attempt for part2
                return httpx.Response(
                    403,
                    text=(
                        "<Error><Code>ExpiredToken</Code>"
                        "<Message>The provided token has expired.</Message></Error>"
                    ),
                )
            assert request.headers["x-amz-security-token"] == "session-token-2"
            return httpx.Response(200, content=part2_bytes)
        return httpx.Response(404)

    connector = AwsBillingConnector(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        web_identity_token_provider=lambda _aud: "oidc-jwt-token",
        clock=_clock,
    )
    step = await connector.start_or_poll_acquisition(_aws_connection(), _aws_run("2026-09"))

    assert sts_call_count == 2
    assert part2_attempts == 2
    assert step.status == "completed"
    assert step.source_manifest_id == "cur2-manifest-202609-rev2"
    assert len(step.parts) == 2
    assert step.total_read_bytes == len(part1_bytes) + len(part2_bytes)

    ingested = BillingIngestionEngine().ingest_parts(
        step.parts,
        provider="aws",
        pricing_cloud="aws-bedrock",
        source_month="2026-09",
        source_scope=step.source_scope,
    )
    assert ingested.raw_row_count == 6
    assert ingested.eligible_row_count == 6
    assert len(ingested.candidates) == 2

    # Both candidates preserve us-east-1 (with hyphens) and separate in-region vs cross-region
    by_routing = {c.routing_mode: c for c in ingested.candidates}
    assert set(by_routing) == {"in-region", "cross-region"}

    in_region = by_routing["in-region"]
    assert in_region.region == "us-east-1"
    assert in_region.model == "anthropic.claude-3-5-sonnet-20241022-v2:0"
    assert in_region.input_per_million == pytest.approx(2.40)  # 4.80 / 2M
    assert in_region.output_per_million == pytest.approx(12.00)  # 6.00 / 0.5M
    assert in_region.cached_input_per_million == pytest.approx(0.24)  # 0.24 / 1M
    assert in_region.cache_write_tokens == pytest.approx(400_000.0)
    assert in_region.cache_write_cost_usd == pytest.approx(1.20)
    assert in_region.publishable is True

    cross_region = by_routing["cross-region"]
    assert cross_region.region == "us-east-1"
    # SavingsPlanEffectiveCost 2.10 / 1M = 2.10
    assert cross_region.input_per_million == pytest.approx(2.10)
    assert cross_region.output_per_million == pytest.approx(10.50)  # 2.625 / 0.25M
    assert cross_region.publishable is True


# -- 5. Prefix boundary enforcement, Bedrock runtime key rejection, and secret redaction --------


async def test_prefix_boundary_enforcement_and_secret_redaction() -> None:
    # 5a. Direct unit checks on validate_scoped_s3_key
    assert (
        validate_scoped_s3_key(
            "s3://contoso-cur-exports/cur/bedrock-exports/part.parquet",
            expected_bucket="contoso-cur-exports",
            normalized_prefix="cur/bedrock-exports",
        )
        == "cur/bedrock-exports/part.parquet"
    )

    for malicious_key in (
        "other-prefix/secret.csv",
        "cur/bedrock-exports/../../etc/passwd",
        "cur/bedrock-exports/%2e%2e/secret.csv",
        "s3://attacker-bucket/cur/bedrock-exports/part.csv",
        "cur\\bedrock-exports\\part.csv",
    ):
        with pytest.raises(ValidationError):
            validate_scoped_s3_key(
                malicious_key,
                expected_bucket="contoso-cur-exports",
                normalized_prefix="cur/bedrock-exports",
            )

    # 5b. Manifest containing an out-of-prefix reportKey is rejected before download
    manifest_key = (
        "cur/bedrock-exports/bedrock-cur/20260901-20261001/bedrock-cur-Manifest.json"
    )
    bad_manifest = json.dumps(
        {
            "assemblyId": "asm-malicious",
            "bucket": "contoso-cur-exports",
            "currencyCode": "USD",
            "reportKeys": ["other-team-exports/20260901-20261001/part-0001.csv.gz"],
        }
    )

    def bad_manifest_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host.startswith("sts."):
            return httpx.Response(200, text=_sts_xml_response())
        if request.url.params.get("list-type") == "2":
            return httpx.Response(
                200,
                text=_list_objects_xml([(manifest_key, "2026-10-02T00:00:00Z", len(bad_manifest))]),
            )
        if request.url.path.lstrip("/") == manifest_key:
            return httpx.Response(200, text=bad_manifest)
        pytest.fail("Out-of-prefix reportKey must never be requested from S3")

    connector = AwsBillingConnector(
        client=httpx.AsyncClient(transport=httpx.MockTransport(bad_manifest_handler)),
        web_identity_token_provider=lambda _aud: "oidc-jwt",
        clock=_clock,
    )
    with pytest.raises(ValidationError, match="outside the configured prefix"):
        await connector.start_or_poll_acquisition(_aws_connection(), _aws_run("2026-09"))

    # 5c. Bedrock runtime API key is rejected for billing access
    with pytest.raises(ValidationError, match="Bedrock model runtime API key"):
        await connector.start_or_poll_acquisition(
            _aws_connection(),
            _aws_run("2026-09"),
            secret_value="ABSKQmVkcm9ja1J1bnRpbWVLZXlFeGFtcGxlMTIzNDU2",
        )

    check_bedrock_key = await connector.check_access(
        _aws_connection(),
        secret_value="ABSKQmVkcm9ja1J1bnRpbWVLZXlFeGFtcGxlMTIzNDU2",
    )
    assert check_bedrock_key.status == "failed"
    assert "Bedrock model runtime API key" in check_bedrock_key.message
    assert "ABSKQmVkcm9ja1J1bnRpbWVLZXlFeGFtcGxlMTIzNDU2" not in check_bedrock_key.message

    # 5d. Credential redaction in AwsSessionCredentials, views, and redact_aws_secrets
    creds = AwsSessionCredentials(
        access_key_id="ASIAIOSFODNN7EXAMPLE",
        secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        session_token="AQoDYXdzEJr1EXAMPLETOKEN99999",
    )
    assert "ASIAIOSFODNN7EXAMPLE" not in repr(creds)
    assert "wJalrXUtnFEMI" not in repr(creds)
    assert "AQoDYXdzEJr1EXAMPLETOKEN99999" not in str(creds)

    conn = _aws_connection()
    view = billing_connection_view(conn)
    assert "contoso-kv" not in json.dumps(view.model_dump(mode="json"))

    raw_err = (
        "Failed with key ASIAIOSFODNN7EXAMPLE and SessionToken=AQoDYXdzEJr1EXAMPLETOKEN99999 "
        "and secret wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    )
    cleaned_err = redact_aws_secrets(
        raw_err,
        creds.access_key_id,
        creds.secret_access_key,
        creds.session_token,
    )
    assert "ASIAIOSFODNN7EXAMPLE" not in cleaned_err
    assert "wJalrXUtnFEMI" not in cleaned_err
    assert "AQoDYXdzEJr1EXAMPLETOKEN99999" not in cleaned_err


# -- 6. Total download size limit enforcement --------------------------------------------------


async def test_max_upload_bytes_enforced_during_s3_download() -> None:
    manifest_key = (
        "cur/bedrock-exports/bedrock-cur/20260901-20261001/bedrock-cur-Manifest.json"
    )
    part_key = "cur/bedrock-exports/bedrock-cur/20260901-20261001/part-00001.csv"
    manifest_json = json.dumps(
        {
            "assemblyId": "asm-large",
            "bucket": "contoso-cur-exports",
            "currencyCode": "USD",
            "reportKeys": [part_key],
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host.startswith("sts."):
            return httpx.Response(200, text=_sts_xml_response())
        if request.url.params.get("list-type") == "2":
            return httpx.Response(
                200,
                text=_list_objects_xml(
                    [(manifest_key, "2026-10-02T00:00:00Z", len(manifest_json))]
                ),
            )
        path = request.url.path.lstrip("/")
        if path == manifest_key:
            return httpx.Response(200, text=manifest_json)
        if path == part_key:
            return httpx.Response(200, content=b"x" * 2048)
        return httpx.Response(404)

    connector = AwsBillingConnector(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        web_identity_token_provider=lambda _aud: "oidc-jwt",
        clock=_clock,
        max_upload_bytes=1024,
    )
    with pytest.raises(BillingPayloadTooLargeError, match="exceed the maximum allowed"):
        await connector.start_or_poll_acquisition(_aws_connection(), _aws_run("2026-09"))

    await connector.close()
    _ = UpstreamAuthorizationError
