"""Settings, authenticated personal binding, and injected one-way Teams dispatchers."""

import hashlib
import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import quote, urlsplit
from uuid import uuid4

from azure.core.exceptions import AzureError
from pydantic import Field

from mosaic_api.domain import AuditEvent, KeyVaultSecretId, MosaicModel, utc_now
from mosaic_api.errors import (
    ConflictError,
    NotFoundError,
    UpstreamAuthorizationError,
    UpstreamError,
    ValidationError,
)
from mosaic_api.integrations.key_vault import KeyStore
from mosaic_api.integrations.teams import (
    BotTokenSource,
    SecretReader,
    TeamsAuthenticationError,
    TeamsHttpTransport,
    TeamsLifecycleAuthenticator,
    WorkflowEndpointPolicy,
    application_id,
    validate_service_url,
)
from mosaic_api.notifications import (
    CapabilityCheck,
    CapabilityRemediation,
    NotificationChannelHealth,
    NotificationDelivery,
    NotificationEvent,
    NotificationPersona,
    NotificationTestEvidence,
    PersonalTeamsBinding,
    PersonalTeamsSettings,
    PersonalTeamsSettingsInput,
    TeamsDestination,
    TeamsDestinationInput,
)
from mosaic_api.repositories.notifications import NotificationPersistence
from mosaic_api.services.notifications import (
    NotificationDispatchResult,
    notification_summary,
    notification_title,
)


class TeamsTestResult(NotificationTestEvidence):
    pass


class PersonalTeamsStatus(MosaicModel):
    enabled: bool = False
    state: Literal["notInstalled", "installed", "uninstalled", "blocked"] = "notInstalled"
    checks: list[CapabilityCheck] = Field(default_factory=list)
    last_test: NotificationTestEvidence | None = None


class TeamsSetupError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _record_id(kind: str, tenant: str, subject: str = "") -> str:
    return kind + "-" + hashlib.sha256(f"{tenant}:{subject}".encode()).hexdigest()[:40]


def _audit(tenant: str, actor: str, resource: str, kind: str, action: str) -> AuditEvent:
    return AuditEvent(
        id=str(uuid4()),
        tenant_id=tenant,
        action=action,
        resource_type=kind,
        resource_id=resource,
        actor_object_id=actor,
    )


def _origin(value: str | None) -> str:
    if not value or any(char.isspace() for char in value):
        raise ValidationError("Configure the HTTPS MOSAIC console and portal origins for Teams.")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValidationError("Teams MOSAIC origins must be valid HTTPS origins.") from None
    if (
        parts.scheme != "https"
        or not parts.hostname
        or port == 0
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
    ):
        raise ValidationError("Teams MOSAIC origins must be HTTPS origins, without a path.")
    return value.rstrip("/")


def _subject_path(event: NotificationEvent, persona: NotificationPersona) -> str | None:
    subject = event.subject
    if subject.request_id:
        prefix = (
            "/entitlements?tab=requests&selected=" if persona == "admin" else "/requests?request="
        )
        return prefix + quote(subject.request_id, safe="")
    if subject.entitlement_id:
        prefix = "/entitlements?tab=grants&selected=" if persona == "admin" else "/access?grant="
        return prefix + quote(subject.entitlement_id, safe="")
    if persona != "admin":
        return None
    if not subject.run_id:
        return f"/gateways/{quote(subject.gateway_id, safe='')}" if subject.gateway_id else None
    run = quote(subject.run_id, safe="")
    if subject.publication_id and subject.publication_target:
        publication = quote(subject.publication_id, safe="")
        if subject.publication_target == "pool":
            return f"/pools/{publication}?tab=plans&runId={run}"
        page = "/models" if subject.publication_target == "model" else "/mcps"
        return f"{page}?tab=published&selected={publication}&runId={run}"
    if subject.endpoint_id and subject.endpoint_kind:
        endpoint = quote(subject.endpoint_id, safe="")
        page = (
            "/models?tab=endpoints" if subject.endpoint_kind == "model" else "/mcps?tab=registered"
        )
        return f"{page}&selected={endpoint}&syncRun={run}"
    if subject.gateway_id:
        path = f"/gateways/{quote(subject.gateway_id, safe='')}"
        return f"{path}?syncRun={run}" if subject.integration == "gatewayInventory" else path
    return None


