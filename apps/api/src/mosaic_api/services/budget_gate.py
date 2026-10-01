"""Each managed gateway's list of cost centers whose budgets block their calls. See ADR 0023.

The list is the ``mosaic-blocked-cost-centers`` named value. Every governed policy MOSAIC renders
reads it, so a publication's plan creates it before anything that names it, and the budget check
writes what it holds. MOSAIC owns it for the gateway, not for any one publication: a rollback or an
unpublish never deletes it, because the other publications' policies still read it, and API
Management refuses to delete a named value a policy names.

What the list holds is worked out from the budgets' saved state each time it's written, never kept
apart from it, so whoever writes it writes the same thing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog

from mosaic_api.budgets import (
    BLOCKED_COST_CENTERS_NAMED_VALUE,
    NONE_BLOCKED,
    GateState,
    blocked_list,
    budget_key,
    month_of,
    parse_blocked_list,
)
from mosaic_api.domain import (
    ApimResourceId,
    AuditEvent,
    Gateway,
    ManagementMode,
    PublishAction,
    PublishedResourceKind,
    PublishPlanStep,
    new_id,
    utc_now,
)
from mosaic_api.integrations.apim import ApimClient
from mosaic_api.integrations.apim.writer import ApimWriter
from mosaic_api.repositories import BudgetRepository, GatewayRepository

logger = structlog.get_logger()

ClientFactory = Callable[[ApimResourceId], ApimClient]
WriterFactory = Callable[[ApimResourceId], ApimWriter]
SEGMENT = f"namedValues/{BLOCKED_COST_CENTERS_NAMED_VALUE}"
CREATE_REASON = (
    "Create the list of cost centers whose budgets block their calls, which the access policy "
    "reads. MOSAIC keeps it for the gateway, and never deletes it with this publication."
)
KEEP_REASON = (
    "Keep the list of cost centers whose budgets block their calls, which the access policy "
    "reads. MOSAIC's budget check keeps it current."
)


def is_blocked_list(kind: PublishedResourceKind | str, name: str) -> bool:
    """Whether a plan step or result is the gateway's blocked list, which no publication owns."""

    return kind == PublishedResourceKind.NAMED_VALUE and name == BLOCKED_COST_CENTERS_NAMED_VALUE


def live_value(named_value: dict[str, object] | None) -> str | None:
    if named_value is None:
        return None
    properties = named_value.get("properties")
    value = properties.get("value") if isinstance(properties, dict) else None
    return value if isinstance(value, str) else None


async def ensure_blocked_list(
    gate: BlockedListGate | None, client: ApimClient, writer: ApimWriter, tenant_id: str
) -> bool:
    """Create a gateway's blocked list if it lacks one, before a policy that reads it.

    It holds what's blocked now. Without a gate to say, as in a deployment that doesn't check
    budgets, it holds nothing, and the budget check writes it once it runs. A list already there is
    left alone. Returns whether this created it.
    """

    if gate is not None:
        return await gate.ensure(client, writer, tenant_id)
    if await client.get_named_value(BLOCKED_COST_CENTERS_NAMED_VALUE) is not None:
        return False
    await writer.put_plain_named_value(BLOCKED_COST_CENTERS_NAMED_VALUE, NONE_BLOCKED)
    return True


@dataclass
class GateSync:
    """What one write of the lists did."""

    value: str
    # Blocked cost centers the list had no room for.
    left_out: list[str] = field(default_factory=list)
    states: list[GateState] = field(default_factory=list)


