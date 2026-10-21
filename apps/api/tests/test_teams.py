import asyncio
import copy
import importlib.util
import io
import json
import logging
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4
from zipfile import ZipFile

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from mosaic_api.auth import AuthContext, LocalAuthenticator
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from mosaic_api.domain import KeyVaultSecretId
from mosaic_api.errors import (
    DomainError,
    UpstreamAuthorizationError,
    ValidationError,
    domain_error_handler,
    request_validation_error_handler,
)
from mosaic_api.integrations.teams import (
    BOT_ISSUER,
    BOT_KEYS_URL,
    BOT_METADATA_URL,
    BOT_SCOPE,
    ManagedIdentityBotTokenSource,
    SecretBotTokenSource,
    TeamsAuthenticationError,
    TeamsHttpTransport,
    TeamsLifecycleAuthenticator,
    WorkflowEndpointPolicy,
    provider_result,
)
from mosaic_api.notification_delivery_api import notification_delivery_router
from mosaic_api.notifications import (
    NotificationAudience,
    NotificationCategory,
    NotificationChannelHealth,
    NotificationDelivery,
    NotificationEvent,
    NotificationEventType,
    NotificationSubject,
    PersonalTeamsSettingsInput,
    TeamsDestinationInput,
)
from mosaic_api.repositories.memory_notifications import InMemoryNotificationRepository
from mosaic_api.services.notifications import NotificationDispatchResult
from mosaic_api.services.teams import (
    TeamsChannelDispatcher,
    TeamsPersonalDispatcher,
    TeamsService,
)
from mosaic_api.teams_api import teams_router
from PIL import Image
from pydantic import SecretStr

TENANT = "11111111-1111-4111-8111-111111111111"
BOT = "22222222-2222-4222-8222-222222222222"
APP = "33333333-3333-4333-8333-333333333333"
USER = "44444444-4444-4444-8444-444444444444"
OTHER = "55555555-5555-4555-8555-555555555555"
HOST = "fictional.westus.logic.azure.com"
WEBHOOK = f"https://{HOST}/workflows/demo/triggers/manual/paths/invoke?sig=fictional-secret"
SERVICE = "https://smba.trafficmanager.net/amer/"


class Vault:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.deleted: list[str] = []
        self.fail = False

    def new_secret(self, name: str) -> KeyVaultSecretId:
        return KeyVaultSecretId.parse(f"https://fictional.vault.azure.net/secrets/{name}")

    async def put(
        self, secret: KeyVaultSecretId, value: SecretStr, *, tags: dict[str, str]
    ) -> None:
        if self.fail:
            raise RuntimeError(WEBHOOK)
        assert tags["purpose"] == "teamsWorkflow"
        self.values[secret.versionless] = value.get_secret_value()

    async def read(self, secret_uri: str) -> str:
        if self.fail:
            raise UpstreamAuthorizationError("The fictional vault denied access.")
        if secret_uri not in self.values:
            raise ValidationError("The fictional secret is missing.")
        return self.values[secret_uri]

    async def delete(self, secret: KeyVaultSecretId) -> bool:
        self.deleted.append(secret.versionless)
        self.values.pop(secret.versionless, None)
        return True


class Tokens:
    bot_app_id = BOT
    configuration_id = "fictional-bot-token-source"

    async def token(self) -> str:
        return "fictional-oauth-token"


@dataclass
class Provider:
    key: rsa.RSAPrivateKey
    now: datetime
    status: int = 202
    headers: dict[str, str] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    endorsements: list[str] = field(default_factory=lambda: ["msteams"])
    error: type[httpx.RequestError] | None = None
    metadata_requests: int = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == BOT_METADATA_URL:
            self.metadata_requests += 1
            return httpx.Response(
                200,
                json={
                    "issuer": BOT_ISSUER,
                    "jwks_uri": BOT_KEYS_URL,
                    "id_token_signing_alg_values_supported": ["RS256"],
                },
            )
        if str(request.url) == BOT_KEYS_URL:
            jwk = jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
            assert isinstance(jwk, dict)
            return httpx.Response(
                200,
                json={
                    "keys": [
                        {
                            **jwk,
                            "kid": "test-key",
                            "endorsements": self.endorsements,
                        }
                    ]
                },
            )
        self.requests.append(request)
        if self.error:
            raise self.error(WEBHOOK, request=request)
        return httpx.Response(
            self.status,
            headers=self.headers,
            text=WEBHOOK + " should never be reflected",
        )

    def token(self, **overrides: Any) -> str:
        claims = {
            "iss": BOT_ISSUER,
            "aud": BOT,
            "exp": self.now + timedelta(minutes=10),
            "nbf": self.now - timedelta(seconds=5),
            "serviceurl": SERVICE,
            **overrides,
        }
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "test-key"})

    def activity(self, **overrides: Any) -> dict[str, Any]:
        return {
            "type": "installationUpdate",
            "action": "add",
            "channelId": "msteams",
            "timestamp": self.now.isoformat(),
            "serviceUrl": SERVICE,
            "conversation": {
                "id": "fictional-personal-conversation",
                "tenantId": TENANT,
                "conversationType": "personal",
            },
            "channelData": {"tenant": {"id": TENANT}},
            "from": {"id": "29:fictional-user", "aadObjectId": USER},
            "recipient": {"id": "28:" + BOT},
            **overrides,
        }


