from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Self
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def deterministic_id(prefix: str, *parts: str) -> str:
    value = "|".join(part.casefold() for part in parts)
    return f"{prefix}_{uuid5(NAMESPACE_URL, value).hex}"


class MosaicModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
        use_enum_values=True,
    )


class Entity(MosaicModel):
    id: str
    tenant_id: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    etag: str | None = Field(default=None, exclude=True)


class PrincipalKind(StrEnum):
    USER = "user"
    SERVICE_PRINCIPAL = "servicePrincipal"
    MANAGED_IDENTITY = "managedIdentity"


class Principal(Entity):
    entity_type: Literal["principal"] = "principal"
    object_id: str
    kind: PrincipalKind
    label: str | None = None


class Group(Entity):
    entity_type: Literal["group"] = "group"
    name: str
    description: str | None = None


class GroupMembership(Entity):
    entity_type: Literal["groupMembership"] = "groupMembership"
    group_id: str
    principal_id: str


class FoundryConnection(Entity):
    entity_type: Literal["foundryConnection"] = "foundryConnection"
    name: str
    endpoint: AnyHttpUrl
    azure_resource_id: str
    credential_reference_id: str


class CatalogModel(Entity):
    entity_type: Literal["catalogModel"] = "catalogModel"
    provider: str
    model_name: str
    model_version: str | None = None


class ModelDeployment(Entity):
    entity_type: Literal["modelDeployment"] = "modelDeployment"
    foundry_connection_id: str
    catalog_model_id: str | None = None
    deployment_name: str
    endpoint: AnyHttpUrl


class TokenEnforcement(MosaicModel):
    counter_key_expression: str
    tokens_per_minute: int | None = Field(default=None, ge=1)
    token_quota: int | None = Field(default=None, ge=1)
    token_quota_period: Literal["Hourly", "Daily", "Weekly", "Monthly", "Yearly"] | None = None
    estimate_prompt_tokens: bool = True

    @model_validator(mode="after")
    def validate_limits(self) -> Self:
        if self.tokens_per_minute is None and self.token_quota is None:
            raise ValueError("At least one token rate or quota must be configured")
        if self.token_quota is not None and self.token_quota_period is None:
            raise ValueError("A token quota period is required when a quota is configured")
        if self.token_quota is None and self.token_quota_period is not None:
            raise ValueError("A token quota is required when a quota period is configured")
        return self


class Entitlement(Entity):
    entity_type: Literal["entitlement"] = "entitlement"
    group_id: str
    model_deployment_id: str
    enabled: bool = True
    enforcement: TokenEnforcement


class CredentialReference(Entity):
    entity_type: Literal["credentialReference"] = "credentialReference"
    name: str
    secret_uri: AnyHttpUrl


class PolicyRevision(Entity):
    entity_type: Literal["policyRevision"] = "policyRevision"
    entitlement_id: str
    revision: int = Field(ge=1)
    content_sha256: str
    policy_xml: str


class SyncStatus(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class SyncOperation(Entity):
    entity_type: Literal["syncOperation"] = "syncOperation"
    status: SyncStatus
    desired_revision: str
    plan: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    completed_at: datetime | None = None


class AuditEvent(Entity):
    entity_type: Literal["auditEvent"] = "auditEvent"
    action: str
    resource_type: str
    resource_id: str
    actor_object_id: str
    details: dict[str, Any] = Field(default_factory=dict)


class PrincipalCreate(MosaicModel):
    object_id: str = Field(min_length=1, max_length=128)
    kind: PrincipalKind
    label: str | None = Field(default=None, max_length=200)


class PrincipalUpdate(MosaicModel):
    kind: PrincipalKind | None = None
    label: str | None = Field(default=None, max_length=200)

    @field_validator("kind")
    @classmethod
    def kind_cannot_be_null(cls, value: PrincipalKind | None) -> PrincipalKind:
        if value is None:
            raise ValueError("kind cannot be null")
        return value


class GroupCreate(MosaicModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)


class GroupUpdate(MosaicModel):
    description: str | None = Field(default=None, max_length=1000)


class PolicyPreviewRequest(MosaicModel):
    enforcement: TokenEnforcement
    backend_resource: str = "https://cognitiveservices.azure.com"


class PolicyPreview(MosaicModel):
    policy_xml: str
    content_sha256: str
    warnings: list[str] = Field(default_factory=list)
