"""Reviewed Azure model creation, separate from publishing a model through API Management."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, field_validator

from mosaic_api.domain import (
    DEPLOYMENT_NAME_PATTERN,
    Entity,
    ModelEndpoint,
    MosaicModel,
    deterministic_id,
    utc_now,
)

DEPLOYMENT_ACTIONS: tuple[str, ...] = (
    "Microsoft.CognitiveServices/accounts/read",
    "Microsoft.CognitiveServices/accounts/models/read",
    "Microsoft.CognitiveServices/accounts/deployments/read",
    "Microsoft.CognitiveServices/accounts/deployments/write",
)


def deployment_lock_scope(endpoint: ModelEndpoint) -> str:
    account = endpoint.arm_resource().account_scope if endpoint.azure_resource_id else endpoint.id
    return deterministic_id("endpointdeployment", endpoint.azure_cloud, account)


class DeploymentAccess(MosaicModel):
    can_deploy: bool = False
    evaluation: Literal["allowed", "denied", "unknown", "unsupported"] = "unknown"
    scope: str | None = None
    checked_at: datetime = Field(default_factory=utc_now)
    required_actions: list[str] = Field(default_factory=lambda: list(DEPLOYMENT_ACTIONS))
    missing_actions: list[str] = Field(default_factory=list)
    message: str


class DeploymentModelRef(MosaicModel):
    model_format: str = Field(min_length=1, max_length=120)
    model_name: str = Field(min_length=1, max_length=120)
    model_version: str = Field(min_length=1, max_length=120)
    model_publisher: str | None = Field(default=None, max_length=120)
    source: str | None = Field(default=None, max_length=2048)
    source_account: str | None = Field(default=None, max_length=2048)

    def azure_payload(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "format": self.model_format,
                "name": self.model_name,
                "version": self.model_version,
                "publisher": self.model_publisher,
                "source": self.source,
                "sourceAccount": self.source_account,
            }.items()
            if value is not None
        }


class DeploymentCapacity(MosaicModel):
    minimum: int | None = None
    maximum: int | None = None
    step: int | None = None
    default: int | None = None
    allowed_values: list[int] = Field(default_factory=list)


class DeploymentQuota(MosaicModel):
    usage_name: str
    current_value: float = Field(ge=0, allow_inf_nan=False)
    limit: float = Field(ge=0, allow_inf_nan=False)
    available: float = Field(ge=0, allow_inf_nan=False)
    unit: str | None = None
    blocked: bool = False


class DeploymentSku(MosaicModel):
    name: str
    capacity: DeploymentCapacity
    quota: DeploymentQuota | None = None
    deprecation_date: str | None = None


class DeployableModel(DeploymentModelRef):
    is_default_version: bool = False
    lifecycle_status: str | None = None
    deprecation_inference: str | None = None
    capabilities: dict[str, str] = Field(default_factory=dict)
    skus: list[DeploymentSku] = Field(default_factory=list)

    def reference(self) -> DeploymentModelRef:
        return DeploymentModelRef.model_validate(
            self.model_dump(include=set(DeploymentModelRef.model_fields))
        )


class DeploymentTarget(MosaicModel):
    endpoint_id: str
    endpoint_name: str
    account_scope: str
    azure_cloud: Literal["commercial", "government"]
    azure_connection_id: str | None = None
    subscription_id: str
    resource_group: str
    account_name: str
    location: str


class DeploymentCatalogue(MosaicModel):
    endpoint_id: str
    blocking_run_id: str | None = None
    target: DeploymentTarget | None = None
    access: DeploymentAccess
    checked_at: datetime = Field(default_factory=utc_now)
    models: list[DeployableModel] = Field(default_factory=list)
    rai_policies: list[str] = Field(default_factory=list)
    quota_error: str | None = None
    policy_error: str | None = None


class ModelDeploymentCreate(MosaicModel):
    deployment_name: str = Field(min_length=1, max_length=64)
    model: DeploymentModelRef
    sku_name: str = Field(min_length=1, max_length=120)
    capacity: int = Field(strict=True, ge=1)
    rai_policy_name: str | None = Field(default=None, min_length=1, max_length=120)
    version_upgrade_option: (
        Literal["OnceNewDefaultVersionAvailable", "OnceCurrentVersionExpired", "NoAutoUpgrade"]
        | None
    ) = None

    @field_validator("deployment_name")
    @classmethod
    def valid_deployment_name(cls, value: str) -> str:
        if not DEPLOYMENT_NAME_PATTERN.fullmatch(value):
            raise ValueError(
                "Use 1-64 letters, digits, dots, hyphens, or underscores, starting with a letter "
                "or digit."
            )
        return value

    def azure_payload(self) -> dict[str, Any]:
        properties: dict[str, Any] = {"model": self.model.azure_payload()}
        if self.rai_policy_name is not None:
            properties["raiPolicyName"] = self.rai_policy_name
        if self.version_upgrade_option is not None:
            properties["versionUpgradeOption"] = self.version_upgrade_option
        return {
            "sku": {"name": self.sku_name, "capacity": self.capacity},
            "properties": properties,
        }


class DeploymentChecks(MosaicModel):
    quota: DeploymentQuota
    available_capacity: float | None = None
    warnings: list[str] = Field(default_factory=list)


class ModelDeploymentPlan(Entity):
    entity_type: Literal["modelDeploymentPlan"] = "modelDeploymentPlan"
    endpoint_id: str
    target: DeploymentTarget
    configuration: ModelDeploymentCreate
    checks: DeploymentChecks
    input_digest: str
    actor_object_id: str


class DeploymentRunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class ModelDeploymentRun(Entity):
    entity_type: Literal["modelDeploymentRun"] = "modelDeploymentRun"
    endpoint_id: str
    plan_id: str
    target: DeploymentTarget
    configuration: ModelDeploymentCreate
    actor_object_id: str
    status: DeploymentRunStatus = DeploymentRunStatus.RUNNING
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    write_attempted: bool = False
    provisioning_state: str | None = None
    error: str | None = None
    refresh_status: Literal["notStarted", "running", "succeeded", "failed"] = "notStarted"
    refresh_error: str | None = None


class DeploymentRecoveryRequest(MosaicModel):
    confirm_worker_stopped: bool = False
    confirm_no_pending_operation: bool = False
