"""Additive approval contracts; neither policies nor model recommendations apply gateway plans."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from mosaic_api.domain import (
    AccessRequest,
    AccessRequestApproval,
    EntitlementEnforcement,
    EntitlementResource,
    EntitlementResourceKind,
    Entity,
    MosaicModel,
    ResourceSummary,
)
from mosaic_api.scheduling import UtcInstant, WeeklySchedule

MAX_BULK_APPROVALS = 100
APPROVAL_EXECUTION_CONCURRENCY = 4
POLICY_SYSTEM_ACTOR = "system:approval-policies"
APPROVAL_EVALUATOR_VERSION = "1"
APPROVAL_PROMPT_VERSION = "1"

RecordId = Annotated[str, Field(min_length=1, max_length=256)]
EnvironmentKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{1,31}$")]
BulkApprovalStatus = Literal[
    "approved", "alreadyApproved", "conflict", "validationFailed", "failed"
]
ApprovalOrigin = Literal["manual", "bulk", "policy"]


class ApprovalBlocker(MosaicModel):
    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=1000)
    next_action: str | None = Field(default=None, max_length=1000)


class ApprovalContextRevision(MosaicModel):
    """Opaque context fingerprint plus source versions; execution must re-read and compare."""

    context_revision: RecordId
    request_etag: str | None = None
    resource_etag: str | None = None
    cost_center_etag: str | None = None
    environment_revision: str | None = None
    eligibility_checked_at: UtcInstant | None = None


class ApprovalPreview(MosaicModel):
    request: AccessRequest
    resource_summary: ResourceSummary | None = None
    revision: ApprovalContextRevision
    current_environment: str | None = None
    production: bool | None = None
    effective_cost_center_id: str | None = None
    effective_enforcement: EntitlementEnforcement | None = None
    limits_source: Literal["explicit", "costCenter", "unrestricted"] = "unrestricted"
    existing_entitlement_id: str | None = None
    mutation_locked: bool = False
    blockers: list[ApprovalBlocker] = Field(default_factory=list)
    requires_plan_apply: Literal[True] = True


class BulkApprovalPreviewInput(MosaicModel):
    request_ids: list[RecordId] = Field(min_length=1, max_length=MAX_BULK_APPROVALS)

    @model_validator(mode="after")
    def _unique_requests(self) -> Self:
        if len(set(self.request_ids)) != len(self.request_ids):
            raise ValueError("Each request ID may appear only once")
        return self


class BulkApprovalItem(MosaicModel):
    request_id: RecordId
    approval: AccessRequestApproval = Field(default_factory=AccessRequestApproval)
    expected_context: ApprovalContextRevision | None = None


class BulkApprovalInput(MosaicModel):
    idempotency_key: RecordId
    items: list[BulkApprovalItem] = Field(min_length=1, max_length=MAX_BULK_APPROVALS)

    @model_validator(mode="after")
    def _unique_requests(self) -> Self:
        ids = [item.request_id for item in self.items]
        if len(set(ids)) != len(ids):
            raise ValueError("Each request ID may appear only once")
        return self


class BulkApprovalUnavailableItem(MosaicModel):
    request_id: RecordId
    blocker: ApprovalBlocker


class BulkApprovalPreview(MosaicModel):
    items: list[ApprovalPreview] = Field(default_factory=list, max_length=MAX_BULK_APPROVALS)
    unavailable: list[BulkApprovalUnavailableItem] = Field(default_factory=list)


class BulkApprovalItemResult(MosaicModel):
    request_id: RecordId
    status: BulkApprovalStatus
    entitlement_id: str | None = None
    message: str | None = Field(default=None, max_length=1000)
    next_action: str | None = Field(default=None, max_length=1000)
    requires_plan_apply: bool = False

    @model_validator(mode="after")
    def _successful_grant(self) -> Self:
        if self.status in {"approved", "alreadyApproved"}:
            if not self.entitlement_id:
                raise ValueError("A successful approval identifies its winning grant")
            self.requires_plan_apply = True
        return self


class BulkApprovalResult(MosaicModel):
    batch_id: RecordId
    idempotency_key: RecordId
    items: list[BulkApprovalItemResult] = Field(min_length=1, max_length=MAX_BULK_APPROVALS)

    @model_validator(mode="after")
    def _one_result_per_request(self) -> Self:
        ids = [item.request_id for item in self.items]
        if len(set(ids)) != len(ids):
            raise ValueError("Each request has exactly one batch outcome")
        return self


class BulkApprovalRecord(Entity):
    entity_type: Literal["bulkApprovalRecord"] = "bulkApprovalRecord"
    actor_object_id: RecordId
    input_fingerprint: RecordId
    result: BulkApprovalResult


class ApprovalPolicyScope(MosaicModel):
    """Empty resource/cost-center lists mean any in the explicitly named environments."""

    environments: list[EnvironmentKey] = Field(min_length=1, max_length=30)
    resource_kinds: list[EntitlementResourceKind] = Field(min_length=1, max_length=5)
    resource_ids: list[RecordId] = Field(default_factory=list, max_length=100)
    cost_center_ids: list[RecordId] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def _classified_environments(self) -> Self:
        if any(value.strip().casefold() == "unclassified" for value in self.environments):
            raise ValueError("Unclassified resources require manual review")
        return self


class ApprovalGrantTemplate(MosaicModel):
    """Automation always names bounded limits; manual approval retains existing defaults."""

    enforcement: EntitlementEnforcement
    cost_center_id: RecordId | None = None
    note: str | None = Field(default=None, max_length=1000)


class ApprovalPolicyInput(MosaicModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    enabled: bool = False
    scope: ApprovalPolicyScope
    production_opt_in: bool = False
    schedule: WeeklySchedule
    minimum_request_age_seconds: int = Field(default=3600, ge=0, le=31_536_000, strict=True)
    grant_template: ApprovalGrantTemplate

    @model_validator(mode="after")
    def _compatible_template(self) -> Self:
        if (
            "mcpServer" in self.scope.resource_kinds
            and self.grant_template.enforcement.tokens is not None
        ):
            raise ValueError("MCP policies cannot configure token limits")
        return self


class ApprovalPolicy(ApprovalPolicyInput, Entity):
    entity_type: Literal["approvalPolicy"] = "approvalPolicy"
    revision: int = Field(default=1, ge=1)
    created_by: str | None = None
    updated_by: str | None = None


class ApprovalPolicySaveInput(ApprovalPolicyInput):
    expected_revision: int | None = Field(default=None, ge=1)
    enable_confirmed: bool = False


class ApprovalEvaluatorUpdate(MosaicModel):
    paused: bool


class ApprovalPolicyDryRun(MosaicModel):
    request_ids: list[RecordId] = Field(min_length=1, max_length=MAX_BULK_APPROVALS)
    policy: ApprovalPolicyInput | None = None


class ApprovalDecisionProvenance(MosaicModel):
    origin: ApprovalOrigin = "manual"
    actor_object_id: RecordId
    batch_id: str | None = None
    policy_id: str | None = None
    policy_revision: int | None = Field(default=None, ge=1)
    evaluator_version: str = APPROVAL_EVALUATOR_VERSION
    context_revision: str | None = None
    matched_facts: list[str] = Field(default_factory=list, max_length=30)
    effective_cost_center_id: str | None = None
    effective_enforcement: EntitlementEnforcement | None = None

    @model_validator(mode="after")
    def _policy_attribution(self) -> Self:
        if self.origin == "policy" and (
            self.actor_object_id != POLICY_SYSTEM_ACTOR
            or not self.policy_id
            or self.policy_revision is None
            or not self.context_revision
        ):
            raise ValueError(
                "Policy decisions require system attribution and policy/context versions"
            )
        return self


class ApprovalPolicyEvaluation(Entity):
    entity_type: Literal["approvalPolicyEvaluation"] = "approvalPolicyEvaluation"
    request_id: RecordId
    context: ApprovalContextRevision
    policy_id: RecordId
    policy_revision: int = Field(ge=1)
    outcome: Literal["eligible", "notMatched", "waiting", "blocked", "ambiguous"]
    evaluated_at: UtcInstant
    next_eligible_at: UtcInstant | None = None
    explanation: str = Field(max_length=2000)
    blockers: list[ApprovalBlocker] = Field(default_factory=list)
    evaluator_version: str = APPROVAL_EVALUATOR_VERSION


class ApprovalEvaluatorState(Entity):
    entity_type: Literal["approvalEvaluatorState"] = "approvalEvaluatorState"
    paused: bool = True
    last_started_at: UtcInstant | None = None
    last_succeeded_at: UtcInstant | None = None
    pending_count: int = Field(default=0, ge=0)
    last_error: str | None = Field(default=None, max_length=1000)
    checkpoint: str | None = None


class AiApprovalSettingsInput(MosaicModel):
    enabled: bool = False
    resource: EntitlementResource | None = None
    caller_principal_id: RecordId | None = None
    entitlement_id: RecordId | None = None
    cost_center_id: RecordId | None = None
    operation: Literal["chatCompletions", "responses"] = "chatCompletions"
    max_input_characters: int = Field(default=12000, ge=1000, le=32000)
    max_output_tokens: int = Field(default=1000, ge=100, le=4000)
    timeout_seconds: int = Field(default=30, ge=1, le=120)
    concurrency: int = Field(default=2, ge=1, le=4)
    max_batch_size: int = Field(default=10, ge=1, le=20)

    @model_validator(mode="after")
    def _governed_caller(self) -> Self:
        if self.resource is not None and self.resource.kind not in {"modelApi", "poolModel"}:
            raise ValueError("AI assistance requires an existing governed model offering")
        if self.enabled and not all(
            (self.resource, self.caller_principal_id, self.entitlement_id, self.cost_center_id)
        ):
            raise ValueError(
                "AI assistance requires a model, dedicated caller, grant, and cost center"
            )
        return self


class AiApprovalSettings(AiApprovalSettingsInput, Entity):
    entity_type: Literal["aiApprovalSettings"] = "aiApprovalSettings"
    revision: int = Field(default=1, ge=1)
    updated_by: str | None = None
    prompt_version: str = APPROVAL_PROMPT_VERSION
    verified_at: UtcInstant | None = None
    verified_context_revision: str | None = None


class AiApprovalSettingsSaveInput(AiApprovalSettingsInput):
    expected_revision: int | None = Field(default=None, ge=1)


class AiApprovalReadiness(MosaicModel):
    ready: bool = False
    configured: bool = False
    connection_verified: bool = False
    response_contract_verified: bool = False
    blockers: list[ApprovalBlocker] = Field(default_factory=list)
    checked_at: UtcInstant
    last_successful_call_at: UtcInstant | None = None
    last_error_at: UtcInstant | None = None
    last_error_code: str | None = None
    remediation_links: list[str] = Field(
        default_factory=lambda: ["/identities", "/entitlements", "/models"]
    )


class AiApprovalOperationState(Entity):
    entity_type: Literal["aiApprovalOperationState"] = "aiApprovalOperationState"
    last_successful_call_at: UtcInstant | None = None
    last_error_at: UtcInstant | None = None
    last_error_code: str | None = None


class AiApprovalOffering(MosaicModel):
    resource: EntitlementResource
    summary: ResourceSummary
    supported_operations: list[Literal["chatCompletions", "responses"]] = Field(
        default_factory=list
    )
    compatible: bool = False
    blockers: list[ApprovalBlocker] = Field(default_factory=list)


class AiApprovalBatchInput(MosaicModel):
    request_ids: list[RecordId] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def _unique_requests(self) -> Self:
        if len(set(self.request_ids)) != len(self.request_ids):
            raise ValueError("Each request ID may appear only once")
        return self


class AiApprovalItemResult(MosaicModel):
    request_id: RecordId
    recommendation: "AiApprovalRecommendation | None" = None
    blocker: ApprovalBlocker | None = None


class AiRecommendationReason(MosaicModel):
    explanation: str = Field(min_length=1, max_length=1000)
    evidence_ids: list[RecordId] = Field(min_length=1, max_length=20)


class AiRecommendationOutput(MosaicModel):
    """Strict advisory model response. No mutation, approval inputs, URLs, or recipients."""

    suggested_outcome: Literal["recommendApprove", "needsReview", "recommendDeny"]
    reasons: list[AiRecommendationReason] = Field(min_length=1, max_length=10)
    missing_information: list[Annotated[str, Field(max_length=500)]] = Field(
        default_factory=list, max_length=20
    )
    risk_flags: list[Annotated[str, Field(max_length=500)]] = Field(
        default_factory=list, max_length=20
    )


class AiApprovalRecommendation(Entity):
    entity_type: Literal["aiApprovalRecommendation"] = "aiApprovalRecommendation"
    request_id: RecordId
    context: ApprovalContextRevision
    config_revision: int = Field(ge=1)
    model_resource: EntitlementResource
    prompt_version: str = APPROVAL_PROMPT_VERSION
    generated_at: UtcInstant
    expires_at: UtcInstant
    recommendation: AiRecommendationOutput
    deterministic_blockers: list[ApprovalBlocker] = Field(default_factory=list)
    generated_by_object_id: str | None = None
    model_context_revision: str | None = None

    @model_validator(mode="after")
    def _expiry(self) -> Self:
        if self.expires_at <= self.generated_at:
            raise ValueError("Recommendations must expire after generation")
        return self
