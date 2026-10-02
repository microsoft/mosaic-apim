import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from apim_double import RESOURCE_ID
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError
from loganalytics_double import served_gate_span, served_only, token_gate_span
from mosaic_api.errors import UpstreamNotFoundError, ValidationError
from mosaic_api.integrations.loganalytics import (
    LogAnalyticsClient,
    LogQueryAccessError,
    LogQueryError,
    LogQueryTooLargeError,
    QueryWindow,
    calls_query,
    denials_query,
    deployment_peaks_query,
    parse_tables,
    peaks_query,
    probe_query,
    query_scope,
)

START = datetime(2026, 9, 29, 10, tzinfo=UTC)
END = datetime(2026, 9, 29, 12, tzinfo=UTC)
WINDOW = QueryWindow(START, END)


class FakeCredential:
    def __init__(self, token: str = "log-token") -> None:
        self.token = token
        self.scopes: list[tuple[str, ...]] = []

    async def get_token(self, *scopes: str, **_kwargs: Any) -> AccessToken:
        self.scopes.append(scopes)
        return AccessToken(self.token, 9_999_999_999)


class FailingCredential:
    async def get_token(self, *_scopes: str, **_kwargs: Any) -> AccessToken:
        raise ClientAuthenticationError("tenant disabled")


async def _no_sleep(_seconds: float) -> None:
    return None


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    credential: FakeCredential | FailingCredential | None = None,
    endpoint: str = "https://api.loganalytics.azure.com",
    sleep: Callable[[float], Awaitable[None]] = _no_sleep,
) -> LogAnalyticsClient:
    return LogAnalyticsClient(
        credential or FakeCredential(),
        endpoint=endpoint,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=sleep,
    )


def _table(
    columns: list[dict[str, str]] | None = None,
    rows: list[list[object]] | None = None,
) -> dict[str, object]:
    return {
        "tables": [
            {
                "columns": columns
                or [
                    {"name": "TimeGenerated", "type": "datetime"},
                    {"name": "requests", "type": "long"},
                    {"name": "deployment", "type": "string"},
                ],
                "rows": rows
                or [["2026-09-29T10:15:00.1234567Z", 3, "gpt-4o"]],
            }
        ]
    }


def test_query_window_accepts_timezone_aware_utc_hours_and_adds_llm_margin() -> None:
    assert WINDOW.hours == 2
    assert WINDOW.timespan == (START - timedelta(hours=1), END + timedelta(hours=1))

    query = calls_query(WINDOW, ["chat-api"])

    assert "let startTime = datetime(2026-09-29T10:00:00Z);" in query
    assert "let endTime = datetime(2026-09-29T12:00:00Z);" in query
    assert "ApiManagementGatewayLlmLog" in query
    assert "where TimeGenerated >= datetime(2026-09-29T09:00:00Z)" in query
    assert "TimeGenerated < datetime(2026-09-29T13:00:00Z)" in query


def test_query_window_rejects_naive_times() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        QueryWindow(datetime(2026, 9, 29, 10), END)


def test_query_window_rejects_times_that_are_not_whole_utc_hours() -> None:
    local_half_hour = datetime(2026, 9, 29, 10, tzinfo=timezone(timedelta(minutes=30)))

    with pytest.raises(ValueError, match="on the hour"):
        QueryWindow(local_half_hour, END)


def test_query_window_rejects_sub_hour_boundaries() -> None:
    with pytest.raises(ValueError, match="on the hour"):
        QueryWindow(START.replace(minute=1), END)


def test_query_window_rejects_empty_or_reversed_ranges() -> None:
    with pytest.raises(ValueError, match="end after"):
        QueryWindow(END, START)


def test_query_window_splits_into_equal_half_open_ranges() -> None:
    first, second = QueryWindow(START, START + timedelta(hours=4)).split()

    assert first == QueryWindow(START, START + timedelta(hours=2))
    assert second == QueryWindow(START + timedelta(hours=2), START + timedelta(hours=4))


