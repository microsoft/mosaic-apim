from datetime import datetime
from typing import Literal
from urllib.parse import urlparse

import structlog
from pydantic import Field

from mosaic_api.domain import (
    Gateway,
    McpEndpoint,
    ModelEndpoint,
    MosaicModel,
    Publication,
    canonical_mcp_url,
    deterministic_id,
    utc_now,
)
from mosaic_api.environments import (
    EnvironmentCatalog,
    EnvironmentVerdict,
    VerdictLevel,
    permits,
)
from mosaic_api.errors import NotFoundError
from mosaic_api.observed import ObservedApi, ObservedBackend, ObservedMcpServer
from mosaic_api.repositories import (
    GatewayRepository,
    McpEndpointRepository,
    ModelEndpointRepository,
)
from mosaic_api.services.directory import Actor
from mosaic_api.services.environments import EnvironmentService

logger = structlog.get_logger()

FindingKind = Literal[
    "blockedPublication",
    "backendCrossesEnvironments",
    "apiCrossesEnvironments",
    "mcpServerCrossesEnvironments",
]
FindingConfidence = Literal["certain", "high", "medium"]
FindingSubjectKind = Literal["publication", "backend", "api", "mcpServer"]
FindingTargetKind = Literal["modelEndpoint", "mcpEndpoint"]

_AZURE_AI_ACCOUNT_DOMAINS = (
    "openai.azure.com",
    "cognitiveservices.azure.com",
    "services.ai.azure.com",
)
_POLICY_LIMITATION = (
    "Backends referenced only from policy, such as a set-backend-service base URL or a named "
    "value, aren't inspected."
)


class EnvironmentFindingSubject(MosaicModel):
    kind: FindingSubjectKind
    id: str
    name: str
    api_name: str | None = None


class EnvironmentFindingTarget(MosaicModel):
    resource_kind: FindingTargetKind
    resource_id: str
    resource_name: str
    environment: str | None = None


class EnvironmentFinding(MosaicModel):
    id: str
    kind: FindingKind
    confidence: FindingConfidence
    gateway_id: str
    gateway_name: str
    gateway_environment: str | None = None
    subject: EnvironmentFindingSubject
    target: EnvironmentFindingTarget
    verdict: EnvironmentVerdict
    evidence: str
    message: str


class EnvironmentFindingList(MosaicModel):
    items: list[EnvironmentFinding] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utc_now)


def normalize_ai_account_host(host: str) -> str | None:
    """Return a normalized Azure AI account key for hosts on supported account domains."""

    candidate = host.strip()
    if not candidate:
        return None
    parsed = urlparse(candidate if "://" in candidate else f"https://{candidate}")
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    for domain in _AZURE_AI_ACCOUNT_DOMAINS:
        suffix = f".{domain}"
        if hostname.endswith(suffix):
            account = hostname[: -len(suffix)]
            if account and "." not in account:
                return account
    return None


