"""Records for read-only model discovery; none authorize an onboarding action."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from mosaic_api.domain import Entity, ModelProvider, MosaicModel
from mosaic_api.model_deployments import DeploymentModelRef

DISCOVERY_RULES_REVISION = "model-lifecycle/1"
DiscoveryStatus = Literal["running", "complete", "partial", "failed", "interrupted"]
CatalogChange = Literal["baseline", "new", "changed", "unchanged", "unlisted", "deploymentOnly"]


class DiscoverySource(MosaicModel):
    registration_at: datetime
    account_scope: str
    provider: ModelProvider
    cloud: Literal["commercial"] = "commercial"
    connection_id: str | None = None
    location: str | None = None
    api_version: str


class DiscoveryRequest(MosaicModel):
    request_id: UUID


class DiscoveryRun(Entity):
    entity_type: Literal["modelDiscoveryRun"] = "modelDiscoveryRun"
    endpoint_id: str
    actor_object_id: str
    source: DiscoverySource
    rules_revision: str = DISCOVERY_RULES_REVISION
    status: DiscoveryStatus = "running"
    started_at: datetime
    completed_at: datetime | None = None
    catalog_observed_at: datetime | None = None
    deployments_observed_at: datetime | None = None
    findings: list[str] = Field(default_factory=list)


class DiscoveryHead(Entity):
    entity_type: Literal["modelDiscoveryHead"] = "modelDiscoveryHead"
    endpoint_id: str
    latest_run_id: str
    complete_run_id: str | None = None


class LifecycleDeployment(MosaicModel):
    name: str
    provisioning_state: str | None = None


class DiscoveryRow(Entity):
    entity_type: Literal["modelDiscoveryRow"] = "modelDiscoveryRow"
    endpoint_id: str
    run_id: str
    proposal_id: str
    model: DeploymentModelRef
    listed: bool
    change: CatalogChange
    catalog_digest: str | None = None
    lifecycle_status: str | None = None
    deployments: list[LifecycleDeployment] = Field(default_factory=list)
    deployment_unconfirmed: bool = False


class ProposalContext(MosaicModel):
    environment: str | None = Field(default=None, min_length=1, max_length=128)
    gateway_id: str | None = Field(default=None, min_length=1, max_length=128)
    cost_center_id: str | None = Field(default=None, min_length=1, max_length=128)


class ProposalContextUpdate(ProposalContext):
    evidence_run_id: str = Field(min_length=1, max_length=128)
    evidence_digest: str = Field(min_length=64, max_length=64)
    expected_revision: int = Field(ge=0)


class SavedProposalContext(Entity):
    entity_type: Literal["modelProposalContext"] = "modelProposalContext"
    endpoint_id: str
    actor_object_id: str
    revision: int
    context: ProposalContext
    context_digest: str
    evidence_run_id: str
    evidence_digest: str


class LifecyclePublication(MosaicModel):
    gateway_id: str
    deployment_name: str
    status: str
    applied_api_recorded: bool


class ProposalSummary(MosaicModel):
    id: str
    model: DeploymentModelRef
    listed: bool
    change: CatalogChange
    deployment_status: Literal["observed", "notObserved", "unconfirmed"]
    context_revision: int = 0


class ProposalList(MosaicModel):
    supported: bool
    latest_attempt: DiscoveryRun | None = None
    last_complete: DiscoveryRun | None = None
    evidence_current: bool = False
    items: list[ProposalSummary] = Field(default_factory=list)


class ProposalDetail(ProposalSummary):
    evidence: DiscoveryRun
    evidence_digest: str
    evidence_current: bool
    lifecycle_status: str | None = None
    deployments: list[LifecycleDeployment]
    publications: list[LifecyclePublication]
    saved_context: SavedProposalContext | None = None
    context_status: Literal["notSaved", "incomplete", "recorded", "outOfDate"] = "notSaved"
    context_notes: list[str] = Field(default_factory=list)
    runtime_freshness: str | None = None
    runtime_checked_at: datetime | None = None
    runtime_rules_revision: str | None = None


LifecycleRecord = DiscoveryHead | DiscoveryRun | DiscoveryRow | SavedProposalContext