@dataclass
class Harness:
    service: TeamsService
    repository: InMemoryNotificationRepository
    vault: Vault
    provider: Provider
    client: httpx.AsyncClient

    async def enable(self) -> None:
        await self.service.save_personal_settings(
            TENANT, USER, PersonalTeamsSettingsInput(enabled=True, app_id=APP, bot_app_id=BOT)
        )

    async def install(self) -> None:
        await self.enable()
        await self.service.lifecycle("Bearer " + self.provider.token(), self.provider.activity())

    async def destination(self) -> Any:
        return await self.service.save_destination(
            TENANT,
            USER,
            TeamsDestinationInput(
                name="Fictional admin channel",
                enabled=True,
                allowed_categories=[NotificationCategory.REQUEST_LIFECYCLE],
                webhook_url=SecretStr(WEBHOOK),
            ),
        )


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
async def harness(signing_key: rsa.RSAPrivateKey) -> AsyncIterator[Harness]:
    provider = Provider(signing_key, datetime.now(UTC))
    vault = Vault()
    repository = InMemoryNotificationRepository()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider.handle), follow_redirects=True
    ) as client:
        service = TeamsService(
            repository,
            tenant_id=TENANT,
            secret_reader=vault,
            secret_writer=vault,
            transport=TeamsHttpTransport(client, WorkflowEndpointPolicy(frozenset({HOST}))),
            authenticator=TeamsLifecycleAuthenticator(client),
            token_source=Tokens(),
            console_origin="https://console.example.invalid",
            portal_origin="https://portal.example.invalid",
            clock=lambda: provider.now,
        )
        yield Harness(service, repository, vault, provider, client)


def event(tenant: str = TENANT) -> NotificationEvent:
    now = datetime.now(UTC)
    return NotificationEvent(
        id="event",
        tenant_id=tenant,
        event_type=NotificationEventType.REQUEST_APPROVED,
        category=NotificationCategory.REQUEST_LIFECYCLE,
        source_id="fictional-source",
        source_revision="1",
        occurred_at=now,
        origin="test",
        subject=NotificationSubject(request_id="fictional-request"),
        audience=NotificationAudience(user_object_ids=[USER]),
    )


@pytest.mark.parametrize(
    "value",
    [
        "http://" + HOST + "/workflows/demo/triggers/manual/paths/invoke?sig=secret",
        WEBHOOK.replace(HOST, "127.0.0.1"),
        WEBHOOK.replace(HOST, HOST + ".evil.invalid"),
        WEBHOOK.replace(HOST, "unlisted.logic.azure.com"),
        WEBHOOK.replace(HOST, "user@" + HOST),
        WEBHOOK + "#fragment",
        WEBHOOK.replace(HOST, HOST + ":8443"),
        WEBHOOK.replace("/workflows/", "/redirect/"),
        WEBHOOK.replace("https:", "file:"),
        WEBHOOK + "\n",
        "https://outlook.office.com/webhook/legacy",
    ],
)
def test_workflows_fail_closed_on_urls(value: str) -> None:
    with pytest.raises(ValidationError) as raised:
        WorkflowEndpointPolicy(frozenset({HOST})).validate(value)
    assert "secret" not in str(raised.value)


def test_workflows_powerplatform_shape_and_empty_default() -> None:
    url = (
        "https://fictional.environment.api.powerplatform.com/powerautomate/automations/direct"
        "/workflows/demo/triggers/manual/paths/invoke?api-version=1&sig=fictional"
    )
    policy = WorkflowEndpointPolicy(frozenset({"fictional.environment.api.powerplatform.com"}))
    assert policy.validate(url) == url
    with pytest.raises(ValidationError):
        WorkflowEndpointPolicy().validate(url)


async def test_write_only_crud_rotation_and_tenant_isolation(harness: Harness) -> None:
    item = await harness.destination()
    assert WEBHOOK not in item.model_dump_json()
    assert "fictional-secret" not in repr(item)
    assert harness.vault.values[item.credential_reference_id] == WEBHOOK
    saved = await harness.service.save_destination(
        TENANT,
        USER,
        TeamsDestinationInput(
            name="Renamed",
            enabled=False,
            allowed_categories=[NotificationCategory.REQUEST_LIFECYCLE],
        ),
        identifier=item.id,
    )
    assert saved.credential_reference_id == item.credential_reference_id
    rotated = await harness.service.save_destination(
        TENANT,
        USER,
        TeamsDestinationInput(
            name="Rotated",
            allowed_categories=[NotificationCategory.REQUEST_LIFECYCLE],
            webhook_url=SecretStr(WEBHOOK + "-rotated"),
        ),
        identifier=item.id,
    )
    assert rotated.credential_reference_id != item.credential_reference_id
    assert item.credential_reference_id in harness.vault.deleted
    assert not await harness.repository.list_teams_destinations(OTHER)
    with pytest.raises(ValidationError, match="single-tenant"):
        await harness.service.list_destinations(OTHER)
    await harness.service.delete_destination(TENANT, USER, item.id)
    assert not await harness.service.list_destinations(TENANT)
    assert not harness.vault.values


async def test_secret_store_errors_are_redacted(harness: Harness) -> None:
    harness.vault.fail = True
    with pytest.raises(ValidationError) as raised:
        await harness.destination()
    assert WEBHOOK not in str(raised.value)
    assert raised.value.__suppress_context__


async def test_shared_access_checks_are_read_only_and_distinguish_secret_failures(
    harness: Harness,
) -> None:
    destination = await harness.destination()
    checks = {
        check.feature: check
        for check in await harness.service.destination_checks(TENANT, destination.id)
    }
    assert checks["teamsWorkflowAccess"].state == "verified"
    assert checks["teamsWorkflowRouting"].state == "needsAction"
    assert checks["teamsWorkflowExecution"].state == "unknown"
    assert not harness.provider.requests
    harness.vault.fail = True
    checks = await harness.service.destination_checks(TENANT, destination.id)
    access = next(check for check in checks if check.feature == "teamsWorkflowAccess")
    assert access.state == "failed" and "secret-read" in (access.reason or "")
    assert "teamsSecretAccessDenied" in access.model_dump_json()
    assert WEBHOOK not in access.model_dump_json()
    harness.vault.fail = False
    harness.vault.values.clear()
    checks = await harness.service.destination_checks(TENANT, destination.id)
    assert any("teamsSecretUnavailable" in check.model_dump_json() for check in checks)
    assert not harness.provider.requests and not harness.vault.deleted