def test_calls_query_contains_gateway_filters_trace_extraction_and_summaries() -> None:
    query = calls_query(WINDOW, ["Chat-API"])

    assert "ApiManagementGatewayLogs" in query
    assert "ApiManagementGatewayLlmLog" in query
    assert "let mosaicApis = dynamic([\"chat-api\"]);" in query
    assert 'extract(@"mosaic-attribution ([^""\\\\]*)"' in query
    assert "ResponseCode == 429, \"throttled\"" in query
    assert "countif(BackendResponseCode == 429)" in query
    assert "by hour = hourofday(TimeGenerated), v, g, m, a, api, subscription" in query


def test_calls_query_orders_and_deduplicates_api_names_deterministically() -> None:
    first = calls_query(WINDOW, ["Orders", "chat-api", "orders"])
    second = calls_query(WINDOW, ["orders", "Orders", "chat-api"])

    assert first == second
    assert "let mosaicApis = dynamic([\"chat-api\",\"orders\"]);" in first


@pytest.mark.parametrize(
    "name",
    [
        "",
        " bad",
        "bad'name",
        'bad"name',
        "bad\\name",
        "bad\nname",
        "bad|where true",
        "bad;drop",
        "bad//comment",
        "\N{CYRILLIC SMALL LETTER A}pi",
        "a" * 257,
    ],
)
def test_api_names_are_validated_before_reaching_kql(name: str) -> None:
    with pytest.raises(ValidationError, match="API name"):
        calls_query(WINDOW, [name])


def test_peaks_query_groups_by_trace_or_subscription_link() -> None:
    query = peaks_query(WINDOW, ["chat-api"])

    assert 'strcat("t:", g, "|", m)' in query
    assert 'strcat("s:", subscription)' in query
    assert "by link, minute = bin(TimeGenerated, 1m)" in query
    assert "by link, hour = hourofday(minute)" in query


def test_denials_query_groups_by_reason_caller_client_and_api() -> None:
    query = denials_query(WINDOW, ["chat-api"])

    assert "| where isnotempty(reason)" in query
    assert 'o = tolower(extract(@"(?:^| )o=([^ ]*)", 1, denial))' in query
    assert "by hour = hourofday(TimeGenerated), reason, o, a, api, v" in query


def test_deployment_peaks_query_sorts_mapping_and_keeps_keys_in_dynamic_literal() -> None:
    query = deployment_peaks_query(WINDOW, {"Orders": "aoai:/east/gpt-4o", "chat-api": "west"})

    assert 'let deploymentOf = dynamic({"chat-api":"west","orders":"aoai:/east/gpt-4o"});' in query
    assert "deploymentKey = tostring(deploymentOf[api])" in query
    assert "countif(BackendResponseCode > 0)" in query
    assert query == deployment_peaks_query(
        WINDOW, {"chat-api": "west", "Orders": "aoai:/east/gpt-4o"}
    )


# What every query that sums tokens has to do with a call's LLM log row before it sums anything.
GATED = ("promptTokens", "completionTokens", "totalTokens")
TOKEN_QUERIES = {
    "calls": (calls_query(WINDOW, ["chat-api"]), "| summarize requests = count()"),
    "peaks": (peaks_query(WINDOW, ["chat-api"]), "| summarize tokens = sum(totalTokens)"),
    "deploymentPeaks": (
        deployment_peaks_query(WINDOW, {"chat-api": "west"}),
        "| summarize tokens = sum(totalTokens)",
    ),
}


@pytest.mark.parametrize("kind", sorted(TOKEN_QUERIES))
def test_queries_read_tokens_only_for_calls_the_model_deployment_served(kind: str) -> None:
    query, summary = TOKEN_QUERIES[kind]

    joined = query.index("| join kind=leftouter llmRows on CorrelationId\n")
    served = served_gate_span(query)
    assert served is not None
    summary_start = query.index(summary)
    assert joined < served[0] < served[1] < summary_start
    for column in GATED:
        gated = token_gate_span(query, column)
        assert gated is not None
        assert served[0] < gated[0] < gated[1] < summary_start
    # The double that answers these queries in tests reads the same rule from them.
    assert served_only(query) == {"promptTokens", "completionTokens", "totalTokens"}


