"""Log Analytics queries over one Azure resource's logs.

MOSAIC queries the resource-centric endpoint, ``/v1{resourceId}/query``. A gateway's diagnostic
setting decides which workspace its logs land in, so MOSAIC needs to read that API Management
instance's logs, which Monitoring Reader on the instance grants, and never a whole workspace.
"""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
import structlog
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import ClientAuthenticationError

from mosaic_api.errors import (
    UpstreamAuthorizationError,
    UpstreamError,
    UpstreamNotFoundError,
    ValidationError,
)

logger = structlog.get_logger()

Row = dict[str, Any]
Sleep = Callable[[float], Awaitable[None]]

MAX_ATTEMPTS = 4
MAX_RETRY_DELAY_SECONDS = 30.0
# Log Analytics runs five concurrent queries per caller and queues the rest. MOSAIC keeps one slot
# free for an administrator's readiness probe.
MAX_CONCURRENT_QUERIES = 4
# How long Log Analytics may spend on one query before answering. Its default is three minutes and
# its ceiling ten; MOSAIC asks for the default explicitly and waits a little longer for the reply.
QUERY_WAIT_SECONDS = 180
_READ_TIMEOUT_SECONDS = QUERY_WAIT_SECONDS + 30

_RESOURCE_ID = re.compile(
    r"^/subscriptions/[0-9A-Fa-f-]{36}/resourceGroups/[-\w.()]{1,90}"
    r"/providers/[A-Za-z0-9.]{1,100}/[A-Za-z0-9]{1,100}/[-\w.]{1,260}$"
)
# A partial result is never rolled up: the day is retried whole. Narrowing the time range is what
# resolves these, so they say so.
_NARROWABLE_MARKERS: tuple[str, ...] = (
    "e_query_result_set_too_large",
    "result set has exceeded",
    "e_low_memory_condition",
    "e_runaway_query",
    "timed out",
    "timeout",
)
# A fuzzy union reports a leg it couldn't resolve, such as a table that has had no rows yet, as a
# warning. The other legs are complete, so the result stands.
_RESOLUTION_MARKERS: tuple[str, ...] = ("failed to resolve", "could not be resolved")


class LogQueryError(UpstreamError):
    """Log Analytics refused the query or returned only part of its result."""

    code = "telemetry_query_failed"


class LogQueryTooLargeError(LogQueryError):
    """The result outgrew Log Analytics' limits; the same query over less time will fit."""

    code = "telemetry_query_too_large"


class LogQueryAccessError(UpstreamAuthorizationError):
    """MOSAIC's identity may not read this resource's logs."""

    code = "telemetry_forbidden"


class LogsQuery(Protocol):
    async def query(
        self, resource_id: str, query: str, *, start: datetime, end: datetime
    ) -> list[Row]: ...


def query_scope(endpoint: str) -> str:
    """The token audience for a Log Analytics query endpoint.

    Azure Commercial accepts the ``api.loganalytics.io`` audience on both of its hosts. Sovereign
    clouds use their own host as the audience.
    """

    host = (urlsplit(endpoint).hostname or "").casefold()
    if host in {"api.loganalytics.azure.com", "api.loganalytics.io"}:
        return "https://api.loganalytics.io/.default"
    return f"https://{host}/.default"