async def test_shared_test_evidence_persists_and_secret_rotation_invalidates_it(
    harness: Harness,
) -> None:
    destination = await harness.destination()
    result = await harness.service.test_shared(TENANT, destination.id)
    assert result.outcome == "providerAccepted" and result.configuration_revision
    reloaded = TeamsService(
        harness.repository,
        tenant_id=TENANT,
        secret_reader=harness.vault,
        secret_writer=harness.vault,
        transport=harness.service.transport,
        authenticator=TeamsLifecycleAuthenticator(harness.client),
        token_source=Tokens(),
        console_origin="https://console.example.invalid",
        portal_origin="https://portal.example.invalid",
        clock=lambda: harness.provider.now,
    )
    checks = await reloaded.destination_checks(TENANT, destination.id)
    assert next(check for check in checks if check.feature == "teamsTest").state == "verified"
    harness.vault.values[destination.credential_reference_id] = WEBHOOK + "&rotation=2"
    checks = await reloaded.destination_checks(TENANT, destination.id)
    test = next(check for check in checks if check.feature == "teamsTest")
    assert test.state == "needsAction" and test.last_test_at == result.tested_at
    assert "stale" in (test.reason or "")
    assert len(harness.provider.requests) == 1


async def test_older_inflight_test_does_not_replace_a_newer_configuration_result(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = await harness.destination()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def send(*args: Any, **kwargs: Any) -> NotificationDispatchResult:
        if not entered.is_set():
            entered.set()
            await release.wait()
        return NotificationDispatchResult("providerAccepted")

    monkeypatch.setattr(harness.service, "send_shared", send)
    older = asyncio.create_task(harness.service.test_shared(TENANT, destination.id))
    await entered.wait()
    try:
        harness.provider.now += timedelta(seconds=1)
        await harness.service.save_destination(
            TENANT,
            USER,
            TeamsDestinationInput(
                name="Updated fictional channel",
                enabled=True,
                allowed_categories=[NotificationCategory.REQUEST_LIFECYCLE],
            ),
            identifier=destination.id,
        )
        newer = await harness.service.test_shared(TENANT, destination.id)
    finally:
        release.set()
        older_result = await older
    assert older_result.configuration_revision != newer.configuration_revision
    assert "newer test result" in older_result.message
    checks = await harness.service.destination_checks(TENANT, destination.id)
    check = next(item for item in checks if item.feature == "teamsTest")
    assert check.state == "verified"
    assert check.last_test_at == newer.tested_at
    assert check.configuration_revision == newer.configuration_revision


async def test_private_checks_and_test_evidence_are_caller_and_persona_scoped(
    harness: Harness,
) -> None:
    await harness.install()
    checks = await harness.service.personal_checks(TENANT, USER, "portal", verify_access=True)
    assert next(check for check in checks if check.feature == "teamsBotOAuth").state == "verified"
    assert not harness.provider.requests
    result = await harness.service.test_personal(TENANT, USER, "portal")
    status = await harness.service.personal_status(TENANT, USER, "portal")
    assert status.last_test and status.last_test.tested_at == result.tested_at
    assert "private Teams chat" in result.message and "Workflow" not in result.message
    assert (
        next(check for check in status.checks if check.feature == "teamsTest").state == "verified"
    )
    assert (await harness.service.personal_status(TENANT, USER, "admin")).last_test is None
    assert (await harness.service.personal_status(TENANT, OTHER, "portal")).last_test is None
    assert "conversationId" not in status.model_dump_json()
    assert "serviceUrl" not in status.model_dump_json()
    await harness.enable()
    changed = await harness.service.personal_status(TENANT, USER, "portal")
    assert (
        next(check for check in changed.checks if check.feature == "teamsTest").state
        == "needsAction"
    )
    assert len(harness.provider.requests) == 1


async def test_private_oauth_diagnostics_and_transient_sends_are_not_conflated(
    harness: Harness,
) -> None:
    await harness.install()

    class UnavailableTokens(Tokens):
        async def token(self) -> str:
            raise TeamsAuthenticationError(
                "Bot OAuth endpoint unavailable.",
                code="teamsBotOAuthUnavailable",
                retryable=True,
                retry_after_seconds=120,
            )

    service = TeamsService(
        harness.repository,
        tenant_id=TENANT,
        secret_reader=harness.vault,
        secret_writer=harness.vault,
        transport=harness.service.transport,
        authenticator=TeamsLifecycleAuthenticator(harness.client),
        token_source=UnavailableTokens(),
        console_origin="https://console.example.invalid",
        portal_origin="https://portal.example.invalid",
        clock=lambda: harness.provider.now,
    )
    checks = await service.personal_checks(TENANT, USER, "portal", verify_access=True)
    oauth = next(check for check in checks if check.feature == "teamsBotOAuth")
    assert oauth.state == "failed" and oauth.reason == "Bot OAuth endpoint unavailable."
    result = await service.test_personal(TENANT, USER, "portal")
    assert result.outcome == "retryableFailure" and result.error_code == "teamsBotOAuthUnavailable"
    assert (await service.personal_status(TENANT, USER)).state == "installed"
    assert not harness.provider.requests


@pytest.mark.parametrize(
    "status,outcome",
    [
        (200, "providerAccepted"),
        (202, "providerAccepted"),
        (302, "permanentFailure"),
        (400, "permanentFailure"),
        (401, "permanentFailure"),
        (403, "permanentFailure"),
        (429, "retryableFailure"),
        (503, "unknownOutcome"),
    ],
)
async def test_shared_provider_acceptance_and_redacted_failures(
    harness: Harness, status: int, outcome: str
) -> None:
    destination = await harness.destination()
    harness.provider.status = status
    harness.provider.headers = {"Location": "https://evil.invalid", "Retry-After": "12"}
    result = await harness.service.test_shared(TENANT, destination.id)
    assert result.outcome == outcome
    assert result.confirmed_delivered is False
    assert WEBHOOK not in result.model_dump_json()
    assert len(harness.provider.requests) == 1
    assert not harness.provider.requests[0].headers.get("Authorization")
    if status < 300:
        assert "unconfirmed" in result.message
    if status in {429, 503}:
        dispatch = await harness.service.send_shared(TENANT, destination.id, [])
        assert dispatch.retry_after_seconds == 12


async def test_shared_rechecks_secret_endpoint_categories_and_disabled(harness: Harness) -> None:
    destination = await harness.destination()
    result = await harness.service.send_shared(TENANT, destination.id, [event(OTHER)])
    assert result.outcome == "suppressed"
    assert not harness.provider.requests
    harness.vault.values[destination.credential_reference_id] = "https://evil.invalid?sig=secret"
    result = await harness.service.send_shared(TENANT, destination.id, [event()])
    assert result.outcome == "permanentFailure"
    assert not harness.provider.requests
    harness.vault.values[destination.credential_reference_id] = WEBHOOK
    await harness.service.save_destination(
        TENANT,
        USER,
        TeamsDestinationInput(
            name="Disabled", allowed_categories=[NotificationCategory.REQUEST_LIFECYCLE]
        ),
        identifier=destination.id,
    )
    assert (await harness.service.test_shared(TENANT, destination.id)).outcome == "suppressed"


async def test_http_info_logs_do_not_leak_workflow_secret(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    destination = await harness.destination()
    with caplog.at_level(logging.INFO, logger="httpx"):
        result = await harness.service.test_shared(TENANT, destination.id)
    assert result.outcome == "providerAccepted"
    assert "fictional-secret" not in caplog.text and WEBHOOK not in caplog.text
    assert "redacted" in caplog.text


@pytest.mark.parametrize(
    "error,outcome",
    [
        (httpx.ReadTimeout, "unknownOutcome"),
        (httpx.ConnectError, "retryableFailure"),
    ],
)
async def test_transport_ambiguous_timeouts_not_success(
    harness: Harness, error: type[httpx.RequestError], outcome: str
) -> None:
    destination = await harness.destination()
    harness.provider.error = error
    result = await harness.service.test_shared(TENANT, destination.id)
    assert result.outcome == outcome
    assert "fictional-secret" not in result.model_dump_json()


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "https://evil.invalid"},
        {"aud": OTHER},
        {"tid": OTHER},
        {"serviceurl": "https://smba.trafficmanager.net/other/"},
        {"exp": timedelta(minutes=-10)},
        {"nbf": timedelta(minutes=10)},
    ],
)
async def test_callback_rejects_invalid_jwt_claims(
    harness: Harness, overrides: dict[str, Any]
) -> None:
    await harness.enable()
    overrides = {
        claim: harness.provider.now + value if isinstance(value, timedelta) else value
        for claim, value in overrides.items()
    }
    with pytest.raises(TeamsAuthenticationError):
        await harness.service.lifecycle(
            "Bearer " + harness.provider.token(**overrides), harness.provider.activity()
        )
    assert await harness.repository.get_personal_teams_binding(TENANT, USER) is None


