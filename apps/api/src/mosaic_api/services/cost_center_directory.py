"""Small durable directory workflows; Graph is never called while a Cosmos lease is held."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any, Literal, Protocol
from uuid import uuid4

from mosaic_api.cost_center_directory import (
    AttachCostCenterGroup,
    CostCenterDirectoryOperation,
    CostCenterGroupSetup,
    CreateCostCenterGroup,
    DirectoryOperationErrorDetails,
    DirectoryOperationState,
    EntraGroupBinding,
    EntraGroupProvenance,
    EntraGroupVerificationState,
    cost_center_member_operation_id,
    cost_center_provisioning_operation_id,
    directory_object_id,
)
from mosaic_api.cost_center_management import (
    CostCenterDirectMember,
    CostCenterDirectoryMembers,
    CostCenterDirectoryOperationView,
    CostCenterGroupCapabilities,
    directory_operation_view,
    member_cursor,
    member_next_link,
)
from mosaic_api.cost_centers import (
    COST_CENTERS_BUSY,
    COST_CENTERS_SCOPE,
    CostCenter,
    general_cost_center,
)
from mosaic_api.domain import (
    AuditEvent,
    Entitlement,
    PrincipalKind,
    general_cost_center_id,
    new_id,
    utc_now,
)
from mosaic_api.errors import (
    ChangeNotRecordedError,
    ConflictError,
    DirectoryDisabledError,
    DirectoryError,
    DirectoryForbiddenError,
    DomainError,
    NotFoundError,
    ValidationError,
)
from mosaic_api.integrations.graph import (
    DirectoryLookup,
    GraphGroup,
    GroupFailureKind,
    GroupManagement,
    GroupManagementError,
    GroupWriteOutcome,
    GroupWriteResult,
)
from mosaic_api.repositories import CostCenterRepository, GatewayRepository
from mosaic_api.services.directory import Actor
from mosaic_api.services.model_access import scope_lease


class DirectoryAccessChecks(Protocol):
    async def sync_cost_center_access(
        self, actor: Actor, cost_center_id: str
    ) -> list[Entitlement]: ...

    async def member_removed_from_cost_center(
        self,
        actor: Actor,
        cost_center_id: str,
        member_object_id: str,
        member_kind: PrincipalKind | None,
    ) -> None: ...


def _audit(
    actor: Actor, action: str, resource_id: str, details: dict[str, Any] | None = None
) -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id=actor.tenant_id,
        action=action,
        resource_type="costCenter",
        resource_id=resource_id,
        actor_object_id=actor.object_id,
        details=details or {},
    )


def _error_message(error: DomainError) -> str:
    parts = [error.message]
    if isinstance(error, GroupManagementError):
        if error.http_status is not None:
            parts.append(f"HTTP {error.http_status}")
        if error.graph_code:
            parts.append(error.graph_code)
        if error.graph_message:
            parts.append(error.graph_message)
    return "; ".join(parts)[:2000]


class CostCenterDirectoryService:
    def __init__(
        self,
        repository: CostCenterRepository,
        gateways: GatewayRepository,
        *,
        directory_lookup: DirectoryLookup | None,
        group_management: GroupManagement | None,
        directory_tenant_id: str | None,
        access_checks: DirectoryAccessChecks,
        unavailable_reason: str | None = None,
    ) -> None:
        self._repository = repository
        self._gateways = gateways
        self._directory = directory_lookup
        self._management = group_management
        self._tenant_id = directory_tenant_id or (
            group_management.tenant_id if group_management is not None else None
        )
        self._checks = access_checks
        self._unavailable_reason = unavailable_reason or (
            "Cost-center group management requires the configured commercial Entra directory. "
            "Local mode never substitutes a local membership roster."
        )

    @asynccontextmanager
    async def _lease(self, actor: Actor) -> AsyncIterator[None]:
        async with scope_lease(
            self._gateways, actor.tenant_id, COST_CENTERS_SCOPE, busy_message=COST_CENTERS_BUSY
        ):
            yield

    async def _stored(self, actor: Actor, cost_center_id: str) -> CostCenter:
        current = await self._repository.get_cost_center(actor.tenant_id, cost_center_id)
        if current is None:
            if cost_center_id == general_cost_center_id(actor.tenant_id):
                return general_cost_center(actor.tenant_id)
            raise NotFoundError("Cost center not found", details={"id": cost_center_id})
        return current

    def _check_actor(self, actor: Actor) -> None:
        if self._tenant_id is not None and actor.tenant_id.casefold() != self._tenant_id.casefold():
            raise DirectoryForbiddenError(
                "Cost-center directory actions are restricted to this tenant"
            )

    def _directory_id(self, actor: Actor) -> str:
        self._check_actor(actor)
        if self._tenant_id is None:
            raise DirectoryDisabledError(self._unavailable_reason)
        try:
            return directory_object_id(self._tenant_id)
        except ValueError as exc:
            raise DirectoryDisabledError(self._unavailable_reason) from exc

    def _management_for(self, actor: Actor) -> GroupManagement:
        directory_id = self._directory_id(actor)
        if self._management is None:
            raise DirectoryDisabledError(self._unavailable_reason)
        if self._management.tenant_id != directory_id:
            raise DirectoryForbiddenError(
                "The group adapter belongs to a different directory tenant"
            )
        return self._management

    @staticmethod
    def require_custom_center(current: CostCenter) -> None:
        if current.is_general:
            raise ValidationError(
                "General is exempt and cannot receive Entra group setup or members"
            )
        if current.group_id is not None or current.members:
            raise ConflictError(
                "This legacy local cost center requires explicit reconfiguration or recreation. "
                "Local groups and members are not Entra bindings.",
                details={"reason": "legacyLocalGroup", "costCenterId": current.id},
            )

    async def capabilities(self, actor: Actor) -> CostCenterGroupCapabilities:
        self._check_actor(actor)
        try:
            management = self._management_for(actor)
            capabilities = await management.capabilities()
            if capabilities.tenant_id != self._directory_id(actor):
                raise DirectoryForbiddenError("Capability information belongs to another tenant")
        except DomainError as exc:
            return CostCenterGroupCapabilities(
                available=False,
                directory_tenant_id=self._tenant_id,
                message=_error_message(exc),
                error_code=exc.code,
            )
        return CostCenterGroupCapabilities(
            available=True,
            directory_tenant_id=capabilities.tenant_id,
            can_read_groups=capabilities.can_read_groups,
            can_create_groups=capabilities.can_create_groups,
            can_add_users=capabilities.can_add_users,
            can_add_service_principals=capabilities.can_add_service_principals,
            can_remove_members=capabilities.can_remove_members,
            application_permissions=sorted(capabilities.application_permissions),
            message=(
                "These are advisory token-consent observations, not a guarantee of permission "
                "to edit a particular group. A verified group remains configured when editing "
                "is read-only. No group is created to test permissions."
            ),
        )

    async def setup_group(
        self, actor: Actor, cost_center_id: str, setup: CostCenterGroupSetup
    ) -> None:
        self.require_custom_center(await self._stored(actor, cost_center_id))
        directory_id = self._directory_id(actor)
        if isinstance(setup, AttachCostCenterGroup):
            async with self._lease(actor):
                current = await self._stored(actor, cost_center_id)
                self.require_custom_center(current)
                binding = current.entra_binding
                if binding is not None:
                    if (
                        binding.identity != (directory_id, setup.group_object_id)
                        or binding.provenance != EntraGroupProvenance.ATTACHED
                    ):
                        raise ConflictError("An existing Entra binding cannot be replaced")
                else:
                    await self._repository.save_cost_center(
                        current.model_copy(
                            update={
                                "entra_binding": EntraGroupBinding(
                                    directory_tenant_id=directory_id,
                                    group_object_id=setup.group_object_id,
                                    provenance=EntraGroupProvenance.ATTACHED,
                                ),
                                "updated_at": utc_now(),
                            }
                        ),
                        _audit(
                            actor,
                            "costCenter.groupAttached",
                            current.id,
                            {
                                "directoryTenantId": directory_id,
                                "groupObjectId": setup.group_object_id,
                                "acknowledgeMembershipChanges": True,
                            },
                        ),
                    )
            await self._verify_binding(actor, cost_center_id)
            return
        await self._provision_group(actor, cost_center_id, setup)

    async def _provision_group(
        self, actor: Actor, cost_center_id: str, setup: CreateCostCenterGroup
    ) -> None:
        directory_id = self._directory_id(actor)
        operation_id = cost_center_provisioning_operation_id(actor.tenant_id, cost_center_id)
        execute = False
        async with self._lease(actor):
            current = await self._stored(actor, cost_center_id)
            self.require_custom_center(current)
            operation = await self._repository.get_directory_operation(
                actor.tenant_id, operation_id
            )
            if operation is None:
                operation = CostCenterDirectoryOperation(
                    id=operation_id,
                    tenant_id=actor.tenant_id,
                    cost_center_id=current.id,
                    kind="createGroup",
                    directory_tenant_id=directory_id,
                    display_name=setup.display_name or current.name,
                    description=(
                        f"MOSAIC cost center {current.id}; provisioning operation {operation_id}."
                    ),
                    requested_by=actor.object_id,
                )
                operation = await self._repository.create_directory_operation(
                    operation, self._operation_audit(actor, operation, "requested")
                )
                execute = True
            elif (
                operation.kind != "createGroup"
                or operation.cost_center_id != cost_center_id
                or operation.directory_tenant_id != directory_id
                or (setup.display_name is not None and operation.display_name != setup.display_name)
            ):
                raise ConflictError("This provisioning operation has a different recorded intent")
        if execute:
            await self._execute(actor, operation)
        elif operation.group_object_id is not None:
            await self.recover(actor, cost_center_id, operation.id)
        # A repeated create is a status read. Failed operations need an explicit retry;
        # pending/unknown without identity never admit a second POST.

    @staticmethod
    def _assert_group(group: GraphGroup, directory_id: str, group_id: str) -> None:
        if group.tenant_id != directory_id or group.object_id != group_id:
            raise DirectoryForbiddenError("Graph returned a different directory/group identity")
        if group.management_unsupported_reasons:
            raise GroupManagementError(
                "; ".join(group.management_unsupported_reasons),
                kind=GroupFailureKind.UNSUPPORTED_GROUP,
            )

    async def _verify_binding(self, actor: Actor, cost_center_id: str) -> None:
        current = await self._stored(actor, cost_center_id)
        binding = current.entra_binding
        if binding is None:
            raise ConflictError("This cost center has no group identity to verify")
        if binding.is_verified:
            await self._checks.sync_cost_center_access(actor, cost_center_id)
            return
        try:
            management = self._management_for(actor)
            group = await management.validate_group(binding.group_object_id)
            self._assert_group(group, self._directory_id(actor), binding.group_object_id)
            error: DomainError | None = None
        except DomainError as exc:
            error = exc
        except Exception as exc:
            error = DirectoryError(f"Group verification could not complete: {exc}")
        async with self._lease(actor):
            # Operation-intent writes change the center's ETag, so never reuse a pre-I/O copy.
            current = await self._stored(actor, cost_center_id)
            if current.entra_binding is None or current.entra_binding.identity != binding.identity:
                raise ConflictError("The cost center's Entra identity changed during verification")
            if not current.entra_binding.is_verified:
                verified = binding.model_copy(
                    update={
                        "verification_state": (
                            EntraGroupVerificationState.VERIFIED
                            if error is None
                            else EntraGroupVerificationState.FAILED
                        ),
                        "verified_at": utc_now() if error is None else None,
                        "verification_error": None if error is None else _error_message(error),
                    }
                )
                await self._repository.save_cost_center(
                    current.model_copy(update={"entra_binding": verified, "updated_at": utc_now()}),
                    _audit(
                        actor,
                        "costCenter.groupVerified"
                        if error is None
                        else "costCenter.groupVerificationFailed",
                        current.id,
                        {
                            "directoryTenantId": binding.directory_tenant_id,
                            "groupObjectId": binding.group_object_id,
                            "errorCode": None if error is None else error.code,
                            "error": None if error is None else _error_message(error),
                        },
                    ),
                )
        if error is None:
            await self._checks.sync_cost_center_access(actor, cost_center_id)

    async def _bind_created_group(
        self, actor: Actor, operation: CostCenterDirectoryOperation
    ) -> None:
        if (
            operation.state != DirectoryOperationState.SUCCEEDED
            or operation.group_object_id is None
        ):
            raise ConflictError("Record the successful create identity before binding it")
        if operation.directory_tenant_id != self._directory_id(actor):
            raise DirectoryForbiddenError("The recorded create belongs to another directory")
        async with self._lease(actor):
            current = await self._stored(actor, operation.cost_center_id)
            self.require_custom_center(current)
            binding = current.entra_binding
            if binding is not None:
                if (
                    binding.identity != (operation.directory_tenant_id, operation.group_object_id)
                    or binding.provenance != EntraGroupProvenance.CREATED
                ):
                    raise ConflictError("The center does not match the recorded create identity")
            else:
                await self._repository.save_cost_center(
                    current.model_copy(
                        update={
                            "entra_binding": EntraGroupBinding(
                                directory_tenant_id=operation.directory_tenant_id,
                                group_object_id=operation.group_object_id,
                                provenance=EntraGroupProvenance.CREATED,
                            ),
                            "updated_at": utc_now(),
                        }
                    ),
                    _audit(
                        actor,
                        "costCenter.createdGroupBound",
                        current.id,
                        {"operationId": operation.id, "groupObjectId": operation.group_object_id},
                    ),
                )
        await self._verify_binding(actor, operation.cost_center_id)

    async def _bound_center(self, actor: Actor, cost_center_id: str) -> CostCenter:
        directory_id = self._directory_id(actor)
        current = await self._stored(actor, cost_center_id)
        self.require_custom_center(current)
        binding = current.entra_binding
        if binding is None or not binding.is_verified:
            raise ConflictError(
                "Finish and verify this cost center's Entra group setup first",
                details={"reason": "setupRequired", "costCenterId": cost_center_id},
            )
        if binding.directory_tenant_id != directory_id:
            raise DirectoryForbiddenError("The bound group belongs to another directory tenant")
        reservation = await self._repository.get_group_reservation(
            actor.tenant_id, directory_id, binding.group_object_id
        )
        if reservation is None or reservation.cost_center_id != current.id:
            raise ConflictError("The center does not exclusively reserve its bound Entra group")
        return current

    async def list_members(
        self,
        actor: Actor,
        cost_center_id: str,
        *,
        page_size: int = 100,
        cursor: str | None = None,
    ) -> CostCenterDirectoryMembers:
        current = await self._bound_center(actor, cost_center_id)
        group_id = current.verified_group_object_id
        assert group_id is not None
        management = self._management_for(actor)
        group = await management.validate_group(group_id)
        self._assert_group(group, self._directory_id(actor), group_id)
        page = await management.list_members(
            group_id, page_size=page_size, next_link=member_next_link(cursor)
        )
        if page.tenant_id != self._directory_id(actor) or page.group_object_id != group_id:
            raise DirectoryForbiddenError("The membership page belongs to another directory/group")
        return CostCenterDirectoryMembers(
            directory_tenant_id=page.tenant_id,
            group_object_id=page.group_object_id,
            members=[
                CostCenterDirectMember(
                    object_id=member.object_id,
                    kind=member.kind,
                    odata_type=member.odata_type,
                    display_name=member.display_name,
                    detail=member.detail,
                    app_id=member.app_id,
                )
                for member in page.members
            ],
            next_cursor=member_cursor(page.next_link),
        )

    async def add_member(
        self,
        actor: Actor,
        cost_center_id: str,
        object_id: str,
        member_kind: PrincipalKind,
        *,
        idempotency_key: str,
    ) -> CostCenterDirectoryOperationView:
        return await self._member_operation(
            actor, cost_center_id, object_id, member_kind, "addMember", idempotency_key
        )

    async def remove_member(
        self,
        actor: Actor,
        cost_center_id: str,
        object_id: str,
        *,
        idempotency_key: str,
    ) -> CostCenterDirectoryOperationView:
        # Limited-info or deleted objects can still have a removable direct relationship.
        member_kind = None
        await self._bound_center(actor, cost_center_id)
        if self._directory is not None:
            with suppress(DirectoryError, DirectoryForbiddenError):
                member = await self._directory.get_object(self._object_id(object_id))
                member_kind = member.kind if member is not None else None
        return await self._member_operation(
            actor, cost_center_id, object_id, member_kind, "removeMember", idempotency_key
        )

    @staticmethod
    def _object_id(value: str) -> str:
        try:
            return directory_object_id(value)
        except ValueError as exc:
            raise ValidationError("A directory member object ID must be a GUID") from exc

    async def _member_operation(
        self,
        actor: Actor,
        cost_center_id: str,
        object_id: str,
        member_kind: PrincipalKind | None,
        kind: Literal["addMember", "removeMember"],
        idempotency_key: str,
    ) -> CostCenterDirectoryOperationView:
        member_id = self._object_id(object_id)
        key = idempotency_key.strip()
        if not key or len(key) > 128:
            raise ValidationError("Use an Idempotency-Key containing 1 to 128 characters")
        operation_id = cost_center_member_operation_id(actor.tenant_id, cost_center_id, key)
        execute = False
        async with self._lease(actor):
            current = await self._bound_center(actor, cost_center_id)
            existing = await self._repository.get_directory_operation(actor.tenant_id, operation_id)
            if existing is not None:
                if (
                    existing.cost_center_id != cost_center_id
                    or existing.kind != kind
                    or existing.directory_tenant_id != self._directory_id(actor)
                    or existing.group_object_id != current.verified_group_object_id
                    or existing.member_object_id != member_id
                    or (kind == "addMember" and existing.member_kind != member_kind)
                ):
                    raise ConflictError("This Idempotency-Key names a different membership intent")
                operation = existing
            else:
                unsettled = await self._repository.list_directory_operations(
                    actor.tenant_id,
                    current.id,
                    limit=1,
                    member_object_id=member_id,
                    unsettled_only=True,
                )
                if unsettled:
                    raise ConflictError(
                        "Recover the existing uncertain membership operation before another write",
                        details={"operationId": unsettled[0].id},
                    )
                operation = CostCenterDirectoryOperation(
                    id=operation_id,
                    tenant_id=actor.tenant_id,
                    cost_center_id=current.id,
                    kind=kind,
                    directory_tenant_id=self._directory_id(actor),
                    group_object_id=current.verified_group_object_id,
                    member_object_id=member_id,
                    member_kind=member_kind,
                    requested_by=actor.object_id,
                )
                operation = await self._repository.create_directory_operation(
                    operation, self._operation_audit(actor, operation, "requested")
                )
                execute = True
        if execute:
            operation = await self._execute(actor, operation)
        elif operation.kind == "removeMember" and operation.state in {
            DirectoryOperationState.PENDING,
            DirectoryOperationState.SUCCEEDED,
            DirectoryOperationState.UNKNOWN,
        }:
            await self._after_removal(actor, operation)
        return directory_operation_view(operation)

    async def _preflight(
        self, actor: Actor, operation: CostCenterDirectoryOperation
    ) -> GroupManagement:
        management = self._management_for(actor)
        if operation.directory_tenant_id != self._directory_id(actor):
            raise DirectoryForbiddenError("The operation belongs to another directory tenant")
        if operation.kind == "createGroup":
            current = await self._stored(actor, operation.cost_center_id)
            self.require_custom_center(current)
            if current.entra_binding is not None:
                raise ConflictError("This cost center already has an Entra binding")
            return management
        current = await self._bound_center(actor, operation.cost_center_id)
        if current.verified_group_object_id != operation.group_object_id:
            raise DirectoryForbiddenError("A membership write cannot target an arbitrary group")
        assert operation.group_object_id is not None
        group = await management.validate_group(operation.group_object_id)
        self._assert_group(group, operation.directory_tenant_id, operation.group_object_id)
        if operation.kind == "addMember":
            if self._directory is None:
                raise DirectoryDisabledError(
                    "Verify the member with server-side directory lookup first"
                )
            assert operation.member_object_id is not None
            member = await self._directory.get_object(operation.member_object_id)
            if member is None:
                raise NotFoundError("The member was not found in the configured Entra directory")
            if (
                member.object_id != operation.member_object_id
                or member.kind != operation.member_kind
            ):
                raise ValidationError("The member kind or object ID does not match Entra")
        return management

    @staticmethod
    def _operation_audit(
        actor: Actor,
        operation: CostCenterDirectoryOperation,
        action: str,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        return _audit(
            actor,
            f"costCenter.directoryOperation.{action}",
            operation.cost_center_id,
            {
                "operationId": operation.id,
                "kind": operation.kind,
                "state": operation.state,
                "directoryTenantId": operation.directory_tenant_id,
                "groupObjectId": operation.group_object_id,
                "memberObjectId": operation.member_object_id,
                "memberKind": operation.member_kind,
                "errorCode": operation.error_code,
                "error": operation.error_message,
                **(details or {}),
            },
        )

    async def _execute(
        self, actor: Actor, operation: CostCenterDirectoryOperation
    ) -> CostCenterDirectoryOperation:
        try:
            management = await self._preflight(actor, operation)
        except Exception as exc:
            return await self._failed(actor, operation, exc, may_have_written=False)
        try:
            # client-request-id is tracing only. The conditional operation intent admits this
            # single execution, not Graph-side idempotency or permission to replay a timeout.
            request_id = str(uuid4())
            if operation.kind == "createGroup":
                assert operation.display_name is not None
                result = await management.create_group(
                    operation.display_name,
                    description=operation.description,
                    request_id=request_id,
                )
            elif operation.kind == "addMember":
                assert operation.group_object_id is not None
                assert operation.member_object_id is not None
                assert operation.member_kind is not None
                result = await management.add_member(
                    operation.group_object_id,
                    operation.member_object_id,
                    member_kind=operation.member_kind,
                    request_id=request_id,
                )
            else:
                assert operation.group_object_id is not None
                assert operation.member_object_id is not None
                result = await management.remove_member(
                    operation.group_object_id, operation.member_object_id, request_id=request_id
                )
            self._assert_result(operation, result)
        except Exception as exc:
            return await self._failed(actor, operation, exc, may_have_written=True)
        succeeded = operation.model_copy(
            update={
                "state": DirectoryOperationState.SUCCEEDED,
                "group_object_id": result.group_object_id,
                "updated_at": utc_now(),
            }
        )
        try:
            succeeded = await self._persist_outcome(actor, succeeded)
        except Exception:
            if operation.kind == "removeMember":
                await self._after_removal(actor, succeeded)
            raise
        if operation.kind == "createGroup":
            # Persist the actual ID BEFORE any validation/binding. Recovery uses this object,
            # never another POST, a matching display name, or a request-supplied replacement.
            await self._finish_created_binding(actor, succeeded)
        elif operation.kind == "removeMember":
            await self._after_removal(actor, succeeded)
        return succeeded

    @staticmethod
    def _assert_result(operation: CostCenterDirectoryOperation, result: GroupWriteResult) -> None:
        expected = {
            "createGroup": "create_group",
            "addMember": "add_member",
            "removeMember": "remove_member",
        }[operation.kind]
        try:
            valid_id = directory_object_id(result.group_object_id) == result.group_object_id
        except ValueError:
            valid_id = False
        if (
            result.tenant_id != operation.directory_tenant_id
            or result.operation != expected
            or not valid_id
            or result.outcome != GroupWriteOutcome.CONFIRMED
            or (
                operation.kind != "createGroup"
                and result.group_object_id != operation.group_object_id
            )
            or result.member_object_id != operation.member_object_id
        ):
            raise GroupManagementError(
                "The directory write returned an unverified outcome or a different target",
                kind=GroupFailureKind.INVALID_RESPONSE,
                outcome=GroupWriteOutcome.UNKNOWN,
            )

    async def _failed(
        self,
        actor: Actor,
        operation: CostCenterDirectoryOperation,
        error: Exception,
        *,
        may_have_written: bool,
    ) -> CostCenterDirectoryOperation:
        if isinstance(error, GroupManagementError):
            uncertain = error.outcome != GroupWriteOutcome.NOT_APPLIED
            code, message = error.kind.value, _error_message(error)
            details = error.details
            error_details = DirectoryOperationErrorDetails(
                http_status=error.http_status,
                graph_code=error.graph_code[:256] if error.graph_code is not None else None,
                graph_message=error.graph_message[:2000]
                if error.graph_message is not None
                else None,
                request_id=error.request_id[:256] if error.request_id is not None else None,
                client_request_id=(
                    error.client_request_id[:256] if error.client_request_id is not None else None
                ),
                retry_after=error.retry_after,
            )
        elif isinstance(error, DomainError):
            uncertain = may_have_written
            code, message = error.code, error.message
            details = error.details
            error_details = None
        else:
            uncertain = may_have_written
            code = "unknown_outcome" if uncertain else "directory_unavailable"
            message = f"The directory operation could not complete: {error}"
            details = {}
            error_details = None
        failed = operation.model_copy(
            update={
                "state": (
                    DirectoryOperationState.UNKNOWN if uncertain else DirectoryOperationState.FAILED
                ),
                "error_code": code[:128],
                "error_message": message[:2000],
                "error_details": error_details,
                "updated_at": utc_now(),
            }
        )
        try:
            return await self._persist_outcome(actor, failed, details=details)
        finally:
            if operation.kind == "removeMember" and uncertain:
                await self._after_removal(actor, failed)

    async def _persist_outcome(
        self,
        actor: Actor,
        outcome: CostCenterDirectoryOperation,
        *,
        details: dict[str, Any] | None = None,
    ) -> CostCenterDirectoryOperation:
        try:
            async with self._lease(actor):
                current = await self._operation(actor, outcome.cost_center_id, outcome.id)
                if current.intent() != outcome.intent():
                    raise ConflictError("The directory operation's intent changed")
                if current.result() == outcome.result():
                    return current
                return await self._repository.save_directory_operation(
                    outcome.model_copy(update={"etag": current.etag}),
                    self._operation_audit(actor, outcome, outcome.state.value, details),
                )
        except Exception as exc:
            # Cosmos may have committed even if the response was lost. Re-read before declaring
            # uncertainty, and retain an observed create ID if a subsequent write can succeed.
            with suppress(Exception):
                current = await self._operation(actor, outcome.cost_center_id, outcome.id)
                if current.intent() == outcome.intent() and current.result() == outcome.result():
                    return current
                if current.state in {
                    DirectoryOperationState.PENDING,
                    DirectoryOperationState.UNKNOWN,
                }:
                    unknown = outcome.model_copy(
                        update={
                            "etag": current.etag,
                            "state": DirectoryOperationState.UNKNOWN,
                            "error_code": "resultPersistenceFailed",
                            "error_message": (
                                "The directory result could not be durably acknowledged. "
                                "Recover this operation; do not repeat the write."
                            ),
                            "updated_at": utc_now(),
                        }
                    )
                    async with self._lease(actor):
                        await self._repository.save_directory_operation(
                            unknown, self._operation_audit(actor, unknown, "unknown")
                        )
            raise ChangeNotRecordedError(
                "The directory outcome could not be recorded. Do not repeat the write.",
                details={
                    "costCenterId": outcome.cost_center_id,
                    "operationId": outcome.id,
                    "directoryOutcome": outcome.state,
                    "observedGroupObjectId": (
                        outcome.group_object_id if outcome.kind == "createGroup" else None
                    ),
                },
            ) from exc

    async def _after_removal(self, actor: Actor, operation: CostCenterDirectoryOperation) -> None:
        assert operation.member_object_id is not None
        try:
            await self._checks.member_removed_from_cost_center(
                actor,
                operation.cost_center_id,
                operation.member_object_id,
                operation.member_kind,
            )
        except Exception as exc:
            raise ChangeNotRecordedError(
                "The directory removal requires individual access/key rechecks, but they could "
                "not be scheduled. Recover this operation before treating keys as revoked.",
                details={
                    "costCenterId": operation.cost_center_id,
                    "operationId": operation.id,
                    "directoryState": operation.state,
                    "individualAccessRecheckPending": True,
                },
            ) from exc

    async def _finish_created_binding(
        self, actor: Actor, operation: CostCenterDirectoryOperation
    ) -> None:
        try:
            await self._bind_created_group(actor, operation)
        except DomainError:
            raise
        except Exception as exc:
            raise ChangeNotRecordedError(
                "The Entra group was created, but its setup could not finish. Recover the "
                "recorded operation to verify and bind that same group; do not create another.",
                details={
                    "costCenterId": operation.cost_center_id,
                    "operationId": operation.id,
                    "groupObjectId": operation.group_object_id,
                },
            ) from exc

    async def _operation(
        self, actor: Actor, cost_center_id: str, operation_id: str
    ) -> CostCenterDirectoryOperation:
        operation = await self._repository.get_directory_operation(actor.tenant_id, operation_id)
        if operation is None or operation.cost_center_id != cost_center_id:
            raise NotFoundError("Directory operation not found in this cost center")
        return operation

    async def list_operations(
        self, actor: Actor, cost_center_id: str, *, limit: int = 50
    ) -> list[CostCenterDirectoryOperationView]:
        await self._stored(actor, cost_center_id)
        return [
            directory_operation_view(operation)
            for operation in await self._repository.list_directory_operations(
                actor.tenant_id, cost_center_id, limit=limit
            )
        ]

    async def get_operation(
        self, actor: Actor, cost_center_id: str, operation_id: str
    ) -> CostCenterDirectoryOperationView:
        await self._stored(actor, cost_center_id)
        return directory_operation_view(await self._operation(actor, cost_center_id, operation_id))

    async def retry(
        self, actor: Actor, cost_center_id: str, operation_id: str
    ) -> CostCenterDirectoryOperationView:
        self._directory_id(actor)
        async with self._lease(actor):
            await self._stored(actor, cost_center_id)
            operation = await self._operation(actor, cost_center_id, operation_id)
            if operation.state != DirectoryOperationState.FAILED:
                raise ConflictError(
                    "Only a definitive failed operation can be retried; recover uncertain outcomes",
                    details={"operationId": operation.id, "state": operation.state},
                )
            if operation.kind != "createGroup":
                unsettled = await self._repository.list_directory_operations(
                    actor.tenant_id,
                    cost_center_id,
                    limit=1,
                    member_object_id=operation.member_object_id,
                    unsettled_only=True,
                )
                if unsettled:
                    raise ConflictError(
                        "Recover the other uncertain membership operation first",
                        details={"operationId": unsettled[0].id},
                    )
            pending = operation.model_copy(
                update={
                    "state": DirectoryOperationState.PENDING,
                    "error_code": None,
                    "error_message": None,
                    "error_details": None,
                    "updated_at": utc_now(),
                }
            )
            pending = await self._repository.save_directory_operation(
                pending, self._operation_audit(actor, pending, "retryRequested")
            )
        return directory_operation_view(await self._execute(actor, pending))

    async def recover(
        self, actor: Actor, cost_center_id: str, operation_id: str
    ) -> CostCenterDirectoryOperationView:
        self._directory_id(actor)
        await self._stored(actor, cost_center_id)
        operation = await self._operation(actor, cost_center_id, operation_id)
        if operation.directory_tenant_id != self._directory_id(actor):
            raise DirectoryForbiddenError("This operation belongs to another directory")
        if operation.kind == "createGroup":
            if operation.group_object_id is None:
                return directory_operation_view(operation)
            if operation.state == DirectoryOperationState.UNKNOWN:
                management = self._management_for(actor)
                group = await management.validate_group(operation.group_object_id)
                self._assert_group(group, operation.directory_tenant_id, operation.group_object_id)
                operation = await self._persist_outcome(
                    actor,
                    operation.model_copy(
                        update={
                            "state": DirectoryOperationState.SUCCEEDED,
                            "error_code": None,
                            "error_message": None,
                            "error_details": None,
                            "updated_at": utc_now(),
                        }
                    ),
                    details={"reconciledRecordedIdentity": True},
                )
            await self._finish_created_binding(actor, operation)
        elif operation.state == DirectoryOperationState.UNKNOWN:
            if operation.kind == "removeMember":
                await self._after_removal(actor, operation)
            # Recovery uses direct pages, not a bounded/transitive preview. A finite deadline
            # and page budget keep an enormous or concurrently changing group from hanging.
            present = await self.direct_membership(
                actor, cost_center_id, operation.member_object_id
            )
            desired = present if operation.kind == "addMember" else not present
            if desired:
                operation = await self._persist_outcome(
                    actor,
                    operation.model_copy(
                        update={
                            "state": DirectoryOperationState.SUCCEEDED,
                            "error_code": None,
                            "error_message": None,
                            "error_details": None,
                            "updated_at": utc_now(),
                        }
                    ),
                    details={"reconciledDirectMembership": True},
                )
        elif operation.kind == "removeMember" and operation.state in {
            DirectoryOperationState.PENDING,
            DirectoryOperationState.SUCCEEDED,
        }:
            await self._after_removal(actor, operation)
        return directory_operation_view(operation)

    async def direct_membership(
        self, actor: Actor, cost_center_id: str, object_id: str | None
    ) -> bool:
        member_id = self._object_id(object_id or "")
        cursor = None
        seen: set[str] = set()
        try:
            async with asyncio.timeout(60):
                for _ in range(100):
                    page = await self.list_members(
                        actor, cost_center_id, page_size=999, cursor=cursor
                    )
                    if any(member.object_id == member_id for member in page.members):
                        return True
                    cursor = page.next_cursor
                    if cursor is None:
                        return False
                    if cursor in seen:
                        break
                    seen.add(cursor)
        except TimeoutError as exc:
            raise DirectoryError(
                "Direct membership recovery timed out; its outcome is unchanged"
            ) from exc
        raise DirectoryError(
            "Direct membership recovery did not complete all pages; its outcome is unchanged"
        )
