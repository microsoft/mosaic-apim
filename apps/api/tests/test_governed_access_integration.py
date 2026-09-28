import httpx
import pytest
from mosaic_api.domain import (
    BindingSource,
    EntitlementEnforcement,
    EntitlementUpdate,
    PublishRunStatus,
    TokenEnforcement,
)
from mosaic_api.errors import ConflictError, NotFoundError
from mosaic_api.integrations.apim.credentials import ApimCredentialClient
from mosaic_api.services.directory import Actor
from mosaic_api.services.portal_access import PortalAccessService
from test_governed_lifecycle import ACTOR, APPLICATION, AUDIENCE, TENANT, USER, Harness

MODEL_CLIENT = "44444444-4444-4444-4444-444444444444"


async def test_publishing_to_self_service_key_and_revoke_uses_trusted_applied_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness()
    try:
        await harness.setup()
        user = await harness.grant()
        await harness.grants.update_entitlement(
            ACTOR,
            user.id,
            EntitlementUpdate(
                enforcement=EntitlementEnforcement(
                    tokens=TokenEnforcement(
                        counter_key_expression="@(context.Subscription?.Key)",
                        tokens_per_minute=1000,
                    )
                )
            ),
        )
        application = await harness.grant(APPLICATION, application=True)
        await harness.govern()
        assert (await harness.apply()).status == PublishRunStatus.SUCCEEDED
        assert not any("listSecrets" in path for path in harness.apim.requests)

        original = harness.apim.handler
        reveals: list[str] = []

        def with_credentials(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path.endswith("/listSecrets"):
                reveals.append(request.url.path)
                return httpx.Response(
                    200,
                    json={
                        "primaryKey": "integration-fixture-primary",
                        "secondaryKey": "integration-fixture-secondary",
                    },
                )
            return original(request)

        monkeypatch.setattr(harness.apim, "handler", with_credentials)
        portal = PortalAccessService(
            harness.grants,
            repository=harness.entitlements,
            directory_repository=harness.directory,
            gateway_repository=harness.gateways,
            credential_factory=lambda resource: ApimCredentialClient(harness.arm, resource),
            model_runtime_client_id=AUDIENCE,
            model_client_id=MODEL_CLIENT,
        )
        owner = Actor(USER, TENANT)
        listed = await portal.list_for_caller(owner)
        assert len(listed) == 1
        assert listed[0].runtime is not None and listed[0].runtime.status == "applied"
        assert listed[0].binding is not None
        assert listed[0].binding.source == BindingSource.ORCHESTRATED
        connection = await portal.connection(owner, user.id)
        assert {operation.name for operation in connection.operations} == {
            "chat-completions", "responses",
        }
        assert connection.entra_scope == f"api://{AUDIENCE}/Models.Invoke"
        assert connection.entra_client_id == MODEL_CLIENT
        assert (
            await portal.reveal_key(owner, user.id, "primary")
        ).key == "integration-fixture-primary"

        with pytest.raises(NotFoundError):
            await portal.reveal_key(owner, application.id, "primary")
        assert len(reveals) == 1
        app_owner = Actor(APPLICATION, TENANT)
        app_connection = await portal.connection(app_owner, application.id)
        assert app_connection.entra_scope == f"api://{AUDIENCE}/.default"
        assert app_connection.entra_client_id is None

        await harness.grants.update_entitlement(ACTOR, user.id, EntitlementUpdate(enabled=False))
        pending = await portal.list_for_caller(owner)
        assert pending[0].runtime is not None
        assert pending[0].runtime.status == "revocationPending"
        with pytest.raises(ConflictError):
            await portal.reveal_key(owner, user.id, "primary")
        assert len(reveals) == 1
        assert (await harness.apply()).status == PublishRunStatus.SUCCEEDED
        revoked = await portal.list_for_caller(owner)
        assert revoked[0].runtime is not None and revoked[0].runtime.status == "revoked"
        with pytest.raises(ConflictError):
            await portal.reveal_key(owner, user.id, "primary")
        assert (
            await portal.reveal_key(app_owner, application.id, "secondary")
        ).key == "integration-fixture-secondary"
        assert len(reveals) == 2

        for event in harness.entitlements.audit_events.values():
            assert "integration-fixture-" not in event.model_dump_json()
        for publication in await harness.gateways.list_publications(TENANT):
            assert "integration-fixture-" not in publication.model_dump_json()
    finally:
        await harness.close()