async def test_callback_rejects_unsigned_forged_or_missing_tokens(harness: Harness) -> None:
    await harness.enable()
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(
        {"iss": BOT_ISSUER, "aud": BOT},
        other_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )
    for token in ("", "Bearer nonsense", "Bearer " + forged, "Basic fake"):
        with pytest.raises(TeamsAuthenticationError):
            await harness.service.lifecycle(token, harness.provider.activity())
    assert await harness.repository.get_personal_teams_binding(TENANT, USER) is None


async def test_callback_requires_teams_signing_key_endorsement(harness: Harness) -> None:
    await harness.enable()
    harness.provider.endorsements = ["emulator"]
    with pytest.raises(TeamsAuthenticationError, match="endorsed"):
        await harness.service.lifecycle(
            "Bearer " + harness.provider.token(), harness.provider.activity()
        )


@pytest.mark.parametrize(
    "mutation",
    [
        {"conversation": {"conversationType": "channel", "id": "channel", "tenantId": TENANT}},
        {"conversation": {"conversationType": "groupChat", "id": "group", "tenantId": TENANT}},
        {"conversation": {"conversationType": "personal", "id": "personal", "tenantId": OTHER}},
        {"channelData": {"tenant": {"id": OTHER}}},
        {"channelData": {"tenant": {"id": TENANT}, "team": {"id": "shared"}}},
        {"recipient": {"id": "28:" + OTHER}},
        {"from": {"id": "fake", "aadObjectId": "not-an-entra-object"}},
        {"channelId": "emulator"},
        {"timestamp": timedelta(minutes=-30)},
    ],
)
async def test_binding_rejects_nonpersonal_cross_tenant_or_stale_activity(
    harness: Harness, mutation: dict[str, Any]
) -> None:
    await harness.enable()
    mutation = {
        key: (harness.provider.now + value).isoformat() if isinstance(value, timedelta) else value
        for key, value in mutation.items()
    }
    with pytest.raises(TeamsAuthenticationError):
        await harness.service.lifecycle(
            "Bearer " + harness.provider.token(), harness.provider.activity(**mutation)
        )
    assert await harness.repository.get_personal_teams_binding(TENANT, USER) is None


