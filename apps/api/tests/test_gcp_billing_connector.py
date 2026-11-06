"""Tests for Google Cloud Billing BigQuery connector, auth flows, pagination, and ingestion."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from azure.core.credentials import AccessToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from mosaic_api.billing_pricing import (
    BillingAcquisitionRun,
    BillingConnection,
    BillingConnector,
)
from mosaic_api.errors import ValidationError
from mosaic_api.integrations.billing_gcp import (
    BIGQUERY_SCOPE,
    CLOUD_PLATFORM_SCOPE,
    DEFAULT_GCP_MAX_BYTES_BILLED,
    OAUTH_JWT_BEARER_GRANT_TYPE,
    OAUTH_TOKEN_EXCHANGE_GRANT_TYPE,
    BillingIngestionEngine,
    GcpBillingConnector,
    build_gcp_billing_export_query,
    deterministic_bigquery_job_id,
)
from mosaic_api.services.billing_ingestion import (
    BillingPayloadTooLargeError,
    ingest_billing_parts,
)

TENANT = "tenant-gcp-billing"


def _clock() -> datetime:
    return datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def _generate_rsa_private_key_pem() -> tuple[str, Any]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    return pem, key.public_key()


def _valid_billing_table_schema() -> dict[str, Any]:
    return {
        "fields": [
            {"name": "billing_account_id", "type": "STRING", "mode": "NULLABLE"},
            {
                "name": "project",
                "type": "RECORD",
                "mode": "NULLABLE",
                "fields": [{"name": "id", "type": "STRING", "mode": "NULLABLE"}],
            },
            {
                "name": "service",
                "type": "RECORD",
                "mode": "NULLABLE",
                "fields": [
                    {"name": "id", "type": "STRING", "mode": "NULLABLE"},
                    {"name": "description", "type": "STRING", "mode": "NULLABLE"},
                ],
            },
            {
                "name": "sku",
                "type": "RECORD",
                "mode": "NULLABLE",
                "fields": [
                    {"name": "id", "type": "STRING", "mode": "NULLABLE"},
                    {"name": "description", "type": "STRING", "mode": "NULLABLE"},
                ],
            },
            {"name": "usage_start_time", "type": "TIMESTAMP", "mode": "NULLABLE"},
            {"name": "usage_end_time", "type": "TIMESTAMP", "mode": "NULLABLE"},
            {
                "name": "usage",
                "type": "RECORD",
                "mode": "NULLABLE",
                "fields": [
                    {"name": "amount", "type": "FLOAT", "mode": "NULLABLE"},
                    {"name": "unit", "type": "STRING", "mode": "NULLABLE"},
                    {"name": "amount_in_pricing_units", "type": "FLOAT", "mode": "NULLABLE"},
                    {"name": "pricing_unit", "type": "STRING", "mode": "NULLABLE"},
                ],
            },
            {"name": "cost", "type": "FLOAT", "mode": "NULLABLE"},
            {"name": "cost_type", "type": "STRING", "mode": "NULLABLE"},
            {"name": "currency", "type": "STRING", "mode": "NULLABLE"},
            {"name": "currency_conversion_rate", "type": "FLOAT", "mode": "NULLABLE"},
            {
                "name": "invoice",
                "type": "RECORD",
                "mode": "NULLABLE",
                "fields": [{"name": "month", "type": "STRING", "mode": "NULLABLE"}],
            },
            {
                "name": "location",
                "type": "RECORD",
                "mode": "NULLABLE",
                "fields": [{"name": "region", "type": "STRING", "mode": "NULLABLE"}],
            },
            {
                "name": "resource",
                "type": "RECORD",
                "mode": "NULLABLE",
                "fields": [
                    {"name": "name", "type": "STRING", "mode": "NULLABLE"},
                    {"name": "global_name", "type": "STRING", "mode": "NULLABLE"},
                ],
            },
            {
                "name": "price",
                "type": "RECORD",
                "mode": "NULLABLE",
                "fields": [{"name": "effective_price", "type": "NUMERIC", "mode": "NULLABLE"}],
            },
            {
                "name": "credits",
                "type": "RECORD",
                "mode": "REPEATED",
                "fields": [
                    {"name": "id", "type": "STRING", "mode": "NULLABLE"},
                    {"name": "name", "type": "STRING", "mode": "NULLABLE"},
                    {"name": "full_name", "type": "STRING", "mode": "NULLABLE"},
                    {"name": "type", "type": "STRING", "mode": "NULLABLE"},
                    {"name": "amount", "type": "FLOAT", "mode": "NULLABLE"},
                ],
            },
        ]
    }


def _query_result_schema() -> dict[str, Any]:
    return {
        "fields": [
            {"name": "billing_account_id", "type": "STRING", "mode": "NULLABLE"},
            {"name": "project_id", "type": "STRING", "mode": "NULLABLE"},
            {"name": "service_description", "type": "STRING", "mode": "NULLABLE"},
            {"name": "sku_id", "type": "STRING", "mode": "NULLABLE"},
            {"name": "sku_description", "type": "STRING", "mode": "NULLABLE"},
            {"name": "location_region", "type": "STRING", "mode": "NULLABLE"},
            {"name": "resource_name", "type": "STRING", "mode": "NULLABLE"},
            {"name": "resource_global_name", "type": "STRING", "mode": "NULLABLE"},
            {"name": "usage_start_time", "type": "TIMESTAMP", "mode": "NULLABLE"},
            {"name": "usage_end_time", "type": "TIMESTAMP", "mode": "NULLABLE"},
            {"name": "usage_amount", "type": "FLOAT", "mode": "NULLABLE"},
            {"name": "usage_unit", "type": "STRING", "mode": "NULLABLE"},
            {"name": "usage_amount_in_pricing_units", "type": "FLOAT", "mode": "NULLABLE"},
            {"name": "usage_pricing_unit", "type": "STRING", "mode": "NULLABLE"},
            {"name": "cost", "type": "FLOAT", "mode": "NULLABLE"},
            {"name": "cost_type", "type": "STRING", "mode": "NULLABLE"},
            {"name": "currency", "type": "STRING", "mode": "NULLABLE"},
            {"name": "currency_conversion_rate", "type": "FLOAT", "mode": "NULLABLE"},
            {"name": "price_effective_price", "type": "NUMERIC", "mode": "NULLABLE"},
            {"name": "invoice_month", "type": "STRING", "mode": "NULLABLE"},
            {
                "name": "credits",
                "type": "RECORD",
                "mode": "REPEATED",
                "fields": [
                    {"name": "id", "type": "STRING", "mode": "NULLABLE"},
                    {"name": "name", "type": "STRING", "mode": "NULLABLE"},
                    {"name": "full_name", "type": "STRING", "mode": "NULLABLE"},
                    {"name": "type", "type": "STRING", "mode": "NULLABLE"},
                    {"name": "amount", "type": "FLOAT", "mode": "NULLABLE"},
                ],
            },
        ]
    }


def _connection(
    *,
    max_bytes_billed: int | None = None,
    location: str | None = "US",
    service_account_email: str | None = (
        "mosaic-billing-reader@contoso-finops.iam.gserviceaccount.com"
    ),
    workload_identity_audience: str | None = (
        "//iam.googleapis.com/projects/123456789/locations/global/"
        "workloadIdentityPools/mosaic-pool/providers/entra-oidc"
    ),
) -> BillingConnection:
    return BillingConnection(
        id="conn-gcp-1",
        tenant_id=TENANT,
        name="GCP Vertex Billing Export",
        provider="gcp",
        pricing_cloud="gcp-vertex",
        gcp_billing_account_id="012345-6789AB-CDEF01",
        gcp_project_id="contoso-finops",
        gcp_dataset_id="billing_export",
        gcp_table_id="gcp_billing_export_resource_v1_012345_6789AB_CDEF01",
        gcp_location=location,
        gcp_service_account_email=service_account_email,
        gcp_workload_identity_audience=workload_identity_audience,
        gcp_max_bytes_billed=max_bytes_billed,
    )


def _run(
    *,
    run_id: str = "run-gcp-2026-09",
    source_month: str = "2026-09",
    operation_id: str | None = None,
    operation_location: str | None = None,
) -> BillingAcquisitionRun:
    return BillingAcquisitionRun(
        id=run_id,
        tenant_id=TENANT,
        connection_id="conn-gcp-1",
        connection_name="GCP Vertex Billing Export",
        provider="gcp",
        pricing_cloud="gcp-vertex",
        acquisition_kind="connected",
        source_month=source_month,
        operation_id=operation_id,
        operation_location=operation_location,
    )


class _FakeEntraCredential:
    def __init__(
        self,
        subject_token: str = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.entra.sig",
    ) -> None:
        self.subject_token = subject_token
        self.requested_scopes: list[str] = []

    async def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        self.requested_scopes.extend(scopes)
        return AccessToken(self.subject_token, int(_clock().timestamp()) + 3600)

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


# -- 1. SQL Builder & Identifier Validation ----------------------------------------------------


def test_build_gcp_billing_export_query_validates_identifiers_and_preserves_credits_array() -> None:
    sql = build_gcp_billing_export_query(
        "contoso-finops",
        "billing_export",
        "gcp_billing_export_resource_v1_012345",
    )
    assert "`contoso-finops.billing_export.gcp_billing_export_resource_v1_012345`" in sql
    assert "usage_start_time >= TIMESTAMP(@usage_start)" in sql
    assert "usage_start_time < TIMESTAMP(@usage_end)" in sql
    assert "@billing_account_id" in sql
    assert "invoice.month AS invoice_month" in sql
    # Must NOT filter the observation window by invoice.month in the WHERE clause
    where_clause = sql.split("WHERE", 1)[1]
    assert "invoice.month" not in where_clause
    # Must aggregate credits inside a correlated scalar subquery rather than top-level UNNEST
    assert (
        "(SELECT ARRAY_AGG(STRUCT(c.id, c.name, c.full_name, c.type, c.amount)) "
        "FROM UNNEST(credits) AS c) AS credits"
    ) in sql

    for malicious in (
        "proj`; DROP TABLE x; --",
        "dataset with spaces",
        "table/slash",
        "../traversal",
        "",
    ):
        with pytest.raises(ValidationError, match=r"unsupported characters|missing"):
            build_gcp_billing_export_query("contoso-finops", "billing_export", malicious)


# -- 2. Workload Identity Federation + SA Impersonation + check_access -------------------------


async def test_wif_token_exchange_service_account_impersonation_and_check_access() -> None:
    cred = _FakeEntraCredential()
    conn = _connection()
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))

        if request.url == httpx.URL("https://sts.googleapis.com/v1/token"):
            form = parse_qs(request.content.decode("utf-8"))
            assert form["grant_type"] == [OAUTH_TOKEN_EXCHANGE_GRANT_TYPE]
            assert form["audience"] == [conn.gcp_workload_identity_audience]
            assert form["subject_token"] == [cred.subject_token]
            assert form["scope"] == [CLOUD_PLATFORM_SCOPE]
            return httpx.Response(
                200,
                json={"access_token": "sts-federated-token-123", "token_type": "Bearer"},
            )

        if request.url.host == "iamcredentials.googleapis.com":
            assert request.headers["Authorization"] == "Bearer sts-federated-token-123"
            body = json.loads(request.content.decode("utf-8"))
            assert body["scope"] == [BIGQUERY_SCOPE]
            return httpx.Response(
                200,
                json={
                    "accessToken": "impersonated-bq-token-456",
                    "expireTime": "2026-10-15T13:00:00Z",
                },
            )

        if request.method == "GET" and "/datasets/billing_export/tables/" in str(request.url):
            assert request.headers["Authorization"] == "Bearer impersonated-bq-token-456"
            return httpx.Response(
                200,
                json={
                    "location": "US",
                    "schema": _valid_billing_table_schema(),
                },
            )

        if request.method == "POST" and str(request.url).endswith("/projects/contoso-finops/jobs"):
            assert request.headers["Authorization"] == "Bearer impersonated-bq-token-456"
            job_req = json.loads(request.content.decode("utf-8"))
            assert job_req["configuration"]["dryRun"] is True
            query_cfg = job_req["configuration"]["query"]
            assert query_cfg["useLegacySql"] is False
            assert query_cfg["maximumBytesBilled"] == str(DEFAULT_GCP_MAX_BYTES_BILLED)
            assert query_cfg["parameterMode"] == "NAMED"
            param_names = {p["name"] for p in query_cfg["queryParameters"]}
            assert param_names == {"usage_start", "usage_end", "billing_account_id"}
            return httpx.Response(
                200,
                json={
                    "jobComplete": True,
                    "statistics": {"totalBytesProcessed": "52428800"},
                },
            )

        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        connector: BillingConnector = GcpBillingConnector(
            client=http_client,
            credential=cred,
            clock=_clock,
        )
        result = await connector.check_access(conn)

    assert result.status == "ready"
    assert result.provider == "gcp"
    assert result.scope_summary == (
        "contoso-finops.billing_export.gcp_billing_export_resource_v1_012345_6789AB_CDEF01"
    )
    assert result.supported_source_months == ["2026-09"]
    assert result.details["estimatedBytesProcessed"] == 52_428_800
    assert result.details["maximumBytesBilled"] == DEFAULT_GCP_MAX_BYTES_BILLED
    assert len(calls) == 4


# -- 3. Service Account JWT Signing & Secret Redaction -----------------------------------------


@pytest.mark.parametrize("impersonate", [False, True])
async def test_service_account_jwt_assertion_and_secret_redaction(impersonate: bool) -> None:
    private_key_pem, public_key = _generate_rsa_private_key_pem()
    sa_email = "vault-sa@contoso-finops.iam.gserviceaccount.com"
    sa_secret_json = json.dumps(
        {
            "type": "service_account",
            "project_id": "contoso-finops",
            "private_key_id": "key-id-99",
            "private_key": private_key_pem,
            "client_email": sa_email,
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )
    conn = _connection(
        service_account_email=(
            "target-sa@contoso-finops.iam.gserviceaccount.com" if impersonate else sa_email
        ),
        workload_identity_audience=None,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == httpx.URL("https://oauth2.googleapis.com/token"):
            form = parse_qs(request.content.decode("utf-8"))
            assert form["grant_type"] == [OAUTH_JWT_BEARER_GRANT_TYPE]
            assertion = form["assertion"][0]
            header = jwt.get_unverified_header(assertion)
            assert header["kid"] == "key-id-99"
            decoded = jwt.decode(
                assertion,
                public_key,
                algorithms=["RS256"],
                audience="https://oauth2.googleapis.com/token",
                options={"verify_exp": False},
            )
            assert decoded["iss"] == sa_email
            assert decoded["sub"] == sa_email
            assert decoded["scope"] == (
                "https://www.googleapis.com/auth/cloud-platform"
                if impersonate
                else "https://www.googleapis.com/auth/bigquery"
            )
            assert decoded["iat"] == int(_clock().timestamp())
            assert decoded["exp"] == int(_clock().timestamp()) + 3600
            return httpx.Response(
                200,
                json={"access_token": "sa-jwt-access-token-789"},
            )

        if request.url.host == "iamcredentials.googleapis.com":
            assert impersonate
            assert request.headers["Authorization"] == "Bearer sa-jwt-access-token-789"
            body = json.loads(request.content)
            assert body["scope"] == [BIGQUERY_SCOPE]
            return httpx.Response(200, json={"accessToken": "sa-impersonated-token"})

        if request.method == "GET" and "/tables/" in str(request.url):
            expected_token = "sa-impersonated-token" if impersonate else "sa-jwt-access-token-789"
            assert request.headers["Authorization"] == f"Bearer {expected_token}"
            return httpx.Response(200, json={"schema": _valid_billing_table_schema()})

        if request.method == "POST" and str(request.url).endswith("/jobs"):
            return httpx.Response(
                200,
                json={"jobComplete": True, "statistics": {"totalBytesProcessed": "1024"}},
            )

        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        connector = GcpBillingConnector(client=http_client, clock=_clock)
        check = await connector.check_access(conn, secret_value=sa_secret_json)

    assert check.status == "ready"
    assert check.has_stored_secret is True
    serialized = json.dumps(check.model_dump(mode="json"))
    assert "PRIVATE KEY" not in serialized
    assert "sa-jwt-access-token-789" not in serialized


@pytest.mark.parametrize("impersonate", [False, True])
async def test_external_account_credential_json_with_impersonation_url(impersonate: bool) -> None:
    ext_secret_json = json.dumps(
        {
            "type": "external_account",
            "audience": (
                "//iam.googleapis.com/projects/123/locations/global/"
                "workloadIdentityPools/p/providers/entra"
            ),
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
            "token_url": "https://sts.googleapis.com/v1/token",
            "service_account_impersonation_url": (
                "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
                "ext-reader@contoso-finops.iam.gserviceaccount.com:generateAccessToken"
            ),
        }
    )
    if not impersonate:
        credential_data = json.loads(ext_secret_json)
        credential_data.pop("service_account_impersonation_url")
        ext_secret_json = json.dumps(credential_data)
    conn = _connection(service_account_email=None, workload_identity_audience=None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == httpx.URL("https://sts.googleapis.com/v1/token"):
            form = parse_qs(request.content.decode())
            assert form["scope"] == [CLOUD_PLATFORM_SCOPE if impersonate else BIGQUERY_SCOPE]
            return httpx.Response(200, json={"access_token": "ext-sts-tok"})
        if "iamcredentials.googleapis.com" in str(request.url):
            assert impersonate
            assert request.headers["Authorization"] == "Bearer ext-sts-tok"
            assert json.loads(request.content)["scope"] == [BIGQUERY_SCOPE]
            return httpx.Response(200, json={"accessToken": "ext-impersonated-tok"})
        if request.method == "GET" and "/tables/" in str(request.url):
            expected_token = "ext-impersonated-tok" if impersonate else "ext-sts-tok"
            assert request.headers["Authorization"] == f"Bearer {expected_token}"
            return httpx.Response(200, json={"schema": _valid_billing_table_schema()})
        if request.method == "POST" and str(request.url).endswith("/jobs"):
            return httpx.Response(
                200,
                json={"jobComplete": True, "statistics": {"totalBytesProcessed": "2048"}},
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = GcpBillingConnector(
            client=client,
            subject_token_provider=lambda _aud: _async_str("subject-jwt-tok"),
            clock=_clock,
        )
        check = await connector.check_access(conn, secret_value=ext_secret_json)

    assert check.status == "ready"


@pytest.mark.parametrize("token_source", ["entra", "raw", "external", "embedded"])
@pytest.mark.parametrize("impersonate", [False, True])
async def test_wif_auth_paths_request_job_capable_scopes(
    token_source: str, impersonate: bool
) -> None:
    conn = _connection(
        service_account_email=(
            "reader@contoso-finops.iam.gserviceaccount.com" if impersonate else None
        )
    )
    credential = _FakeEntraCredential("entra-subject")
    secret: str | None = None
    expected_subject = "entra-subject"
    if token_source == "raw":
        secret = "raw-subject"
        expected_subject = secret
    elif token_source in {"external", "embedded"}:
        credential_data = {
            "type": "external_account",
            "audience": conn.gcp_workload_identity_audience,
        }
        if token_source == "embedded":
            credential_data["subject_token"] = "embedded-subject"
            expected_subject = "embedded-subject"
        secret = json.dumps(credential_data)

    stages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "sts.googleapis.com":
            stages.append("sts")
            form = parse_qs(request.content.decode())
            expected_scope = (
                "https://www.googleapis.com/auth/cloud-platform"
                if impersonate
                else "https://www.googleapis.com/auth/bigquery"
            )
            assert form["scope"] == [expected_scope]
            assert form["subject_token"] == [expected_subject]
            return httpx.Response(200, json={"access_token": "job-capable-sts-token"})
        if request.url.host == "iamcredentials.googleapis.com":
            stages.append("iam")
            assert impersonate
            assert json.loads(request.content)["scope"] == [
                "https://www.googleapis.com/auth/bigquery"
            ]
            return httpx.Response(200, json={"accessToken": "job-capable-impersonated-token"})
        expected_token = (
            "job-capable-impersonated-token" if impersonate else "job-capable-sts-token"
        )
        assert request.headers["Authorization"] == f"Bearer {expected_token}"
        if request.method == "GET" and "/tables/" in str(request.url):
            stages.append("table")
            return httpx.Response(200, json={"schema": _valid_billing_table_schema()})
        if request.method == "POST" and request.url.path.endswith("/jobs"):
            stages.append("job")
            configuration = json.loads(request.content)["configuration"]
            query = configuration["query"]
            assert configuration["dryRun"] is True
            assert query["maximumBytesBilled"] == str(DEFAULT_GCP_MAX_BYTES_BILLED)
            assert query["parameterMode"] == "NAMED"
            assert {item["name"] for item in query["queryParameters"]} == {
                "usage_start", "usage_end", "billing_account_id"
            }
            assert query["query"].lstrip().startswith("SELECT")
            return httpx.Response(
                200, json={"jobComplete": True, "statistics": {"totalBytesProcessed": "1024"}}
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = GcpBillingConnector(client=client, credential=credential, clock=_clock)
        check = await connector.check_access(conn, secret_value=secret)

    assert check.status == "ready"
    assert stages == (["sts", "iam", "table", "job"] if impersonate else ["sts", "table", "job"])
    assert bool(credential.requested_scopes) is (token_source in {"entra", "external"})


# -- 4. check_access Failure Modes: IAM Permissions & Missing Schema Columns -------------------


async def test_check_access_fails_with_actionable_iam_roles_and_missing_schema_columns() -> None:
    conn = _connection()

    # Case A: 403 on table metadata -> names roles/bigquery.dataViewer and roles/bigquery.jobUser
    def handler_403_table(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "/tables/" in str(request.url):
            return httpx.Response(
                403,
                json={
                    "error": {
                        "code": 403,
                        "message": "Access Denied: Table contoso-finops:billing_export.table: "
                        "User does not have bigquery.tables.get permission with Bearer secret-tok",
                        "errors": [{"reason": "accessDenied", "message": "Access Denied"}],
                    }
                },
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    async def _static_token() -> str:
        return "secret-tok-value"

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_403_table)) as client:
        connector = GcpBillingConnector(
            client=client,
            token_provider=_static_token,
            clock=_clock,
        )
        res_403_table = await connector.check_access(conn)

    assert res_403_table.status == "failed"
    assert "roles/bigquery.dataViewer" in res_403_table.message
    assert "roles/bigquery.jobUser" in res_403_table.message
    assert "secret-tok-value" not in res_403_table.message

    # Case B: 403 on jobs.insert dryRun -> names jobUser and dataViewer roles
    def handler_403_job(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "/tables/" in str(request.url):
            return httpx.Response(200, json={"schema": _valid_billing_table_schema()})
        if request.method == "POST" and str(request.url).endswith("/jobs"):
            return httpx.Response(
                403,
                json={
                    "error": {
                        "code": 403,
                        "message": "Access Denied: Project contoso-finops: User does not have "
                        "bigquery.jobs.create permission in project contoso-finops.",
                        "errors": [{"reason": "accessDenied", "message": "Access Denied"}],
                    }
                },
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_403_job)) as client:
        connector = GcpBillingConnector(
            client=client,
            token_provider=_static_token,
            clock=_clock,
        )
        res_403_job = await connector.check_access(conn)

    assert res_403_job.status == "failed"
    assert "roles/bigquery.jobUser" in res_403_job.message
    assert "roles/bigquery.dataViewer" in res_403_job.message

    # Case C: Unrelated BigQuery table missing Cloud Billing export columns
    def handler_bad_schema(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "/tables/" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "schema": {
                        "fields": [
                            {"name": "event_id", "type": "STRING"},
                            {"name": "user_email", "type": "STRING"},
                            {"name": "created_at", "type": "TIMESTAMP"},
                        ]
                    }
                },
            )
        raise AssertionError("Should not run dryRun query when table schema is invalid")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_bad_schema)) as client:
        connector = GcpBillingConnector(
            client=client,
            token_provider=_static_token,
            clock=_clock,
        )
        res_schema = await connector.check_access(conn)

    assert res_schema.status == "failed"
    for col in ("service", "sku", "usage", "cost", "currency", "usage_start_time"):
        assert col in res_schema.message
    assert set(res_schema.details["missingColumns"]) == {
        "service",
        "sku",
        "usage",
        "cost",
        "currency",
        "usage_start_time",
    }


# -- 5. Deterministic jobId, Async Polling, Multi-Page Pagination, and End-to-End Ingestion ----


async def test_acquisition_polling_pagination_and_end_to_end_gemini_derivation() -> None:
    conn = _connection(location="US", max_bytes_billed=5_000_000_000)
    run = _run()
    expected_job_id = deterministic_bigquery_job_id(
        run.tenant_id,
        run.id,
        run.source_month,
        connection_id=conn.id,
    )

    page_1_rows = [
        # Row 1: Gemini 1.5 Pro Input Tokens (2M tokens, $3.00 cost)
        # Carries TWO credits on the same row:
        # 1) COMMITTED_USAGE_DISCOUNT of -$0.60 (recurring -> included, reduces cost to $2.40)
        # 2) PROMOTION of -$0.40 (one-off -> excluded so it does NOT depress the derived rate)
        # Also verify that having 2 credits on this row does NOT double-count cost or usage!
        {
            "f": [
                {"v": "012345-6789AB-CDEF01"},
                {"v": "contoso-vertex-prod"},
                {"v": "Vertex AI"},
                {"v": "SKU-GEMINI-15-PRO-IN"},
                {"v": "Gemini 1.5 Pro Input Tokens"},
                {"v": "us-central1"},
                {
                    "v": (
                        "projects/contoso-vertex-prod/locations/us-central1/"
                        "publishers/google/models/gemini-1.5-pro"
                    )
                },
                {
                    "v": (
                        "//aiplatform.googleapis.com/projects/contoso-vertex-prod/"
                        "locations/us-central1/models/gemini-1.5-pro"
                    )
                },
                {"v": "2026-09-14T10:30:00Z"},
                {"v": "2026-09-14T11:00:00Z"},
                {"v": "2000000"},
                {"v": "tokens"},
                {"v": "2000000"},
                {"v": "1 token"},
                {"v": "3.00"},
                {"v": "regular"},
                {"v": "USD"},
                {"v": "1.0"},
                {"v": "0.0000015"},
                {"v": "202609"},
                {
                    "v": [
                        {
                            "v": {
                                "f": [
                                    {"v": "cud-1"},
                                    {"v": "Committed Usage Discount"},
                                    {"v": "Committed Usage Discount: Vertex AI"},
                                    {"v": "COMMITTED_USAGE_DISCOUNT"},
                                    {"v": "-0.60"},
                                ]
                            }
                        },
                        {
                            "v": {
                                "f": [
                                    {"v": "promo-1"},
                                    {"v": "Free Trial Credit"},
                                    {"v": "Promotion Credit"},
                                    {"v": "PROMOTION"},
                                    {"v": "-0.40"},
                                ]
                            }
                        },
                    ]
                },
            ]
        }
    ]

    page_2_rows = [
        # Row 2: Gemini 1.5 Pro Output Tokens (500K tokens in 1K token units = 500, $2.50 cost)
        # Late adjustment appearing on invoice.month 202610 for usage on 2026-09-30!
        # Includes SUBSCRIPTION_BENEFIT of -$0.50 -> effective cost $2.00 over 0.5M tokens = $4.00/M
        {
            "f": [
                {"v": "012345-6789AB-CDEF01"},
                {"v": "contoso-vertex-prod"},
                {"v": "Vertex AI"},
                {"v": "SKU-GEMINI-15-PRO-OUT"},
                {"v": "Gemini 1.5 Pro Output Tokens"},
                {"v": "us-central1"},
                {
                    "v": (
                        "projects/contoso-vertex-prod/locations/us-central1/"
                        "publishers/google/models/gemini-1.5-pro"
                    )
                },
                {"v": None},
                {"v": "2026-09-30T22:00:00Z"},
                {"v": "2026-09-30T23:00:00Z"},
                {"v": "500000"},
                {"v": "tokens"},
                {"v": "500"},
                {"v": "1K tokens"},
                {"v": "2.50"},
                {"v": "regular"},
                {"v": "USD"},
                {"v": "1.0"},
                {"v": "0.005"},
                {"v": "202610"},
                {
                    "v": [
                        {
                            "v": {
                                "f": [
                                    {"v": "sub-1"},
                                    {"v": "Subscription Benefit"},
                                    {"v": "Enterprise Subscription Benefit"},
                                    {"v": "SUBSCRIPTION_BENEFIT"},
                                    {"v": "-0.50"},
                                ]
                            }
                        }
                    ]
                },
            ]
        }
    ]

    poll_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        if request.method == "POST" and str(request.url).endswith("/projects/contoso-finops/jobs"):
            body = json.loads(request.content.decode("utf-8"))
            assert body["jobReference"] == {
                "projectId": "contoso-finops",
                "jobId": expected_job_id,
                "location": "US",
            }
            q_cfg = body["configuration"]["query"]
            assert q_cfg["maximumBytesBilled"] == "5000000000"
            params = {
                p["name"]: p["parameterValue"]["value"] for p in q_cfg["queryParameters"]
            }
            assert params == {
                "usage_start": "2026-09-01T00:00:00Z",
                "usage_end": "2026-10-01T00:00:00Z",
                "billing_account_id": "012345-6789AB-CDEF01",
            }
            return httpx.Response(
                200,
                json={
                    "jobReference": {
                        "projectId": "contoso-finops",
                        "jobId": expected_job_id,
                        "location": "US",
                    },
                    "jobComplete": False,
                    "status": {"state": "RUNNING"},
                },
            )

        if request.method == "GET" and f"/queries/{expected_job_id}" in str(request.url):
            assert request.url.params.get("location") == "US"
            page_token = request.url.params.get("pageToken")
            poll_count += 1
            if poll_count == 1:
                # Still running on first poll
                return httpx.Response(
                    200,
                    json={
                        "jobReference": {"jobId": expected_job_id, "location": "US"},
                        "jobComplete": False,
                    },
                )
            if page_token is None:
                # Completed: return page 1 with pageToken="token-page-2"
                return httpx.Response(
                    200,
                    json={
                        "jobComplete": True,
                        "schema": _query_result_schema(),
                        "rows": page_1_rows,
                        "pageToken": "token-page-2",
                        "totalRows": "2",
                    },
                )
            if page_token == "token-page-2":
                # Return page 2 (final page)
                return httpx.Response(
                    200,
                    json={
                        "jobComplete": True,
                        "schema": _query_result_schema(),
                        "rows": page_2_rows,
                        "totalRows": "2",
                    },
                )

        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async def _token_provider(_conn: BillingConnection) -> str:
        return "test-bq-token"

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = GcpBillingConnector(
            client=client,
            token_provider=_token_provider,
            clock=_clock,
            page_size=1,
        )

        # Step 1: Submit job -> returns running with deterministic job_id
        step_1 = await connector.start_or_poll_acquisition(conn, run)
        assert step_1.status == "running"
        assert step_1.operation_id == expected_job_id
        assert step_1.operation_location == "US"
        assert step_1.poll_after is not None

        # Step 2: Poll 1 -> still running
        run_polling = run.model_copy(
            update={
                "operation_id": step_1.operation_id,
                "operation_location": step_1.operation_location,
            }
        )
        step_2 = await connector.start_or_poll_acquisition(conn, run_polling)
        assert step_2.status == "running"
        assert step_2.operation_id == expected_job_id

        # Step 3: Poll 2 -> jobComplete=True, fetches page 1 + page 2 -> completed
        step_3 = await connector.start_or_poll_acquisition(conn, run_polling)
        assert step_3.status == "completed"
        assert step_3.source_manifest_id == expected_job_id
        assert step_3.source_scope == (
            "contoso-finops.billing_export.gcp_billing_export_resource_v1_012345_6789AB_CDEF01"
        )
        assert len(step_3.parts) == 1
        assert step_3.parts[0].name == "gcp-2026-09.jsonl"
        assert step_3.parts[0].content_type == "application/x-ndjson"
        assert step_3.total_read_bytes == len(step_3.parts[0].content)

    # Feed parts into BillingIngestionEngine & ingest_billing_parts to verify end-to-end rates
    engine = BillingIngestionEngine()
    ingested = engine.ingest_parts(
        step_3.parts,
        provider="gcp",
        pricing_cloud="gcp-vertex",
        source_month="2026-09",
        source_scope=step_3.source_scope,
    )
    direct_ingested = ingest_billing_parts(
        step_3.parts,
        provider="gcp",
        pricing_cloud="gcp-vertex",
        source_month="2026-09",
        source_scope=step_3.source_scope,
    )
    assert ingested.total_eligible_cost_usd == pytest.approx(
        direct_ingested.total_eligible_cost_usd
    )

    assert ingested.raw_row_count == 2
    assert ingested.eligible_row_count == 2
    assert ingested.excluded_row_count == 0
    assert ingested.invoice_months == ["2026-09", "2026-10"]
    assert any(
        issue.code == "late_adjustments_note" and issue.severity == "warning"
        for issue in ingested.issues
    )
    assert any(issue.code == "oneOffCreditsExcluded" for issue in ingested.issues)

    assert len(ingested.candidates) == 1
    gemini = ingested.candidates[0]
    assert gemini.model == "gemini-1.5-pro"
    assert gemini.publisher == "Google"
    assert gemini.region == "us-central1"
    # Verify zero credit double-counting:
    # Input: 2M tokens, $3.00 base - $0.60 CUD = $2.40 eligible ($0.40 promo excluded) -> $1.20 / 1M
    assert gemini.input_tokens == pytest.approx(2_000_000.0)
    assert gemini.input_cost_usd == pytest.approx(2.40)
    assert gemini.input_per_million == pytest.approx(1.20)
    # Output: 500 * 1K = 500K tokens, $2.50 base - $0.50 benefit = $2.00 -> $4.00 / 1M
    assert gemini.output_tokens == pytest.approx(500_000.0)
    assert gemini.output_cost_usd == pytest.approx(2.00)
    assert gemini.output_per_million == pytest.approx(4.00)
    assert gemini.excluded_cost_usd == pytest.approx(0.40)
    assert gemini.publishable is True


# -- 6. maximumBytesBilled Enforcement & bytesBilledLimitExceeded Handling ---------------------


async def test_bytes_billed_limit_exceeded_raises_clear_validation_error() -> None:
    conn = _connection(max_bytes_billed=100_000_000)
    run = _run(operation_id="job-exceeded-1")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "/queries/job-exceeded-1" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "jobComplete": True,
                    "errors": [
                        {
                            "reason": "bytesBilledLimitExceeded",
                            "message": (
                                "Query exceeded limit for bytes billed: 100000000. "
                                "524288000 or higher required."
                            ),
                        }
                    ],
                },
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = GcpBillingConnector(
            client=client,
            token_provider=lambda: _async_str("bq-token"),
            clock=_clock,
        )
        with pytest.raises(ValidationError, match="maximumBytesBilled") as exc_info:
            await connector.start_or_poll_acquisition(conn, run)

    assert exc_info.value.details["reason"] == "bytesBilledLimitExceeded"
    assert exc_info.value.details["maximumBytesBilled"] == 100_000_000


async def test_restart_recovery_handles_409_already_exists_on_job_submission() -> None:
    conn = _connection()
    run = _run()
    expected_job_id = deterministic_bigquery_job_id(
        run.tenant_id,
        run.id,
        run.source_month,
        connection_id=conn.id,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and str(request.url).endswith("/jobs"):
            return httpx.Response(
                409,
                json={
                    "error": {
                        "code": 409,
                        "message": f"Already Exists: Job contoso-finops:US.{expected_job_id}",
                        "errors": [{"reason": "duplicate", "message": "Already Exists"}],
                    }
                },
            )
        if request.method == "GET" and f"/queries/{expected_job_id}" in str(request.url):
            return httpx.Response(
                200,
                json={"jobComplete": False, "status": {"state": "RUNNING"}},
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = GcpBillingConnector(
            client=client,
            token_provider=lambda: _async_str("bq-token"),
            clock=_clock,
        )
        step = await connector.start_or_poll_acquisition(conn, run)

    assert step.status == "running"
    assert step.operation_id == expected_job_id


async def test_result_row_and_byte_bounds_enforced() -> None:
    conn = _connection()
    run = _run(operation_id="job-bounded")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jobComplete": True,
                "schema": {"fields": [{"name": "sku_description", "type": "STRING"}]},
                "rows": [
                    {"f": [{"v": "Gemini 1.5 Pro Input Tokens"}]},
                    {"f": [{"v": "Gemini 1.5 Pro Output Tokens"}]},
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector_rows = GcpBillingConnector(
            client=client,
            token_provider=lambda: _async_str("bq-token"),
            clock=_clock,
            max_result_rows=1,
        )
        with pytest.raises(ValidationError, match="maximum allowed row count"):
            await connector_rows.start_or_poll_acquisition(conn, run)

        connector_bytes = GcpBillingConnector(
            client=client,
            token_provider=lambda: _async_str("bq-token"),
            clock=_clock,
            max_result_bytes=1024,
        )
        # Make a row payload > 1024 bytes
        large_rows = [{"f": [{"v": "X" * 2000}]}]

    def large_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jobComplete": True,
                "schema": {"fields": [{"name": "sku_description", "type": "STRING"}]},
                "rows": large_rows,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(large_handler)) as client:
        connector_bytes = GcpBillingConnector(
            client=client,
            token_provider=lambda: _async_str("bq-token"),
            clock=_clock,
            max_result_bytes=1024,
        )
        with pytest.raises(BillingPayloadTooLargeError, match="exceeds the maximum allowed size"):
            await connector_bytes.start_or_poll_acquisition(conn, run)


async def test_ssrf_protection_rejects_non_google_token_uri() -> None:
    private_key_pem, _ = _generate_rsa_private_key_pem()
    bad_secret = json.dumps(
        {
            "type": "service_account",
            "client_email": "sa@contoso-finops.iam.gserviceaccount.com",
            "private_key": private_key_pem,
            "token_uri": "https://attacker.example.com/token",
        }
    )
    conn = _connection(workload_identity_audience=None)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(500))
    ) as client:
        connector = GcpBillingConnector(client=client, clock=_clock)
        with pytest.raises(ValidationError, match=r"googleapis\.com"):
            await connector.start_or_poll_acquisition(conn, _run(), secret_value=bad_secret)


async def _async_str(value: str) -> str:
    return value
