import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

import httpx
import structlog
from azure.cosmos.aio import CosmosClient
from azure.identity.aio import DefaultAzureCredential, ManagedIdentityCredential
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from mosaic_api.analytics_api import analytics_router
from mosaic_api.api import portal_router, router
from mosaic_api.approvals_api import approvals_router
from mosaic_api.auth import EntraAuthenticator, LocalAuthenticator
from mosaic_api.azure_connections import (
    AzureConnectionRepository,
    AzureConnectionService,
    CosmosAzureConnectionRepository,
    InMemoryAzureConnectionRepository,
)
from mosaic_api.azure_connections_api import azure_connections_router
from mosaic_api.budgets_api import budgets_router, portal_budgets_router
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings, get_settings
from mosaic_api.cost_centers_api import cost_centers_router, portal_cost_centers_router
from mosaic_api.directory_api import directory_router
from mosaic_api.domain import Entitlement, KeyVaultSecretId, utc_now
from mosaic_api.errors import (
    DomainError,
    NotFoundError,
    ValidationError,
    domain_error_handler,
    request_validation_error_handler,
)
from mosaic_api.integrations.aoai import CognitiveServicesClient
from mosaic_api.integrations.aoai.backend_key_access import KeyVaultLocator
from mosaic_api.integrations.aoai.client import SubscriptionScanner
from mosaic_api.integrations.aoai.key_check import EndpointKeyProbe
from mosaic_api.integrations.apim import ApimClient, ApimWriter, ArmClient
from mosaic_api.integrations.apim.credentials import ApimCredentialClient, ApimKeyManager
from mosaic_api.integrations.azure_connections import AzureClientRegistry
from mosaic_api.integrations.email import GraphEmailClient
from mosaic_api.integrations.graph import DirectoryLookup, GraphDirectoryLookup
from mosaic_api.integrations.graph.notifications import GraphNotificationDirectory
from mosaic_api.integrations.key_vault import KeyVaultSecretWriter
from mosaic_api.integrations.loganalytics import LogAnalyticsClient
from mosaic_api.integrations.mcp import EntraTokenProvider, KeyVaultSecretReader
from mosaic_api.integrations.teams import (
    BotTokenSource,
    ManagedIdentityBotTokenSource,
    SecretBotTokenSource,
    TeamsHttpTransport,
    TeamsLifecycleAuthenticator,
    WorkflowEndpointPolicy,
)
from mosaic_api.mcp_publishing_api import mcp_publishing_router
from mosaic_api.model_pools_api import model_pools_router
from mosaic_api.notification_delivery_api import notification_delivery_router
from mosaic_api.notification_inbox_api import notifications_router, portal_notifications_router
from mosaic_api.notification_readiness_api import notification_readiness_router
from mosaic_api.notifications import CapabilityCheck, NotificationEvent
from mosaic_api.notifications_preferences_api import (
    notification_preferences_router,
    portal_notification_preferences_router,
)
from mosaic_api.observability import configure_logging, configure_telemetry, instrument_requests
from mosaic_api.pricing import load_seed
from mosaic_api.pricing_api import pricing_router
from mosaic_api.repositories import (
    BudgetRepository,
    CosmosBudgetRepository,
    CosmosCostCenterRepository,
    CosmosDirectoryRepository,
    CosmosEntitlementRepository,
    CosmosEnvironmentRepository,
    CosmosGatewayRepository,
    CosmosMcpEndpointRepository,
    CosmosModelEndpointRepository,
    CosmosPricingRepository,
    CosmosUsageRollupRepository,
    CostCenterRepository,
    DirectoryRepository,
    EntitlementRepository,
    EnvironmentRepository,
    GatewayRepository,
    InMemoryBudgetRepository,
    InMemoryCostCenterRepository,
    InMemoryDirectoryRepository,
    InMemoryEntitlementRepository,
    InMemoryEnvironmentRepository,
    InMemoryGatewayRepository,
    InMemoryMcpEndpointRepository,
    InMemoryModelEndpointRepository,
    InMemoryPricingRepository,
    InMemoryUsageRollupRepository,
    McpEndpointRepository,
    ModelEndpointRepository,
    PricingRepository,
    UsageRollupRepository,
)
from mosaic_api.repositories.cosmos_approvals import CosmosApprovalRepository
from mosaic_api.repositories.cosmos_notifications import CosmosNotificationRepository
from mosaic_api.repositories.memory_approvals import InMemoryApprovalRepository
from mosaic_api.repositories.memory_notifications import InMemoryNotificationRepository
from mosaic_api.repositories.notifications import NotificationPersistence
from mosaic_api.services import (
    DirectoryService,
    EntitlementService,
    EnvironmentFindingsService,
    EnvironmentService,
    GatewayService,
    McpEndpointService,
    ModelEndpointService,
    PortalService,
    PublishingService,
    UsageService,
)
from mosaic_api.services.analytics import AnalyticsService
from mosaic_api.services.approval_ai import ApprovalAiService, HttpGovernedApprovalModel
from mosaic_api.services.approval_events import GrantNotificationRecipientResolver
from mosaic_api.services.approvals import ApprovalService
from mosaic_api.services.budget_gate import BlockedListGate
from mosaic_api.services.budget_notification_bridge import BudgetNotificationBridge
from mosaic_api.services.budgets import BudgetService
from mosaic_api.services.capability_notification_sources import CapabilityNotificationSource
from mosaic_api.services.cost_centers import CostCenterService
from mosaic_api.services.directory import Actor
from mosaic_api.services.environment_notification_sources import EnvironmentNotificationSource
from mosaic_api.services.feature_readiness import (
    CapabilityProbe,
    NotificationReadinessService,
    ai_approval_capability,
    container_retention_probe,
    gateway_usage_probe,
)
from mosaic_api.services.mcp_endpoints import build_mcp_client_factory
from mosaic_api.services.mcp_publishing import McpPublishingService
from mosaic_api.services.model_pools import ModelPoolService
from mosaic_api.services.notification_digests import (
    DigestNotificationDispatcher,
    NotificationDigestCoordinator,
)
from mosaic_api.services.notification_email import GraphNotificationDispatcher
from mosaic_api.services.notification_preferences import NotificationPreferenceService
from mosaic_api.services.notification_sources import (
    CosmosNotificationRunSource,
    InMemoryNotificationRunSource,
    NotificationRunSource,
    NotificationSourceService,
)
from mosaic_api.services.notification_visibility import (
    NotificationVisibility,
    usage_role_authorizer,
)
from mosaic_api.services.notifications import NotificationService
from mosaic_api.services.pool_health import PoolHealthService
from mosaic_api.services.portal_access import PortalAccessService
from mosaic_api.services.pricing import PricingService
from mosaic_api.services.scheduled_features import ScheduledFeatureWorker
from mosaic_api.services.teams import TeamsChannelDispatcher, TeamsPersonalDispatcher, TeamsService
from mosaic_api.services.telemetry import TelemetryService
from mosaic_api.services.usage import RollupUsageSource, SimulatedUsageSource, UsageSource
from mosaic_api.services.usage_notification_source import PortalUsageNotificationSource
from mosaic_api.services.usage_notifications import (
    DirectoryUsageNotificationIdentities,
    UsageNotificationProducer,
)
from mosaic_api.services.usage_rollup import UsageRollupService
from mosaic_api.teams_api import teams_router

