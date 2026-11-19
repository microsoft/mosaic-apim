"""Refresh Azure account evidence and save non-executable onboarding context."""

import asyncio
import hashlib
import json
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import timedelta
from itertools import batched
from typing import Any
from uuid import UUID

from pydantic import ValidationError as ModelValidationError

from mosaic_api.clouds import ConnectionValidator, validate_resource_connection
from mosaic_api.domain import (
    COGNITIVE_SERVICES_API_VERSION,
    AuditEvent,
    CognitiveServicesResourceId,
    EndpointAuthMode,
    ModelEndpoint,
    ModelProvider,
    VerdictLevel,
    deterministic_id,
    utc_now,
)
from mosaic_api.endpoint_readiness import runtime_access_view
from mosaic_api.environments import compatibility_fingerprint, permits
from mosaic_api.errors import ConflictError, DomainError, NotFoundError, ValidationError
from mosaic_api.integrations.aoai.client import CognitiveServicesClient
from mosaic_api.integrations.aoai.inventory import _properties, _text
from mosaic_api.model_deployments import DeploymentModelRef
from mosaic_api.model_lifecycle import (
    DISCOVERY_RULES_REVISION,
    CatalogChange,
    DiscoveryHead,
    DiscoveryRow,
    DiscoveryRun,
    DiscoverySource,
    LifecycleDeployment,
    LifecyclePublication,
    ProposalContext,
    ProposalContextUpdate,
    ProposalDetail,
    ProposalList,
    ProposalSummary,
    SavedProposalContext,
)
from mosaic_api.repositories import (
    CostCenterRepository,
    EnvironmentRepository,
    GatewayRepository,
    ModelEndpointRepository,
)
from mosaic_api.services.directory import Actor
from mosaic_api.services.environments import load_environment_catalog

_TIMEOUT = timedelta(minutes=5)
_ACCOUNT_KINDS = {
    "openai": ModelProvider.AZURE_OPENAI,
    "aiservices": ModelProvider.AZURE_AI_FOUNDRY,
}
_REF_FIELDS = ("model_publisher", "source", "source_account")


