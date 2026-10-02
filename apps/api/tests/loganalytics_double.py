"""A stand-in for Log Analytics that answers MOSAIC's queries from raw gateway calls.

Each query is recognised by its shape, and answered by applying in Python what the KQL does:
the same filters, grouping, and sums. Tests describe the calls a gateway handled, and the rollup job
reads them back exactly as it would read the real tables.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from mosaic_api.integrations.loganalytics import PROBE_HOURS, Row
from mosaic_api.integrations.loganalytics.kql import LLM_LOG_MARGIN, MCP_CALL_ALLOWANCE
from mosaic_api.usage_telemetry import LATENCY_BUCKETS_MS

_START = re.compile(r"let startTime = datetime\(([^)]+)\);")
_END = re.compile(r"let endTime = datetime\(([^)]+)\);")
_APIS = re.compile(r"let mosaicApis = dynamic\((\[.*?\])\);")
_DEPLOYMENTS = re.compile(r"let deploymentOf = dynamic\((\{.*?\})\);")


@dataclass
class GatewayCall:
    """One gateway log row, with the LLM log row that goes with it when there is one."""

    time: datetime
    api: str
    response_code: int = 200
    backend_code: int = 200
    total_time_ms: int = 120
    backend_time_ms: int = 100
    subscription: str = ""
    # The attribution trace: version, grant key, validated member, and client app.
    grant: str = ""
    member: str = ""
    client_app: str = ""
    version: str = "1"
    traced: bool = True
    # The trace's MCP call reference: on a model call, the MCP call an application named; on an
    # MCP call to a server with a model caller, its own request ID, beside that model caller.
    reference: str = ""
    model_caller: str = ""
    # A refusal by MOSAIC's policy: its reason, and the caller and client app it validated.
    denial_reason: str = ""
    denied_caller: str = ""
    last_error_source: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    deployment: str | None = None
    model: str | None = None

    @property
    def total_tokens(self) -> int | None:
        if self.prompt_tokens is None and self.completion_tokens is None:
            return None
        return (self.prompt_tokens or 0) + (self.completion_tokens or 0)

    @property
    def reason(self) -> str:
        if self.denial_reason:
            return self.denial_reason
        if not self.traced and self.response_code == 401:
            source = self.last_error_source
            return "token-invalid" if "token" in source or "jwt" in source else "unauthenticated"
        return ""

    @property
    def status(self) -> str:
        code = self.response_code
        source = self.last_error_source
        if code == 429:
            return "throttled"
        if code == 403 and ("token-limit" in source or "quota" in source):
            return "quota"
        if 200 <= code < 400:
            return "ok"
        if 400 <= code < 500:
            return "clientError"
        return "serverError"


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _latency(calls: Iterable[GatewayCall]) -> dict[str, int]:
    bounds = LATENCY_BUCKETS_MS
    counts = [0] * (len(bounds) + 1)
    for call in calls:
        index = next(
            (position for position, bound in enumerate(bounds) if call.total_time_ms < bound),
            len(bounds),
        )
        counts[index] += 1
    return {f"l{index}": count for index, count in enumerate(counts)}


def _sum(values: Iterable[int | None]) -> int:
    return sum(value or 0 for value in values)


@dataclass
class FakeLogs:
    """Answers :class:`~mosaic_api.integrations.loganalytics.LogsQuery` from ``calls``."""

    calls: list[GatewayCall] = field(default_factory=list)
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    # Raised for a resource ID before answering, to test how failures are handled.
    failures: dict[str, Exception] = field(default_factory=dict)
    # Windows longer than this many hours are refused as too large, to test splitting.
    max_hours: int | None = None
    queries: list[tuple[str, str]] = field(default_factory=list)
    # The window of every query answered, apart from probes.
    answered: list[tuple[datetime, datetime]] = field(default_factory=list)

    async def query(
        self, resource_id: str, query: str, *, start: datetime, end: datetime
    ) -> list[Row]:
        failure = self.failures.get(resource_id)
        if failure is not None:
            raise failure
        kind = self._kind(query)
        self.queries.append((kind, resource_id))
        if kind == "probe":
            return self._probe()
        window_start = _parse_time(_START.search(query).group(1))  # type: ignore[union-attr]
        window_end = _parse_time(_END.search(query).group(1))  # type: ignore[union-attr]
        if self.max_hours is not None and window_end - window_start > timedelta(
            hours=self.max_hours
        ):
            from mosaic_api.integrations.loganalytics import LogQueryTooLargeError

            raise LogQueryTooLargeError("The query's result is too large")
        self.answered.append((window_start, window_end))
        apis = set(json.loads(_APIS.search(query).group(1)))  # type: ignore[union-attr]
        rows = [
            call
            for call in self.calls
            if window_start <= call.time < window_end and call.api.casefold() in apis
        ]
        if kind == "denials":
            return self._denials([call for call in rows if call.reason])
        admitted = [call for call in rows if not call.reason]
        if kind == "deploymentPeaks":
            mapping = json.loads(_DEPLOYMENTS.search(query).group(1))  # type: ignore[union-attr]
            return self._deployment_peaks(admitted, mapping)
        if kind == "peaks":
            return self._peaks(admitted)
        return self._calls(admitted, self._mcp_calls(window_start, window_end, apis))

    def _mcp_calls(
        self, start: datetime, end: datetime, apis: set[str]
    ) -> dict[str, GatewayCall]:
        """The calls query's MCP leg: traced calls naming a model caller, by their reference.

        Read either side of the window, by the LLM margin, and the earliest call wins a reference.
        """

        found: dict[str, GatewayCall] = {}
        for call in sorted(self.calls, key=lambda item: item.time):
            if not (start - LLM_LOG_MARGIN <= call.time < end + LLM_LOG_MARGIN):
                continue
            if call.api.casefold() not in apis or not call.traced:
                continue
            if call.reference and call.model_caller:
                found.setdefault(call.reference.casefold(), call)
        return found

    @staticmethod
    def _kind(query: str) -> str:
        if "by source" in query:
            return "probe"
        if "deploymentOf" in query:
            return "deploymentPeaks"
        if "by link, hour" in query:
            return "peaks"
        if "isnotempty(reason)" in query:
            return "denials"
        return "calls"

    def _probe(self) -> list[Row]:
        since = self.clock() - timedelta(hours=PROBE_HOURS)
        recent = [call for call in self.calls if call.time > since]
        rows: list[Row] = []
        if recent:
            rows.append(
                {
                    "source": "gateway",
                    "rows": len(recent),
                    "traced": sum(1 for call in recent if call.traced and call.grant),
                    "lastSeen": max(call.time for call in recent),
                }
            )
        metered = [call for call in recent if call.total_tokens is not None]
        if metered:
            rows.append(
                {
                    "source": "llm",
                    "rows": len(metered),
                    "traced": 0,
                    "lastSeen": max(call.time for call in metered),
                }
            )
        return rows

    @staticmethod
    def _trace(call: GatewayCall) -> tuple[str, str, str, str]:
        if not call.traced:
            return "", "", "", ""
        return (
            call.version,
            call.grant.casefold(),
            call.member.casefold(),
            call.client_app.casefold(),
        )

    @staticmethod
    def _on_behalf(
        call: GatewayCall, mcp_calls: dict[str, GatewayCall]
    ) -> tuple[str, str, str, str, str]:
        """What the calls query reports of the MCP call a model call names, as KQL works it out."""

        reference = call.reference.casefold() if call.traced and not call.model_caller else ""
        if not reference:
            return "", "", "", "", ""
        if reference == "!":
            return "malformed", "", "", "", ""
        mcp = mcp_calls.get(reference)
        if mcp is None:
            return "missing", "", "", "", ""
        allowance = MCP_CALL_ALLOWANCE.total_seconds() * 1000
        apart = abs((call.time - mcp.time).total_seconds() * 1000)
        if apart > mcp.total_time_ms + allowance:
            return "late", "", "", "", ""
        return (
            "found",
            mcp.grant.casefold(),
            mcp.member.casefold(),
            mcp.model_caller.casefold(),
            mcp.api.casefold(),
        )

    def _calls(
        self, calls: list[GatewayCall], mcp_calls: dict[str, GatewayCall] | None = None
    ) -> list[Row]:
        groups: dict[tuple[Any, ...], list[GatewayCall]] = defaultdict(list)
        for call in calls:
            version, grant, member, client_app = self._trace(call)
            key = (
                call.time.hour,
                version,
                grant,
                member,
                client_app,
                call.api.casefold(),
                call.subscription.casefold(),
                call.deployment if call.total_tokens is not None else None,
                call.model if call.total_tokens is not None else None,
                *self._on_behalf(call, mcp_calls or {}),
            )
            groups[key].append(call)
        rows: list[Row] = []
        for key, members in groups.items():
            (
                hour,
                version,
                grant,
                member,
                client_app,
                api,
                subscription,
                deployment,
                model,
                on_behalf,
                mcp_grant,
                mcp_member,
                model_caller,
                mcp_api,
            ) = key
            statuses = [call.status for call in members]
            rows.append(
                {
                    "hour": hour,
                    "v": version,
                    "g": grant,
                    "m": member,
                    "a": client_app,
                    "api": api,
                    "subscription": subscription,
                    "deployment": deployment or "",
                    "model": model or "",
                    "onBehalf": on_behalf,
                    "og": mcp_grant,
                    "om": mcp_member,
                    "oi": model_caller,
                    "oapi": mcp_api,
                    "requests": len(members),
                    "ok": statuses.count("ok"),
                    "throttled": statuses.count("throttled"),
                    "quota": statuses.count("quota"),
                    "clientErrors": statuses.count("clientError"),
                    "serverErrors": statuses.count("serverError"),
                    "backendThrottled": sum(1 for call in members if call.backend_code == 429),
                    "keyRequests": sum(1 for call in members if call.subscription),
                    "metered": sum(1 for call in members if call.total_tokens is not None),
                    "promptTokens": _sum(call.prompt_tokens for call in members),
                    "completionTokens": _sum(call.completion_tokens for call in members),
                    "totalTokens": _sum(call.total_tokens for call in members),
                    "totalTime": sum(call.total_time_ms for call in members),
                    "backendTime": sum(call.backend_time_ms for call in members),
                    **_latency(members),
                    "lastSeen": max(call.time for call in members),
                }
            )
        return rows

    @staticmethod
    def _busiest_minutes(
        groups: dict[tuple[str, datetime], list[GatewayCall]],
        requests: Callable[[list[GatewayCall]], int],
        name: str,
    ) -> list[Row]:
        peaks: dict[tuple[str, int], tuple[int, int]] = {}
        for (key, minute), members in groups.items():
            tokens = _sum(call.total_tokens for call in members)
            count = requests(members)
            held = peaks.get((key, minute.hour), (0, 0))
            peaks[(key, minute.hour)] = (max(held[0], tokens), max(held[1], count))
        return [
            {name: key, "hour": hour, "peakTokens": tokens, "peakRequests": count}
            for (key, hour), (tokens, count) in peaks.items()
        ]

    def _peaks(self, calls: list[GatewayCall]) -> list[Row]:
        groups: dict[tuple[str, datetime], list[GatewayCall]] = defaultdict(list)
        for call in calls:
            version, grant, member, _ = self._trace(call)
            if version == "1" and grant:
                link = f"t:{grant}|{member}"
            elif call.subscription:
                link = f"s:{call.subscription.casefold()}"
            else:
                continue
            minute = call.time.replace(second=0, microsecond=0)
            groups[(link, minute)].append(call)
        return self._busiest_minutes(groups, len, "link")

    def _deployment_peaks(self, calls: list[GatewayCall], mapping: dict[str, str]) -> list[Row]:
        groups: dict[tuple[str, datetime], list[GatewayCall]] = defaultdict(list)
        for call in calls:
            key = mapping.get(call.api.casefold())
            if key:
                groups[(key, call.time.replace(second=0, microsecond=0))].append(call)
        return self._busiest_minutes(
            groups,
            lambda members: sum(1 for call in members if call.backend_code > 0),
            "deploymentKey",
        )

    @staticmethod
    def _denials(calls: list[GatewayCall]) -> list[Row]:
        groups: dict[tuple[Any, ...], list[GatewayCall]] = defaultdict(list)
        for call in calls:
            traced = bool(call.denial_reason)
            key = (
                call.time.hour,
                call.reason,
                call.denied_caller.casefold() if traced else "",
                call.client_app.casefold() if traced else "",
                call.api.casefold(),
                call.version if traced else "",
            )
            groups[key].append(call)
        return [
            {
                "hour": hour,
                "reason": reason,
                "o": caller,
                "a": client_app,
                "api": api,
                "v": version,
                "requests": len(members),
                "lastSeen": max(call.time for call in members),
            }
            for (hour, reason, caller, client_app, api, version), members in groups.items()
        ]
