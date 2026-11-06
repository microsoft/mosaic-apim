"""Administrator views for scoped Entra setup and direct membership operations."""

import base64
import binascii
from typing import Literal

from pydantic import Field

from mosaic_api.cost_center_directory import CostCenterDirectoryOperation, DirectoryOperationState
from mosaic_api.cost_centers import CostCenterView
from mosaic_api.domain import MosaicModel, PrincipalKind
from mosaic_api.errors import ValidationError

MEMBER_LISTING_NOTICE = (
    "Direct members only; nested group members are not flattened into this editor. "
    "Listing uses Microsoft Graph beta, the documented workaround for v1.0 omitting service "
    "principals. Beta is a preview and is not supported for production applications."
)
PROPAGATION_NOTICE = (
    "Directory membership and token refresh can take time. Already-issued tokens retain their "
    "claims until expiry; managed identities may cache tokens or authorization for around "
    "24 hours. A directory update does not immediately invalidate tokens."
)
KEY_REVOCATION_NOTICE = (
    "Individual access and keys require an eligibility recheck and, where needed, reviewed "
    "gateway revoke/apply. Removing directory membership does not itself revoke an issued key."
)


class CostCenterGroupCapabilities(MosaicModel):
    available: bool
    directory_tenant_id: str | None = None
    can_read_groups: bool = False
    can_create_groups: bool = False
    can_add_users: bool = False
    can_add_service_principals: bool = False
    can_remove_members: bool = False
    application_permissions: list[str] = Field(default_factory=list)
    advisory: Literal[True] = True
    member_listing_api: Literal["beta"] = "beta"
    member_listing_notice: str = MEMBER_LISTING_NOTICE
    message: str | None = None
    error_code: str | None = None


class CostCenterDirectoryOperationView(CostCenterDirectoryOperation):
    directory_updated: bool = False
    can_retry: bool = False
    propagation_notice: str | None = None
    key_revocation_notice: str | None = None
    recovery_notice: str | None = None


def directory_operation_view(
    operation: CostCenterDirectoryOperation,
) -> CostCenterDirectoryOperationView:
    uncertain = operation.state in {
        DirectoryOperationState.PENDING,
        DirectoryOperationState.UNKNOWN,
    }
    member_change = operation.kind != "createGroup"
    observed_change = operation.state == DirectoryOperationState.SUCCEEDED or uncertain
    recovery_notice = None
    if uncertain:
        recovery_notice = (
            "The outcome is not confirmed. Recover checks recorded identity or direct "
            "membership; it never repeats this write."
        )
        if operation.state == DirectoryOperationState.PENDING and member_change:
            recovery_notice = (
                "This write has no recorded outcome and may still be in flight. Recover does "
                "not replay or settle a pending write. If execution was interrupted, an "
                "administrator must investigate in Entra before any further membership write."
            )
        if operation.kind == "createGroup" and operation.group_object_id is None:
            recovery_notice = (
                "The create outcome has no recorded group identity. Do not retry creation or "
                "attach a display-name match. An administrator must investigate in Entra; "
                "MOSAIC cannot safely identify or recreate this group automatically."
            )
    return CostCenterDirectoryOperationView.model_validate(
        {
            **operation.model_dump(by_alias=False),
            "etag": operation.etag,
            "directory_updated": operation.state == DirectoryOperationState.SUCCEEDED,
            "can_retry": operation.state == DirectoryOperationState.FAILED,
            "propagation_notice": (
                PROPAGATION_NOTICE if member_change and observed_change else None
            ),
            "key_revocation_notice": (
                KEY_REVOCATION_NOTICE
                if operation.kind == "removeMember" and observed_change
                else None
            ),
            "recovery_notice": recovery_notice,
        }
    )


class CostCenterManagementView(CostCenterView):
    # Detail only. Listing centers does not fan out into Graph or operation history.
    directory_operations: list[CostCenterDirectoryOperationView] = Field(default_factory=list)


class CostCenterDirectoryMemberAdd(MosaicModel):
    # Checked against server-side directory lookup, never trusted as permission evidence.
    member_kind: PrincipalKind


class CostCenterDirectMember(MosaicModel):
    object_id: str
    kind: PrincipalKind | None = None
    odata_type: str
    display_name: str | None = None
    detail: str | None = None
    app_id: str | None = None
    membership_type: Literal["direct"] = "direct"


class CostCenterDirectoryMembers(MosaicModel):
    directory_tenant_id: str
    group_object_id: str
    members: list[CostCenterDirectMember] = Field(default_factory=list)
    next_cursor: str | None = None
    api_version: Literal["beta"] = "beta"
    preview_notice: str = MEMBER_LISTING_NOTICE


def member_cursor(next_link: str | None) -> str | None:
    return (
        base64.urlsafe_b64encode(next_link.encode("utf-8")).decode("ascii")
        if next_link is not None
        else None
    )


def member_next_link(cursor: str | None) -> str | None:
    if cursor is None:
        return None
    try:
        return base64.b64decode(cursor, altchars=b"-_", validate=True).decode("utf-8")
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise ValidationError("Invalid directory membership paging cursor") from exc
