"""Isolated synthetic tests for AzureBillingConnector (Cost Details API and Export Manifest)."""

from __future__ import annotations

import gzip
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from azure.core.credentials import AccessToken
from mosaic_api.azure_connections import (
    AzureConnection,
    AzureConnectionService,
    InMemoryAzureConnectionRepository,
)
from mosaic_api.billing_pricing import (
    BillingAcquisitionRun,
    BillingConnection,
    BillingConnector,
)
from mosaic_api.domain import (
    AuditEvent,
    ModelEndpoint,
    ModelEndpointCapabilities,
    ModelProvider,
    new_id,
)
from mosaic_api.errors import (
    UpstreamAuthorizationError,
    UpstreamError,
    ValidationError,
)
from mosaic_api.integrations.billing_azure import (
    AzureBillingConnector,
    parse_azure_billing_scope,
    redact_azure_url,
    redact_sensitive_text,
    validate_azure_arm_url,
    validate_azure_billing_scope,
    validate_azure_blob_url,
    validate_azure_export_manifest_url,
)
from mosaic_api.observed import ObservedModelDeployment
from mosaic_api.pricing import build_book, deployment_facts, load_seed
from mosaic_api.repositories import (
    InMemoryGatewayRepository,
    InMemoryMcpEndpointRepository,
    InMemoryModelEndpointRepository,
)
from mosaic_api.services.billing_ingestion import (
    BillingPayloadTooLargeError,
    ingest_billing_parts,
)

TENANT = "tenant-azure-billing"
SUB_SCOPE = "/subscriptions/00000000-0000-0000-0000-000000000001"
EA_SCOPE = "/providers/Microsoft.Billing/billingAccounts/12345678"
MCA_PROFILE_SCOPE = (
    "/providers/Microsoft.Billing/billingAccounts/12345678-abcd"
    "/billingProfiles/BP-9999"
)


def _clock() -> datetime:
    return datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def _make_connection(
    *,
    scope: str = SUB_SCOPE,
    azure_cloud: Any = "commercial",
    pricing_cloud: str = "commercial",
    mode: Any = "costDetailsApi",
    manifest_url: str | None = None,
    azure_connection_id: str | None = None,
) -> BillingConnection:
    return BillingConnection(
        id="conn-az-1",
        tenant_id=TENANT,
        name="Contoso Azure Billing",
        provider="azure",
        pricing_cloud=pricing_cloud,
        azure_cloud=azure_cloud,
        azure_connection_id=azure_connection_id,
        azure_scope=scope,
        azure_retrieval_mode=mode,
        azure_export_manifest_url=manifest_url,
    )


def _make_run(
    *,
    source_month: str = "2026-09",
    operation_id: str | None = None,
    operation_location: str | None = None,
    pricing_cloud: str = "commercial",
) -> BillingAcquisitionRun:
    return BillingAcquisitionRun(
        id="run-az-1",
        tenant_id=TENANT,
        connection_id="conn-az-1",
        connection_name="Contoso Azure Billing",
        provider="azure",
        pricing_cloud=pricing_cloud,
        acquisition_kind="connected",
        source_month=source_month,
        status="running" if operation_location else "queued",
        operation_id=operation_id,
        operation_location=operation_location,
    )


# -- 1. Scope & URL validation ------------------------------------------------------------------