async def test_personal_install_send_uninstall_block_and_no_fallback(harness: Harness) -> None:
    destination = await harness.destination()
    assert destination.enabled
    before = await harness.service.test_personal(TENANT, USER, "portal")
    assert before.outcome == "suppressed" and not harness.provider.requests
    await harness.install()
    assert (await harness.service.personal_status(TENANT, USER)).state == "installed"
    result = await harness.service.test_personal(TENANT, USER, "portal")
    assert result.outcome == "providerAccepted"
    request = harness.provider.requests[-1]
    assert request.url.host == "smba.trafficmanager.net"
    assert request.headers["Authorization"] == "Bearer fictional-oauth-token"
    assert b"portal.example.invalid" in request.content
    assert b"Action.Submit" not in request.content
    assert not (await harness.service.personal_status(TENANT, OTHER)).state == "installed"
    harness.provider.status = 403
    result = await harness.service.test_personal(TENANT, USER, "portal")
    assert result.outcome == "permanentFailure"
    assert (await harness.service.personal_status(TENANT, USER)).state == "blocked"
    count = len(harness.provider.requests)
    assert (await harness.service.test_personal(TENANT, USER, "portal")).outcome == "suppressed"
    assert len(harness.provider.requests) == count
    harness.provider.now += timedelta(seconds=1)
    await harness.service.lifecycle(
        "Bearer " + harness.provider.token(), harness.provider.activity()
    )
    harness.provider.now += timedelta(seconds=1)
    await harness.service.lifecycle(
        "Bearer " + harness.provider.token(), harness.provider.activity(action="remove")
    )
    assert (await harness.service.personal_status(TENANT, USER)).state == "uninstalled"
    assert (await harness.service.test_personal(TENANT, USER, "portal")).outcome == "suppressed"
    assert all(req.url.host != HOST for req in harness.provider.requests)


async def test_binding_no_rebind_replay_or_message_action(harness: Harness) -> None:
    await harness.install()
    original = harness.provider.activity()
    await harness.service.lifecycle("Bearer " + harness.provider.token(), original)
    harness.provider.now += timedelta(seconds=1)
    other = copy.deepcopy(harness.provider.activity())
    other["conversation"]["id"] = "attempted-new-conversation"
    with pytest.raises(TeamsAuthenticationError, match="rebound"):
        await harness.service.lifecycle("Bearer " + harness.provider.token(), other)
    await harness.service.lifecycle(
        "Bearer " + harness.provider.token(), harness.provider.activity(action="remove")
    )
    with pytest.raises(TeamsAuthenticationError, match="older"):
        await harness.service.lifecycle("Bearer " + harness.provider.token(), original)
    harness.provider.now += timedelta(seconds=1)
    await harness.service.lifecycle(
        "Bearer " + harness.provider.token(),
        harness.provider.activity(type="message", text="approve"),
    )
    assert (await harness.service.personal_status(TENANT, USER)).state == "uninstalled"


async def test_bot_membership_lifecycle_and_settings_identity(harness: Harness) -> None:
    await harness.enable()
    await harness.service.lifecycle(
        "Bearer " + harness.provider.token(),
        harness.provider.activity(type="conversationUpdate", membersAdded=[{"id": "28:" + BOT}]),
    )
    assert (await harness.service.personal_status(TENANT, USER)).state == "installed"
    for settings in (
        PersonalTeamsSettingsInput(enabled=True, app_id=APP, bot_app_id=OTHER),
        PersonalTeamsSettingsInput(cloud="usGovernment"),
    ):
        with pytest.raises(ValidationError):
            await harness.service.save_personal_settings(TENANT, USER, settings)


async def test_disabling_delivery_does_not_hide_authenticated_uninstall(harness: Harness) -> None:
    await harness.install()
    await harness.service.save_personal_settings(
        TENANT,
        USER,
        PersonalTeamsSettingsInput(
            enabled=False,
            app_id=APP,
            bot_app_id=BOT,
        ),
    )
    harness.provider.now += timedelta(seconds=1)
    await harness.service.lifecycle(
        "Bearer " + harness.provider.token(), harness.provider.activity(action="remove")
    )
    status = await harness.service.personal_status(TENANT, USER)
    assert not status.enabled and status.state == "uninstalled"
    assert not harness.provider.requests


@pytest.mark.parametrize(
    "tenant,console,portal",
    [
        ("local-development", None, None),
        (TENANT, None, None),
        ("local-development", "http://localhost:5173", "http://localhost:5174"),
    ],
)
async def test_disabled_local_service_needs_no_fake_tenant_or_origins(
    harness: Harness, tenant: str, console: str | None, portal: str | None
) -> None:
    service = TeamsService(
        harness.repository,
        tenant_id=tenant,
        secret_reader=None,
        secret_writer=None,
        transport=harness.service.transport,
        authenticator=TeamsLifecycleAuthenticator(harness.client),
        token_source=None,
        console_origin=console,
        portal_origin=portal,
    )
    assert not (await service.personal_settings(tenant)).enabled
    saved = await service.save_personal_settings(tenant, USER, PersonalTeamsSettingsInput())
    assert not saved.enabled and saved.tenant_id == tenant
    assert not await service.list_destinations(tenant)
    checks = await service.capability_checks(tenant)
    assert {check.state for check in checks} == {"notConfigured", "disabled"}
    result = await service.test_personal(tenant, USER, "portal")
    assert result.outcome == "suppressed"
    assert not harness.provider.requests


@pytest.mark.parametrize(
    "tenant,console,portal",
    [
        ("local-development", "https://console.example.invalid", "https://portal.example.invalid"),
        (TENANT, None, None),
        (TENANT, "https://console.example.invalid", None),
        (TENANT, "http://localhost:5173", "https://portal.example.invalid"),
    ],
)
async def test_enabling_private_delivery_still_requires_uuid_tenant_and_https_origins(
    harness: Harness, tenant: str, console: str | None, portal: str | None
) -> None:
    service = TeamsService(
        harness.repository,
        tenant_id=tenant,
        secret_reader=None,
        secret_writer=None,
        transport=harness.service.transport,
        authenticator=TeamsLifecycleAuthenticator(harness.client),
        token_source=Tokens(),
        console_origin=console,
        portal_origin=portal,
    )
    with pytest.raises(ValidationError, match=r"UUIDs|HTTPS"):
        await service.save_personal_settings(
            tenant, USER, PersonalTeamsSettingsInput(enabled=True, app_id=APP, bot_app_id=BOT)
        )
    assert await harness.repository.get_personal_teams_settings(tenant) is None
    assert not harness.provider.requests