def test_served_only_is_tolerant_of_equivalent_whitespace() -> None:
    query = """
ApiManagementGatewayLogs
| join kind=leftouter llmRows on CorrelationId
| extend served  =  isnotnull( BackendResponseCode )
    and   BackendResponseCode   between ( 200..299 )
| extend promptTokens=iff( served,promptTokens,long( null ) ),
    completionTokens = iff(
        served,
        completionTokens,
        long(null)
    ),
    totalTokens = iff(served, totalTokens, long(null))
| summarize tokens = sum(totalTokens)
"""

    assert served_only(query) == {"promptTokens", "completionTokens", "totalTokens"}


@pytest.mark.parametrize(
    ("query", "gated"),
    [
        (
            """
| extend promptTokens = iff(served, promptTokens, long(null)),
    completionTokens = iff(served, completionTokens, long(null)),
    totalTokens = iff(served, totalTokens, long(null))
""",
            frozenset(),
        ),
        (
            """
| extend served = isnotnull(BackendResponseCode)
    and BackendResponseCode between (200 .. 499)
| extend promptTokens = iff(served, promptTokens, long(null)),
    completionTokens = iff(served, completionTokens, long(null)),
    totalTokens = iff(served, totalTokens, long(null))
""",
            frozenset(),
        ),
        (
            """
| extend served = isnotnull(BackendResponseCode)
    and BackendResponseCode between (200 .. 299)
| extend promptTokens = iff(served, promptTokens, long(null)),
    completionTokens = completionTokens,
    totalTokens = iff(served, totalTokens, long(null))
""",
            frozenset({"promptTokens", "totalTokens"}),
        ),
    ],
)
def test_served_only_rejects_missing_or_changed_token_gates(
    query: str, gated: frozenset[str]
) -> None:
    assert served_only(query) == gated


def test_calls_query_still_counts_every_admitted_call_and_its_status() -> None:
    query = calls_query(WINDOW, ["chat-api"])
    summary = query[query.index("| summarize requests = count()") :]

    # Requests and statuses come from the gateway log alone, whatever the model did.
    for count in (
        'throttled = countif(status == "throttled")',
        'quota = countif(status == "quota")',
        "backendThrottled = countif(BackendResponseCode == 429)",
        # A call counts as metered only when its tokens are read, so only when it was served.
        "metered = countif(isnotnull(totalTokens))",
    ):
        assert count in summary
    assert "served" not in summary


def test_queries_that_sum_no_tokens_read_no_llm_log() -> None:
    for query in (denials_query(WINDOW, ["chat-api"]), probe_query()):
        assert served_only(query) == frozenset()
        assert "llmRows" not in query


@pytest.mark.parametrize(
    "key",
    [
        "",
        "bad key",
        "bad'key",
        'bad"key',
        "bad\\key",
        "bad\nkey",
        "bad|key",
        "bad;key",
        "a" * 301,
    ],
)
def test_deployment_keys_are_validated_before_reaching_kql(key: str) -> None:
    with pytest.raises(ValidationError, match="deployment key"):
        deployment_peaks_query(WINDOW, {"chat-api": key})


def test_probe_query_is_read_only_and_checks_recent_gateway_and_llm_logs() -> None:
    query = probe_query()

    assert "TimeGenerated > ago(24h)" in query
    assert 'source = "gateway"' in query
    assert 'source = "llm"' in query
    assert 'tostring(TraceRecords) contains "mosaic-attribution"' in query
    assert "summarize rows = count(), traced = countif(traced)" in query