def test_scope_validation_supports_ea_mca_and_subscription_and_rejects_management_groups() -> None:
    sub = parse_azure_billing_scope(" /subscriptions/00000000-0000-0000-0000-000000000001/ ")
    assert sub.scope == SUB_SCOPE
    assert sub.kind == "subscription"

    ba = parse_azure_billing_scope("/providers/microsoft.billing/billingaccounts/987654")
    assert ba.scope == "/providers/Microsoft.Billing/billingAccounts/987654"
    assert ba.kind == "billingAccount"

    bp = parse_azure_billing_scope(
        "/providers/Microsoft.Billing/billingAccounts/ba-1/billingProfiles/bp-2"
    )
    assert bp.scope == "/providers/Microsoft.Billing/billingAccounts/ba-1/billingProfiles/bp-2"
    assert bp.kind == "billingProfile"

    inv = parse_azure_billing_scope(
        "/providers/Microsoft.Billing/billingAccounts/ba-1/billingProfiles/bp-2/invoiceSections/is-3"
    )
    assert inv.kind == "invoiceSection"

    ea = parse_azure_billing_scope(
        "/providers/Microsoft.Billing/billingAccounts/ba-1/enrollmentAccounts/ea-4"
    )
    assert ea.kind == "enrollmentAccount"

    dept = parse_azure_billing_scope(
        "/providers/Microsoft.Billing/billingAccounts/ba-1/departments/dept-5"
    )
    assert dept.kind == "department"

    cust = parse_azure_billing_scope(
        "/providers/Microsoft.Billing/billingAccounts/ba-1/customers/cust-6"
    )
    assert cust.kind == "customer"

    # Management Group scope must be explicitly rejected with actionable guidance
    with pytest.raises(ValidationError) as exc_info:
        validate_azure_billing_scope("/providers/Microsoft.Management/managementGroups/mg-corp")
    assert exc_info.value.details.get("reason") == "managementGroupUnsupported"
    assert "management group" in exc_info.value.message.casefold()
    assert "/subscriptions/" in exc_info.value.message
    assert "/providers/Microsoft.Billing/billingAccounts/" in exc_info.value.message
    assert "upload" in exc_info.value.message.casefold()

    with pytest.raises(ValidationError):
        validate_azure_billing_scope("/resourceGroups/rg-only")


# -- 2. check_access: success, 403 role guidance, management group, PAYG/MSDN -------------------