class TeamsService:
    def __init__(
        self,
        repository: NotificationPersistence,
        *,
        tenant_id: str,
        secret_reader: SecretReader | None,
        secret_writer: KeyStore | None,
        transport: TeamsHttpTransport,
        authenticator: TeamsLifecycleAuthenticator,
        token_source: BotTokenSource | None,
        console_origin: str | None,
        portal_origin: str | None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.repository = repository
        self.tenant_id = tenant_id.casefold()
        self._reader = secret_reader
        self._writer = secret_writer
        self.transport = transport
        self._authenticator = authenticator
        self._tokens = token_source
        self._console = console_origin
        self._portal = portal_origin
        self._clock = clock

    def _tenant(self, tenant: str) -> None:
        if tenant.casefold() != self.tenant_id:
            raise ValidationError("This Teams integration is single-tenant.")

    def _private_configuration(self) -> None:
        application_id(self.tenant_id)
        _origin(self._console)
        _origin(self._portal)

    def _private_configuration_ready(self) -> bool:
        try:
            self._private_configuration()
        except (ValidationError, ValueError):
            return False
        return True

    @property
    def endpoint_policy(self) -> WorkflowEndpointPolicy:
        return self.transport.policy

    async def _workflow_url(self, item: TeamsDestination) -> str:
        if self._reader is None:
            raise TeamsSetupError(
                "teamsSecretReaderMissing",
                "Configure the API's Key Vault connection before testing this destination.",
            )
        try:
            url = await self._reader.read(item.credential_reference_id)
        except UpstreamAuthorizationError:
            raise TeamsSetupError(
                "teamsSecretAccessDenied",
                "The API cannot read the Workflow secret. Grant its managed identity secret-read "
                "access on the configured vault and check vault network restrictions.",
            ) from None
        except (ValidationError, NotFoundError):
            raise TeamsSetupError(
                "teamsSecretUnavailable",
                "The saved Workflow secret is missing, empty, or invalid. Edit the destination "
                "and save the current Workflow webhook URL again.",
            ) from None
        except (UpstreamError, AzureError):
            raise TeamsSetupError(
                "teamsVaultUnavailable",
                "Key Vault could not be reached. Check the vault endpoint and network access.",
                retryable=True,
            ) from None
        try:
            return self.endpoint_policy.validate(url)
        except ValidationError:
            raise TeamsSetupError(
                "teamsEndpointNotAllowed",
                "The stored URL is not an approved Teams Workflow endpoint. Use the webhook "
                "trigger's secret-URL mode and add its exact hostname to "
                "MOSAIC_TEAMS_WORKFLOW_ALLOWED_HOSTS; legacy connectors are not supported.",
            ) from None

    def _test_scope(self, tenant: str, target: str, persona: str = "") -> str:
        return _record_id("teamsTest", tenant, f"{target}:{persona}")

    def _revision(
        self,
        revision: int,
        *,
        binding: PersonalTeamsBinding | None = None,
        secret_fingerprint: str | None = None,
    ) -> str:
        values = [
            revision,
            self._console,
            self._portal,
            sorted(self.endpoint_policy.allowed_hosts),
            self._tokens.bot_app_id if self._tokens else None,
            self._tokens.configuration_id if self._tokens else None,
            secret_fingerprint,
            binding.bot_app_id if binding else None,
            binding.authenticated_at.isoformat() if binding else None,
            binding.state if binding else None,
        ]
        return hashlib.sha256(json.dumps(values).encode()).hexdigest()[:24]

    async def _last_test(self, tenant: str, scope: str) -> NotificationTestEvidence | None:
        item = await self.repository.get_channel_health(tenant, scope)
        return item.last_test if item else None

    async def _record_test(
        self,
        tenant: str,
        scope: str,
        channel: Literal["teamsChannel", "teamsPersonal"],
        result: TeamsTestResult,
    ) -> TeamsTestResult:
        for _ in range(3):
            current = await self.repository.get_channel_health(tenant, scope)
            if current and current.last_test and current.last_test.tested_at > result.tested_at:
                return result.model_copy(
                    update={
                        "message": result.message
                        + " A newer test result is already saved and has been preserved."
                    }
                )
            item = current or NotificationChannelHealth(
                id=scope, tenant_id=tenant, channel=channel, destination_id=scope
            )
            try:
                await self.repository.save_channel_health(
                    item.model_copy(update={"last_test": result, "updated_at": self._clock()})
                )
                return result
            except ConflictError:
                continue
        raise ConflictError(
            "The test ran, but its result could not be saved. Refresh readiness and check Teams."
        )

    async def _test_check(
        self, tenant: str, scope: str, revision: str, *, path: str
    ) -> CapabilityCheck:
        test = await self._last_test(tenant, scope)
        current = bool(test and test.configuration_revision == revision)
        fresh = bool(test and timedelta(0) <= self._clock() - test.tested_at < timedelta(minutes=5))
        accepted = bool(test and test.outcome == "providerAccepted")
        return CapabilityCheck(
            feature="teamsTest",
            scope=scope,
            checked_at=self._clock(),
            state="verified" if current and fresh and accepted else "needsAction",
            configuration_revision=revision,
            last_test_at=test.tested_at if test else None,
            fresh_until=test.tested_at + timedelta(minutes=5)
            if test and current and fresh and accepted
            else None,
            reason=(
                test.message
                if test and current and fresh
                else "Previous test evidence is stale or belongs to different configuration."
                if test
                else "No explicit test has been recorded for this setup."
            ),
            evidence=[f"Explicit test outcome: {test.outcome}."] if test and current else [],
            remediation=CapabilityRemediation(
                action="Run Check access, then send a synthetic test. Provider acceptance is "
                "not confirmed receipt; check Teams and, for shared sends, Workflow run history.",
                path=path,
            ),
        )

    async def destination_checks(self, tenant: str, identifier: str) -> list[CapabilityCheck]:
        item = await self.destination(tenant, identifier)
        now = self._clock()
        path = "/settings?tab=notifications&notificationView=teams"
        checks: list[CapabilityCheck] = []
        try:
            _origin(self._console)
        except ValidationError:
            origin = False
        else:
            origin = True
        checks.append(
            CapabilityCheck(
                feature="teamsWorkflowConfiguration",
                scope=identifier,
                checked_at=now,
                state="verified" if origin and item.enabled else "needsAction",
                reason="Destination enabled and HTTPS console origin configured."
                if origin and item.enabled
                else "Enable this destination and configure "
                "MOSAIC_FRONTEND_URL as an HTTPS origin.",
                evidence=["Saved destination and console origin checks passed."]
                if origin and item.enabled
                else [],
                remediation=CapabilityRemediation(
                    action="Enable the destination in MOSAIC and set the deployed HTTPS console "
                    "origin. Saving does not test Workflow execution.",
                    path=path,
                    owner="azureAdmin",
                ),
            )
        )
        fingerprint = None
        try:
            url = await self._workflow_url(item)
        except TeamsSetupError as error:
            checks.append(
                CapabilityCheck(
                    feature="teamsWorkflowAccess",
                    scope=identifier,
                    checked_at=now,
                    state="failed",
                    reason=str(error),
                    evidence=[f"Diagnostic: {error.code}."],
                    remediation=CapabilityRemediation(
                        action=str(error),
                        path=path,
                        owner="azureAdmin",
                    ),
                )
            )
        else:
            fingerprint = hashlib.sha256(url.encode()).hexdigest()
            checks.append(
                CapabilityCheck(
                    feature="teamsWorkflowAccess",
                    scope=identifier,
                    checked_at=now,
                    state="verified",
                    fresh_until=now + timedelta(minutes=5),
                    reason=(
                        "The API read the secret and validated its supported URL and exact host."
                    ),
                    evidence=[
                        "Key Vault secret read and endpoint policy checks passed. "
                        "No webhook was called and no secret was written."
                    ],
                )
            )
        channels = await self.repository.get_channel_settings(tenant)
        routes = await self.repository.list_routes(tenant)
        routed = bool(
            channels
            and channels.teams_channel_enabled
            and any(
                route.enabled
                and route.teams_destination_id == identifier
                and route.channel == "teamsChannel"
                and set(route.categories) & set(item.allowed_categories)
                for route in routes
            )
        )
        checks.append(
            CapabilityCheck(
                feature="teamsWorkflowRouting",
                scope=identifier,
                checked_at=now,
                state="verified" if routed else "needsAction",
                reason="Shared channel and an enabled, category-compatible route are configured."
                if routed
                else "Normal notifications need the shared channel enabled and a matching "
                "administrator route. An explicit test does not prove routing.",
                evidence=["An enabled shared route matches this destination."] if routed else [],
                remediation=CapabilityRemediation(
                    action="Enable Shared Teams in Channels and create an administrator route "
                    "whose destination and categories match.",
                    path="/settings?tab=notifications&notificationView=routes",
                ),
            )
        )
        checks.append(
            CapabilityCheck(
                feature="teamsWorkflowExecution",
                scope=identifier,
                checked_at=now,
                state="unknown",
                reason="Workflow owner, Teams connection, DLP policy, "
                "and downstream actions require "
                "administrator verification. Trigger acceptance alone cannot verify them.",
                remediation=CapabilityRemediation(
                    action="Open the Workflow's run history; "
                    "check its owner/co-owner, enabled state, "
                    "Teams connection and post-card action. Then confirm receipt in the channel.",
                    path=path,
                    owner="teamsAdmin",
                ),
            )
        )
        checks.append(
            await self._test_check(
                tenant,
                self._test_scope(tenant, identifier),
                self._revision(item.revision, secret_fingerprint=fingerprint),
                path=path,
            )
        )
        return checks

    async def personal_checks(
        self, tenant: str, user: str, persona: NotificationPersona, *, verify_access: bool = False
    ) -> list[CapabilityCheck]:
        settings = await self.personal_settings(tenant)
        binding = await self.repository.get_personal_teams_binding(tenant, user)
        now = self._clock()
        path = "/profile" if persona == "admin" else "/notification-preferences"
        matching = bool(
            settings.enabled
            and settings.cloud == "public"
            and self._tokens
            and self._tokens.bot_app_id == settings.bot_app_id
            and self._private_configuration_ready()
        )
        checks = [
            CapabilityCheck(
                feature="teamsBotConfiguration",
                scope="self",
                checked_at=now,
                state="verified" if matching else "needsAction",
                reason="The configured single-tenant bot matches the outbound OAuth identity."
                if matching
                else "The bot registration, outbound identity, or HTTPS callback origins "
                "are missing or mismatched.",
                evidence=["Saved bot ID matches the configured OAuth source; HTTPS origins exist."]
                if matching
                else [],
                remediation=CapabilityRemediation(
                    action="Ask the administrator to check the Teams catalog app ID, "
                    "bot client ID, MOSAIC_TEAMS_BOT_AUTH_MODE and the identity "
                    "attached to the API host. Configure "
                    "the Azure Bot messaging endpoint to /api/v1/teams/messages.",
                    path=path,
                    owner="teamsAdmin",
                ),
            )
        ]
        if verify_access:
            failure = None
            if matching and self._tokens:
                try:
                    await self._tokens.token()
                except TeamsAuthenticationError as error:
                    failure = str(error)
                except AzureError:
                    failure = "Bot OAuth could not acquire a token. Check the exact bot identity, "
                    failure += "host attachment, tenant, or Key Vault credential expiry."
            else:
                failure = "Complete the bot identity configuration before checking OAuth."
            checks.append(
                CapabilityCheck(
                    feature="teamsBotOAuth",
                    scope="self",
                    checked_at=now,
                    state="failed" if failure else "verified",
                    reason=failure or "The runtime acquired a Bot Connector token without sending.",
                    evidence=[]
                    if failure
                    else ["The configured bot OAuth source returned a token."],
                    fresh_until=None if failure else now + timedelta(minutes=5),
                    remediation=CapabilityRemediation(
                        action="Correct the bot's user-assigned identity attachment or rotate its "
                        "Key Vault-referenced app credential, then check access again.",
                        path=path,
                        owner="azureAdmin",
                    ),
                )
            )
        installed = bool(
            binding and binding.state == "installed" and binding.bot_app_id == settings.bot_app_id
        )
        checks.append(
            CapabilityCheck(
                feature="teamsPersonalInstallation",
                scope="self",
                checked_at=now,
                state="verified" if installed else "needsAction",
                reason="An authenticated personal installation is bound to this account."
                if installed
                else "Your personal installation is "
                f"{binding.state if binding else 'not installed'} or belongs to a different bot.",
                evidence=["An authenticated Teams lifecycle event established this binding."]
                if installed
                else [],
                remediation=CapabilityRemediation(
                    action="Install or unblock the approved MOSAIC app in personal scope. If the "
                    "bot registration changed, reinstall it. Ask the Teams administrator if app "
                    "policy prevents installation; never paste a conversation ID.",
                    path=path,
                    owner="user",
                ),
            )
        )
        channels = await self.repository.get_channel_settings(tenant)
        preferences = await self.repository.get_preferences(tenant, user, persona)
        opted_in = bool(
            preferences and any(category.teams.mode != "off" for category in preferences.categories)
        )
        enabled = bool(channels and channels.teams_personal_enabled)
        checks.append(
            CapabilityCheck(
                feature="teamsPersonalPreferences",
                scope="self",
                checked_at=now,
                state="verified" if enabled and opted_in else "needsAction",
                reason="The tenant channel and your private Teams preferences are enabled."
                if enabled and opted_in
                else "Normal delivery needs the tenant's private Teams "
                "channel and your category preferences enabled. The self-test is separate.",
                evidence=["Tenant enablement and this persona's opt-in are present."]
                if enabled and opted_in
                else [],
                remediation=CapabilityRemediation(
                    action="Ask the administrator to enable the private Teams channel, then choose "
                    "the categories you want in your own notification preferences.",
                    path=path,
                    owner="user",
                ),
            )
        )
        checks.append(
            await self._test_check(
                tenant,
                self._test_scope(tenant, user, persona),
                self._revision(settings.revision, binding=binding),
                path=path,
            )
        )
        return checks

    async def list_destinations(self, tenant: str) -> list[TeamsDestination]:
        self._tenant(tenant)
        return await self.repository.list_teams_destinations(tenant)

    async def destination(self, tenant: str, identifier: str) -> TeamsDestination:
        self._tenant(tenant)
        item = await self.repository.get_teams_destination(tenant, identifier)
        if item is None:
            raise NotFoundError("The Teams destination does not exist.")
        return item

    async def save_destination(
        self,
        tenant: str,
        actor: str,
        payload: TeamsDestinationInput,
        *,
        identifier: str | None = None,
    ) -> TeamsDestination:
        self._tenant(tenant)
        if payload.enabled:
            _origin(self._console)
        old = await self.destination(tenant, identifier) if identifier else None
        reference = old.credential_reference_id if old else None
        new_secret: KeyVaultSecretId | None = None
        if payload.webhook_url is not None:
            self.endpoint_policy.validate(payload.webhook_url.get_secret_value())
            if self._writer is None:
                raise ValidationError("Configure MOSAIC's Key Vault secret writer first.")
            new_secret = self._writer.new_secret("mosaic-teams-" + uuid4().hex)
            try:
                await self._writer.put(
                    new_secret,
                    payload.webhook_url,
                    tags={"managedBy": "MOSAIC", "purpose": "teamsWorkflow"},
                )
            except Exception:
                raise ValidationError(
                    "MOSAIC could not store the Teams secret in Key Vault."
                ) from None
            reference = new_secret.versionless
        if reference is None:
            raise ValidationError("A new Teams destination requires a write-only webhook URL.")
        item = TeamsDestination(
            id=old.id if old else str(uuid4()),
            tenant_id=tenant,
            name=payload.name,
            enabled=payload.enabled,
            allowed_categories=payload.allowed_categories,
            credential_reference_id=reference,
            revision=old.revision + 1 if old else 1,
            updated_by=actor,
            created_at=old.created_at if old else self._clock(),
            updated_at=self._clock(),
            etag=old.etag if old else None,
        )
        try:
            saved = await self.repository.save_teams_destination(
                item, _audit(tenant, actor, item.id, "teamsDestination", "teams.destination.saved")
            )
        except Exception:
            if new_secret is not None and self._writer is not None:
                try:
                    await self._writer.delete(new_secret)
                except Exception:
                    pass
            raise
        # Never mutate an existing secret before the CAS succeeds. Orphans are safe but need
        # operator cleanup when the vault is unavailable; the secret is never put into Cosmos.
        if old and new_secret and self._writer:
            try:
                await self._writer.delete(KeyVaultSecretId.parse(old.credential_reference_id))
            except Exception:
                pass
        return saved

    async def delete_destination(self, tenant: str, actor: str, identifier: str) -> None:
        item = await self.destination(tenant, identifier)
        await self.repository.delete_teams_destination(
            item, _audit(tenant, actor, item.id, "teamsDestination", "teams.destination.deleted")
        )
        if self._writer:
            try:
                await self._writer.delete(KeyVaultSecretId.parse(item.credential_reference_id))
            except Exception:
                # Deletion from MOSAIC disables dispatch even if Key Vault cleanup is unavailable.
                pass

    async def personal_settings(self, tenant: str) -> PersonalTeamsSettings:
        self._tenant(tenant)
        return await self.repository.get_personal_teams_settings(tenant) or PersonalTeamsSettings(
            id=_record_id("personalTeamsSettings", tenant), tenant_id=tenant
        )

    async def save_personal_settings(
        self, tenant: str, actor: str, payload: PersonalTeamsSettingsInput
    ) -> PersonalTeamsSettings:
        old = await self.personal_settings(tenant)
        if payload.cloud != "public":
            raise ValidationError(
                "Personal Teams currently supports the public Microsoft cloud only."
            )
        if payload.app_id:
            application_id(payload.app_id)
        if payload.bot_app_id:
            application_id(payload.bot_app_id)
        if payload.enabled:
            self._private_configuration()
        if payload.enabled and (
            self._tokens is None or self._tokens.bot_app_id != (payload.bot_app_id or "").casefold()
        ):
            raise ValidationError(
                "Configure OAuth for this exact Azure Bot identity before enabling."
            )
        values = payload.model_dump(by_alias=False)
        if payload.app_id:
            values["app_id"] = application_id(payload.app_id)
        if payload.bot_app_id:
            values["bot_app_id"] = application_id(payload.bot_app_id)
        item = PersonalTeamsSettings(
            **values,
            id=old.id,
            tenant_id=tenant,
            created_at=old.created_at,
            updated_at=self._clock(),
            etag=old.etag,
            revision=old.revision + 1 if old.etag else 1,
        )
        return await self.repository.save_personal_teams_settings(
            item, _audit(tenant, actor, item.id, "personalTeamsSettings", "teams.personal.saved")
        )

    async def lifecycle(self, authorization: str, activity: dict[str, Any]) -> None:
        settings = await self.personal_settings(self.tenant_id)
        if not settings.bot_app_id or settings.cloud != "public":
            raise TeamsAuthenticationError("Personal Teams registration is not configured.")
        try:
            application_id(self.tenant_id)
            application_id(settings.bot_app_id)
        except ValidationError:
            raise TeamsAuthenticationError(
                "A valid tenant and bot registration are required."
            ) from None
        service_url = activity.get("serviceUrl")
        if not isinstance(service_url, str):
            raise TeamsAuthenticationError("A Connector service URL is required.")
        claims = await self._authenticator.authenticate(
            authorization, bot_app_id=settings.bot_app_id, service_url=service_url
        )
        try:
            # Connector JWTs normally have no tid; the authenticated activity supplies tenant.
            # If Microsoft includes tid, it must agree too.
            if claims.get("tid") is not None and application_id(claims["tid"]) != self.tenant_id:
                raise TeamsAuthenticationError("The Connector token tenant does not match.")
            conversation = activity["conversation"]
            sender = activity["from"]
            recipient = activity["recipient"]
            channel_data = activity.get("channelData", {})
            tenant = channel_data["tenant"]["id"]
            if (
                activity.get("channelId") != "msteams"
                or conversation.get("conversationType") != "personal"
                or application_id(tenant) != self.tenant_id
                or application_id(conversation["tenantId"]) != self.tenant_id
                or channel_data.get("team")
                or channel_data.get("channel")
                or recipient["id"] != "28:" + settings.bot_app_id
            ):
                raise TeamsAuthenticationError(
                    "Only this tenant's personal bot lifecycle is allowed."
                )
            user = application_id(sender["aadObjectId"])
            if sender.get("id") == recipient["id"]:
                raise TeamsAuthenticationError("The lifecycle sender must be a person.")
            identifier = conversation["id"]
            if not isinstance(identifier, str) or not 0 < len(identifier) <= 256:
                raise TeamsAuthenticationError("A personal conversation ID is required.")
            timestamp = datetime.fromisoformat(activity["timestamp"].replace("Z", "+00:00"))
            if timestamp.tzinfo is None or abs(self._clock() - timestamp) > timedelta(minutes=5):
                raise TeamsAuthenticationError("The lifecycle timestamp is stale or invalid.")
            state: Literal["installed", "uninstalled", "blocked"] | None = None
            if activity["type"] == "installationUpdate":
                if activity.get("action") == "add":
                    state = "installed"
                elif activity.get("action") == "remove":
                    state = "uninstalled"
            elif activity["type"] == "conversationUpdate":
                bot_id = recipient["id"]
                if any(member.get("id") == bot_id for member in activity.get("membersRemoved", [])):
                    state = "uninstalled"
                elif any(member.get("id") == bot_id for member in activity.get("membersAdded", [])):
                    state = "installed"
            if state is None:
                # No message content, invoke, command, approval or chat action is processed.
                return
            old = await self.repository.get_personal_teams_binding(self.tenant_id, user)
            if old:
                if old.authenticated_at > timestamp:
                    raise TeamsAuthenticationError("The lifecycle event is older than its binding.")
                if old.authenticated_at == timestamp:
                    if old.conversation_id == identifier and old.state == state:
                        return
                    raise TeamsAuthenticationError("The lifecycle timestamp conflicts.")
                if (
                    old.state == "installed"
                    and old.bot_app_id == settings.bot_app_id
                    and old.conversation_id != identifier
                ):
                    raise TeamsAuthenticationError(
                        "An installed personal conversation cannot be rebound."
                    )
                if state == "uninstalled" and (
                    old.conversation_id != identifier or old.bot_app_id != settings.bot_app_id
                ):
                    raise TeamsAuthenticationError(
                        "Uninstall does not match the personal conversation."
                    )
            binding = PersonalTeamsBinding(
                id=_record_id("personalTeamsBinding", self.tenant_id, user),
                tenant_id=self.tenant_id,
                user_object_id=user,
                conversation_id=identifier,
                service_url=validate_service_url(service_url),
                bot_app_id=settings.bot_app_id,
                authenticated_at=timestamp.astimezone(UTC),
                state=state,
                etag=old.etag if old else None,
                created_at=old.created_at if old else self._clock(),
                updated_at=self._clock(),
            )
            await self.repository.save_personal_teams_binding(binding)
        except TeamsAuthenticationError:
            raise
        except (KeyError, ValueError, TypeError, AttributeError, ValidationError):
            raise TeamsAuthenticationError(
                "The personal Teams lifecycle activity is invalid."
            ) from None

    def card(
        self,
        events: Sequence[NotificationEvent],
        *,
        persona: NotificationPersona = "admin",
        test: bool = False,
    ) -> dict[str, Any]:
        origin = _origin(self._console if persona == "admin" else self._portal)
        body: list[dict[str, Any]] = [
            {
                "type": "TextBlock",
                "text": "MOSAIC notification test" if test else "MOSAIC",
                "weight": "Bolder",
                "wrap": True,
            }
        ]
        actions: list[dict[str, str]] = []
        for event in events[:10]:
            body.extend(
                [
                    {"type": "TextBlock", "text": notification_title(event), "wrap": True},
                    {"type": "TextBlock", "text": notification_summary(event), "wrap": True},
                ]
            )
            path = _subject_path(event, persona)
            if path:
                actions.append(
                    {
                        "type": "Action.OpenUrl",
                        "title": "Open in MOSAIC",
                        "url": origin + path,
                    }
                )
        if len(events) > 10:
            body.append(
                {
                    "type": "TextBlock",
                    "text": "More notifications are available in MOSAIC.",
                    "wrap": True,
                }
            )
        actions = actions[:3]
        actions.append(
            {
                "type": "Action.OpenUrl",
                "title": "View notifications",
                "url": origin + "/notifications",
            }
        )
        actions.append(
            {
                "type": "Action.OpenUrl",
                "title": "Manage notifications",
                "url": origin + ("/profile" if persona == "admin" else "/notification-preferences"),
            }
        )
        return {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.2",
            "body": body,
            "actions": actions,
        }

    async def send_shared(
        self,
        tenant: str,
        identifier: str,
        events: Sequence[NotificationEvent],
        *,
        test: bool = False,
    ) -> NotificationDispatchResult:
        item = await self.destination(tenant, identifier)
        if not item.enabled:
            return NotificationDispatchResult(
                "suppressed", safe_error="Teams destination is disabled."
            )
        try:
            _origin(self._console)
        except (ValidationError, ValueError):
            return NotificationDispatchResult(
                "permanentFailure",
                error_code="teamsOriginNotConfigured",
                safe_error="Configure the HTTPS MOSAIC console origin before sending Teams cards.",
            )
        if any(
            event.tenant_id != tenant or event.category not in item.allowed_categories
            for event in events
        ):
            return NotificationDispatchResult(
                "suppressed", safe_error="The Teams destination scope excludes this event."
            )
        try:
            url = await self._workflow_url(item)
            return await self.transport.workflow(url, self.card(events, test=test))
        except TeamsSetupError as error:
            return NotificationDispatchResult(
                "retryableFailure" if error.retryable else "permanentFailure",
                error_code=error.code,
                safe_error=str(error),
            )

    async def send_personal(
        self,
        tenant: str,
        user: str,
        persona: NotificationPersona,
        events: Sequence[NotificationEvent],
        *,
        test: bool = False,
    ) -> NotificationDispatchResult:
        settings = await self.personal_settings(tenant)
        binding = await self.repository.get_personal_teams_binding(tenant, user)
        if (
            not settings.enabled
            or settings.cloud != "public"
            or not binding
            or binding.state != "installed"
            or binding.cloud != "public"
            or binding.bot_app_id != settings.bot_app_id
            or self._tokens is None
            or self._tokens.bot_app_id != settings.bot_app_id
            or any(event.tenant_id != tenant for event in events)
        ):
            return NotificationDispatchResult(
                "suppressed",
                safe_error="Personal Teams is unavailable; no shared-channel fallback is used.",
            )
        if not self._private_configuration_ready():
            return NotificationDispatchResult(
                "permanentFailure",
                error_code="teamsConfigurationInvalid",
                safe_error="Personal Teams needs a UUID tenant and HTTPS console/portal origins.",
            )
        try:
            token = await self._tokens.token()
        except TeamsAuthenticationError as error:
            return NotificationDispatchResult(
                "retryableFailure" if error.retryable else "permanentFailure",
                error_code=error.code,
                safe_error=str(error),
                retry_after_seconds=error.retry_after_seconds,
            )
        except AzureError:
            return NotificationDispatchResult(
                "retryableFailure",
                error_code="teamsBotOAuthUnavailable",
                safe_error="Bot OAuth is unavailable. Check its identity and network access.",
            )
        try:
            result = await self.transport.personal(
                service_url=binding.service_url,
                conversation_id=binding.conversation_id,
                token=token,
                card=self.card(events, persona=persona, test=test),
            )
        except TeamsAuthenticationError:
            return NotificationDispatchResult(
                "permanentFailure",
                error_code="teamsInvalidBinding",
                safe_error="The saved personal Teams binding is invalid. Reinstall the app.",
            )
        if result.error_code in {"teamsHttp403", "teamsHttp404", "teamsHttp410"}:
            # A concurrent uninstall/reinstall must win over an old send's blocked response.
            try:
                await self.repository.save_personal_teams_binding(
                    binding.model_copy(
                        update={
                            "state": "blocked",
                            "authenticated_at": self._clock(),
                            "updated_at": self._clock(),
                        }
                    )
                )
            except ConflictError:
                pass
        return result

    async def test_shared(self, tenant: str, identifier: str) -> TeamsTestResult:
        started_at = self._clock()
        item = await self.destination(tenant, identifier)
        url = None
        observed: NotificationDispatchResult | None = None
        if item.enabled:
            try:
                url = await self._workflow_url(item)
            except TeamsSetupError as error:
                observed = NotificationDispatchResult(
                    "retryableFailure" if error.retryable else "permanentFailure",
                    error_code=error.code,
                    safe_error=str(error),
                )
        if observed is None:
            observed = await self.send_shared(tenant, identifier, [], test=True)
        result = self._test_result(
            observed,
            started_at=started_at,
            shared=True,
            revision=self._revision(
                item.revision,
                secret_fingerprint=hashlib.sha256(url.encode()).hexdigest() if url else None,
            ),
        )
        return await self._record_test(
            tenant, self._test_scope(tenant, identifier), "teamsChannel", result
        )

    async def test_personal(
        self, tenant: str, user: str, persona: NotificationPersona
    ) -> TeamsTestResult:
        started_at = self._clock()
        settings = await self.personal_settings(tenant)
        binding = await self.repository.get_personal_teams_binding(tenant, user)
        result = self._test_result(
            await self.send_personal(tenant, user, persona, [], test=True),
            started_at=started_at,
            revision=self._revision(settings.revision, binding=binding),
        )
        return await self._record_test(
            tenant, self._test_scope(tenant, user, persona), "teamsPersonal", result
        )

    def _test_result(
        self,
        result: NotificationDispatchResult,
        *,
        started_at: datetime,
        revision: str,
        shared: bool = False,
    ) -> TeamsTestResult:
        return TeamsTestResult(
            outcome=result.outcome,
            tested_at=started_at,
            configuration_revision=revision,
            error_code=result.error_code,
            message=(
                "Teams accepted the test request. Delivery is unconfirmed; "
                + (
                    "check Teams and Workflow run history."
                    if shared
                    else "check your private Teams chat."
                )
                if result.outcome == "providerAccepted"
                else result.safe_error or "The Teams test was not accepted."
            ),
        )

    async def capability_checks(
        self, tenant: str, *, user_object_id: str | None = None
    ) -> list[CapabilityCheck]:
        settings = await self.personal_settings(tenant)
        checks: list[CapabilityCheck] = []
        if user_object_id is None:
            try:
                _origin(self._console)
                origin_ready = True
            except (ValidationError, ValueError):
                origin_ready = False
            configured = bool(
                self._reader
                and self._writer
                and self.endpoint_policy.allowed_hosts
                and origin_ready
            )
            checks.append(
                CapabilityCheck(
                    feature="teamsChannel",
                    scope="workflows",
                    state="needsAction" if configured else "notConfigured",
                    reason=(
                        "Key Vault and endpoint policy are configured; "
                        "saved destinations still need explicit tests."
                        if configured
                        else "Configure Key Vault, exact Workflows hosts and HTTPS console origin."
                    ),
                    checked_at=self._clock(),
                    remediation=CapabilityRemediation(
                        action="Follow the Teams setup guide and test a fictional notification.",
                        path="/settings",
                        owner="teamsAdmin",
                    ),
                )
            )
        binding = (
            await self.repository.get_personal_teams_binding(tenant, user_object_id)
            if user_object_id
            else None
        )
        private_ready = self._private_configuration_ready()
        if not private_ready:
            personal_reason = "Personal Teams needs a UUID tenant and HTTPS console/portal origins."
        elif self._tokens is None:
            personal_reason = "Configure OAuth for the exact single-tenant Azure Bot identity."
        elif binding:
            personal_reason = (
                f"Personal app binding is {binding.state}; delivery remains unverified."
            )
        else:
            personal_reason = "Enable the single-tenant bot and install the app in personal scope."
        checks.append(
            CapabilityCheck(
                feature="teamsPersonal",
                scope="personal",
                state=(
                    "disabled"
                    if not settings.enabled
                    else "notConfigured"
                    if self._tokens is None or not private_ready
                    else "needsAction"
                ),
                reason=personal_reason,
                checked_at=self._clock(),
                configuration_revision=str(settings.revision),
                remediation=CapabilityRemediation(
                    action="Install MOSAIC personally, opt in, and explicitly send a test.",
                    path="/notifications",
                    owner="user",
                ),
            )
        )
        return checks

    async def personal_status(
        self, tenant: str, user: str, persona: NotificationPersona = "portal"
    ) -> PersonalTeamsStatus:
        settings = await self.personal_settings(tenant)
        binding = await self.repository.get_personal_teams_binding(tenant, user)
        return PersonalTeamsStatus(
            enabled=settings.enabled,
            state=binding.state if binding else "notInstalled",
            checks=await self.personal_checks(tenant, user, persona),
            last_test=await self._last_test(tenant, self._test_scope(tenant, user, persona)),
        )


class TeamsChannelDispatcher:
    supports_idempotency = False

    def __init__(self, service: TeamsService) -> None:
        self._service = service

    async def send(
        self, delivery: NotificationDelivery, events: Sequence[NotificationEvent]
    ) -> NotificationDispatchResult:
        if delivery.channel != "teamsChannel" or delivery.persona is not None:
            return NotificationDispatchResult(
                "suppressed", safe_error="Not a shared Teams delivery."
            )
        return await self._service.send_shared(
            delivery.tenant_id, delivery.recipient_reference, events
        )


class TeamsPersonalDispatcher:
    supports_idempotency = False

    def __init__(self, service: TeamsService) -> None:
        self._service = service

    async def send(
        self, delivery: NotificationDelivery, events: Sequence[NotificationEvent]
    ) -> NotificationDispatchResult:
        if delivery.channel != "teamsPersonal" or delivery.persona is None:
            return NotificationDispatchResult(
                "suppressed", safe_error="Not a personal Teams delivery."
            )
        return await self._service.send_personal(
            delivery.tenant_id, delivery.recipient_reference, delivery.persona, events
        )
