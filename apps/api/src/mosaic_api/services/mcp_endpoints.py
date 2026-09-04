"""MCP server registration, reachability verification, and tool discovery.

The sibling of :mod:`mosaic_api.services.model_endpoints`, with one structural difference that is
the whole point of ADR 0007: a model endpoint has an Azure control plane, so MOSAIC can ask ARM
whether it may read the resource without touching it. An MCP server has no control plane. The only
way to answer "can MOSAIC read this" is to connect, so preflight here *is* a live call — it runs
the handshake and stops, and discovery is a separate, explicitly triggered sync.

MOSAIC never calls a tool, and creates nothing in Azure or API Management.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime

import httpx
import structlog
from pydantic import AnyHttpUrl

from mosaic_api.domain import (
    AuditEvent,
    CapabilitySupport,
    CredentialReference,
    GatewaySyncStatus,
    McpAuthChallenge,
    McpAuthMode,
    McpDiscoveryAccess,
    McpDiscoveryEvaluation,
    McpEndpoint,
    McpEndpointCapabilities,
    McpEndpointCreate,
    McpEndpointStatus,
    McpEndpointSyncRun,
    McpEndpointUpdate,
    McpInventorySummary,
    canonical_mcp_url,
    deterministic_id,
    mcp_endpoint_id,
    new_id,
    utc_now,
)
from mosaic_api.errors import ConflictError, DomainError, NotFoundError, ValidationError
from mosaic_api.integrations.mcp import (
    EntraTokenProvider,
    KeyVaultSecretReader,
    McpAuthorizationRequiredError,
    McpClient,
    McpError,
    McpToolCollector,
    McpUnreachableError,
    McpUnsupportedProtocolError,
    McpUnsupportedTransportError,
    admit_mcp_url,
)
from mosaic_api.observed import ObservedMcpTool
from mosaic_api.repositories import McpEndpointRepository
from mosaic_api.services.directory import Actor

logger = structlog.get_logger()

STALE_RUN_MESSAGE = "The API restarted while this sync was running; its result is unknown."
TOOL_ENTITY_TYPE = "observedMcpTool"

McpClientFactory = Callable[[str, str | None], McpClient]
SecretResolver = Callable[[str], Awaitable[str]]
TokenResolver = Callable[[str], Awaitable[str]]


def _status_for(error: McpError) -> McpEndpointStatus:
    """Map a discovery failure onto the record's status.

    The two "unsupported" states are held apart from the failure states because they are not
    faults an operator clears by retrying — the server answered, and the answer was that it speaks
    something MOSAIC does not.
    """

    if isinstance(error, McpAuthorizationRequiredError):
        return McpEndpointStatus.UNAUTHORIZED
    if isinstance(error, McpUnsupportedProtocolError):
        return McpEndpointStatus.UNSUPPORTED_PROTOCOL
    if isinstance(error, McpUnsupportedTransportError):
        return McpEndpointStatus.UNSUPPORTED_TRANSPORT
    if isinstance(error, McpUnreachableError):
        return McpEndpointStatus.UNREACHABLE
    return McpEndpointStatus.DEGRADED


def _access_for(error: McpError) -> McpDiscoveryAccess:
    """Never report "MOSAIC could not evaluate this" as "MOSAIC was denied".

    A ``401`` carries what the server actually asked for, so an operator sees the scope and
    resource-metadata URL rather than a bare failure.
    """

    if isinstance(error, McpAuthorizationRequiredError):
        return McpDiscoveryAccess(
            can_discover=False,
            evaluation=McpDiscoveryEvaluation.AUTHORIZATION_REQUIRED,
            checked_at=utc_now(),
            challenge=McpAuthChallenge(
                scheme=error.scheme,
                resource_metadata_url=error.resource_metadata_url,
                scope=error.scope,
            ),
            message=error.message,
        )
    return McpDiscoveryAccess(
        can_discover=False,
        evaluation=McpDiscoveryEvaluation.NOT_EVALUATED,
        checked_at=utc_now(),
        message=error.message,
    )


class McpEndpointService:
    def __init__(
        self,
        repository: McpEndpointRepository,
        *,
        client_factory: McpClientFactory,
        secret_resolver: SecretResolver | None = None,
        token_resolver: TokenResolver | None = None,
        require_https: bool = True,
        allow_private_endpoints: bool = False,
    ) -> None:
        self._repository = repository
        self._client_factory = client_factory
        self._secret_resolver = secret_resolver
        self._token_resolver = token_resolver
        self._require_https = require_https
        self._allow_private = allow_private_endpoints
        self._active: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    async def aclose(self) -> None:
        """Cancel and drain background work before the clients it uses are closed."""

        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._active.clear()

    @staticmethod
    def _audit(actor: Actor, action: str, resource_id: str) -> AuditEvent:
        return AuditEvent(
            id=new_id("audit"),
            tenant_id=actor.tenant_id,
            action=action,
            resource_type="mcpEndpoint",
            resource_id=resource_id,
            actor_object_id=actor.object_id,
        )

    def _admit(self, url: str) -> str:
        return admit_mcp_url(
            url, require_https=self._require_https, allow_private=self._allow_private
        )

    async def list_endpoints(self, actor: Actor) -> list[McpEndpoint]:
        return await self._repository.list_endpoints(actor.tenant_id)

    async def get_endpoint(self, actor: Actor, endpoint_id: str) -> McpEndpoint:
        endpoint = await self._repository.get_endpoint(actor.tenant_id, endpoint_id)
        if not endpoint:
            raise NotFoundError("MCP server was not found", details={"id": endpoint_id})
        return endpoint

    async def register(self, actor: Actor, request: McpEndpointCreate) -> McpEndpoint:
        # Admit the URL exactly as submitted. Canonicalising first would strip any embedded
        # credentials before the guard could object to them.
        self._admit(str(request.endpoint))
        url = canonical_mcp_url(str(request.endpoint))
        existing = await self._repository.find_endpoint_by_url(actor.tenant_id, url)
        if existing:
            raise ConflictError(
                "This MCP server is already registered with MOSAIC",
                details={"id": existing.id, "name": existing.name},
            )

        credential_id: str | None = None
        if request.credential_secret_uri is not None:
            credential_id = deterministic_id("credential", actor.tenant_id, url)
            await self._repository.save_credential(
                CredentialReference(
                    id=credential_id,
                    tenant_id=actor.tenant_id,
                    name=f"{url} token",
                    secret_uri=request.credential_secret_uri,
                ),
                self._audit(actor, "credentialReference.recorded", credential_id),
            )

        assert request.auth_mode is not None
        notes: list[str] = []
        if request.auth_mode == McpAuthMode.API_KEY:
            notes.append(
                "MOSAIC stores only the Key Vault secret URI for this server. The token itself is "
                "read at discovery time and never persisted."
            )
        endpoint = McpEndpoint(
            id=mcp_endpoint_id(actor.tenant_id, url),
            tenant_id=actor.tenant_id,
            name=(request.name or url).strip(),
            # The canonical form, not the submitted one: it is what MOSAIC actually dials, and
            # storing the raw value would echo anything the URL carried back to every caller.
            endpoint=AnyHttpUrl(url),
            environment_label=request.environment_label,
            auth_mode=request.auth_mode,
            credential_reference_id=credential_id,
            resource_audience=request.resource_audience,
            capabilities=McpEndpointCapabilities(notes=notes),
        )
        endpoint = await self._apply_preflight(endpoint)
        return await self._repository.create_endpoint(
            endpoint, self._audit(actor, "mcpEndpoint.registered", endpoint.id)
        )

    async def update(
        self, actor: Actor, endpoint_id: str, request: McpEndpointUpdate
    ) -> McpEndpoint:
        endpoint = await self.get_endpoint(actor, endpoint_id)
        changes = request.model_dump(exclude_unset=True, by_alias=False)
        secret_uri = changes.pop("credential_secret_uri", None)
        audience = changes.get("resource_audience")
        if "name" in changes and changes["name"] is not None:
            changes["name"] = str(changes["name"]).strip()
        if secret_uri is not None:
            if endpoint.auth_mode != McpAuthMode.API_KEY:
                raise ValidationError(
                    "This MCP server does not authenticate with a key, so it has no secret URI.",
                    details={"authMode": str(endpoint.auth_mode)},
                )
            url = canonical_mcp_url(str(endpoint.endpoint))
            credential_id = endpoint.credential_reference_id or deterministic_id(
                "credential", actor.tenant_id, url
            )
            await self._repository.save_credential(
                CredentialReference(
                    id=credential_id,
                    tenant_id=actor.tenant_id,
                    name=f"{url} token",
                    secret_uri=secret_uri,
                ),
                self._audit(actor, "credentialReference.recorded", credential_id),
            )
            changes["credential_reference_id"] = credential_id
        if audience is not None and endpoint.auth_mode != McpAuthMode.MANAGED_IDENTITY:
            raise ValidationError(
                "This MCP server does not authenticate with a managed identity, so it has no "
                "audience.",
                details={"authMode": str(endpoint.auth_mode)},
            )
        updated = McpEndpoint.model_validate(
            {
                **endpoint.model_dump(by_alias=False),
                **changes,
                "etag": endpoint.etag,
                "updated_at": utc_now(),
            }
        )
        return await self._repository.save_endpoint(
            updated, self._audit(actor, "mcpEndpoint.updated", updated.id)
        )

    async def delete(self, actor: Actor, endpoint_id: str) -> None:
        endpoint = await self.get_endpoint(actor, endpoint_id)
        await self._repository.delete_endpoint(
            endpoint, self._audit(actor, "mcpEndpoint.removed", endpoint.id)
        )

    async def preflight(self, actor: Actor, endpoint_id: str) -> McpEndpoint:
        endpoint = await self.get_endpoint(actor, endpoint_id)
        checked = await self._apply_preflight(endpoint)
        return await self._repository.record_endpoint_state(checked)

    async def _authorization_for(self, endpoint: McpEndpoint) -> str | None:
        """Resolve the credential to present, at call time and never before."""

        if endpoint.auth_mode == McpAuthMode.API_KEY:
            if not endpoint.credential_reference_id or self._secret_resolver is None:
                raise ValidationError(
                    "This MCP server needs a Key Vault secret that MOSAIC cannot resolve.",
                    details={"endpointId": endpoint.id},
                )
            credential = await self._repository.get_credential(
                endpoint.tenant_id, endpoint.credential_reference_id
            )
            if credential is None:
                raise ValidationError(
                    "The credential reference for this MCP server is missing.",
                    details={"endpointId": endpoint.id},
                )
            return f"Bearer {await self._secret_resolver(str(credential.secret_uri))}"
        if endpoint.auth_mode == McpAuthMode.MANAGED_IDENTITY:
            if not endpoint.resource_audience or self._token_resolver is None:
                raise ValidationError(
                    "This MCP server needs an audience MOSAIC cannot resolve a token for.",
                    details={"endpointId": endpoint.id},
                )
            return f"Bearer {await self._token_resolver(endpoint.resource_audience)}"
        return None

    async def _connect(self, endpoint: McpEndpoint) -> McpClient:
        url = self._admit(canonical_mcp_url(str(endpoint.endpoint)))
        return self._client_factory(url, await self._authorization_for(endpoint))

    async def _apply_preflight(self, endpoint: McpEndpoint) -> McpEndpoint:
        """Run the handshake and stop.

        Discovery is a separate, explicit action, so preflight stays cheap enough to run on every
        registration without pulling a whole tool catalogue. Every failure is *recorded* rather
        than raised: refusing to register a server because Key Vault was briefly unreachable would
        lose the administrator's intent over a transient fault.
        """

        try:
            client = await self._connect(endpoint)
            async with client:
                session = await client.initialize()
                await client.notify_initialized()
        except McpError as error:
            return endpoint.model_copy(
                update={
                    "access": _access_for(error),
                    "status": _status_for(error),
                    "updated_at": utc_now(),
                }
            )
        except DomainError as error:
            return endpoint.model_copy(
                update={
                    "access": McpDiscoveryAccess(
                        can_discover=False,
                        evaluation=McpDiscoveryEvaluation.NOT_EVALUATED,
                        checked_at=utc_now(),
                        message=error.message,
                    ),
                    "status": McpEndpointStatus.DEGRADED,
                    "updated_at": utc_now(),
                }
            )
        except Exception:
            # Preflight is diagnostic. Nothing it hits may cost an administrator their intent, so
            # even an unexpected fault is recorded on the record rather than raised at the caller.
            logger.exception("mcp_preflight_failed", endpoint_id=endpoint.id)
            return endpoint.model_copy(
                update={
                    "access": McpDiscoveryAccess(
                        can_discover=False,
                        evaluation=McpDiscoveryEvaluation.NOT_EVALUATED,
                        checked_at=utc_now(),
                        message="MOSAIC could not complete the connection check.",
                    ),
                    "status": McpEndpointStatus.DEGRADED,
                    "updated_at": utc_now(),
                }
            )

        return endpoint.model_copy(
            update={
                "access": McpDiscoveryAccess(
                    can_discover=True,
                    evaluation=McpDiscoveryEvaluation.HANDSHAKE,
                    checked_at=utc_now(),
                ),
                "capabilities": McpEndpointCapabilities(
                    protocol_version=session.protocol_version,
                    server_name=session.server_name,
                    server_title=session.server_title,
                    server_version=session.server_version,
                    instructions=session.instructions,
                    supports_tools=(
                        CapabilitySupport.AVAILABLE
                        if session.supports_tools
                        else CapabilitySupport.UNAVAILABLE
                    ),
                    session_managed=session.session_managed,
                    notes=endpoint.capabilities.notes,
                ),
                "status": McpEndpointStatus.CONNECTED,
                "updated_at": utc_now(),
            }
        )

    async def start_sync(self, actor: Actor, endpoint_id: str) -> McpEndpointSyncRun:
        endpoint = await self.get_endpoint(actor, endpoint_id)
        if not endpoint.access.can_discover:
            raise ConflictError(
                "MOSAIC cannot reach this MCP server yet. Check the connection and try again.",
                details={"status": str(endpoint.status)},
            )
        # Claim synchronously, for the same reason gateway and model endpoint syncs do: the run
        # must be persisted before the task starts, and that await would let a second request slip
        # past a lock.
        if endpoint_id in self._active:
            raise ConflictError(
                "A sync is already running for this MCP server",
                details={"endpointId": endpoint_id},
            )
        self._active.add(endpoint_id)
        run = McpEndpointSyncRun(
            id=new_id("syncrun"),
            tenant_id=actor.tenant_id,
            endpoint_id=endpoint_id,
            actor_object_id=actor.object_id,
        )
        try:
            await self._repository.save_endpoint_sync_run(run)
        except Exception:
            self._active.discard(endpoint_id)
            raise
        task = asyncio.create_task(self._run_sync(endpoint, run))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run

    async def sync_now(self, actor: Actor, endpoint_id: str) -> McpEndpointSyncRun:
        """Run a sync to completion. Used by tests, not by request handlers."""

        run = await self.start_sync(actor, endpoint_id)
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        completed = await self._repository.get_endpoint_sync_run(actor.tenant_id, run.id)
        return completed or run

    async def _run_sync(self, endpoint: McpEndpoint, run: McpEndpointSyncRun) -> None:
        started = utc_now()
        try:
            await self._collect_and_persist(endpoint, run, started)
        except asyncio.CancelledError:
            raise
        except McpError as error:
            logger.info("mcp_sync_failed", endpoint_id=endpoint.id, code=error.code)
            await self._record_failure(
                endpoint, run, started, error.message, _status_for(error), _access_for(error)
            )
        except Exception as error:
            logger.exception("mcp_sync_failed", endpoint_id=endpoint.id)
            await self._record_failure(
                endpoint,
                run,
                started,
                str(error),
                McpEndpointStatus.DEGRADED,
                McpDiscoveryAccess(
                    can_discover=False,
                    evaluation=McpDiscoveryEvaluation.NOT_EVALUATED,
                    checked_at=utc_now(),
                    message=str(error),
                ),
            )
        finally:
            self._active.discard(endpoint.id)

    async def _collect_and_persist(
        self, endpoint: McpEndpoint, run: McpEndpointSyncRun, started: datetime
    ) -> None:
        client = await self._connect(endpoint)
        async with client:
            collector = McpToolCollector(
                client, tenant_id=endpoint.tenant_id, endpoint_id=endpoint.id
            )
            snapshot = await collector.collect()

        # Re-read rather than writing back the copy captured when the sync started: an
        # administrator may have removed or renamed the server while it was being read.
        current = await self._repository.get_endpoint(endpoint.tenant_id, endpoint.id)
        if current is None:
            logger.info("mcp_sync_discarded", endpoint_id=endpoint.id, reason="removed")
            return

        removed = await self._repository.replace_observed_for_endpoint(
            endpoint.tenant_id,
            endpoint.id,
            snapshot.entities(),
            snapshot.snapshot_id,
            snapshot.incomplete_types,
        )
        summary = snapshot.summary()
        status = GatewaySyncStatus.PARTIAL if snapshot.errors else GatewaySyncStatus.SUCCEEDED
        await self._finish_run(
            run, status, started, errors=snapshot.errors, counts=summary, removed=removed
        )
        await self._repository.record_endpoint_state(
            current.model_copy(
                update={
                    "capabilities": snapshot.capabilities().model_copy(
                        update={"notes": current.capabilities.notes}
                    ),
                    "access": McpDiscoveryAccess(
                        can_discover=True,
                        evaluation=McpDiscoveryEvaluation.HANDSHAKE,
                        checked_at=utc_now(),
                    ),
                    "inventory": summary,
                    "last_synced_at": utc_now(),
                    "last_sync_error": "; ".join(snapshot.errors) or None,
                    "status": (
                        McpEndpointStatus.DEGRADED
                        if snapshot.errors
                        else McpEndpointStatus.CONNECTED
                    ),
                    "updated_at": utc_now(),
                }
            )
        )

    async def _record_failure(
        self,
        endpoint: McpEndpoint,
        run: McpEndpointSyncRun,
        started: datetime,
        reason: str,
        status: McpEndpointStatus,
        access: McpDiscoveryAccess,
    ) -> None:
        """Best-effort bookkeeping for a failed sync; must never raise back into the task."""

        try:
            await self._finish_run(run, GatewaySyncStatus.FAILED, started, errors=[reason])
            current = await self._repository.get_endpoint(endpoint.tenant_id, endpoint.id)
            if current is None:
                return
            await self._repository.record_endpoint_state(
                current.model_copy(
                    update={
                        # The access verdict must move with the status. Leaving a stale
                        # ``canDiscover: true`` beside ``status: unauthorized`` would hide the
                        # challenge the server sent and keep offering a sync it just refused.
                        "access": access,
                        "status": status,
                        "last_sync_error": reason,
                        "updated_at": utc_now(),
                    }
                )
            )
        except Exception:
            logger.exception("mcp_sync_failure_not_recorded", endpoint_id=endpoint.id)

    async def _finish_run(
        self,
        run: McpEndpointSyncRun,
        status: GatewaySyncStatus,
        started: datetime,
        *,
        errors: list[str],
        counts: McpInventorySummary | None = None,
        removed: int = 0,
    ) -> None:
        completed = utc_now()
        await self._repository.save_endpoint_sync_run(
            run.model_copy(
                update={
                    "status": status,
                    "completed_at": completed,
                    "duration_ms": int((completed - started).total_seconds() * 1000),
                    "counts": counts or McpInventorySummary(),
                    "removed": removed,
                    "errors": errors,
                    "updated_at": completed,
                }
            )
        )

    async def get_sync_run(self, actor: Actor, run_id: str) -> McpEndpointSyncRun:
        run = await self._repository.get_endpoint_sync_run(actor.tenant_id, run_id)
        if not run:
            raise NotFoundError("Sync run was not found", details={"id": run_id})
        return run

    async def list_sync_runs(self, actor: Actor, endpoint_id: str) -> list[McpEndpointSyncRun]:
        await self.get_endpoint(actor, endpoint_id)
        return await self._repository.list_endpoint_sync_runs(actor.tenant_id, endpoint_id)

    async def reap_stale_sync_runs(self, tenant_id: str) -> int:
        """Mark runs orphaned by a restart as failed instead of leaving them pending forever."""

        stale = await self._repository.list_unfinished_endpoint_sync_runs(tenant_id)
        active = set(self._active)
        reaped = 0
        for run in stale:
            if run.endpoint_id in active:
                continue
            completed = utc_now()
            await self._repository.save_endpoint_sync_run(
                run.model_copy(
                    update={
                        "status": GatewaySyncStatus.FAILED,
                        "completed_at": completed,
                        "errors": [*run.errors, STALE_RUN_MESSAGE],
                        "updated_at": completed,
                    }
                )
            )
            reaped += 1
        return reaped

    async def list_tools(self, actor: Actor, endpoint_id: str) -> list[ObservedMcpTool]:
        await self.get_endpoint(actor, endpoint_id)
        items = await self._repository.list_observed_for_endpoint(
            ObservedMcpTool, actor.tenant_id, endpoint_id, TOOL_ENTITY_TYPE
        )
        return sorted(items, key=lambda item: item.display_name.casefold())


def build_mcp_client_factory(
    http: httpx.AsyncClient,
) -> McpClientFactory:
    def factory(url: str, authorization: str | None) -> McpClient:
        return McpClient(url=url, http=http, authorization=authorization)

    return factory


def build_credential_resolvers(
    reader: KeyVaultSecretReader, tokens: EntraTokenProvider
) -> tuple[SecretResolver, TokenResolver]:
    return reader.read, tokens.token_for