class BlockedListGate:
    def __init__(
        self,
        repository: BudgetRepository,
        *,
        gateway_repository: GatewayRepository,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._repository = repository
        self._gateways = gateway_repository
        self._clock = clock

    async def blocked(self, tenant_id: str) -> list[str]:
        """The cost centers whose budgets block their calls now, oldest block first.

        Only this month's blocks, and only while their budget still exists and still blocks, so
        turning blocking off or removing the budget lifts a block on the very next write.
        """

        month = month_of(self._clock())
        budgets = {item.id: item for item in await self._repository.list_budgets(tenant_id)}
        states = [
            state
            for state in await self._repository.list_budget_states(tenant_id)
            if state.blocked
            and state.month == month
            and state.cost_center_id
            and (budget := budgets.get(state.budget_id)) is not None
            and budget.blocks
            and budget.cost_center_id == state.cost_center_id
        ]
        states.sort(
            key=lambda state: (
                state.blocked_at or datetime.min.replace(tzinfo=UTC),
                state.cost_center_id or "",
            )
        )
        return [state.cost_center_id for state in states if state.cost_center_id]

    async def value(self, tenant_id: str) -> tuple[str, list[str]]:
        """The list's content now, and the blocked cost centers it has no room for."""

        blocked = await self.blocked(tenant_id)
        value, left = blocked_list(budget_key(item) for item in blocked)
        left_keys = set(left)
        return value, [item for item in blocked if budget_key(item) in left_keys]

    # -- publishing -------------------------------------------------------------------------

    @staticmethod
    def plan_step(resource: ApimResourceId, *, exists: bool) -> PublishPlanStep:
        """The step every governed plan starts with, so the list exists before a policy names it."""

        return PublishPlanStep(
            kind=PublishedResourceKind.NAMED_VALUE,
            name=BLOCKED_COST_CENTERS_NAMED_VALUE,
            action=PublishAction.NO_CHANGE if exists else PublishAction.CREATE,
            reason=KEEP_REASON if exists else CREATE_REASON,
            resource_id=f"{resource.canonical}/{SEGMENT}",
            existed=exists,
            stage="prepare",
        )

    async def ensure(self, client: ApimClient, writer: ApimWriter, tenant_id: str) -> bool:
        """Create the list on a gateway that lacks it, holding what's blocked now.

        A list already there is left alone: the budget check owns what it holds. Returns whether
        this created it.
        """

        if await client.get_named_value(BLOCKED_COST_CENTERS_NAMED_VALUE) is not None:
            return False
        value, _ = await self.value(tenant_id)
        await writer.put_plain_named_value(BLOCKED_COST_CENTERS_NAMED_VALUE, value)
        logger.info("blocked_list_created", gateway=writer.resource.service_name)
        return True

    # -- the budget check -------------------------------------------------------------------

    async def gateways(self, tenant_id: str) -> list[Gateway]:
        """Managed gateways that carry a publication of MOSAIC's, whose policy reads the list."""

        published = {
            item.gateway_id for item in await self._gateways.list_publications(tenant_id)
        } | {item.gateway_id for item in await self._gateways.list_mcp_publications(tenant_id)}
        return [
            gateway
            for gateway in await self._gateways.list_gateways(tenant_id)
            if gateway.management_mode == ManagementMode.MANAGE and gateway.id in published
        ]

    async def sync(
        self,
        tenant_id: str,
        client_factory: ClientFactory,
        writer_factory: WriterFactory,
        *,
        audit: Callable[[AuditEvent], Awaitable[None]] | None = None,
        gateways: Sequence[Gateway] | None = None,
    ) -> GateSync:
        """Write the list to every gateway whose copy differs from what's blocked now.

        A gateway that can't be read or written keeps what it had, which the state records, and
        the next check tries again. The content is worked out once, from the saved budgets, just
        before the writes, so it's never older than this call.
        """

        value, left_out = await self.value(tenant_id)
        result = GateSync(value=value, left_out=left_out)
        for gateway in gateways if gateways is not None else await self.gateways(tenant_id):
            result.states.append(
                await self._sync_gateway(
                    tenant_id, gateway, value, left_out, client_factory, writer_factory, audit
                )
            )
        if left_out:
            logger.error("blocked_list_full", tenant_id=tenant_id, left_out=len(left_out))
        return result

    async def _sync_gateway(
        self,
        tenant_id: str,
        gateway: Gateway,
        value: str,
        left_out: list[str],
        client_factory: ClientFactory,
        writer_factory: WriterFactory,
        audit: Callable[[AuditEvent], Awaitable[None]] | None,
    ) -> GateState:
        now = self._clock()
        found: dict[str, object] | None = None
        written = False
        try:
            resource = ApimResourceId.parse(gateway.azure_resource_id)
            client = client_factory(resource)
            found = await client.get_named_value(BLOCKED_COST_CENTERS_NAMED_VALUE)
            if found is None or live_value(found) != value:
                await writer_factory(resource).put_plain_named_value(
                    BLOCKED_COST_CENTERS_NAMED_VALUE, value
                )
                written = True
        except Exception as error:
            message = str(error)[:300] or type(error).__name__
            logger.warning("blocked_list_write_failed", gateway_id=gateway.id, error=message)
            return await self._repository.update_gate_state(
                tenant_id,
                gateway.id,
                lambda state: state.model_copy(update={"error": message, "error_at": now}),
            )
        if written and audit is not None:
            before = parse_blocked_list(live_value(found)) or set()
            after = parse_blocked_list(value) or set()
            try:
                await audit(
                    AuditEvent(
                        id=new_id("audit"),
                        tenant_id=tenant_id,
                        action="gateway.blockedCostCentersUpdated",
                        resource_type="gateway",
                        resource_id=gateway.id,
                        actor_object_id="system:budgets",
                        details={
                            "created": found is None,
                            "blocked": len(after),
                            "added": len(after - before),
                            "removed": len(before - after),
                        },
                    )
                )
            except Exception:
                logger.exception("blocked_list_audit_failed", gateway_id=gateway.id)
        return await self._repository.update_gate_state(
            tenant_id,
            gateway.id,
            lambda state: state.model_copy(
                update={
                    "value": value,
                    "synced_at": now,
                    "error": None,
                    "error_at": None,
                    "left_out": list(left_out),
                }
            ),
        )