def test_query_scope_uses_public_cloud_audience_for_public_hosts() -> None:
    assert query_scope("https://api.loganalytics.azure.com") == "https://api.loganalytics.io/.default"
    assert query_scope("https://api.loganalytics.io") == "https://api.loganalytics.io/.default"


def test_query_scope_uses_endpoint_host_for_sovereign_clouds() -> None:
    assert query_scope("https://api.loganalytics.us") == "https://api.loganalytics.us/.default"


async def test_client_posts_resource_centric_query_with_token_headers_and_timespan() -> None:
    seen: list[httpx.Request] = []
    credential = FakeCredential()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_table())

    result = await _client(handler, credential=credential).query(
        RESOURCE_ID,
        "Heartbeat | count",
        start=START,
        end=END,
    )

    assert credential.scopes == [("https://api.loganalytics.io/.default",)]
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert request.url == f"https://api.loganalytics.azure.com/v1{RESOURCE_ID}/query"
    assert request.headers["Authorization"] == "Bearer log-token"
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["Prefer"] == "wait=180"
    assert request.read()
    assert result == [
        {
            "TimeGenerated": datetime(2026, 9, 29, 10, 15, 0, 123456, tzinfo=UTC),
            "requests": 3,
            "deployment": "gpt-4o",
        }
    ]


async def test_client_body_contains_query_and_iso8601_timespan() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"tables": []})

    await _client(handler).query(RESOURCE_ID, "Gateway | count", start=START, end=END)

    assert bodies == [
        {
            "query": "Gateway | count",
            "timespan": "2026-09-29T10:00:00Z/2026-09-29T12:00:00Z",
        }
    ]


def test_parse_tables_returns_empty_rows_for_missing_or_empty_tables() -> None:
    assert parse_tables({}) == []
    assert parse_tables({"tables": []}) == []


def test_parse_tables_preserves_null_cells() -> None:
    assert parse_tables(_table(rows=[[None, 0, None]])) == [
        {"TimeGenerated": None, "requests": 0, "deployment": None}
    ]


def test_parse_tables_reads_the_primary_table_when_multiple_tables_are_returned() -> None:
    payload = _table(rows=[["2026-09-29T10:00:00Z", 1, "first"]])
    payload["tables"].append(
        {
            "columns": [{"name": "ignored", "type": "string"}],
            "rows": [["second"]],
        }
    )

    assert parse_tables(payload) == [
        {
            "TimeGenerated": datetime(2026, 9, 29, 10, tzinfo=UTC),
            "requests": 1,
            "deployment": "first",
        }
    ]


@pytest.mark.parametrize(
    "payload", [[], {"tables": [{}]}, {"tables": [{"columns": [], "rows": [1]}]}]
)
def test_parse_tables_rejects_unreadable_payloads(payload: object) -> None:
    with pytest.raises(LogQueryError, match="could not read"):
        parse_tables(payload)


async def test_client_rejects_non_apim_resource_ids_before_sending() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("request should not be sent")

    with pytest.raises(ValidationError, match="full Azure resource ID"):
        await _client(handler).query("/not/a/resource", "query", start=START, end=END)


async def test_client_retries_429_with_retry_after_then_succeeds() -> None:
    attempts = 0
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json=_table(rows=[["2026-09-29T10:00:00Z", attempts, "ok"]]))

    rows = await _client(handler, sleep=sleep).query(RESOURCE_ID, "query", start=START, end=END)

    assert sleeps == [7.0]
    assert attempts == 2
    assert rows[0]["requests"] == 2


async def test_client_retries_5xx_with_exponential_backoff_then_succeeds() -> None:
    attempts = 0
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"tables": []})

    assert (
        await _client(handler, sleep=sleep).query(RESOURCE_ID, "query", start=START, end=END)
        == []
    )
    assert attempts == 3
    assert sleeps == [1.0, 2.0]