def _timespan(start: datetime, end: datetime) -> str:
    return (
        f"{start.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}/"
        f"{end.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    # Log Analytics writes seven fractional digits; Python reads at most six.
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_tables(payload: object) -> list[Row]:
    """The primary result table as dictionaries, with datetime columns parsed."""

    if not isinstance(payload, dict):
        raise LogQueryError("Log Analytics returned a response MOSAIC could not read")
    tables = payload.get("tables")
    if not isinstance(tables, list) or not tables:
        return []
    table = tables[0]
    if not isinstance(table, dict):
        raise LogQueryError("Log Analytics returned a response MOSAIC could not read")
    columns = table.get("columns")
    rows = table.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise LogQueryError("Log Analytics returned a response MOSAIC could not read")
    names = [str(column.get("name")) for column in columns if isinstance(column, dict)]
    datetimes = {
        str(column.get("name"))
        for column in columns
        if isinstance(column, dict) and column.get("type") == "datetime"
    }
    parsed: list[Row] = []
    for row in rows:
        if not isinstance(row, list) or len(row) != len(names):
            raise LogQueryError("Log Analytics returned a row MOSAIC could not read")
        item = dict(zip(names, row, strict=True))
        for name in datetimes:
            item[name] = _parse_datetime(item[name])
        parsed.append(item)
    return parsed


def _error_text(error: object) -> str:
    return json.dumps(error, sort_keys=True, default=str).casefold()


def _error_message(error: object) -> str:
    if isinstance(error, dict):
        details = error.get("details")
        if isinstance(details, list):
            for detail in details:
                if not isinstance(detail, dict):
                    continue
                inner = detail.get("innererror")
                if isinstance(inner, dict) and isinstance(inner.get("message"), str):
                    return str(inner["message"])[:300]
                if isinstance(detail.get("message"), str):
                    return str(detail["message"])[:300]
        if isinstance(error.get("message"), str):
            return str(error["message"])[:300]
    return "Log Analytics did not complete the query"


def _partial_failure(error: object) -> LogQueryError | None:
    """What a partial result means: narrow the range, fail, or, for a fuzzy leg, nothing."""

    text = _error_text(error)
    if any(marker in text for marker in _NARROWABLE_MARKERS):
        return LogQueryTooLargeError(
            "The telemetry query returned too much data; MOSAIC will query less time at once",
            details={"reason": _error_message(error)},
        )
    details = error.get("details") if isinstance(error, dict) else None
    if (
        isinstance(details, list)
        and details
        and all(
            any(marker in _error_text(detail) for marker in _RESOLUTION_MARKERS)
            for detail in details
        )
    ):
        return None
    return LogQueryError(
        "Log Analytics returned only part of the telemetry query's result",
        details={"reason": _error_message(error)},
    )


class LogAnalyticsClient:
    def __init__(
        self,
        credential: AsyncTokenCredential,
        *,
        endpoint: str,
        client: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._credential = credential
        self._endpoint = str(endpoint).rstrip("/")
        self._scope = query_scope(self._endpoint)
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(_READ_TIMEOUT_SECONDS, connect=10.0), follow_redirects=False
        )
        self._owns_client = client is None
        self._sleep = sleep
        self._slots = asyncio.Semaphore(MAX_CONCURRENT_QUERIES)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _authorization(self) -> str:
        try:
            token = await self._credential.get_token(self._scope)
        except ClientAuthenticationError as error:
            raise LogQueryAccessError(
                "MOSAIC could not acquire a Log Analytics token",
                details={"reason": str(error)[:300]},
            ) from error
        return "Bearer " + token.token

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        header = response.headers.get("Retry-After")
        if header:
            try:
                return min(max(float(header), 0.0), MAX_RETRY_DELAY_SECONDS)
            except ValueError:
                pass
        return min(2.0**attempt, MAX_RETRY_DELAY_SECONDS)

    async def query(
        self, resource_id: str, query: str, *, start: datetime, end: datetime
    ) -> list[Row]:
        if not _RESOURCE_ID.fullmatch(resource_id):
            raise ValidationError("Telemetry queries need a full Azure resource ID")
        url = f"{self._endpoint}/v1{resource_id}/query"
        body = {"query": query, "timespan": _timespan(start, end)}
        async with self._slots:
            return await self._send(url, body)

    async def _send(self, url: str, body: dict[str, str]) -> list[Row]:
        last_status: int | None = None
        for attempt in range(MAX_ATTEMPTS):
            headers = {
                "Authorization": await self._authorization(),
                "Content-Type": "application/json",
                "Prefer": f"wait={QUERY_WAIT_SECONDS}",
            }
            try:
                response = await self._client.post(url, json=body, headers=headers)
            except httpx.TimeoutException as error:
                raise LogQueryTooLargeError(
                    "Log Analytics did not answer in time; MOSAIC will query less time at once"
                ) from error
            except httpx.HTTPError as error:
                if attempt == MAX_ATTEMPTS - 1:
                    raise LogQueryError("MOSAIC could not reach Log Analytics") from error
                await self._sleep(min(2.0**attempt, MAX_RETRY_DELAY_SECONDS))
                continue
            last_status = response.status_code
            if response.status_code in {401, 403}:
                raise LogQueryAccessError(
                    "MOSAIC's identity needs Monitoring Reader on this gateway to read its logs",
                    details={"statusCode": response.status_code},
                )
            if response.status_code == 404:
                raise UpstreamNotFoundError(
                    "Log Analytics could not find the gateway's resource",
                    details={"statusCode": 404},
                )
            if response.status_code == 504:
                # A query that outlasts the service's wait is too big for one pass.
                raise LogQueryTooLargeError(
                    "Log Analytics timed out; MOSAIC will query less time at once"
                )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == MAX_ATTEMPTS - 1:
                    break
                delay = self._retry_delay(response, attempt)
                logger.warning(
                    "log_analytics_query_retry",
                    status_code=response.status_code,
                    delay_seconds=delay,
                )
                await self._sleep(delay)
                continue
            try:
                payload = response.json()
            except ValueError as error:
                raise LogQueryError(
                    "Log Analytics returned a response MOSAIC could not read"
                ) from error
            error_body = payload.get("error") if isinstance(payload, dict) else None
            if response.status_code >= 400:
                raise LogQueryError(
                    "Log Analytics rejected the telemetry query",
                    details={
                        "statusCode": response.status_code,
                        "reason": _error_message(error_body),
                    },
                )
            if error_body:
                failure = _partial_failure(error_body)
                if failure is not None:
                    raise failure
                logger.info("log_analytics_query_warning", reason=_error_message(error_body))
            return parse_tables(payload)
        raise LogQueryError(
            "Log Analytics stayed unavailable after retries",
            details={"statusCode": last_status},
        )