logger = structlog.get_logger()


def _credential(settings: Settings) -> DefaultAzureCredential | ManagedIdentityCredential:
    if settings.environment in {Environment.LOCAL, Environment.TEST}:
        return DefaultAzureCredential()
    return ManagedIdentityCredential()


async def _resume_automatic_keys(
    app: FastAPI, tenant_id: str, *, interval_seconds: float = 300
) -> None:
    while True:
        try:
            await app.state.portal_access_service.resume_automatic_keys(tenant_id)
        except Exception:
            logger.exception("automatic_key_recovery_failed")
        await asyncio.sleep(interval_seconds)


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    configure_logging(app_settings)
    configure_telemetry(app_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        credential = _credential(app_settings)
        cosmos_client: CosmosClient | None = None
        repository: DirectoryRepository
        gateway_repository: GatewayRepository
        endpoint_repository: ModelEndpointRepository
        entitlement_repository: EntitlementRepository
        environment_repository: EnvironmentRepository
        connection_repository: AzureConnectionRepository
        mcp_repository: McpEndpointRepository
        rollup_repository: UsageRollupRepository
        pricing_repository: PricingRepository
        cost_center_repository: CostCenterRepository
        budget_repository: BudgetRepository
        approval_repository: InMemoryApprovalRepository | CosmosApprovalRepository
        notification_events: dict[tuple[str, str], NotificationEvent] = {}
        notification_repository: NotificationPersistence
        operation_source: NotificationRunSource
        retention_probes: tuple[CapabilityProbe, ...] = ()
        if app_settings.repository_backend is RepositoryBackend.MEMORY:
            repository = InMemoryDirectoryRepository()
            approval_repository = InMemoryApprovalRepository()
            connection_repository = InMemoryAzureConnectionRepository()
            gateway_repository = InMemoryGatewayRepository()
            endpoint_repository = InMemoryModelEndpointRepository()
            entitlement_repository = InMemoryEntitlementRepository(
                directory_repository=repository,
                notification_events=notification_events,
                approval_policies=approval_repository.policies,
            )
            mcp_repository = InMemoryMcpEndpointRepository()
            environment_repository = InMemoryEnvironmentRepository(
                gateway_repository, endpoint_repository, mcp_repository
            )
            rollup_repository = InMemoryUsageRollupRepository()
            pricing_repository = InMemoryPricingRepository()
            cost_center_repository = InMemoryCostCenterRepository()
            budget_repository = InMemoryBudgetRepository()
            notification_repository = InMemoryNotificationRepository(
                events=notification_events,
                audit_events=repository.audit_events,
            )
            operation_source = InMemoryNotificationRunSource(
                gateway_repository.sync_runs,
                gateway_repository.publish_runs,
                endpoint_repository.sync_runs,
                mcp_repository.sync_runs,
            )
        else:
            cosmos_client = CosmosClient(str(app_settings.cosmos_endpoint), credential=credential)
            approval_repository = CosmosApprovalRepository(
                cosmos_client,
                app_settings.cosmos_database,
                app_settings.cosmos_desired_state_container,
                app_settings.cosmos_audit_events_container,
                owns_client=False,
            )
            connection_repository = CosmosAzureConnectionRepository(
                cosmos_client,
                app_settings.cosmos_database,
                app_settings.cosmos_desired_state_container,
                app_settings.cosmos_audit_events_container,
                owns_client=False,
            )
            rollup_repository = CosmosUsageRollupRepository(
                cosmos_client,
                app_settings.cosmos_database,
                app_settings.cosmos_usage_rollups_container,
                owns_client=False,
            )
            repository = CosmosDirectoryRepository(
                cosmos_client,
                app_settings.cosmos_database,
                app_settings.cosmos_desired_state_container,
                app_settings.cosmos_audit_events_container,
                owns_client=False,
            )
            gateway_repository = CosmosGatewayRepository(
                cosmos_client,
                app_settings.cosmos_database,
                app_settings.cosmos_desired_state_container,
                app_settings.cosmos_audit_events_container,
                app_settings.cosmos_sync_operations_container,
                app_settings.cosmos_observed_state_container,
                owns_client=False,
            )
            endpoint_repository = CosmosModelEndpointRepository(
                cosmos_client,
                app_settings.cosmos_database,
                app_settings.cosmos_desired_state_container,
                app_settings.cosmos_audit_events_container,
                app_settings.cosmos_sync_operations_container,
                app_settings.cosmos_observed_state_container,
                owns_client=False,
            )
            mcp_repository = CosmosMcpEndpointRepository(
                cosmos_client,
                app_settings.cosmos_database,
                app_settings.cosmos_desired_state_container,
                app_settings.cosmos_audit_events_container,
                app_settings.cosmos_sync_operations_container,
                app_settings.cosmos_observed_state_container,
                owns_client=False,
            )
            entitlement_repository = CosmosEntitlementRepository(
                cosmos_client,
                app_settings.cosmos_database,
                app_settings.cosmos_desired_state_container,
                app_settings.cosmos_audit_events_container,
                owns_client=False,
            )
            environment_repository = CosmosEnvironmentRepository(
                cosmos_client,
                app_settings.cosmos_database,
                app_settings.cosmos_desired_state_container,
                app_settings.cosmos_audit_events_container,
                owns_client=False,
            )
            pricing_repository = CosmosPricingRepository(
                cosmos_client,
                app_settings.cosmos_database,
                app_settings.cosmos_desired_state_container,
                app_settings.cosmos_audit_events_container,
                owns_client=False,
            )
            cost_center_repository = CosmosCostCenterRepository(
                cosmos_client,
                app_settings.cosmos_database,
                app_settings.cosmos_desired_state_container,
                app_settings.cosmos_audit_events_container,
                owns_client=False,
            )
            budget_repository = CosmosBudgetRepository(
                cosmos_client,
                app_settings.cosmos_database,
                app_settings.cosmos_desired_state_container,
                app_settings.cosmos_audit_events_container,
                app_settings.cosmos_usage_rollups_container,
                owns_client=False,
            )
            notification_repository = CosmosNotificationRepository(
                cosmos_client,
                app_settings.cosmos_database,
                app_settings.cosmos_desired_state_container,
                app_settings.cosmos_audit_events_container,
                owns_client=False,
            )
            operation_source = CosmosNotificationRunSource(
                cosmos_client.get_database_client(
                    app_settings.cosmos_database
                ).get_container_client(app_settings.cosmos_sync_operations_container)
            )
            database = cosmos_client.get_database_client(app_settings.cosmos_database)
            retention_probes = (
                container_retention_probe(
                    database.get_container_client(app_settings.cosmos_desired_state_container),
                    scope="desired-state",
                ),
                container_retention_probe(
                    database.get_container_client(app_settings.cosmos_usage_rollups_container),
                    scope="usage-rollups",
                ),
            )
        authenticator = (
            LocalAuthenticator(app_settings.tenant_id, app_settings.local_roles)
            if app_settings.auth_mode is AuthMode.LOCAL
            else EntraAuthenticator(app_settings)
        )
        directory_lookup: DirectoryLookup | None = (
            GraphDirectoryLookup(
                credential,
                endpoint=str(app_settings.graph_endpoint),
                tenant_id=app_settings.tenant_id,
            )
            if app_settings.auth_mode is AuthMode.ENTRA
            else None
        )
        arm_client = ArmClient(credential)
        uses_rollups = app_settings.uses_usage_rollups
        log_client = (
            LogAnalyticsClient(credential, endpoint=str(app_settings.log_analytics_endpoint))
            if uses_rollups
            else None
        )
        management_vault_reader = KeyVaultSecretReader(credential)
        management_vault_writer = (
            KeyVaultSecretWriter(credential, str(app_settings.management_key_vault_uri))
            if app_settings.management_key_vault_uri
            else None
        )

        async def read_management_secret(secret_uri: str) -> str:
            secret = KeyVaultSecretId.parse(secret_uri)
            configured = app_settings.management_key_vault_uri
            if configured is None or secret.vault_uri.rstrip("/") != str(configured).rstrip("/"):
                raise ValidationError(
                    "Management credentials must be in the isolated credentials vault"
                )
            return await management_vault_reader.read(secret_uri)

        connection_service = AzureConnectionService(
            connection_repository,
            tenant_id=app_settings.tenant_id,
            gateways=gateway_repository,
            endpoints=endpoint_repository,
            mcps=mcp_repository,
            secret_reader=read_management_secret,
            secret_store=management_vault_writer,
        )
        azure_clients = AzureClientRegistry(
            connection_service,
            host_arm=arm_client,
            host_credential=credential,
            host_logs=log_client,
        )
        app.state.azure_connection_service = connection_service
        app.state.azure_connection_repository = connection_repository
        app.state.azure_clients = azure_clients
        gateway_service = GatewayService(
            gateway_repository,
            client_factory=lambda resource: ApimClient(azure_clients.arm_for(resource), resource),
            connection_validator=connection_service.validate_resource,
            principal_id=app_settings.managed_identity_principal_id,
            identity_resolver=arm_client.caller_object_id,
            bootstrap_resource_id=app_settings.apim_bootstrap_resource_id,
            environment_repository=environment_repository,
        )
        key_vault_reader = KeyVaultSecretReader(credential)
        # Where MOSAIC keeps an API key an administrator gives it (ADR 0021). Without a vault of
        # its own, MOSAIC takes only the URI of a secret someone else stored.
        key_vault_writer = (
            KeyVaultSecretWriter(credential, str(app_settings.key_vault_uri))
            if app_settings.key_vault_uri
            else None
        )
        # A key-authenticated endpoint's key goes only to its own Azure AI host, over a client
        # that follows no redirects and shares nothing with the ARM pool.
        endpoint_key_probe = EndpointKeyProbe()
        subscription_scanner = SubscriptionScanner(arm_client)
        # Every gateway MOSAIC manages holds the list of cost centers whose budgets block their
        # calls, which every governed policy reads. See ADR 0023.
        blocked_list = BlockedListGate(
            budget_repository,
            gateway_repository=gateway_repository,
            enabled=app_settings.budgets_enabled,
        )
        model_endpoint_service = ModelEndpointService(
            endpoint_repository,
            gateway_repository=gateway_repository,
            client_factory=lambda resource: CognitiveServicesClient(
                azure_clients.arm_for(resource), resource
            ),
            connection_validator=connection_service.validate_resource,
            scanner=subscription_scanner,
            scanner_factory=lambda context: SubscriptionScanner(azure_clients.arm_for(context)),
            principal_id=app_settings.managed_identity_principal_id,
            identity_resolver=arm_client.caller_object_id,
            bootstrap_subscription_id=app_settings.apim_subscription_id,
            environment_repository=environment_repository,
            secret_resolver=key_vault_reader.read,
            key_probe=endpoint_key_probe.check,
            vault_locator=KeyVaultLocator(
                arm_client,
                scanner=subscription_scanner,
                known_vault_ids=(
                    [app_settings.key_vault_resource_id]
                    if app_settings.key_vault_resource_id
                    else []
                ),
            ),
            key_store=key_vault_writer,
        )
        publishing_service = PublishingService(
            gateway_repository,
            endpoint_repository=endpoint_repository,
            client_factory=lambda resource: ApimClient(azure_clients.arm_for(resource), resource),
            writer_factory=lambda resource: ApimWriter(azure_clients.arm_for(resource), resource),
            directory_repository=repository,
            entitlement_repository=entitlement_repository,
            model_runtime_client_id=app_settings.model_runtime_client_id,
            security_group_claims=app_settings.entra_group_claims,
            environment_repository=environment_repository,
            cost_center_repository=cost_center_repository,
            blocked_list=blocked_list,
        )
        mcp_publishing_service = McpPublishingService(
            gateway_repository,
            mcp_endpoint_repository=mcp_repository,
            entitlement_repository=entitlement_repository,
            directory_repository=repository,
            client_factory=lambda resource: ApimClient(azure_clients.arm_for(resource), resource),
            writer_factory=lambda resource: ApimWriter(azure_clients.arm_for(resource), resource),
            runtime_client_id=app_settings.model_runtime_client_id,
            security_group_claims=app_settings.entra_group_claims,
            environment_repository=environment_repository,
            cost_center_repository=cost_center_repository,
            blocked_list=blocked_list,
        )
        model_pool_service = ModelPoolService(
            gateway_repository,
            endpoint_repository=endpoint_repository,
            client_factory=lambda resource: ApimClient(azure_clients.arm_for(resource), resource),
            writer_factory=lambda resource: ApimWriter(azure_clients.arm_for(resource), resource),
            environment_repository=environment_repository,
            directory_repository=repository,
            entitlement_repository=entitlement_repository,
            cost_center_repository=cost_center_repository,
            model_runtime_client_id=app_settings.model_runtime_client_id,
            security_group_claims=app_settings.entra_group_claims,
            blocked_list=blocked_list,
            pricing_repository=pricing_repository,
        )
        # A dedicated client for outbound MCP calls: redirects are refused per request, and the
        # connection pool for operator-supplied hosts is kept away from the ARM one.
        mcp_http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(app_settings.mcp_discovery_timeout_seconds, connect=10.0),
            follow_redirects=False,
        )
        mcp_endpoint_service = McpEndpointService(
            mcp_repository,
            gateway_repository=gateway_repository,
            entitlement_repository=entitlement_repository,
            client_factory=build_mcp_client_factory(mcp_http_client),
            connection_validator=connection_service.validate_resource,
            secret_resolver=key_vault_reader.read,
            token_resolver=EntraTokenProvider(credential).token_for,
            connection_token_resolver=azure_clients.token_for_connection,
            require_https=app_settings.environment is Environment.AZURE,
            allow_private_endpoints=app_settings.mcp_allow_private_endpoints,
            environment_repository=environment_repository,
        )
        app.state.repository = repository
        app.state.directory_lookup = directory_lookup
        app.state.gateway_repository = gateway_repository
        app.state.model_endpoint_repository = endpoint_repository
        app.state.entitlement_repository = entitlement_repository
        app.state.environment_repository = environment_repository
        app.state.mcp_endpoint_repository = mcp_repository
        app.state.cost_center_repository = cost_center_repository
        entitlement_service = EntitlementService(
            entitlement_repository,
            directory_repository=repository,
            gateway_repository=gateway_repository,
            endpoint_repository=endpoint_repository,
            directory_lookup=directory_lookup,
            cost_center_repository=cost_center_repository,
            environment_repository=environment_repository,
        )
        app.state.directory_service = DirectoryService(
            repository,
            gateway_repository=gateway_repository,
            entitlement_repository=entitlement_repository,
            directory_lookup=directory_lookup,
            group_claims_enabled=app_settings.entra_group_claims,
            cost_center_repository=cost_center_repository,
            cost_center_checks=entitlement_service,
        )
        app.state.gateway_service = gateway_service
        app.state.model_endpoint_service = model_endpoint_service
        app.state.publishing_service = publishing_service
        app.state.mcp_publishing_service = mcp_publishing_service
        app.state.model_pool_service = model_pool_service
        app.state.mcp_endpoint_service = mcp_endpoint_service
        app.state.entitlement_service = entitlement_service
        app.state.cost_center_service = CostCenterService(
            cost_center_repository,
            directory_repository=repository,
            entitlement_repository=entitlement_repository,
            gateway_repository=gateway_repository,
            entitlements=entitlement_service,
        )
        environment_service = EnvironmentService(
            environment_repository,
            gateway_repository=gateway_repository,
            endpoint_repository=endpoint_repository,
            mcp_repository=mcp_repository,
            entitlement_repository=entitlement_repository,
            directory_repository=repository,
        )
        app.state.environment_service = environment_service
        app.state.environment_findings_service = EnvironmentFindingsService(
            environment_service=environment_service,
            gateway_repository=gateway_repository,
            endpoint_repository=endpoint_repository,
            mcp_repository=mcp_repository,
        )
        app.state.portal_access_service = PortalAccessService(
            entitlement_service,
            repository=entitlement_repository,
            directory_repository=repository,
            gateway_repository=gateway_repository,
            credential_factory=lambda resource: ApimCredentialClient(
                azure_clients.arm_for(resource), resource
            ),
            model_runtime_client_id=app_settings.model_runtime_client_id,
            model_client_id=app_settings.model_client_id,
            key_manager_factory=lambda resource: ApimKeyManager(
                azure_clients.arm_for(resource), resource
            ),
        )
        app.state.portal_service = PortalService(
            entitlement_service,
            directory_repository=repository,
            gateway_repository=gateway_repository,
            endpoint_repository=endpoint_repository,
        )
        feature_http_client = httpx.AsyncClient(timeout=30, follow_redirects=False)
        approval_service = ApprovalService(
            approval_repository,
            entitlements=entitlement_service,
            hosting_verified=app_settings.scheduled_workers_always_on,
        )
        approval_ai_service = ApprovalAiService(
            approval_repository,
            entitlements=entitlement_service,
            portal_access=app.state.portal_access_service,
            directory_repository=repository,
            model=HttpGovernedApprovalModel(credential, client=feature_http_client),
        )
        app.state.approval_repository = approval_repository
        app.state.approval_service = approval_service
        app.state.approval_ai_service = approval_ai_service
        telemetry_service = TelemetryService(
            gateway_repository=gateway_repository,
            rollup_repository=rollup_repository,
            entitlement_repository=entitlement_repository,
            client_factory=lambda resource: ApimClient(azure_clients.arm_for(resource), resource),
            writer_factory=lambda resource: ApimWriter(azure_clients.arm_for(resource), resource),
            logs=log_client,
            logs_factory=azure_clients.logs_for if uses_rollups else None,
            rollups_enabled=uses_rollups and app_settings.usage_rollup_enabled,
            interval_seconds=app_settings.usage_rollup_interval_seconds,
            principal_id=app_settings.managed_identity_principal_id,
            identity_resolver=arm_client.caller_object_id,
            gateway_identity_resolver=lambda gateway: azure_clients.arm_for(
                gateway
            ).caller_object_id(),
        )
        pool_health_service = PoolHealthService(
            model_pool_service,
            gateway_repository=gateway_repository,
            endpoint_repository=endpoint_repository,
            logs=log_client,
            logs_factory=azure_clients.logs_for if uses_rollups else None,
            principal_id=app_settings.managed_identity_principal_id,
            identity_resolver=arm_client.caller_object_id,
            gateway_identity_resolver=lambda gateway: azure_clients.arm_for(
                gateway
            ).caller_object_id(),
        )
        rollup_service = (
            UsageRollupService(
                rollup_repository,
                gateway_repository=gateway_repository,
                entitlement_repository=entitlement_repository,
                directory_repository=repository,
                logs=log_client,
                logs_factory=azure_clients.logs_for,
                telemetry=telemetry_service,
                tenant_id=app_settings.tenant_id,
                interval_seconds=app_settings.usage_rollup_interval_seconds,
                retention_days=app_settings.usage_rollup_retention_days,
                backfill_max_days=app_settings.usage_rollup_backfill_max_days,
                cost_center_repository=cost_center_repository,
                endpoint_repository=endpoint_repository,
            )
            if log_client is not None and app_settings.usage_rollup_enabled
            else None
        )
        usage_source: UsageSource = (
            RollupUsageSource(
                rollup_repository, interval_seconds=app_settings.usage_rollup_interval_seconds
            )
            if uses_rollups
            else SimulatedUsageSource(environment_repository=environment_repository)
        )
        pricing_service = PricingService(
            pricing_repository,
            endpoint_repository=endpoint_repository,
            gateway_repository=gateway_repository,
            rollup_repository=rollup_repository,
            seed=load_seed(),
        )
        app.state.pricing_repository = pricing_repository
        app.state.pricing_service = pricing_service
        app.state.usage_rollup_repository = rollup_repository
        app.state.telemetry_service = telemetry_service
        app.state.pool_health_service = pool_health_service
        app.state.usage_rollup_service = rollup_service
        app.state.usage_service = UsageService(
            app.state.portal_service,
            source=usage_source,
            gateway_repository=gateway_repository,
            endpoint_repository=endpoint_repository,
            environment_repository=environment_repository,
            pricing=pricing_service,
            entitlement_repository=entitlement_repository,
            cost_center_repository=cost_center_repository,
        )
        app.state.analytics_service = AnalyticsService(
            rollup_repository,
            gateway_repository=gateway_repository,
            entitlement_repository=entitlement_repository,
            directory_repository=repository,
            environment_repository=environment_repository,
            endpoint_repository=endpoint_repository,
            rollups=rollup_service,
            directory_lookup=directory_lookup,
            pricing=pricing_service,
            configured=uses_rollups,
            interval_seconds=app_settings.usage_rollup_interval_seconds,
            retention_days=app_settings.usage_rollup_retention_days,
            cost_center_repository=cost_center_repository,
        )
        email_credential = ManagedIdentityCredential()
        email_client = GraphEmailClient(
            email_credential,
            tenant_id=app_settings.tenant_id,
            endpoint=str(app_settings.graph_endpoint),
            principal_id=app_settings.managed_identity_principal_id,
        )
        console_origin = (
            str(app_settings.frontend_url).rstrip("/")
            if app_settings.frontend_url and app_settings.frontend_url.scheme == "https"
            else None
        )
        portal_origin = (
            str(app_settings.portal_url).rstrip("/")
            if app_settings.portal_url and app_settings.portal_url.scheme == "https"
            else None
        )
        notification_directory = (
            GraphNotificationDirectory(
                directory_lookup,
                api_client_id=app_settings.api_client_id,
                admin_role=app_settings.required_role,
            )
            if isinstance(directory_lookup, GraphDirectoryLookup) and app_settings.api_client_id
            else None
        )
        portal_role_directory = (
            GraphNotificationDirectory(
                directory_lookup,
                api_client_id=app_settings.api_client_id,
                admin_role=app_settings.portal_role,
            )
            if isinstance(directory_lookup, GraphDirectoryLookup) and app_settings.api_client_id
            else None
        )
        usage_producer = UsageNotificationProducer(
            notification_repository,
            PortalUsageNotificationSource(app.state.usage_service),
            DirectoryUsageNotificationIdentities(repository, directory_lookup=directory_lookup),
            tenant_id=app_settings.tenant_id,
            authorize_recipient=usage_role_authorizer(
                app_settings,
                notification_directory,
                portal_role_directory,
            ),
        )
        grant_recipients = GrantNotificationRecipientResolver(
            repository,
            directory_lookup=directory_lookup,
        )
        visibility = NotificationVisibility(
            entitlement_repository,
            repository,
            grant_recipients=grant_recipients.expand,
            grant_member=grant_recipients.contains,
            usage_authorizer=usage_producer.authorize_delivery,
        )
        preferences = NotificationPreferenceService(
            notification_repository,
            directory=notification_directory,
            recipient_visibility=visibility,
        )
        digests = NotificationDigestCoordinator(
            notification_repository,
            preferences,
            tenant_id=app_settings.tenant_id,
            hosting_verified=app_settings.scheduled_workers_always_on,
        )
        bot_credential: ManagedIdentityCredential | None = None
        bot_tokens: BotTokenSource | None = None
        if app_settings.teams_bot_auth_mode == "managedIdentity" and app_settings.teams_bot_app_id:
            bot_credential = ManagedIdentityCredential(client_id=app_settings.teams_bot_app_id)
            bot_tokens = ManagedIdentityBotTokenSource(
                bot_credential,
                bot_app_id=app_settings.teams_bot_app_id,
            )
        elif (
            app_settings.teams_bot_auth_mode == "clientSecret"
            and app_settings.teams_bot_app_id
            and app_settings.teams_bot_secret_reference
        ):
            bot_tokens = SecretBotTokenSource(
                feature_http_client,
                key_vault_reader,
                tenant_id=app_settings.tenant_id,
                bot_app_id=app_settings.teams_bot_app_id,
                secret_reference=str(app_settings.teams_bot_secret_reference),
            )
        teams_service = TeamsService(
            notification_repository,
            tenant_id=app_settings.tenant_id,
            secret_reader=key_vault_reader,
            secret_writer=key_vault_writer,
            transport=TeamsHttpTransport(
                feature_http_client,
                WorkflowEndpointPolicy(frozenset(app_settings.teams_workflow_allowed_hosts)),
            ),
            authenticator=TeamsLifecycleAuthenticator(feature_http_client),
            token_source=bot_tokens,
            console_origin=console_origin,
            portal_origin=portal_origin,
        )
        email_dispatcher = GraphNotificationDispatcher(
            email_client,
            budget_repository,
            mailbox_resolver=preferences.mailbox_resolver,
            frontend_url=console_origin,
            portal_url=portal_origin,
        )
        budget_bridge = BudgetNotificationBridge(
            budget_repository,
            notification_repository,
            cost_center_repository=cost_center_repository,
            sender=email_client,
            fallback=email_dispatcher,
            tenant_id=app_settings.tenant_id,
            enabled=app_settings.budgets_enabled,
        )
        notification_service = NotificationService(
            notification_repository,
            audience_resolver=digests,
            delivery_authorizer=preferences,
            legacy_budget_authorizer=budget_bridge,
            dispatchers={
                "email": DigestNotificationDispatcher(digests, budget_bridge),
                "teamsChannel": DigestNotificationDispatcher(
                    digests,
                    TeamsChannelDispatcher(teams_service),
                ),
                "teamsPersonal": DigestNotificationDispatcher(
                    digests,
                    TeamsPersonalDispatcher(teams_service),
                ),
            },
            hosting_verified=app_settings.scheduled_workers_always_on,
        )
        app.state.notification_repository = notification_repository
        app.state.notification_preference_service = preferences
        app.state.notification_service = notification_service
        app.state.notification_digest_service = digests
        app.state.usage_notification_producer = usage_producer
        app.state.teams_service = teams_service
        app.state.budget_notification_bridge = budget_bridge
        budget_service = BudgetService(
            budget_repository,
            cost_center_repository=cost_center_repository,
            gateway_repository=gateway_repository,
            gate=blocked_list,
            client_factory=lambda resource: ApimClient(azure_clients.arm_for(resource), resource),
            writer_factory=lambda resource: ApimWriter(azure_clients.arm_for(resource), resource),
            analytics=app.state.analytics_service,
            email=email_client,
            portal=app.state.portal_service,
            tenant_id=app_settings.tenant_id,
            priced=uses_rollups,
            interval_seconds=app_settings.budget_interval_seconds,
            fast_interval_seconds=app_settings.budget_fast_interval_seconds,
            email_directory=notification_directory,
            enabled=app_settings.budgets_enabled,
            notification_bridge=budget_bridge,
        )
        app.state.budget_repository = budget_repository
        app.state.budget_service = budget_service
        system_actor = Actor(
            tenant_id=app_settings.tenant_id,
            object_id="mosaic-notification-worker",
        )

        async def resolve_notification_grant(identifier: str) -> Entitlement | None:
            try:
                return await entitlement_service.get_entitlement(system_actor, identifier)
            except NotFoundError:
                return None

        source_service = NotificationSourceService(
            notification_repository,
            operation_source,
            tenant_id=app_settings.tenant_id,
            resolve_grant=resolve_notification_grant,
        )

        async def ai_readiness(actor: Actor) -> list[CapabilityCheck]:
            return [
                ai_approval_capability(
                    await approval_ai_service.readiness(actor),
                    now=utc_now(),
                )
            ]

        async def team_readiness(actor: Actor) -> list[CapabilityCheck]:
            return await teams_service.capability_checks(actor.tenant_id)

        async def personal_team_readiness(actor: Actor) -> list[CapabilityCheck]:
            return await teams_service.personal_checks(
                actor.tenant_id,
                actor.object_id,
                "portal",
            )

        async def admin_personal_team_readiness(actor: Actor) -> list[CapabilityCheck]:
            return await teams_service.personal_checks(
                actor.tenant_id,
                actor.object_id,
                "admin",
            )

        async def directory_readiness(actor: Actor) -> list[CapabilityCheck]:
            return await preferences.readiness_checks(actor.tenant_id)

        async def personal_directory_readiness(actor: Actor) -> list[CapabilityCheck]:
            return await preferences.readiness_checks(actor.tenant_id, actor.object_id)

        async def digest_readiness(actor: Actor) -> list[CapabilityCheck]:
            return await digests.readiness_checks()

        async def admin_usage_readiness(actor: Actor) -> list[CapabilityCheck]:
            return await usage_producer.readiness_checks(
                actor.tenant_id,
                actor.object_id,
                "admin",
            )

        async def personal_usage_readiness(actor: Actor) -> list[CapabilityCheck]:
            return await usage_producer.readiness_checks(
                actor.tenant_id,
                actor.object_id,
                "portal",
            )

        readiness = NotificationReadinessService(
            app_settings,
            notification_repository,
            notification_repository,
            budget_repository,
            worker_names=(
                "notifications",
                "notificationSources",
                "notificationUsage",
                "notificationCapabilities",
                "notificationFindings",
                "notificationDigestSchedule",
                "notificationBudgetBridge",
            ),
            worker_intervals={
                "notificationUsage": app_settings.usage_rollup_interval_seconds,
                "notificationCapabilities": app_settings.usage_rollup_interval_seconds,
                "notificationFindings": app_settings.usage_rollup_interval_seconds,
            },
            admin_probes=(
                *retention_probes,
                ai_readiness,
                team_readiness,
                directory_readiness,
                digest_readiness,
                gateway_usage_probe(gateway_repository, telemetry_service, configured=uses_rollups),
            ),
            admin_self_probes=(admin_usage_readiness, admin_personal_team_readiness),
            personal_probes=(
                personal_team_readiness,
                personal_directory_readiness,
                personal_usage_readiness,
            ),
        )
        app.state.notification_readiness_service = readiness
        capability_source = CapabilityNotificationSource(
            notification_repository,
            lambda: readiness.admin_checks(system_actor, include_personal=False),
            tenant_id=app_settings.tenant_id,
        )
        finding_source = EnvironmentNotificationSource(
            notification_repository,
            lambda: app.state.environment_findings_service.list_findings(system_actor),
            tenant_id=app_settings.tenant_id,
        )
        feature_workers = [
            ScheduledFeatureWorker(
                notification_repository,
                gateway_repository,
                action,
                tenant_id=app_settings.tenant_id,
                name=name,
                interval_seconds=interval,
                timeout_seconds=timeout,
            )
            for name, action, interval, timeout in (
                (
                    "notificationSources",
                    source_service.reconcile,
                    app_settings.notification_interval_seconds,
                    45,
                ),
                (
                    "notificationUsage",
                    usage_producer.reconcile,
                    app_settings.usage_rollup_interval_seconds,
                    150,
                ),
                (
                    "notificationCapabilities",
                    capability_source.reconcile,
                    app_settings.usage_rollup_interval_seconds,
                    150,
                ),
                (
                    "notificationFindings",
                    finding_source.reconcile,
                    app_settings.usage_rollup_interval_seconds,
                    45,
                ),
                (
                    "notificationDigestSchedule",
                    digests.reconcile,
                    app_settings.notification_interval_seconds,
                    120,
                ),
                (
                    "notificationBudgetBridge",
                    budget_bridge.reconcile,
                    app_settings.notification_interval_seconds,
                    120,
                ),
            )
        ]
        # Budgets are judged from rolled-up usage, so the check runs only where there is some.
        budget_loop = app_settings.budgets_enabled and uses_rollups
        app.state.authenticator = authenticator
        try:
            reaped = await gateway_service.reap_stale_sync_runs(app_settings.tenant_id)
            if reaped:
                logger.warning("gateway_sync_runs_reaped", count=reaped)
        except Exception:
            logger.exception("gateway_sync_reap_failed")
        try:
            reaped = await model_endpoint_service.reap_stale_sync_runs(app_settings.tenant_id)
            if reaped:
                logger.warning("endpoint_sync_runs_reaped", count=reaped)
        except Exception:
            logger.exception("endpoint_sync_reap_failed")
        try:
            reaped = await publishing_service.reap_stale_publish_runs(app_settings.tenant_id)
            if reaped:
                logger.warning("publish_runs_reaped", count=reaped)
        except Exception:
            logger.exception("publish_reap_failed")
        try:
            reaped = await mcp_publishing_service.reap_stale_publish_runs(app_settings.tenant_id)
            if reaped:
                logger.warning("mcp_publish_runs_reaped", count=reaped)
        except Exception:
            logger.exception("mcp_publish_reap_failed")
        try:
            reaped = await model_pool_service.reap_stale_publish_runs(app_settings.tenant_id)
            if reaped:
                logger.warning("pool_publish_runs_reaped", count=reaped)
        except Exception:
            logger.exception("pool_publish_reap_failed")
        try:
            reaped = await mcp_endpoint_service.reap_stale_sync_runs(app_settings.tenant_id)
            if reaped:
                logger.warning("mcp_endpoint_sync_runs_reaped", count=reaped)
        except Exception:
            logger.exception("mcp_endpoint_sync_reap_failed")
        automatic_keys_task = asyncio.create_task(
            _resume_automatic_keys(app, app_settings.tenant_id),
            name="automatic-key-recovery",
        )
        gateway_service.schedule_bootstrap(app_settings.tenant_id)
        if rollup_service is not None:
            if budget_loop:
                rollup_service.add_listener(budget_service.rollups_changed)
            rollup_service.start()
        if budget_loop:
            budget_service.start()
        notification_loops = (
            app_settings.notifications_enabled and app_settings.environment is not Environment.TEST
        )
        if notification_loops:
            for worker in feature_workers:
                worker.start()
        notification_task = (
            asyncio.create_task(
                notification_service.run(
                    app_settings.tenant_id,
                    interval_seconds=app_settings.notification_interval_seconds,
                ),
                name="notifications",
            )
            if notification_loops
            else None
        )
        approval_task = (
            asyncio.create_task(
                approval_service.run_forever(
                    app_settings.tenant_id,
                    interval_seconds=app_settings.approval_interval_seconds,
                ),
                name="approvalEvaluator",
            )
            if app_settings.approval_evaluator_enabled
            else None
        )
        logger.info(
            "application_started",
            environment=app_settings.environment,
            usage_source="logAnalytics" if uses_rollups else "simulated",
        )
        try:
            yield
        finally:
            automatic_keys_task.cancel()
            with suppress(asyncio.CancelledError):
                await automatic_keys_task
            if notification_task is not None:
                notification_task.cancel()
                with suppress(asyncio.CancelledError):
                    await notification_task
            for worker in feature_workers:
                await worker.aclose()
            if approval_task is not None:
                approval_task.cancel()
                with suppress(asyncio.CancelledError):
                    await approval_task
            await feature_http_client.aclose()
            if bot_credential is not None:
                await bot_credential.close()
            await budget_service.aclose()
            if rollup_service is not None:
                await rollup_service.aclose()
            await gateway_service.aclose()
            await model_endpoint_service.aclose()
            await publishing_service.aclose()
            await mcp_publishing_service.aclose()
            await model_pool_service.aclose()
            await mcp_endpoint_service.aclose()
            if directory_lookup:
                await directory_lookup.close()
            await authenticator.close()
            await azure_clients.close()
            await arm_client.close()
            await management_vault_reader.close()
            if management_vault_writer is not None:
                await management_vault_writer.close()
            await key_vault_reader.close()
            if key_vault_writer is not None:
                await key_vault_writer.close()
            await endpoint_key_probe.close()
            await email_client.close()
            await email_credential.close()
            await mcp_http_client.aclose()
            if log_client is not None:
                await log_client.close()
            await repository.close()
            await gateway_repository.close()
            await endpoint_repository.close()
            await entitlement_repository.close()
            await environment_repository.close()
            await connection_repository.close()
            await mcp_repository.close()
            await rollup_repository.close()
            await pricing_repository.close()
            await cost_center_repository.close()
            await budget_repository.close()
            await approval_repository.close()
            await notification_repository.close()
            if cosmos_client:
                await cosmos_client.close()
            await credential.close()

    app = FastAPI(
        title="MOSAIC API",
        version="0.1.0",
        description="Desired-state control plane for Azure API Management AI gateway governance.",
        lifespan=lifespan,
    )
    # The instrumentation wraps the middleware stack when it is built, so it is the outermost
    # layer, around the middleware added below, and records the requests they answer themselves.
    instrument_requests(app, app_settings)
    app.state.settings = app_settings
    app.add_exception_handler(DomainError, domain_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    if app_settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=app_settings.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "If-Match", "X-Correlation-ID"],
            # The console saves each CSV export under the name the API gives it.
            expose_headers=["Content-Disposition", "Retry-After"],
        )

    @app.middleware("http")
    async def correlation_id(request: Request, call_next: Any) -> Any:
        correlation = request.headers.get("X-Correlation-ID")
        response = await call_next(request)
        if request.url.path.startswith("/api/v1/"):
            response.headers["Cache-Control"] = "no-store, private"
        if request.url.path.endswith("/keys/reveal"):
            response.headers["Pragma"] = "no-cache"
        if correlation:
            response.headers["X-Correlation-ID"] = correlation
        return response

    # App Service's Always On pings the root every five minutes to keep the API loaded. The API
    # serves nothing there, so it answers with an empty 200 that needs no sign-in and says nothing
    # about the API, rather than a 404 that would count as a failed request. It isn't recorded.
    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def root() -> Response:
        return Response()

    @app.get("/healthz", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"])
    async def ready(request: Request) -> JSONResponse:
        repository = getattr(request.app.state, "repository", None)
        gateway_repository = getattr(request.app.state, "gateway_repository", None)
        endpoint_repository = getattr(request.app.state, "model_endpoint_repository", None)
        entitlement_repository = getattr(request.app.state, "entitlement_repository", None)
        mcp_repository = getattr(request.app.state, "mcp_endpoint_repository", None)
        environment_repository = getattr(request.app.state, "environment_repository", None)
        connection_repository = getattr(request.app.state, "azure_connection_repository", None)
        rollup_repository = getattr(request.app.state, "usage_rollup_repository", None)
        pricing_repository = getattr(request.app.state, "pricing_repository", None)
        cost_center_repository = getattr(request.app.state, "cost_center_repository", None)
        budget_repository = getattr(request.app.state, "budget_repository", None)
        approval_repository = getattr(request.app.state, "approval_repository", None)
        notification_repository = getattr(request.app.state, "notification_repository", None)
        is_ready = (
            repository is not None
            and await repository.ready()
            and gateway_repository is not None
            and await gateway_repository.ready()
            and endpoint_repository is not None
            and await endpoint_repository.ready()
            and entitlement_repository is not None
            and await entitlement_repository.ready()
            and mcp_repository is not None
            and await mcp_repository.ready()
            and environment_repository is not None
            and await environment_repository.ready()
            and connection_repository is not None
            and await connection_repository.ready()
            and rollup_repository is not None
            and await rollup_repository.ready()
            and pricing_repository is not None
            and await pricing_repository.ready()
            and cost_center_repository is not None
            and await cost_center_repository.ready()
            and budget_repository is not None
            and await budget_repository.ready()
            and approval_repository is not None
            and await approval_repository.ready()
            and notification_repository is not None
            and await notification_repository.ready()
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK if is_ready else status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "ready" if is_ready else "notReady"},
        )

    app.include_router(router)
    app.include_router(approvals_router)
    app.include_router(notification_readiness_router)
    app.include_router(notifications_router)
    app.include_router(portal_notifications_router)
    app.include_router(notification_delivery_router)
    app.include_router(notification_preferences_router)
    app.include_router(portal_notification_preferences_router)
    app.include_router(teams_router)
    app.include_router(azure_connections_router)
    app.include_router(mcp_publishing_router)
    app.include_router(model_pools_router)
    app.include_router(directory_router)
    app.include_router(analytics_router)
    app.include_router(pricing_router)
    app.include_router(cost_centers_router)
    app.include_router(portal_cost_centers_router)
    app.include_router(budgets_router)
    app.include_router(portal_budgets_router)
    app.include_router(portal_router)
    return app