async def test_unconfigured_origins_rechecked_before_existing_destination_or_binding_sends(
    harness: Harness,
) -> None:
    destination = await harness.destination()
    await harness.install()
    service = TeamsService(
        harness.repository,
        tenant_id=TENANT,
        secret_reader=harness.vault,
        secret_writer=harness.vault,
        transport=harness.service.transport,
        authenticator=TeamsLifecycleAuthenticator(harness.client),
        token_source=Tokens(),
        console_origin=None,
        portal_origin=None,
    )
    assert (await service.test_shared(TENANT, destination.id)).outcome == "permanentFailure"
    assert (await service.test_personal(TENANT, USER, "portal")).outcome == "permanentFailure"
    assert not harness.provider.requests
    with pytest.raises(ValidationError, match="HTTPS"):
        await service.save_destination(
            TENANT,
            USER,
            TeamsDestinationInput(
                name="Existing",
                enabled=True,
                allowed_categories=[NotificationCategory.REQUEST_LIFECYCLE],
            ),
            identifier=destination.id,
        )


async def test_dispatcher_contract_and_safe_deep_links(harness: Harness) -> None:
    await harness.install()
    shared = TeamsChannelDispatcher(harness.service)
    personal = TeamsPersonalDispatcher(harness.service)
    assert not shared.supports_idempotency and not personal.supports_idempotency
    delivery = NotificationDelivery(
        id="personal-delivery",
        tenant_id=TENANT,
        event_ids=["event"],
        channel="teamsPersonal",
        recipient_reference=USER,
        persona="portal",
    )
    assert (await personal.send(delivery, [event()])).outcome == "providerAccepted"
    card = json.loads(harness.provider.requests[-1].content)["attachments"][0]["content"]
    assert card["actions"][0]["url"].startswith("https://portal.example.invalid/requests?request=")
    assert all(action["type"] == "Action.OpenUrl" for action in card["actions"])
    assert (await shared.send(delivery, [event()])).outcome == "suppressed"
    checks = await harness.service.capability_checks(TENANT, user_object_id=USER)
    assert all(check.state != "verified" for check in checks)
    assert all(check.feature != "teamsChannel" for check in checks)


@pytest.mark.parametrize(
    "subject,path",
    [
        (
            NotificationSubject(
                publication_id="source/id", publication_target="model", run_id="run/id&a"
            ),
            "/models?tab=published&selected=source%2Fid&runId=run%2Fid%26a",
        ),
        (
            NotificationSubject(
                publication_id="source/id", publication_target="mcp", run_id="run/id&a"
            ),
            "/mcps?tab=published&selected=source%2Fid&runId=run%2Fid%26a",
        ),
        (
            NotificationSubject(
                publication_id="source/id", publication_target="pool", run_id="run/id&a"
            ),
            "/pools/source%2Fid?tab=plans&runId=run%2Fid%26a",
        ),
        (
            NotificationSubject(endpoint_id="source/id", endpoint_kind="model", run_id="run/id&a"),
            "/models?tab=endpoints&selected=source%2Fid&syncRun=run%2Fid%26a",
        ),
        (
            NotificationSubject(endpoint_id="source/id", endpoint_kind="mcp", run_id="run/id&a"),
            "/mcps?tab=registered&selected=source%2Fid&syncRun=run%2Fid%26a",
        ),
        (
            NotificationSubject(
                gateway_id="source/id", run_id="run/id&a", integration="gatewayInventory"
            ),
            "/gateways/source%2Fid?syncRun=run%2Fid%26a",
        ),
        (
            NotificationSubject(
                gateway_id="source/id", run_id="run/id&a", integration="gatewayApply"
            ),
            "/gateways/source%2Fid",
        ),
        (
            NotificationSubject(gateway_id="source/id"),
            "/gateways/source%2Fid",
        ),
    ],
)
def test_admin_cards_use_exact_historical_run_links_without_portal_leakage(
    harness: Harness, subject: NotificationSubject, path: str
) -> None:
    source = event().model_copy(update={"subject": subject})
    admin = harness.service.card([source], persona="admin")
    assert admin["actions"][0]["url"] == "https://console.example.invalid" + path
    assert all(action["type"] == "Action.OpenUrl" for action in admin["actions"])
    portal = harness.service.card([source], persona="portal")
    assert [action["url"] for action in portal["actions"]] == [
        "https://portal.example.invalid/notifications",
        "https://portal.example.invalid/notification-preferences",
    ]


def test_card_subject_link_bound_retains_inbox_and_preferences(harness: Harness) -> None:
    card = harness.service.card([event()] * 10)
    assert len(card["actions"]) == 5
    assert card["actions"][-2]["url"] == "https://console.example.invalid/notifications"
    assert card["actions"][-1]["url"] == "https://console.example.invalid/profile"