def _host(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    parsed = urlparse(candidate if "://" in candidate else f"https://{candidate}")
    return (parsed.hostname or "").casefold() or None


def _environment_name(catalog: EnvironmentCatalog, key: str | None) -> str:
    if key is None:
        return "Unclassified"
    match = next((item for item in catalog.environments if item.key == key), None)
    return match.display_name if match else key


def _message(catalog: EnvironmentCatalog, gateway_env: str | None, endpoint_env: str | None) -> str:
    return (
        f"A {_environment_name(catalog, gateway_env)} gateway routes to a "
        f"{_environment_name(catalog, endpoint_env)} endpoint."
    )


class EnvironmentFindingsService:
    def __init__(
        self,
        *,
        environment_service: EnvironmentService,
        gateway_repository: GatewayRepository,
        endpoint_repository: ModelEndpointRepository,
        mcp_repository: McpEndpointRepository,
    ) -> None:
        self._environment_service = environment_service
        self._gateways = gateway_repository
        self._endpoints = endpoint_repository
        self._mcp = mcp_repository

    async def list_findings(
        self, actor: Actor, *, gateway_id: str | None = None
    ) -> EnvironmentFindingList:
        catalog = await self._environment_service.catalog(actor)
        gateways = await self._load_gateways(actor, gateway_id)
        model_endpoints = await self._endpoints.list_endpoints(actor.tenant_id)
        mcp_endpoints = await self._mcp.list_endpoints(actor.tenant_id)
        publications = await self._gateways.list_publications(
            actor.tenant_id, gateway_id=gateway_id
        )

        endpoints_by_host, endpoints_by_account = self._index_model_endpoints(model_endpoints)
        mcp_by_url = self._index_mcp_endpoints(mcp_endpoints)
        own_api_names = {
            (item.gateway_id, item.api_name.casefold())
            for item in publications
            if item.may_own_gateway_state()
        }

        items: list[EnvironmentFinding] = []
        seen: set[tuple[str, str, str, str, str]] = set()
        for publication in publications:
            if publication.may_own_gateway_state():
                gateway = next(
                    (item for item in gateways if item.id == publication.gateway_id), None
                )
                endpoint = next(
                    (
                        item
                        for item in model_endpoints
                        if item.id == publication.model_endpoint_id
                    ),
                    None,
                )
                if gateway and endpoint:
                    self._add_blocked_publication(
                        items, seen, catalog, gateway, publication, endpoint
                    )

        limitations = [_POLICY_LIMITATION]
        for gateway in gateways:
            try:
                backends = await self._gateways.list_observed(
                    ObservedBackend, actor.tenant_id, gateway.id, "observedBackend"
                )
                apis = await self._gateways.list_observed(
                    ObservedApi, actor.tenant_id, gateway.id, "observedApi"
                )
                mcp_servers = await self._gateways.list_observed(
                    ObservedMcpServer, actor.tenant_id, gateway.id, "observedMcpServer"
                )
            except Exception:
                logger.warning("environment_findings_inventory_failed", gateway_id=gateway.id)
                limitations.append(
                    f"MOSAIC couldn't read observed inventory for gateway {gateway.name}; "
                    "findings for that gateway may be incomplete."
                )
                continue

            for backend in backends:
                self._add_url_model_findings(
                    items,
                    seen,
                    catalog,
                    gateway,
                    EnvironmentFindingSubject(
                        kind="backend",
                        id=backend.id,
                        name=backend.title or backend.name,
                    ),
                    "backendCrossesEnvironments",
                    "Backend",
                    backend.url,
                    endpoints_by_host,
                    endpoints_by_account,
                )
            for api in apis:
                if (gateway.id, api.name.casefold()) in own_api_names:
                    continue
                self._add_url_model_findings(
                    items,
                    seen,
                    catalog,
                    gateway,
                    EnvironmentFindingSubject(
                        kind="api",
                        id=api.id,
                        name=api.display_name,
                        api_name=api.name,
                    ),
                    "apiCrossesEnvironments",
                    "API",
                    api.service_url,
                    endpoints_by_host,
                    endpoints_by_account,
                )
            for server in mcp_servers:
                self._add_mcp_findings(items, seen, catalog, gateway, server, mcp_by_url)

        items.sort(
            key=lambda item: (
                item.gateway_name.casefold(),
                item.kind,
                item.subject.name.casefold(),
                item.target.resource_name.casefold(),
            )
        )
        return EnvironmentFindingList(items=items, limitations=limitations)

    async def _load_gateways(self, actor: Actor, gateway_id: str | None) -> list[Gateway]:
        if gateway_id is None:
            return await self._gateways.list_gateways(actor.tenant_id)
        gateway = await self._gateways.get_gateway(actor.tenant_id, gateway_id)
        if gateway is None:
            raise NotFoundError("Gateway was not found", details={"id": gateway_id})
        return [gateway]

    @staticmethod
    def _index_model_endpoints(
        endpoints: list[ModelEndpoint],
    ) -> tuple[dict[str, list[ModelEndpoint]], dict[str, list[ModelEndpoint]]]:
        by_host: dict[str, list[ModelEndpoint]] = {}
        by_account: dict[str, list[ModelEndpoint]] = {}
        for endpoint in endpoints:
            host = _host(str(endpoint.endpoint))
            if host is None:
                continue
            by_host.setdefault(host, []).append(endpoint)
            account = normalize_ai_account_host(host)
            if account is not None:
                by_account.setdefault(account, []).append(endpoint)
        for bucket in (*by_host.values(), *by_account.values()):
            bucket.sort(key=lambda item: (item.name.casefold(), item.id))
        return by_host, by_account

    @staticmethod
    def _index_mcp_endpoints(endpoints: list[McpEndpoint]) -> dict[str, list[McpEndpoint]]:
        by_url: dict[str, list[McpEndpoint]] = {}
        for endpoint in endpoints:
            try:
                key = canonical_mcp_url(str(endpoint.endpoint))
            except ValueError:
                continue
            by_url.setdefault(key, []).append(endpoint)
        for bucket in by_url.values():
            bucket.sort(key=lambda item: (item.name.casefold(), item.id))
        return by_url

    def _add_blocked_publication(
        self,
        items: list[EnvironmentFinding],
        seen: set[tuple[str, str, str, str, str]],
        catalog: EnvironmentCatalog,
        gateway: Gateway,
        publication: Publication,
        endpoint: ModelEndpoint,
    ) -> None:
        verdict = permits(catalog, gateway.environment, endpoint.environment)
        if verdict.level != VerdictLevel.BLOCKED:
            return
        subject = EnvironmentFindingSubject(
            kind="publication", id=publication.id, name=publication.display_name
        )
        target = EnvironmentFindingTarget(
            resource_kind="modelEndpoint",
            resource_id=endpoint.id,
            resource_name=endpoint.name,
            environment=endpoint.environment,
        )
        self._add(
            items,
            seen,
            catalog,
            gateway,
            kind="blockedPublication",
            confidence="certain",
            subject=subject,
            target=target,
            verdict=verdict,
            evidence=(
                f'Publication "{publication.display_name}" is applied on gateway '
                f'"{gateway.name}" and targets registered model endpoint "{endpoint.name}".'
            ),
            message=(
                f"{_message(catalog, gateway.environment, endpoint.environment)} "
                "The data changed outside MOSAIC's environment rules."
            ),
        )

    def _add_url_model_findings(
        self,
        items: list[EnvironmentFinding],
        seen: set[tuple[str, str, str, str, str]],
        catalog: EnvironmentCatalog,
        gateway: Gateway,
        subject: EnvironmentFindingSubject,
        kind: Literal["backendCrossesEnvironments", "apiCrossesEnvironments"],
        noun: str,
        url: str | None,
        endpoints_by_host: dict[str, list[ModelEndpoint]],
        endpoints_by_account: dict[str, list[ModelEndpoint]],
    ) -> None:
        host = _host(url)
        if host is None:
            return
        matches: list[tuple[ModelEndpoint, FindingConfidence]] = [
            (endpoint, "high") for endpoint in endpoints_by_host.get(host, [])
        ]
        account = normalize_ai_account_host(host)
        if account is not None:
            exact_ids = {endpoint.id for endpoint, _ in matches}
            matches.extend(
                (endpoint, "medium")
                for endpoint in endpoints_by_account.get(account, [])
                if endpoint.id not in exact_ids
            )
        for endpoint, confidence in matches:
            verdict = permits(catalog, gateway.environment, endpoint.environment)
            if verdict.level != VerdictLevel.BLOCKED:
                continue
            target = EnvironmentFindingTarget(
                resource_kind="modelEndpoint",
                resource_id=endpoint.id,
                resource_name=endpoint.name,
                environment=endpoint.environment,
            )
            self._add(
                items,
                seen,
                catalog,
                gateway,
                kind=kind,
                confidence=confidence,
                subject=subject,
                target=target,
                verdict=verdict,
                evidence=(
                    f'{noun} "{subject.name}" points at {url}, the registered model endpoint '
                    f'"{endpoint.name}".'
                ),
                message=_message(catalog, gateway.environment, endpoint.environment),
            )

    def _add_mcp_findings(
        self,
        items: list[EnvironmentFinding],
        seen: set[tuple[str, str, str, str, str]],
        catalog: EnvironmentCatalog,
        gateway: Gateway,
        server: ObservedMcpServer,
        mcp_by_url: dict[str, list[McpEndpoint]],
    ) -> None:
        if not server.service_url:
            return
        try:
            key = canonical_mcp_url(server.service_url)
        except ValueError:
            return
        for endpoint in mcp_by_url.get(key, []):
            verdict = permits(catalog, gateway.environment, endpoint.environment)
            if verdict.level != VerdictLevel.BLOCKED:
                continue
            subject = EnvironmentFindingSubject(
                kind="mcpServer",
                id=server.id,
                name=server.display_name,
                api_name=server.name,
            )
            target = EnvironmentFindingTarget(
                resource_kind="mcpEndpoint",
                resource_id=endpoint.id,
                resource_name=endpoint.name,
                environment=endpoint.environment,
            )
            self._add(
                items,
                seen,
                catalog,
                gateway,
                kind="mcpServerCrossesEnvironments",
                confidence="high",
                subject=subject,
                target=target,
                verdict=verdict,
                evidence=(
                    f'MCP server "{server.display_name}" points at {server.service_url}, the '
                    f'registered MCP endpoint "{endpoint.name}".'
                ),
                message=_message(catalog, gateway.environment, endpoint.environment),
            )

    def _add(
        self,
        items: list[EnvironmentFinding],
        seen: set[tuple[str, str, str, str, str]],
        catalog: EnvironmentCatalog,
        gateway: Gateway,
        *,
        kind: FindingKind,
        confidence: FindingConfidence,
        subject: EnvironmentFindingSubject,
        target: EnvironmentFindingTarget,
        verdict: EnvironmentVerdict,
        evidence: str,
        message: str,
    ) -> None:
        dedupe = (
            gateway.id,
            subject.kind,
            subject.id,
            target.resource_kind,
            target.resource_id,
        )
        if dedupe in seen:
            return
        seen.add(dedupe)
        items.append(
            EnvironmentFinding(
                id=deterministic_id(
                    "environmentFinding",
                    catalog.tenant_id,
                    gateway.id,
                    kind,
                    subject.id,
                    target.resource_id,
                ),
                kind=kind,
                confidence=confidence,
                gateway_id=gateway.id,
                gateway_name=gateway.name,
                gateway_environment=gateway.environment,
                subject=subject,
                target=target,
                verdict=verdict,
                evidence=evidence,
                message=message,
            )
        )