def _digest(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


def _model(raw: dict[str, Any]) -> DeploymentModelRef | None:
    try:
        return DeploymentModelRef(
            model_format=_text(raw.get("format")) or "",
            model_name=_text(raw.get("name")) or "",
            model_version=_text(raw.get("version")) or "",
            model_publisher=_text(raw.get("publisher")),
            source=_text(raw.get("source")),
            source_account=_text(raw.get("sourceAccount")),
        )
    except ModelValidationError:
        return None


def _triple(model: DeploymentModelRef) -> tuple[str, str, str]:
    return model.model_format, model.model_name, model.model_version


def _supported(endpoint: ModelEndpoint) -> bool:
    return (
        endpoint.azure_cloud == "commercial"
        and endpoint.azure_resource_id is not None
        and endpoint.auth_mode == EndpointAuthMode.MANAGED_IDENTITY
        and endpoint.provider in _ACCOUNT_KINDS.values()
    )


def _source(endpoint: ModelEndpoint) -> DiscoverySource:
    return DiscoverySource(
        registration_at=endpoint.created_at,
        account_scope=endpoint.arm_resource().account_scope.casefold(),
        provider=endpoint.provider,
        connection_id=endpoint.azure_connection_id,
        api_version=COGNITIVE_SERVICES_API_VERSION,
    )


def _head_id(endpoint: ModelEndpoint) -> str:
    source = _source(endpoint)
    return deterministic_id(
        "discoveryhead",
        endpoint.tenant_id,
        endpoint.id,
        source.registration_at.isoformat(),
        source.account_scope,
        source.cloud,
        source.connection_id or "",
    )


def _proposal_id(run: DiscoveryRun, model: DeploymentModelRef) -> str:
    return deterministic_id(
        "modelproposal",
        run.tenant_id,
        run.endpoint_id,
        run.source.registration_at.isoformat(),
        run.source.account_scope,
        run.source.cloud,
        str(run.source.provider),
        _digest(model.model_dump(mode="json")),
    )


def _rows(
    run: DiscoveryRun,
    catalog: list[dict[str, Any]],
    deployments: list[dict[str, Any]],
    baseline: list[DiscoveryRow],
    *,
    first: bool,
) -> tuple[list[DiscoveryRow], list[str], bool]:
    previous = {row.proposal_id: row for row in baseline}
    current: dict[str, DiscoveryRow] = {}
    by_triple: dict[tuple[str, str, str], list[DiscoveryRow]] = defaultdict(list)

    def row(model: DeploymentModelRef, *, listed: bool, change: CatalogChange) -> DiscoveryRow:
        proposal_id = _proposal_id(run, model)
        return DiscoveryRow(
            id=deterministic_id("discoveryrow", run.id, proposal_id),
            tenant_id=run.tenant_id,
            endpoint_id=run.endpoint_id,
            run_id=run.id,
            proposal_id=proposal_id,
            model=model,
            listed=listed,
            change=change,
        )

    for item in catalog:
        envelope = item.get("model")
        raw = envelope if isinstance(envelope, dict) else item
        model = _model(raw)
        if model is None:
            return [], ["Azure returned a catalog entry without an exact model identity."], False
        candidate = row(model, listed=True, change="baseline" if first else "new")
        if candidate.proposal_id in current:
            return [], ["Azure returned duplicate catalog identities. Review manually."], False
        candidate.lifecycle_status = _text(raw.get("lifecycleStatus"))
        raw_skus = raw.get("skus", item.get("skus"))
        sku_evidence = (
            sorted(json.dumps(sku, sort_keys=True) for sku in raw_skus)
            if isinstance(raw_skus, list)
            else raw_skus
        )
        candidate.catalog_digest = _digest(
            {
                "lifecycle": candidate.lifecycle_status,
                "capabilities": raw.get("capabilities"),
                "deprecation": raw.get("deprecation"),
                "default": raw.get("isDefaultVersion"),
                "skus": sku_evidence,
            }
        )
        old = previous.get(candidate.proposal_id)
        if old is not None and old.listed:
            if old.catalog_digest != candidate.catalog_digest:
                candidate.change = "changed"
            elif old.change in {"new", "changed"}:
                candidate.change = old.change
            else:
                candidate.change = "unchanged"
        current[candidate.proposal_id] = candidate
        by_triple[_triple(model)].append(candidate)

    for old in previous.values():
        if old.proposal_id not in current:
            missing = row(
                old.model,
                listed=False,
                change="deploymentOnly" if old.change == "deploymentOnly" else "unlisted",
            )
            current[missing.proposal_id] = missing
            by_triple[_triple(old.model)].append(missing)

    findings: list[str] = []
    unknown_deployment = False
    seen_names: set[str] = set()
    for item in deployments:
        name = _text(item.get("name"))
        props = _properties(item)
        raw_model = props.get("model")
        if not name or name.casefold() in seen_names:
            return [], ["Azure returned incomplete or duplicate deployment names."], False
        seen_names.add(name.casefold())
        model = _model(raw_model) if isinstance(raw_model, dict) else None
        if model is None:
            unknown_deployment = True
            continue
        possible = [
            item
            for item in by_triple[_triple(model)]
            if all(
                getattr(model, field) is None
                or getattr(item.model, field) is None
                or getattr(item.model, field) == getattr(model, field)
                for field in _REF_FIELDS
            )
        ]
        if len(possible) > 1 or any(
            any(getattr(model, field) != getattr(match.model, field) for field in _REF_FIELDS)
            for match in possible
        ):
            for match in possible:
                match.deployment_unconfirmed = True
            message = "Some deployments could not be matched to an exact catalog source."
            if message not in findings:
                findings.append(message)
            continue
        match = possible[0] if possible else row(model, listed=False, change="deploymentOnly")
        if not possible:
            current[match.proposal_id] = match
            by_triple[_triple(model)].append(match)
        match.deployments.append(
            LifecycleDeployment(
                name=name,
                provisioning_state=_text(props.get("provisioningState")),
            )
        )

    if unknown_deployment:
        findings.append(
            "Some deployments lack exact model identity. Review missing matches manually."
        )
        for candidate in current.values():
            candidate.deployment_unconfirmed = True
    for candidate in current.values():
        candidate.deployments.sort(key=lambda deployment: deployment.name.casefold())
    return list(current.values()), findings, True


class ModelLifecycleService:
    def __init__(
        self,
        repository: ModelEndpointRepository,
        *,
        gateways: GatewayRepository,
        environments: EnvironmentRepository,
        cost_centers: CostCenterRepository,
        client_factory: Callable[[CognitiveServicesResourceId], CognitiveServicesClient],
        connection_validator: ConnectionValidator | None = None,
    ) -> None:
        self._repository = repository
        self._gateways = gateways
        self._environments = environments
        self._cost_centers = cost_centers
        self._client_factory = client_factory
        self._connection_validator = connection_validator

    async def _endpoint(self, actor: Actor, endpoint_id: str) -> ModelEndpoint:
        endpoint = await self._repository.get_endpoint(actor.tenant_id, endpoint_id)
        if endpoint is None:
            raise NotFoundError("Model endpoint not found")
        return endpoint

    async def _head(self, endpoint: ModelEndpoint) -> DiscoveryHead | None:
        return await self._repository.get_lifecycle(
            DiscoveryHead, endpoint.tenant_id, _head_id(endpoint)
        )

    async def _run(self, endpoint: ModelEndpoint, run_id: str | None) -> DiscoveryRun | None:
        if not run_id or not run_id.startswith("discoveryrun_"):
            return None
        run = await self._repository.get_lifecycle(DiscoveryRun, endpoint.tenant_id, run_id)
        source = _source(endpoint)
        if (
            run is None
            or run.endpoint_id != endpoint.id
            or run.source.registration_at != source.registration_at
            or run.source.account_scope != source.account_scope
            or run.source.connection_id != source.connection_id
        ):
            return None
        return run

    @staticmethod
    def _run_view(run: DiscoveryRun | None) -> DiscoveryRun | None:
        if run and run.status == "running" and utc_now() - run.started_at >= _TIMEOUT:
            return run.model_copy(
                update={
                    "status": "interrupted",
                    "findings": ["The read did not finish. Start a new discovery to retry."],
                }
            )
        return run

    async def get_run(self, actor: Actor, endpoint_id: str, run_id: str) -> DiscoveryRun:
        endpoint = await self._endpoint(actor, endpoint_id)
        run = self._run_view(await self._run(endpoint, run_id)) if _supported(endpoint) else None
        if run is None:
            raise NotFoundError("Discovery run not found")
        return run

    @staticmethod
    def _audit(actor: Actor, action: str, resource_id: str, event_key: str) -> AuditEvent:
        return AuditEvent(
            id=deterministic_id("audit", actor.tenant_id, action, event_key),
            tenant_id=actor.tenant_id,
            actor_object_id=actor.object_id,
            action=action,
            resource_type="modelLifecycle",
            resource_id=resource_id,
        )

    async def discover(self, actor: Actor, endpoint_id: str, request_id: UUID) -> DiscoveryRun:
        endpoint = await self._endpoint(actor, endpoint_id)
        if not _supported(endpoint):
            raise ValidationError(
                "Discovery supports Commercial Azure OpenAI and Foundry ARM registrations only"
            )
        source = _source(endpoint)
        head_id = _head_id(endpoint)
        run_id = deterministic_id(
            "discoveryrun",
            actor.tenant_id,
            endpoint.id,
            source.registration_at.isoformat(),
            str(request_id),
        )
        head = await self._head(endpoint)
        existing = await self._repository.get_lifecycle(DiscoveryRun, actor.tenant_id, run_id)
        if existing is not None:
            return await self._retry(endpoint, run_id)
        latest = self._run_view(await self._run(endpoint, head.latest_run_id)) if head else None
        if latest and latest.status == "running":
            raise ConflictError("Discovery is already running for this endpoint")
        run = DiscoveryRun(
            id=run_id,
            tenant_id=actor.tenant_id,
            endpoint_id=endpoint_id,
            request_id=request_id,
            actor_object_id=actor.object_id,
            source=source,
            started_at=utc_now(),
        )
        next_head = DiscoveryHead(
            id=head_id,
            etag=head.etag if head else None,
            tenant_id=actor.tenant_id,
            endpoint_id=endpoint_id,
            latest_run_id=run_id,
            complete_run_id=head.complete_run_id if head else None,
        )
        try:
            await self._repository.save_lifecycle([run, next_head])
        except ConflictError:
            if await self._repository.get_lifecycle(DiscoveryRun, actor.tenant_id, run_id):
                return await self._retry(endpoint, run_id)
            raise

        # Compare-and-swap the claimed head on completion. A timed-out worker can neither
        # publish its staged rows nor overwrite a newer explicit discovery.
        claimed_head = await self._head(endpoint)
        claimed_run = await self._run(endpoint, run_id)
        if not claimed_head or claimed_head.latest_run_id != run_id or not claimed_run:
            raise ConflictError("Discovery was superseded; reload the endpoint")
        run = claimed_run
        rows: list[DiscoveryRow] = []
        try:
            async with asyncio.timeout(_TIMEOUT.total_seconds()):
                run, rows = await self._collect(actor, endpoint, run, claimed_head.complete_run_id)
                for chunk in batched(rows, 40):
                    await self._repository.save_lifecycle(list(chunk))
        except TimeoutError:
            run = run.model_copy(
                update={
                    "status": "interrupted",
                    "findings": ["The read timed out. Start a new discovery."],
                }
            )
        current = await self._endpoint(actor, endpoint_id)
        if not _supported(current) or _head_id(current) != head_id:
            raise ConflictError("The endpoint registration or connection changed during discovery")
        if utc_now() - run.started_at >= _TIMEOUT and run.status == "complete":
            run = run.model_copy(
                update={
                    "status": "interrupted",
                    "findings": [
                        "The read timed out. Start a new discovery.",
                    ],
                }
            )
        run = run.model_copy(update={"completed_at": utc_now(), "updated_at": utc_now()})
        if run.status == "complete":
            claimed_head.complete_run_id = run.id
        claimed_head.updated_at = utc_now()
        await self._repository.save_lifecycle(
            [run, claimed_head],
            self._audit(actor, f"modelDiscovery.{run.status}", endpoint_id, run.id),
        )
        return run

    async def _retry(self, endpoint: ModelEndpoint, run_id: str) -> DiscoveryRun:
        run = self._run_view(await self._run(endpoint, run_id))
        if run is None:
            raise ConflictError("This discovery request belongs to a different source connection")
        return run

    async def _collect(
        self,
        actor: Actor,
        endpoint: ModelEndpoint,
        run: DiscoveryRun,
        baseline_id: str | None,
    ) -> tuple[DiscoveryRun, list[DiscoveryRow]]:
        try:
            await validate_resource_connection(
                self._connection_validator,
                actor.tenant_id,
                endpoint.azure_connection_id,
                endpoint.azure_cloud,
            )
            client = self._client_factory(endpoint.arm_resource())
            account = await client.get_account()
            provider = _ACCOUNT_KINDS.get((_text((account or {}).get("kind")) or "").casefold())
            location = _text((account or {}).get("location"))
            if provider is None or not location:
                raise ValidationError("Azure did not confirm a supported account kind and region")
        except DomainError as exc:
            return run.model_copy(update={"status": "failed", "findings": [exc.message]}), []
        run.source = run.source.model_copy(update={"provider": provider, "location": location})

        async def read(
            fetch: Callable[[], Awaitable[list[dict[str, Any]]]],
        ) -> tuple[list[dict[str, Any]] | None, str | None]:
            try:
                return await fetch(), None
            except DomainError as exc:
                return None, exc.message

        (catalog, catalog_error), (deployments, deployment_error) = await asyncio.gather(
            read(lambda: client.list_models(strict=True)),
            read(lambda: client.list_deployments(strict=True)),
        )
        observed_at = utc_now()
        if catalog is not None:
            run.catalog_observed_at = observed_at
        if deployments is not None:
            run.deployments_observed_at = observed_at
        if catalog is None or deployments is None:
            run.status = "failed" if catalog is None and deployments is None else "partial"
            run.findings = [
                f"{section}: {error}"
                for section, error in (
                    ("Catalog read", catalog_error),
                    ("Deployment read", deployment_error),
                )
                if error
            ]
            return run, []
        baseline_run = await self._run(endpoint, baseline_id)
        # Account kind/region changes start a new baseline, not a bulk disappearance.
        same_source = bool(
            baseline_run
            and baseline_run.source.provider == provider
            and baseline_run.source.location == location
            and baseline_run.rules_revision == run.rules_revision
        )
        baseline = (
            await self._repository.list_discovery_rows(actor.tenant_id, baseline_id)
            if baseline_id and same_source
            else []
        )
        rows, findings, complete = _rows(run, catalog, deployments, baseline, first=not same_source)
        run.status = "complete" if complete else "partial"
        run.findings = findings
        return run, rows

    async def _snapshot(
        self, endpoint: ModelEndpoint
    ) -> tuple[
        DiscoveryHead | None,
        DiscoveryRun | None,
        DiscoveryRun | None,
        bool,
    ]:
        head = await self._head(endpoint)
        if not head:
            return None, None, None, False
        latest = self._run_view(await self._run(endpoint, head.latest_run_id))
        complete = await self._run(endpoint, head.complete_run_id)
        current = bool(
            latest
            and complete
            and latest.id == complete.id
            and complete.status == "complete"
            and complete.rules_revision == DISCOVERY_RULES_REVISION
        )
        return head, latest, complete, current

    async def _context(self, row: DiscoveryRow) -> SavedProposalContext | None:
        return await self._repository.get_lifecycle(
            SavedProposalContext,
            row.tenant_id,
            row.proposal_id,
        )

    @staticmethod
    def _summary(row: DiscoveryRow, context: SavedProposalContext | None) -> ProposalSummary:
        return ProposalSummary(
            id=row.proposal_id,
            model=row.model,
            listed=row.listed,
            change=row.change,
            deployment_status=(
                "observed"
                if row.deployments
                else "unconfirmed"
                if row.deployment_unconfirmed
                else "notObserved"
            ),
            context_revision=context.revision if context else 0,
        )

    async def list_proposals(self, actor: Actor, endpoint_id: str) -> ProposalList:
        endpoint = await self._endpoint(actor, endpoint_id)
        if not _supported(endpoint):
            return ProposalList(supported=False)
        _, latest, complete, current = await self._snapshot(endpoint)
        items = []
        if complete:
            rows, contexts = await asyncio.gather(
                self._repository.list_discovery_rows(actor.tenant_id, complete.id),
                self._repository.list_proposal_contexts(actor.tenant_id, endpoint.id),
            )
            by_id = {context.id: context for context in contexts}
            items = [self._summary(row, by_id.get(row.proposal_id)) for row in rows]
            items.sort(
                key=lambda item: (
                    item.change not in {"new", "changed"},
                    item.model.model_name.casefold(),
                    item.model.model_version,
                    item.id,
                )
            )
        return ProposalList(
            supported=True,
            latest_attempt=latest,
            last_complete=complete,
            evidence_current=current,
            items=items,
        )

    async def _exact(
        self,
        actor: Actor,
        endpoint_id: str,
        proposal_id: str,
    ) -> tuple[ModelEndpoint, DiscoveryHead, DiscoveryRun, DiscoveryRow, bool]:
        endpoint = await self._endpoint(actor, endpoint_id)
        if _supported(endpoint) and proposal_id.startswith("modelproposal_"):
            head, _, evidence, current = await self._snapshot(endpoint)
            if head and evidence:
                row = await self._repository.get_lifecycle(
                    DiscoveryRow,
                    actor.tenant_id,
                    deterministic_id("discoveryrow", evidence.id, proposal_id),
                )
                if row and row.endpoint_id == endpoint.id and row.run_id == evidence.id:
                    return endpoint, head, evidence, row, current
        raise NotFoundError("Model discovery proposal not found")

    @staticmethod
    def _evidence_digest(run: DiscoveryRun, row: DiscoveryRow) -> str:
        return _digest(
            {
                "source": run.source.model_dump(mode="json"),
                "rules": run.rules_revision,
                "catalog": row.catalog_digest,
                "listed": row.listed,
                "deployments": [item.model_dump(mode="json") for item in row.deployments],
                "deploymentUnconfirmed": row.deployment_unconfirmed,
            }
        )

    async def _context_facts(
        self,
        endpoint: ModelEndpoint,
        context: ProposalContext,
    ) -> tuple[list[str], list[str], str]:
        tenant = endpoint.tenant_id
        catalog = await load_environment_catalog(self._environments, tenant)
        environment = next((e for e in catalog.environments if e.key == context.environment), None)
        gateway = (
            await self._gateways.get_gateway(tenant, context.gateway_id)
            if context.gateway_id
            else None
        )
        center = (
            await self._cost_centers.get_cost_center(tenant, context.cost_center_id)
            if context.cost_center_id
            else None
        )
        notes: list[str] = []
        blockers: list[str] = []
        if context.environment is not None and environment is None:
            blockers.append("The intended environment no longer exists.")
        if context.gateway_id and (gateway is None or gateway.azure_cloud != endpoint.azure_cloud):
            blockers.append("The intended gateway is missing or in a different Azure cloud.")
        if context.cost_center_id and center is None:
            blockers.append("The intended cost center no longer exists.")
        if gateway is not None:
            if context.environment and gateway.environment != context.environment:
                blockers.append("The gateway is not classified in the intended environment.")
            verdict = permits(catalog, gateway.environment, endpoint.environment)
            (blockers if verdict.level == VerdictLevel.BLOCKED else notes).append(verdict.reason)
        if environment and environment.production:
            notes.append("Production controls still apply in any future reviewed workflow.")
        if center and center.setup_state == "setupRequired":
            notes.append("The intended cost center still requires setup.")
        fingerprint = _digest(
            {
                "input": context.model_dump(mode="json"),
                "endpointEnvironment": endpoint.environment,
                "compatibility": (
                    compatibility_fingerprint(catalog, gateway.environment, endpoint.environment)
                    if gateway
                    else None
                ),
                "costCenterSetup": str(center.setup_state) if center else None,
                "notes": notes,
                "blockers": blockers,
            }
        )
        return notes, blockers, fingerprint

    async def get_proposal(
        self, actor: Actor, endpoint_id: str, proposal_id: str
    ) -> ProposalDetail:
        endpoint, _, evidence, row, current = await self._exact(actor, endpoint_id, proposal_id)
        context = await self._context(row)
        digest = self._evidence_digest(evidence, row)
        detail = ProposalDetail(
            **self._summary(row, context).model_dump(),
            evidence=evidence,
            evidence_digest=digest,
            evidence_current=current,
            lifecycle_status=row.lifecycle_status,
            deployments=row.deployments,
            publications=[],
            saved_context=context,
        )
        names = {deployment.name.casefold() for deployment in row.deployments}
        if names:
            publications = await self._gateways.list_publications(actor.tenant_id)
            detail.publications = [
                LifecyclePublication(
                    gateway_id=publication.gateway_id,
                    deployment_name=publication.deployment_name,
                    status=str(publication.status),
                    applied_api_recorded=publication.has_applied_api(),
                )
                for publication in publications
                if publication.model_endpoint_id == endpoint.id
                and publication.deployment_name.casefold() in names
            ]
        if context:
            notes, blockers, fingerprint = await self._context_facts(endpoint, context.context)
            detail.context_notes = [*blockers, *notes]
            if (
                not current
                or digest != context.evidence_digest
                or fingerprint != context.context_digest
            ):
                detail.context_status = "outOfDate"
            elif None in (
                context.context.environment,
                context.context.gateway_id,
                context.context.cost_center_id,
            ):
                detail.context_status = "incomplete"
            else:
                detail.context_status = "recorded"
            runtime = next(
                (
                    item
                    for item in endpoint.runtime_access
                    if item.gateway_id == context.context.gateway_id
                ),
                None,
            )
            if runtime is not None:
                view = runtime_access_view(runtime)
                detail.runtime_freshness = str(view.freshness)
                detail.runtime_checked_at = view.checked_at
                detail.runtime_rules_revision = view.rules_revision
        return detail

    async def save_context(
        self,
        actor: Actor,
        endpoint_id: str,
        proposal_id: str,
        payload: ProposalContextUpdate,
    ) -> ProposalDetail:
        endpoint, head, evidence, row, current = await self._exact(actor, endpoint_id, proposal_id)
        old = await self._context(row)
        context = ProposalContext(
            environment=payload.environment,
            gateway_id=payload.gateway_id,
            cost_center_id=payload.cost_center_id,
        )
        digest = self._evidence_digest(evidence, row)
        if (
            not current
            or payload.evidence_run_id != evidence.id
            or payload.evidence_digest != digest
        ):
            raise ConflictError("Evidence changed or is incomplete; refresh and review it again")
        _, blockers, fingerprint = await self._context_facts(endpoint, context)
        if blockers:
            raise ValidationError(" ".join(blockers))
        revision = old.revision if old else 0
        if old and (
            old.context == context
            and old.context_digest == fingerprint
            and old.evidence_digest == digest
        ):
            if payload.expected_revision in {revision, revision - 1}:
                return await self.get_proposal(actor, endpoint_id, proposal_id)
        if payload.expected_revision != revision:
            raise ConflictError("Someone changed this proposal context; reload before saving")
        saved = SavedProposalContext(
            id=row.proposal_id,
            tenant_id=actor.tenant_id,
            endpoint_id=endpoint_id,
            etag=old.etag if old else None,
            actor_object_id=actor.object_id,
            revision=revision + 1,
            context=context,
            context_digest=fingerprint,
            evidence_run_id=evidence.id,
            evidence_digest=digest,
        )
        await self._repository.save_lifecycle(
            [saved, head],
            self._audit(
                actor, "modelProposal.contextSaved", saved.id, f"{saved.id}:{saved.revision}"
            ),
        )
        return await self.get_proposal(actor, endpoint_id, proposal_id)
