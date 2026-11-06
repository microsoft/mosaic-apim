"""Tenant-bound Entra identities and durable cost-center directory write intents.

These are not a directory roster. A pending intent must commit before Graph is called; an
unknown outcome must be reconciled, never replayed as another create or membership write.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from mosaic_api.domain import (
    Entity,
    MosaicModel,
    PrincipalKind,
    deterministic_id,
    general_cost_center_id,
)


def directory_object_id(value: str) -> str:
    """Canonical GUIDs, not MOSAIC local group identifiers or display names."""

    try:
        return str(UUID(value.strip()))
    except ValueError as exc:
        raise ValueError("An Entra directory or object ID must be a GUID") from exc


class EntraGroupProvenance(StrEnum):
    ATTACHED = "attached"
    CREATED = "created"


class EntraGroupVerificationState(StrEnum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    FAILED = "failed"


class EntraGroupBinding(MosaicModel):
    directory_tenant_id: str
    group_object_id: str
    provenance: EntraGroupProvenance
    verification_state: EntraGroupVerificationState = EntraGroupVerificationState.UNVERIFIED
    verified_at: datetime | None = None
    verification_error: str | None = Field(default=None, max_length=2000)

    _directory_ids = field_validator("directory_tenant_id", "group_object_id")(directory_object_id)

    @model_validator(mode="after")
    def validate_verification(self) -> Self:
        if self.verification_state == EntraGroupVerificationState.VERIFIED:
            if self.verified_at is None:
                raise ValueError("A verified Entra group binding needs verifiedAt")
            if self.verification_error is not None:
                raise ValueError("A verified Entra group binding cannot have a verification error")
        return self

    @property
    def is_verified(self) -> bool:
        return (
            self.verification_state == EntraGroupVerificationState.VERIFIED
            and self.verified_at is not None
        )

    @property
    def identity(self) -> tuple[str, str]:
        return self.directory_tenant_id, self.group_object_id


class AttachCostCenterGroup(MosaicModel):
    mode: Literal["attach"] = "attach"
    group_object_id: str
    acknowledge_membership_changes: Literal[True]

    _group_id = field_validator("group_object_id")(directory_object_id)


class CreateCostCenterGroup(MosaicModel):
    mode: Literal["create"] = "create"
    # None means use the saved center's name, not a directory display-name lookup.
    display_name: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("display_name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("The group display name cannot be blank")
        return None if value is None else value.strip()


CostCenterGroupSetup = Annotated[
    AttachCostCenterGroup | CreateCostCenterGroup, Field(discriminator="mode")
]


class CostCenterSetupState(StrEnum):
    SETUP_REQUIRED = "setupRequired"
    READY = "ready"
    EXEMPT = "exempt"


def cost_center_group_reservation_id(
    tenant_id: str, directory_tenant_id: str, group_object_id: str
) -> str:
    return deterministic_id(
        "costCenterGroupReservation",
        tenant_id,
        directory_object_id(directory_tenant_id),
        directory_object_id(group_object_id),
    )


class CostCenterGroupReservation(Entity):
    """Exclusive ownership within a MOSAIC tenant, retained even if its center is deleted."""

    entity_type: Literal["costCenterGroupReservation"] = "costCenterGroupReservation"
    cost_center_id: str = Field(min_length=1, max_length=128)
    directory_tenant_id: str
    group_object_id: str

    _directory_ids = field_validator("directory_tenant_id", "group_object_id")(directory_object_id)

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.cost_center_id == general_cost_center_id(self.tenant_id):
            raise ValueError("General has no Entra group reservation")
        expected = cost_center_group_reservation_id(
            self.tenant_id, self.directory_tenant_id, self.group_object_id
        )
        if self.id != expected:
            raise ValueError("A group reservation ID must name its directory and group identity")
        return self

    @property
    def identity(self) -> tuple[str, str]:
        return self.directory_tenant_id, self.group_object_id


class DirectoryOperationState(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class DirectoryOperationErrorDetails(MosaicModel):
    http_status: int | None = Field(default=None, ge=100, le=599)
    graph_code: str | None = Field(default=None, max_length=256)
    graph_message: str | None = Field(default=None, max_length=2000)
    request_id: str | None = Field(default=None, max_length=256)
    client_request_id: str | None = Field(default=None, max_length=256)
    retry_after: int | None = Field(default=None, ge=0, le=300)


def cost_center_provisioning_operation_id(tenant_id: str, cost_center_id: str) -> str:
    """One provisioning record per center; unknown creates cannot admit a second POST."""

    return deterministic_id("costCenterGroupProvisioning", tenant_id, cost_center_id)


def cost_center_member_operation_id(
    tenant_id: str, cost_center_id: str, idempotency_key: str
) -> str:
    if not idempotency_key.strip():
        raise ValueError("A membership operation needs an idempotency key")
    return deterministic_id("costCenterMemberOperation", tenant_id, cost_center_id, idempotency_key)


class CostCenterDirectoryOperation(Entity):
    """A small write intent/result, not permission to replay Graph after a timeout.

    Only the successful creator of a pending record may execute it. A known, definitive failure
    can be explicitly retried with a conditional failed -> pending write. Succeeded is terminal;
    unknown permits only reconciliation to succeeded/failed, not another directory write.
    """

    entity_type: Literal["costCenterDirectoryOperation"] = "costCenterDirectoryOperation"
    cost_center_id: str = Field(min_length=1, max_length=128)
    kind: Literal["createGroup", "addMember", "removeMember"]
    directory_tenant_id: str
    # For member writes this is the immutable target. For create it is the observed result.
    group_object_id: str | None = None
    member_object_id: str | None = None
    member_kind: PrincipalKind | None = None
    display_name: str | None = Field(default=None, min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=1000)
    requested_by: str = Field(min_length=1, max_length=256)
    state: DirectoryOperationState = DirectoryOperationState.PENDING
    error_code: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=2000)
    error_details: DirectoryOperationErrorDetails | None = None

    _directory_id = field_validator("directory_tenant_id")(directory_object_id)

    @field_validator("group_object_id", "member_object_id")
    @classmethod
    def validate_optional_id(cls, value: str | None) -> str | None:
        return None if value is None else directory_object_id(value)

    @model_validator(mode="after")
    def validate_operation(self) -> Self:
        if self.kind == "createGroup":
            if self.id != cost_center_provisioning_operation_id(
                self.tenant_id, self.cost_center_id
            ):
                raise ValueError("Group provisioning has one stable operation ID per cost center")
            if self.member_object_id is not None or self.member_kind is not None:
                raise ValueError("Group provisioning cannot carry a membership target")
            if self.display_name is None or not self.display_name.strip():
                raise ValueError("Group provisioning needs a display name")
            if self.state == DirectoryOperationState.SUCCEEDED and self.group_object_id is None:
                raise ValueError("Successful group provisioning needs the observed group object ID")
            if self.state in {DirectoryOperationState.PENDING, DirectoryOperationState.FAILED}:
                if self.group_object_id is not None:
                    raise ValueError(
                        "A known created group cannot be treated as safe to create again"
                    )
        else:
            if self.group_object_id is None or self.member_object_id is None:
                raise ValueError("A membership operation needs group and member object IDs")
            if self.display_name is not None or self.description is not None:
                raise ValueError("A membership operation cannot carry group creation parameters")
        if self.state in {DirectoryOperationState.PENDING, DirectoryOperationState.SUCCEEDED}:
            if (
                self.error_code is not None
                or self.error_message is not None
                or self.error_details is not None
            ):
                raise ValueError("Pending or successful operations cannot carry a directory error")
        return self

    def intent(self) -> tuple[object, ...]:
        """Immutable write parameters; a created group's observed ID is not a POST parameter."""

        return (
            self.tenant_id,
            self.cost_center_id,
            self.kind,
            self.directory_tenant_id,
            self.group_object_id if self.kind != "createGroup" else None,
            self.member_object_id,
            self.member_kind,
            self.display_name,
            self.description,
            self.requested_by,
        )

    def result(self) -> tuple[object, ...]:
        return (
            self.state,
            self.group_object_id,
            self.error_code,
            self.error_message,
            self.error_details,
        )