async def test_single_tenant_secret_oauth_sends_real_token_request_not_stub(
    harness: Harness,
) -> None:
    secret = harness.vault.new_secret("bot-oauth")
    harness.vault.values[secret.versionless] = "fictional-client-secret"
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"token_type": "Bearer", "access_token": "test-token"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = SecretBotTokenSource(
            client,
            harness.vault,
            tenant_id=TENANT,
            bot_app_id=BOT,
            secret_reference=secret.versionless,
        )
        assert await source.token() == "test-token"
        assert str(seen[0].url) == (f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token")
        assert b"client_secret=fictional-client-secret" in seen[0].content
        assert b"api.botframework.com" in seen[0].content
        harness.vault.fail = True
        with pytest.raises(TeamsAuthenticationError) as raised:
            await source.token()
        assert WEBHOOK not in str(raised.value)


@pytest.mark.parametrize("status", [302, 400, 401, 403, 503])
async def test_oauth_failure_is_real_and_redacted(harness: Harness, status: int) -> None:
    secret = harness.vault.new_secret("bot-oauth")
    harness.vault.values[secret.versionless] = "fictional-client-secret"
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status,
            headers={"Location": "https://evil.invalid"},
            text="fictional-client-secret " + WEBHOOK,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), follow_redirects=True
    ) as client:
        source = SecretBotTokenSource(
            client,
            harness.vault,
            tenant_id=TENANT,
            bot_app_id=BOT,
            secret_reference=secret.versionless,
        )
        with pytest.raises(TeamsAuthenticationError) as raised:
            await source.token()
        assert WEBHOOK not in str(raised.value)
        assert "fictional-client-secret" not in str(raised.value)
        assert len(requests) == 1


@pytest.mark.parametrize("personal", [False, True])
@pytest.mark.parametrize("status", [408, 500, 503])
def test_uncertain_teams_responses_preserve_retry_after(personal: bool, status: int) -> None:
    result = provider_result(
        httpx.Response(status, headers={"Retry-After": "120"}), personal=personal
    )
    assert result.outcome == "unknownOutcome" and result.retry_after_seconds == 120
    assert provider_result(httpx.Response(status), personal=personal).retry_after_seconds is None
    future = format_datetime(datetime.now(UTC) + timedelta(seconds=120), usegmt=True)
    seconds = provider_result(
        httpx.Response(status, headers={"Retry-After": future}), personal=personal
    ).retry_after_seconds
    assert seconds is not None and 115 <= seconds <= 120
    assert (
        provider_result(
            httpx.Response(status, headers={"Retry-After": "invalid"}), personal=personal
        ).retry_after_seconds
        == 60
    )


def test_retry_after_and_connector_message_identity() -> None:
    response = httpx.Response(200, json={"id": "fictional-message-id"})
    assert provider_result(response, personal=True).provider_message_id == "fictional-message-id"
    assert provider_result(response).provider_message_id is None
    assert (
        provider_result(
            httpx.Response(200, json={"id": WEBHOOK}), personal=True
        ).provider_message_id
        is None
    )
    for value in ("9" * 5000, "invalid", ""):
        assert (
            provider_result(httpx.Response(429, headers={"Retry-After": value})).retry_after_seconds
            == 60
        )
    future = format_datetime(datetime.now(UTC) + timedelta(seconds=120), usegmt=True)
    seconds = provider_result(
        httpx.Response(429, headers={"Retry-After": future})
    ).retry_after_seconds
    assert seconds is not None and 115 <= seconds <= 120


async def test_card_size_is_bounded_before_provider_send(harness: Harness) -> None:
    result = await harness.service.transport.workflow(WEBHOOK, {"text": "fictional" * 5000})
    assert result.outcome == "permanentFailure"
    assert result.error_code == "teamsPayloadTooLarge"
    assert not harness.provider.requests


