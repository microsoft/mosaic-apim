"""Log Analytics: the queries MOSAIC runs over gateway logs, and the client that runs them."""

from .client import (
    LogAnalyticsClient,
    LogQueryAccessError,
    LogQueryError,
    LogQueryTooLargeError,
    LogsQuery,
    Row,
    parse_tables,
    query_scope,
)
from .kql import (
    API_NAME,
    DEPLOYMENT_KEY,
    PROBE_HOURS,
    QueryWindow,
    calls_query,
    denials_query,
    deployment_peaks_query,
    peaks_query,
    probe_query,
)

__all__ = [
    "API_NAME",
    "DEPLOYMENT_KEY",
    "PROBE_HOURS",
    "LogAnalyticsClient",
    "LogQueryAccessError",
    "LogQueryError",
    "LogQueryTooLargeError",
    "LogsQuery",
    "QueryWindow",
    "Row",
    "calls_query",
    "denials_query",
    "deployment_peaks_query",
    "parse_tables",
    "peaks_query",
    "probe_query",
    "query_scope",
]