async def test_check_access_success_and_permission_or_agreement_failures() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == "Bearer test-arm-token"
        path = request.url.path
        if "00000000-0000-0000-0000-000000000001" in path:
            return httpx.Response(200, json={"value": []})
        if "00000000-0000-0000-0000-000000000403" in path:
            return httpx.Response(
                403,
                json={
                    "error": {
                        "code": "AuthorizationFailed",
                        "message": "The client does not have authorization to perform action.",
                    }
                },
            )
        if "00000000-0000-0000-0000-000000000400" in path:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": "OfferTypeNotSupported",
                        "message": (
                            "Cost Details API is not supported for Pay-As-You-Go or MSDN "
                            "Visual Studio subscriptions."
                        ),
                    }
                },
            )
        return httpx.Response(500, json={"error": {"code": "InternalError"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        connector: BillingConnector = AzureBillingConnector(
            http_client=http_client,
            token_provider=lambda _conn, _sec: _async_str("test-arm-token"),
            clock=_clock,
        )

        # 1. Ready on supported subscription scope
        ok_result = await connector.check_access(_make_connection(scope=SUB_SCOPE))
        assert ok_result.status == "ready"
        assert ok_result.scope_summary == SUB_SCOPE
        assert len(ok_result.supported_source_months) == 13
        assert ok_result.supported_source_months[0] == "2026-09"
        assert ok_result.supported_source_months[-1] == "2025-09"

        # 2. 403 Forbidden -> actionable minimal role guidance
        forbidden_conn = _make_connection(
            scope="/subscriptions/00000000-0000-0000-0000-000000000403"
        )
        forbidden_result = await connector.check_access(forbidden_conn)
        assert forbidden_result.status == "failed"
        assert "Cost Management Reader" in forbidden_result.message
        assert "EnrollmentReader" in forbidden_result.message
        assert "Billing profile reader" in forbidden_result.message
        assert forbidden_result.details["reason"] == "missingBillingPermission"

        # 3. Management Group scope -> rejected before calling ARM
        mg_conn = _make_connection(
            scope="/providers/Microsoft.Management/managementGroups/mg-enterprise"
        )
        mg_result = await connector.check_access(mg_conn)
        assert mg_result.status == "failed"
        assert mg_result.details["reason"] == "managementGroupUnsupported"
        assert "management group" in mg_result.message.casefold()
        assert "upload" in mg_result.message.casefold()

        # 4. 400 Unsupported PAYG / MSDN subscription -> suggests exportManifest or CSV upload
        payg_conn = _make_connection(
            scope="/subscriptions/00000000-0000-0000-0000-000000000400"
        )
        payg_result = await connector.check_access(payg_conn)
        assert payg_result.status == "failed"
        assert payg_result.details["reason"] == "unsupportedBillingAgreement"
        assert "Pay-As-You-Go" in payg_result.message
        assert "exportManifest" in payg_result.message
        assert "upload" in payg_result.message.casefold()


async def _async_str(value: str) -> str:
    return value


# -- 3. Asynchronous 202 -> Running -> Completed + multi-partition ingestion --------------------


async def test_async_cost_details_polling_multi_partition_download_and_ingestion() -> None:
    op_url = (
        "https://management.azure.com/subscriptions/00000000-0000-0000-0000-000000000001"
        "/providers/Microsoft.CostManagement/costDetailsOperationResults/op-sep-2026"
        "?api-version=2022-05-01"
    )
    blob_1_url = (
        "https://ccmreportstorage.blob.core.windows.net/costdetails/202609/part-00001.csv"
        "?sv=2022-11-02&sr=b&sig=SECRET_SAS_SIGNATURE_ONE&se=2026-10-16T00:00:00Z&sp=r"
    )
    blob_2_url = (
        "https://ccmreportstorage.blob.core.windows.net/costdetails/202609/part-00002.csv.gz"
        "?sv=2022-11-02&sr=b&sig=SECRET_SAS_SIGNATURE_TWO&se=2026-10-16T00:00:00Z&sp=r"
    )

    aoai_res_id = (
        "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-ai"
        "/providers/Microsoft.CognitiveServices/accounts/contoso-aoai"
    )
    csv_header = (
        "Date,MeterCategory,MeterName,Product,Quantity,UnitOfMeasure,EffectivePrice,"
        "CostInBillingCurrency,BillingCurrencyCode,ChargeType,PricingModel,"
        "ResourceId,ResourceLocation\n"
    )
    part_1_csv = (
        csv_header
        + f"2026-09-10,Foundry Models,gpt 4o 1120 Inp glbl Tokens,Azure OpenAI,2000,1K Tokens,"
        f"0.0018,3.60,USD,Usage,OnDemand,{aoai_res_id},eastus2\n"
    ).encode("utf-8")
    part_2_csv_gz = gzip.compress(
        (
            csv_header
            + f"2026-09-11,Foundry Models,gpt 4o 1120 Outp glbl Tokens,Azure OpenAI,500,1K Tokens,"
            f"0.0072,3.60,USD,Usage,OnDemand,{aoai_res_id},eastus2\n"
        ).encode("utf-8")
    )

    poll_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        if (
            request.method == "POST"
            and request.url.path.endswith("/generateCostDetailsReport")
        ):
            assert request.headers.get("Authorization") == "Bearer arm-secret-token"
            body = request.read().decode("utf-8")
            assert '"metric":"AmortizedCost"' in body.replace(" ", "")
            assert '"start":"2026-09-01"' in body.replace(" ", "")
            assert '"end":"2026-09-30"' in body.replace(" ", "")
            return httpx.Response(
                202,
                headers={"Location": op_url, "Retry-After": "15"},
                json={"id": "op-sep-2026", "status": "Running"},
            )

        if request.method == "GET" and "costDetailsOperationResults/op-sep-2026" in str(
            request.url
        ):
            assert request.headers.get("Authorization") == "Bearer arm-secret-token"
            poll_count += 1
            if poll_count == 1:
                return httpx.Response(
                    200,
                    headers={"Retry-After": "20"},
                    json={"id": "op-sep-2026", "name": "op-sep-2026", "status": "Running"},
                )
            return httpx.Response(
                200,
                json={
                    "id": (
                        f"{SUB_SCOPE}/providers/Microsoft.CostManagement"
                        "/costDetailsOperationResults/op-sep-2026"
                    ),
                    "name": "manifest-sep-2026",
                    "status": "Completed",
                    "manifest": {
                        "manifestVersion": "2022-05-01",
                        "dataFormat": "Csv",
                        "byteCount": len(part_1_csv) + len(part_2_csv_gz),
                        "blobCount": 2,
                        "compressData": False,
                        "requestContext": {
                            "requestScope": SUB_SCOPE,
                            "requestBody": {
                                "metric": "AmortizedCost",
                                "timePeriod": {
                                    "start": "2026-09-01",
                                    "end": "2026-09-30",
                                },
                            },
                        },
                        "blobs": [
                            {"blobLink": blob_1_url, "byteCount": len(part_1_csv)},
                            {"blobLink": blob_2_url, "byteCount": len(part_2_csv_gz)},
                        ],
                    },
                },
            )

        if request.method == "GET" and request.url.host == "ccmreportstorage.blob.core.windows.net":
            # Signed SAS blob downloads must not leak the ARM Bearer token
            assert "Authorization" not in request.headers
            if request.url.path.endswith("part-00001.csv"):
                return httpx.Response(200, content=part_1_csv)
            if request.url.path.endswith("part-00002.csv.gz"):
                return httpx.Response(200, content=part_2_csv_gz)

        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        connector = AzureBillingConnector(
            http_client=http_client,
            token_provider=lambda _conn, _sec: _async_str("arm-secret-token"),
            clock=_clock,
        )
        conn = _make_connection(scope=SUB_SCOPE)
        run = _make_run(source_month="2026-09")

        # Step 1: Start report -> 202 Accepted
        step_1 = await connector.start_or_poll_acquisition(conn, run)
        assert step_1.status == "running"
        assert step_1.operation_id == "op-sep-2026"
        assert step_1.operation_location == op_url
        assert step_1.poll_after == datetime(2026, 10, 15, 12, 0, 15, tzinfo=UTC)

        # Step 2: First poll -> Running
        run = run.model_copy(
            update={
                "status": "running",
                "operation_id": step_1.operation_id,
                "operation_location": step_1.operation_location,
            }
        )
        step_2 = await connector.start_or_poll_acquisition(conn, run)
        assert step_2.status == "running"
        assert step_2.poll_after == datetime(2026, 10, 15, 12, 0, 20, tzinfo=UTC)

        # Step 3: Second poll -> Completed with 2 partitions downloaded
        step_3 = await connector.start_or_poll_acquisition(conn, run)
        assert step_3.status == "completed"
        assert step_3.source_scope == SUB_SCOPE
        assert step_3.source_manifest_id == "manifest-sep-2026"
        assert len(step_3.parts) == 2
        assert step_3.parts[0].name == "part-00001.csv"
        assert step_3.parts[1].name == "part-00002.csv.gz"
        assert step_3.total_read_bytes == len(part_1_csv) + len(part_2_csv_gz)

        # Verify no SAS signature or token appears anywhere in the returned model
        dumped_repr = f"{step_3!r} {step_3.model_dump()}"
        assert "SECRET_SAS_SIGNATURE" not in dumped_repr
        assert "sig=" not in dumped_repr
        assert "sv=" not in dumped_repr
        assert "arm-secret-token" not in dumped_repr

        # Step 4: Feed downloaded parts into the shared billing ingestion pipeline end-to-end
        endpoint_repo = InMemoryModelEndpointRepository()
        endpoint = ModelEndpoint(
            id="endpoint-aoai",
            tenant_id=TENANT,
            name="Contoso AOAI",
            provider=ModelProvider.AZURE_OPENAI,
            endpoint="https://contoso-aoai.openai.azure.com",
            azure_resource_id=aoai_res_id,
            account_name="contoso-aoai",
            capabilities=ModelEndpointCapabilities(location="eastus2"),
        )
        obs = ObservedModelDeployment(
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
        )
        await endpoint_repo.save_endpoint(
            endpoint,
            AuditEvent(
                id=new_id("audit"),
                tenant_id=TENANT,
                action="test.seed",
                resource_type="modelEndpoint",
                resource_id=endpoint.id,
                actor_object_id="admin-1",
            ),
        )
        facts = deployment_facts(endpoint, observed=obs)
        facts_map = {facts.key: facts}
        book = build_book([], load_seed())

        ingested = ingest_billing_parts(
            step_3.parts,
            provider="azure",
            pricing_cloud="commercial",
            source_month="2026-09",
            source_scope=step_3.source_scope,
            deployments=facts_map,
            book=book,
            today=_clock().date(),
        )
        assert ingested.raw_row_count == 2
        assert ingested.eligible_row_count == 2
        assert ingested.total_eligible_cost_usd == pytest.approx(7.20)
        assert len(ingested.candidates) == 1
        cand = ingested.candidates[0]
        assert cand.model == "gpt-4o"
        assert cand.input_per_million == pytest.approx(1.80)
        assert cand.output_per_million == pytest.approx(7.20)
        assert cand.publishable is True
        assert cand.suggested_deployment_key == "endpoint-aoai/chat"
        assert cand.mapping_confidence == "exact"


# -- 4. Azure Government routing, host validation, and AzureConnectionService integration -------


async def test_azure_government_routing_and_connection_service_integration() -> None:
    gov_op_url = (
        "https://management.usgovcloudapi.net/providers/Microsoft.Billing"
        "/billingAccounts/gov-ea-1/providers/Microsoft.CostManagement"
        "/costDetailsOperationResults/gov-op-1?api-version=2022-05-01"
    )
    gov_blob_url = (
        "https://govcostreports.blob.core.usgovcloudapi.net/reports/gov-part-1.csv"
        "?sv=2022-11-02&sig=GOV_SAS_SECRET"
    )
    csv_bytes = (
        b"Date,MeterCategory,MeterName,Product,Quantity,UnitOfMeasure,"
        b"CostInBillingCurrency,BillingCurrencyCode\n"
        b"2026-09-15,Foundry Models,gpt 4o 1120 Inp glbl Tokens,Azure OpenAI,"
        b"1000,1K Tokens,2.00,USD\n"
    )

    conn_repo = InMemoryAzureConnectionRepository()
    az_conn = AzureConnection(
        id="azconn-gov-1",
        tenant_id=TENANT,
        name="USGov Management SP",
        azure_cloud="government",
        azure_tenant_id="00000000-0000-0000-0000-000000000099",
        client_id="00000000-0000-0000-0000-000000000088",
        credential_secret_uri="https://gov-kv.vault.usgovcloudapi.net/secrets/sp",
    )
    await conn_repo.create_connection(
        az_conn,
        AuditEvent(
            id=new_id("audit"),
            tenant_id=TENANT,
            action="azureConnection.created",
            resource_type="azureConnection",
            resource_id=az_conn.id,
            actor_object_id="admin-1",
        ),
    )

    issued_scopes: list[tuple[str, ...]] = []

    async def fake_issuer(
        connection: AzureConnection, secret: str, scopes: tuple[str, ...]
    ) -> AccessToken:
        assert connection.azure_cloud == "government"
        assert secret == "vault-resolved-secret"
        issued_scopes.append(scopes)
        return AccessToken("gov-arm-bearer-token", 9999999999)

    async def fake_secret_reader(_uri: str) -> str:
        return "vault-resolved-secret"

    az_service = AzureConnectionService(
        conn_repo,
        tenant_id=TENANT,
        gateways=InMemoryGatewayRepository(),
        endpoints=InMemoryModelEndpointRepository(),
        mcps=InMemoryMcpEndpointRepository(),
        secret_reader=fake_secret_reader,
        secret_store=None,
        token_issuer=fake_issuer,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "management.usgovcloudapi.net":
            assert request.headers.get("Authorization") == "Bearer gov-arm-bearer-token"
            if request.method == "POST":
                return httpx.Response(
                    202,
                    headers={"Azure-AsyncOperation": gov_op_url, "Retry-After": "10"},
                )
            return httpx.Response(
                200,
                json={
                    "name": "gov-manifest-1",
                    "status": "Completed",
                    "manifest": {
                        "manifestVersion": "2022-05-01",
                        "dataFormat": "Csv",
                        "requestContext": {
                            "requestBody": {
                                "metric": "AmortizedCost",
                                "timePeriod": {"start": "2026-09-01", "end": "2026-09-30"},
                            }
                        },
                        "blobs": [{"blobLink": gov_blob_url, "byteCount": len(csv_bytes)}],
                    },
                },
            )
        if request.url.host == "govcostreports.blob.core.usgovcloudapi.net":
            return httpx.Response(200, content=csv_bytes)
        raise AssertionError(f"Unexpected host: {request.url.host}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        connector = AzureBillingConnector(
            http_client=http_client,
            connection_service=az_service,
            clock=_clock,
        )
        gov_conn = _make_connection(
            scope="/providers/Microsoft.Billing/billingAccounts/gov-ea-1",
            azure_cloud="government",
            pricing_cloud="government",
            azure_connection_id="azconn-gov-1",
        )
        gov_run = _make_run(source_month="2026-09", pricing_cloud="government")

        started = await connector.start_or_poll_acquisition(gov_conn, gov_run)
        assert started.status == "running"
        assert started.operation_location == gov_op_url
        assert issued_scopes == [("https://management.usgovcloudapi.net/.default",)]

        polled = await connector.start_or_poll_acquisition(
            gov_conn,
            gov_run.model_copy(update={"operation_location": started.operation_location}),
        )
        assert polled.status == "completed"
        assert len(polled.parts) == 1
        assert polled.parts[0].name == "gov-part-1.csv"
        assert "GOV_SAS_SECRET" not in polled.model_dump_json()

    # Cross-cloud mismatch: Government connection must reject commercial blob and ARM hosts
    with pytest.raises(ValidationError, match=r"different Azure cloud|Azure commercial"):
        validate_azure_blob_url(
            "https://commercialacct.blob.core.windows.net/container/part.csv",
            cloud="government",
            allow_sas_query=True,
        )
    with pytest.raises(ValidationError, match=r"management\.usgovcloudapi\.net"):
        validate_azure_arm_url(
            "https://management.azure.com/subscriptions/sub-1",
            cloud="government",
        )


# -- 5. SSRF protection and SAS token redaction -------------------------------------------------


async def test_ssrf_untrusted_host_rejection_and_complete_sas_redaction() -> None:
    # 1. Direct URL validators block IMDS, loopback, private IPs, HTTP, and untrusted hosts
    for unsafe_url in (
        "http://contoso.blob.core.windows.net/exports/manifest.json",
        "https://169.254.169.254/metadata/identity/oauth2/token",
        "https://localhost/exports/manifest.json",
        "https://127.0.0.1/exports/manifest.json",
        "https://10.0.0.5/exports/manifest.json",
        "https://evil.example.com/exports/manifest.json",
        "https://.blob.core.windows.net/exports/manifest.json",
        "https://sub.account.blob.core.windows.net/exports/manifest.json",
    ):
        with pytest.raises(ValidationError):
            validate_azure_blob_url(unsafe_url, cloud="commercial", allow_sas_query=True)

    # 2. Stored azure_export_manifest_url with SAS query parameters is rejected AND redacted
    sas_manifest_url = (
        "https://contosocost.blob.core.windows.net/exports/manifest.json"
        "?sv=2022-11-02&ss=b&srt=sco&sp=rl&se=2026-12-01T00:00:00Z&sig=TOP_SECRET_CONFIG_SIG"
    )
    with pytest.raises(ValidationError) as exc_info:
        validate_azure_export_manifest_url(sas_manifest_url, cloud="commercial")
    assert "TOP_SECRET_CONFIG_SIG" not in exc_info.value.message
    assert "TOP_SECRET_CONFIG_SIG" not in str(exc_info.value.details)
    assert "sig=" not in exc_info.value.message
    assert "sig=" not in str(exc_info.value.details)

    # 3. check_access on exportManifest with SAS token in config fails cleanly without leaking sig=
    connector = AzureBillingConnector(
        token_provider=lambda _c, _s: _async_str("token-123"),
        clock=_clock,
    )
    bad_conn = _make_connection(
        mode="exportManifest",
        manifest_url=sas_manifest_url,
    )
    check_res = await connector.check_access(bad_conn)
    assert check_res.status == "failed"
    assert "TOP_SECRET_CONFIG_SIG" not in check_res.model_dump_json()
    assert "sig=" not in check_res.model_dump_json()

    # 4. Manifest returning an untrusted blobLink or failing blob download never leaks SAS tokens
    op_url = (
        f"https://management.azure.com{SUB_SCOPE}/providers/Microsoft.CostManagement"
        "/costDetailsOperationResults/op-ssrf?api-version=2022-05-01"
    )
    failing_sas_blob = (
        "https://ccmreportstorage.blob.core.windows.net/costdetails/part-fail.csv"
        "?sv=2022-11-02&sr=b&sig=LEAK_TEST_SIGNATURE_999"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "costDetailsOperationResults/op-ssrf" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "name": "op-ssrf?sig=MANIFEST_NAME_SIG",
                    "status": "Completed",
                    "manifest": {
                        "manifestVersion": "2022-05-01",
                        "dataFormat": "Csv",
                        "requestContext": {"requestBody": {"metric": "AmortizedCost"}},
                        "blobs": [{"blobLink": failing_sas_blob}],
                    },
                },
            )
        if request.url.host == "ccmreportstorage.blob.core.windows.net":
            return httpx.Response(
                500,
                text=f"Storage failure for {failing_sas_blob}",
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        err_connector = AzureBillingConnector(
            http_client=http_client,
            token_provider=lambda _c, _s: _async_str("token-123"),
            clock=_clock,
        )
        run = _make_run(operation_location=op_url)
        with pytest.raises(UpstreamError) as dl_exc:
            await err_connector.start_or_poll_acquisition(_make_connection(), run)

        assert "LEAK_TEST_SIGNATURE_999" not in dl_exc.value.message
        assert "LEAK_TEST_SIGNATURE_999" not in str(dl_exc.value.details)
        assert "sig=" not in dl_exc.value.message
        assert "sig=" not in str(dl_exc.value.details)

    assert redact_azure_url(failing_sas_blob) == (
        "https://ccmreportstorage.blob.core.windows.net/costdetails/part-fail.csv"
    )
    assert "LEAK_TEST_SIGNATURE_999" not in redact_sensitive_text(
        f"Failed at {failing_sas_blob} with Bearer my-secret-jwt.token.here"
    )


# -- 6. >13 month lookback rejection & ActualCost manifest rejection ----------------------------


async def test_lookback_limit_and_actual_cost_manifest_rejection() -> None:
    manifest_url = "https://contosocost.blob.core.windows.net/exports/202609/manifest.json"
    blob_url = "https://contosocost.blob.core.windows.net/exports/202609/part-0.csv"
    csv_bytes = (
        b"Date,MeterCategory,MeterName,Product,Quantity,UnitOfMeasure,"
        b"CostInBillingCurrency,BillingCurrencyCode\n"
        b"2025-07-15,Foundry Models,gpt 4o 1120 Inp glbl Tokens,Azure OpenAI,"
        b"1000,1K Tokens,2.00,USD\n"
    )

    metric_to_return = "ActualCost"
    period_start = "2026-09-01"
    period_end = "2026-09-30"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == manifest_url:
            return httpx.Response(
                200,
                json={
                    "manifestVersion": "2022-05-01",
                    "dataFormat": "Csv",
                    "requestContext": {
                        "requestBody": {
                            "metric": metric_to_return,
                            "timePeriod": {"start": period_start, "end": period_end},
                        }
                    },
                    "blobs": [{"blobLink": blob_url}],
                },
            )
        if str(request.url) == blob_url:
            return httpx.Response(200, content=csv_bytes)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        connector = AzureBillingConnector(
            http_client=http_client,
            token_provider=lambda _c, _s: _async_str("token-123"),
            clock=_clock,
        )

        # 1. >13 months old (2025-08 is 14 months before 2026-10) is rejected on costDetailsApi
        old_run = _make_run(source_month="2025-08")
        with pytest.raises(ValidationError) as lookback_exc:
            await connector.start_or_poll_acquisition(_make_connection(), old_run)
        assert lookback_exc.value.details.get("reason") == "lookbackExceedsCostDetailsLimit"
        assert "13 months" in lookback_exc.value.message
        assert "exportManifest" in lookback_exc.value.message

        # 2. ActualCost manifest is rejected on both check_access and start_or_poll_acquisition
        manifest_conn = _make_connection(
            mode="exportManifest",
            manifest_url=manifest_url,
        )
        check_actual = await connector.check_access(manifest_conn)
        assert check_actual.status == "failed"
        assert check_actual.details.get("reason") == "actualCostNotSupported"
        assert "AmortizedCost" in check_actual.message
        assert "ActualCost" in check_actual.message

        with pytest.raises(ValidationError) as actual_exc:
            await connector.start_or_poll_acquisition(
                manifest_conn, _make_run(source_month="2026-09")
            )
        assert actual_exc.value.details.get("reason") == "actualCostNotSupported"
        assert "AmortizedCost" in actual_exc.value.message

        # 3. Switching the export manifest to AmortizedCost succeeds, even for >13-month lookback
        metric_to_return = "AmortizedCost"
        period_start = "2025-07-01"
        period_end = "2025-07-31"
        check_amortized = await connector.check_access(manifest_conn)
        assert check_amortized.status == "ready"

        older_manifest_step = await connector.start_or_poll_acquisition(
            manifest_conn, _make_run(source_month="2025-07")
        )
        assert older_manifest_step.status == "completed"
        assert len(older_manifest_step.parts) == 1
        assert older_manifest_step.parts[0].name == "part-0.csv"


# -- 7. NoDataFound, Failed status, and payload byte/partition bounds ---------------------------


async def test_no_data_found_failed_operation_and_size_bounds() -> None:
    op_nodata = (
        f"https://management.azure.com{EA_SCOPE}/providers/Microsoft.CostManagement"
        "/costDetailsOperationResults/op-nodata?api-version=2022-05-01"
    )
    op_failed = (
        f"https://management.azure.com{EA_SCOPE}/providers/Microsoft.CostManagement"
        "/costDetailsOperationResults/op-failed?api-version=2022-05-01"
    )
    op_big = (
        f"https://management.azure.com{EA_SCOPE}/providers/Microsoft.CostManagement"
        "/costDetailsOperationResults/op-big?api-version=2022-05-01"
    )
    blob_url = "https://ccmreportstorage.blob.core.windows.net/costdetails/big.csv"

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "op-nodata" in url_str:
            return httpx.Response(
                200,
                json={"name": "op-nodata", "status": "NoDataFound"},
            )
        if "op-failed" in url_str:
            return httpx.Response(
                200,
                json={
                    "name": "op-failed",
                    "status": "Failed",
                    "error": {
                        "code": "ReportTimeout",
                        "message": (
                            "Failed generating https://ccm.blob.core.windows.net/x?sig=HIDDEN_SIG"
                        ),
                    },
                },
            )
        if "op-big" in url_str:
            return httpx.Response(
                200,
                json={
                    "name": "op-big",
                    "status": "Completed",
                    "manifest": {
                        "manifestVersion": "2022-05-01",
                        "dataFormat": "Csv",
                        "requestContext": {"requestBody": {"metric": "AmortizedCost"}},
                        "blobs": [{"blobLink": blob_url}],
                    },
                },
            )
        if url_str == blob_url:
            return httpx.Response(200, content=b"x" * 2048)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        connector = AzureBillingConnector(
            http_client=http_client,
            token_provider=lambda _c, _s: _async_str("token-123"),
            clock=_clock,
            max_total_bytes=1024,
        )
        conn = _make_connection(scope=EA_SCOPE)

        # NoDataFound returns completed with empty parts and warning
        nodata_step = await connector.start_or_poll_acquisition(
            conn, _make_run(operation_location=op_nodata)
        )
        assert nodata_step.status == "completed"
        assert nodata_step.parts == []
        assert len(nodata_step.warnings) == 1
        assert "NoDataFound" in nodata_step.warnings[0]

        # Failed raises UpstreamError with SAS query parameters redacted
        with pytest.raises(UpstreamError) as fail_exc:
            await connector.start_or_poll_acquisition(
                conn, _make_run(operation_location=op_failed)
            )
        assert "HIDDEN_SIG" not in fail_exc.value.message
        assert "ReportTimeout" in fail_exc.value.message

        # Exceeding max_total_bytes raises BillingPayloadTooLargeError
        with pytest.raises(BillingPayloadTooLargeError):
            await connector.start_or_poll_acquisition(
                conn, _make_run(operation_location=op_big)
            )


async def test_acquisition_permission_and_throttling_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "00000000-0000-0000-0000-000000000403" in request.url.path:
            return httpx.Response(
                403,
                json={"error": {"code": "AuthorizationFailed", "message": "Denied"}},
            )
        return httpx.Response(
            429,
            headers={"Retry-After": "45"},
            json={"error": {"code": "TooManyRequests", "message": "Throttled"}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        connector = AzureBillingConnector(
            http_client=http_client,
            token_provider=lambda _c, _s: _async_str("token-123"),
            clock=_clock,
        )
        forbidden_conn = _make_connection(
            scope="/subscriptions/00000000-0000-0000-0000-000000000403"
        )
        with pytest.raises(UpstreamAuthorizationError) as auth_exc:
            await connector.start_or_poll_acquisition(forbidden_conn, _make_run())
        assert "Cost Management Reader" in auth_exc.value.message

        throttled_conn = _make_connection(scope=SUB_SCOPE)
        with pytest.raises(UpstreamError) as throttle_exc:
            await connector.start_or_poll_acquisition(throttled_conn, _make_run())
        assert throttle_exc.value.details.get("retryAfter") == 45