async def test_secret_write_is_rolled_back_on_repository_conflict(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mosaic_api.errors import ConflictError

    old = await harness.destination()

    async def fail(*args: Any, **kwargs: Any) -> Any:
        raise ConflictError("Simulated concurrent edit.")

    monkeypatch.setattr(harness.repository, "save_teams_destination", fail)
    with pytest.raises(ConflictError):
        await harness.service.save_destination(
            TENANT,
            USER,
            TeamsDestinationInput(
                name="Rotated",
                webhook_url=SecretStr(WEBHOOK + "-new"),
                allowed_categories=[NotificationCategory.REQUEST_LIFECYCLE],
            ),
            identifier=old.id,
        )
    assert harness.vault.values == {old.credential_reference_id: WEBHOOK}


async def test_managed_identity_uses_exact_bot_scope() -> None:
    from typing import cast

    from apim_double import FakeCredential
    from azure.core.credentials_async import AsyncTokenCredential

    credential = FakeCredential()
    source = ManagedIdentityBotTokenSource(cast(AsyncTokenCredential, credential), bot_app_id=BOT)
    assert source.bot_app_id == BOT
    assert await source.token()

    # The protocol call is real; no personal-send success is fabricated if OAuth fails.
    class FailingCredential:
        async def get_token(self, *scopes: str, **kwargs: Any) -> Any:
            assert scopes == (BOT_SCOPE,)
            raise RuntimeError("OAuth is unavailable")

    failed = ManagedIdentityBotTokenSource(
        cast(AsyncTokenCredential, FailingCredential()), bot_app_id=BOT
    )
    with pytest.raises(RuntimeError, match="unavailable"):
        await failed.token()


async def test_api_authorization_self_scope_and_callback_never_local_auth(harness: Harness) -> None:
    app = FastAPI()
    app.state.teams_service = harness.service
    app.state.settings = Settings(
        tenant_id=TENANT,
        auth_mode=AuthMode.LOCAL,
        environment=Environment.TEST,
        repository_backend=RepositoryBackend.MEMORY,
    )
    app.state.authenticator = LocalAuthenticator(TENANT, roles=["User"])
    app.add_exception_handler(DomainError, domain_error_handler)
    from fastapi.exceptions import RequestValidationError

    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    app.include_router(teams_router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test.invalid"
    ) as client:
        assert (await client.get("/api/v1/settings/teams/destinations")).status_code == 403
        assert (await client.get("/api/v1/notifications/me/teams")).status_code == 403
        assert (await client.get("/api/v1/notifications/me/teams/checks")).status_code == 403
        assert (
            await client.get("/api/v1/settings/teams/destinations/other/checks")
        ).status_code == 403
        checks = await client.get("/api/v1/me/notifications/teams/checks?userObjectId=" + OTHER)
        assert checks.status_code == 200
        assert "conversationId" not in checks.text and "serviceUrl" not in checks.text
        assert not harness.provider.requests
        response = await client.get("/api/v1/me/notifications/teams?userObjectId=" + OTHER)
        assert response.status_code == 200
        assert "conversationId" not in response.text and "serviceUrl" not in response.text
        await harness.enable()
        assert (
            await client.post("/api/v1/teams/messages", json=harness.provider.activity())
        ).status_code == 401
        response = await client.post(
            "/api/v1/me/notifications/teams/test", json={"userObjectId": OTHER}
        )
        assert response.json()["outcome"] == "suppressed"
        assert not harness.provider.requests
        app.state.authenticator = LocalAuthenticator(TENANT)
        assert (await client.get("/api/v1/notifications/me/teams")).status_code == 200
        response = await client.post(
            "/api/v1/settings/teams/destinations",
            json={
                "name": "Fictional shared route",
                "allowedCategories": ["requestLifecycle"],
                "webhookUrl": WEBHOOK,
            },
        )
        assert response.status_code == 201 and "fictional-secret" not in response.text
        assert "webhookUrl" not in response.json()
        response = await client.post(
            "/api/v1/settings/teams/destinations",
            json={
                "name": "Test",
                "allowedCategories": ["requestLifecycle"],
                "webhookUrl": "http://evil.invalid?sig=DO-NOT-ECHO",
            },
        )
        assert response.status_code == 422 and "DO-NOT-ECHO" not in response.text


async def test_tenant_channel_health_does_not_expose_scoped_teams_test_history(
    harness: Harness,
) -> None:
    await harness.install()
    destination = await harness.destination()
    await harness.service.test_shared(TENANT, destination.id)
    await harness.service.test_personal(TENANT, USER, "portal")
    await harness.service.test_personal(TENANT, USER, "admin")
    await harness.repository.save_channel_health(
        NotificationChannelHealth(id="ordinary-health", tenant_id=TENANT, channel="email")
    )
    app = FastAPI()
    app.state.notification_service = SimpleNamespace(repository=harness.repository)
    app.state.authenticator = LocalAuthenticator(TENANT)
    app.state.settings = Settings(
        tenant_id=TENANT,
        auth_mode=AuthMode.LOCAL,
        environment=Environment.TEST,
        repository_backend=RepositoryBackend.MEMORY,
    )
    app.include_router(notification_delivery_router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test.invalid"
    ) as client:
        response = await client.get("/api/v1/notifications/channel-health")
        assert response.status_code == 200, response.text
        assert [item["id"] for item in response.json()] == ["ordinary-health"]
        assert "lastTest" not in response.text or response.json()[0]["lastTest"] is None
        assert "teamsTest-" not in response.text


@pytest.mark.parametrize("roles", [frozenset({"Admin"}), frozenset({"Admin", "User"})])
async def test_teams_test_routes_keep_portal_and_admin_personas_separate(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    roles: frozenset[str],
) -> None:
    await harness.install()
    app = FastAPI()
    app.state.teams_service = harness.service
    app.state.settings = Settings(
        tenant_id=TENANT,
        auth_mode=AuthMode.LOCAL,
        environment=Environment.TEST,
        repository_backend=RepositoryBackend.MEMORY,
    )
    authenticator = LocalAuthenticator(TENANT)
    monkeypatch.setattr(
        authenticator,
        "authenticate",
        AsyncMock(
            return_value=AuthContext(
                tenant_id=TENANT,
                object_id=USER,
                roles=roles,
            )
        ),
    )
    app.state.authenticator = authenticator
    app.include_router(teams_router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test.invalid"
    ) as client:
        for path, origin, preferences in (
            ("/me/notifications/teams/test", "portal", "/notification-preferences"),
            ("/notifications/me/teams/test", "console", "/profile"),
        ):
            response = await client.post("/api/v1" + path)
            assert response.status_code == 200
            assert response.json()["outcome"] == "providerAccepted"
            card = json.loads(harness.provider.requests[-1].content)["attachments"][0]["content"]
            urls = [action["url"] for action in card["actions"]]
            assert urls
            assert all(url.startswith(f"https://{origin}.example.invalid/") for url in urls)
            assert f"https://{origin}.example.invalid{preferences}" in urls


def test_personal_package_is_valid_json_personal_only_and_has_original_icons() -> None:
    script = Path("apps") / "teams" / "build_package.py"
    spec = importlib.util.spec_from_file_location("teams_package_builder", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output_dir = Path("apps") / "teams" / "dist" / ("test-" + uuid4().hex)
    output = output_dir / "mosaic.zip"
    try:
        manifest = module.build_package(
            output,
            app_id=APP,
            bot_app_id=BOT,
            website="https://mosaic.example.invalid",
            privacy="https://mosaic.example.invalid/privacy",
            terms="https://mosaic.example.invalid/terms",
        )
        assert manifest["id"] == APP
        assert manifest["bots"][0]["scopes"] == ["personal"]
        assert manifest["bots"][0]["isNotificationOnly"] is True
        assert "commandLists" not in manifest["bots"][0]
        with ZipFile(output) as archive:
            assert set(archive.namelist()) == {"manifest.json", "color.png", "outline.png"}
            for name, size in (("color.png", 192), ("outline.png", 32)):
                with Image.open(io.BytesIO(archive.read(name))) as image:
                    assert image.size == (size, size)
                    assert image.mode == "RGBA"
            assert "${" not in archive.read("manifest.json").decode()
    finally:
        shutil.rmtree(output_dir, ignore_errors=True)