async def test_client_caps_retry_after_and_reports_exhausted_retries() -> None:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, headers={"Retry-After": "999"})

    with pytest.raises(LogQueryError, match="stayed unavailable") as error:
        await _client(handler, sleep=sleep).query(RESOURCE_ID, "query", start=START, end=END)

    assert sleeps == [30.0, 30.0, 30.0]
    assert error.value.details == {"statusCode": 503}


@pytest.mark.parametrize("status_code", [401, 403])
async def test_client_maps_authorization_failures_to_access_errors(status_code: int) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code)

    with pytest.raises(LogQueryAccessError, match="Monitoring Reader") as error:
        await _client(handler).query(RESOURCE_ID, "query", start=START, end=END)

    assert error.value.status_code == 403
    assert error.value.details == {"statusCode": status_code}


async def test_client_maps_not_found_to_upstream_not_found() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with pytest.raises(UpstreamNotFoundError, match="could not find"):
        await _client(handler).query(RESOURCE_ID, "query", start=START, end=END)


async def test_client_maps_gateway_timeout_to_too_large() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(504)

    with pytest.raises(LogQueryTooLargeError, match="query less time"):
        await _client(handler).query(RESOURCE_ID, "query", start=START, end=END)


async def test_client_summarizes_bad_query_errors_from_service_body() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "outer",
                    "details": [{"innererror": {"message": "Bad KQL near summarize"}}],
                }
            },
        )

    with pytest.raises(LogQueryError, match="rejected") as error:
        await _client(handler).query(RESOURCE_ID, "query", start=START, end=END)

    assert error.value.details == {"statusCode": 400, "reason": "Bad KQL near summarize"}


async def test_client_maps_partial_result_too_large_markers_to_too_large_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "tables": [],
                "error": {"message": "E_QUERY_RESULT_SET_TOO_LARGE: result set has exceeded"},
            },
        )

    with pytest.raises(LogQueryTooLargeError, match="too much data") as error:
        await _client(handler).query(RESOURCE_ID, "query", start=START, end=END)

    assert error.value.details == {
        "reason": "E_QUERY_RESULT_SET_TOO_LARGE: result set has exceeded"
    }


async def test_client_maps_partial_errors_without_narrow_markers_to_query_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tables": [], "error": {"message": "one shard failed"}})

    with pytest.raises(LogQueryError, match="only part") as error:
        await _client(handler).query(RESOURCE_ID, "query", start=START, end=END)

    assert error.value.details == {"reason": "one shard failed"}


async def test_client_accepts_fuzzy_union_resolution_warnings() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                **_table(rows=[["2026-09-29T10:00:00Z", 1, "ok"]]),
                "error": {
                    "details": [
                        {"message": "Failed to resolve table ApiManagementGatewayLlmLog"},
                        {"message": "Could not be resolved in this workspace"},
                    ]
                },
            },
        )

    rows = await _client(handler).query(RESOURCE_ID, "query", start=START, end=END)

    assert rows[0]["deployment"] == "ok"


async def test_client_maps_malformed_success_json_to_query_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    with pytest.raises(LogQueryError, match="could not read"):
        await _client(handler).query(RESOURCE_ID, "query", start=START, end=END)


async def test_client_maps_timeouts_to_too_large_errors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    with pytest.raises(LogQueryTooLargeError, match="did not answer in time"):
        await _client(handler).query(RESOURCE_ID, "query", start=START, end=END)


async def test_client_retries_network_errors_then_reports_unreachable() -> None:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    with pytest.raises(LogQueryError, match="could not reach"):
        await _client(handler, sleep=sleep).query(RESOURCE_ID, "query", start=START, end=END)

    assert sleeps == [1.0, 2.0, 4.0]


async def test_client_maps_token_acquisition_failures_to_access_errors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("request should not be sent")

    with pytest.raises(LogQueryAccessError, match="could not acquire") as error:
        await _client(handler, credential=FailingCredential()).query(
            RESOURCE_ID, "query", start=START, end=END
        )

    assert error.value.details == {"reason": "tenant disabled"}
