"""An API key an administrator gives MOSAIC, which MOSAIC keeps in its own Key Vault (ADR 0021).

An administrator who can't store a resource's key in Key Vault pastes it into MOSAIC. MOSAIC writes
it into a new secret in the vault deployed with it, keeps only the secret's identifier, and from
then on reads and publishes the key exactly as it does one the administrator stored (ADR 0018).
These drive the real service, writer, API and logging against doubles of Key Vault.
"""

import asyncio
import json
import logging
import sys
from typing import Any

import httpx
import pytest
from aoai_double import AI_RESOURCE_ID
from apim_double import FakeCredential
from fastapi.testclient import TestClient
from key_vault_double import SECRET_URI, VAULT_NAME, FakeKeyStore
from mosaic_api.config import Settings
from mosaic_api.domain import (
    KeyVaultSecretId,
    ModelEndpointCreate,
    ModelEndpointStatus,
    ModelEndpointUpdate,
    PublishedResourceKind,
    PublishRunStatus,
)
from mosaic_api.errors import (
    ChangeNotRecordedError,
    ConflictError,
    UpstreamAuthorizationError,
    UpstreamError,
    ValidationError,
)
from mosaic_api.integrations.key_vault import (
    MANAGED_BY,
    MANAGED_BY_TAG,
    MODEL_ENDPOINT_TAG,
    STORED_KEY_PREFIX,
    KeyVaultSecretWriter,
)
from mosaic_api.main import create_app
from mosaic_api.observability import render_exceptions
from mosaic_api.services import model_endpoints
from pydantic import SecretStr
from pydantic import ValidationError as PydanticValidationError
from test_key_endpoints import ACTOR, KEY, ORIGIN, PROJECT_URL, KeyWorld, keyed
from test_key_publishing import CLAUDE_API, CLAUDE_KEY, KeyHarness

# Fictional, and distinct from the first key, so a test can tell which one went where.
NEW_KEY = "fictional-ROTATED-key-never-logged-5678"
VAULT_URI = f"https://{VAULT_NAME}.vault.azure.net"


def pasted(**overrides: object) -> ModelEndpointCreate:
    """A registration that gives MOSAIC the key itself rather than a secret's URI."""

    return keyed(credential_secret_uri=None, api_key=KEY, **overrides)


class LogRecorder:
    """Every call made to the model endpoint service's logger, at whatever level.

    Recorded in place of the logger itself rather than through structlog's capture, which a logger
    cached under an earlier configuration in the same test run never reaches.
    """

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def __getattr__(self, level: str) -> Any:
        def log(event: str, *args: object, **fields: object) -> None:
            self.entries.append({"level": level, "event": event, "args": args, **fields})

        return log


@pytest.fixture
def logged(monkeypatch: pytest.MonkeyPatch) -> LogRecorder:
    recorder = LogRecorder()
    monkeypatch.setattr(model_endpoints, "logger", recorder)
    return recorder


@pytest.fixture
def store() -> FakeKeyStore:
    return FakeKeyStore()


@pytest.fixture
async def world(store: FakeKeyStore) -> KeyWorld:
    built = KeyWorld(key_store=store)
    await built.add_gateway()
    built.grant()
    return built


def stored_secret(world: KeyWorld) -> str:
    [credential] = world.endpoint_repository.credentials.values()
    return str(credential.secret_uri)


def everything_recorded(world: KeyWorld, *extra: object) -> str:
    repository = world.endpoint_repository
    return json.dumps(
        [
            *(item.model_dump(mode="json") for item in repository.endpoints.values()),
            *(item.model_dump(mode="json") for item in repository.credentials.values()),
            *(item.model_dump(mode="json") for item in repository.audit_events.values()),
            *extra,
        ],
        default=str,
    )


class TestRequest:
    def test_a_pasted_key_is_trimmed_and_never_dumped(self) -> None:
        request = keyed(credential_secret_uri=None, api_key=f"  {KEY}\n")

        assert request.api_key is not None
        assert request.api_key.get_secret_value() == KEY
        assert KEY not in repr(request)
        assert KEY not in json.dumps(request.model_dump(mode="json"))
        assert "apiKey" not in request.model_dump(mode="json", by_alias=True)

    @pytest.mark.parametrize(
        "key",
        [
            "has a space inside it, so it isn't a key",
            "line-one-of-a-key\nline-two-of-a-key",
            "short-key",
            "x" * 513,
            "fictional-k\u00e9y-with-an-accent",
        ],
    )
    def test_anything_but_one_printable_key_is_refused_without_repeating_it(
        self, key: str
    ) -> None:
        with pytest.raises(PydanticValidationError) as refused:
            keyed(credential_secret_uri=None, api_key=key)

        assert "no spaces or line breaks" in str(refused.value)
        assert key not in str(refused.value)

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"api_key": KEY}, "not both"),
            ({"credential_secret_uri": None}, "give its API key"),
            (
                {
                    "endpoint": None,
                    "azure_resource_id": AI_RESOURCE_ID,
                    "credential_secret_uri": None,
                    "deployments": None,
                    "api_key": KEY,
                },
                "takes no API key",
            ),
            (
                {
                    "endpoint": "https://models.example.com/v1",
                    "credential_secret_uri": None,
                    "deployments": None,
                    "api_key": KEY,
                },
                "only for an Azure OpenAI or Foundry resource",
            ),
        ],
    )
    def test_a_key_goes_only_where_mosaic_can_keep_it(
        self, overrides: dict[str, object], message: str
    ) -> None:
        with pytest.raises(PydanticValidationError) as refused:
            keyed(**overrides)

        assert message in str(refused.value)
        assert KEY not in str(refused.value)

    def test_an_update_takes_a_new_key_or_a_new_secret_not_both(self) -> None:
        with pytest.raises(PydanticValidationError, match="not both"):
            ModelEndpointUpdate(api_key=SecretStr(KEY), credential_secret_uri=SECRET_URI)  # type: ignore[arg-type]

    def test_an_update_takes_a_new_key_on_its_own(self) -> None:
        with pytest.raises(PydanticValidationError) as refused:
            ModelEndpointUpdate(api_key=SecretStr(KEY), name="New name")

        assert "on its own" in str(refused.value)
        assert KEY not in str(refused.value)


class TestRegistration:
    async def test_the_key_goes_into_a_new_secret_and_only_its_identifier_is_kept(
        self, world: KeyWorld, store: FakeKeyStore, logged: LogRecorder
    ) -> None:
        endpoint = await world.service.register(ACTOR, pasted())

        [(action, name)] = store.calls
        assert action == "put"
        assert name.startswith(f"{STORED_KEY_PREFIX}fabrikam-foundry-")
        assert store.versions[name] == [KEY]
        assert store.tags[name] == {MANAGED_BY_TAG: MANAGED_BY, MODEL_ENDPOINT_TAG: endpoint.id}
        assert stored_secret(world) == f"{VAULT_URI}/secrets/{name}"
        assert endpoint.key_stored_by_mosaic is True
        # MOSAIC read back what it wrote, and the resource accepted it.
        assert world.secret_reads == [stored_secret(world)]
        assert world.probed_keys == [KEY]
        assert endpoint.status == ModelEndpointStatus.CONNECTED
        assert KEY not in everything_recorded(world, logged.entries)

    async def test_two_registrations_of_one_resource_never_share_a_secret(self) -> None:
        names = set()
        for _ in range(2):
            store = FakeKeyStore()
            world = KeyWorld(key_store=store)
            await world.service.register(ACTOR, pasted())
            names.update(store.versions)
        # The vault keeps a deleted secret's name, so a removed endpoint registered again needs
        # a name it hasn't used.
        assert len(names) == 2

    async def test_a_resource_already_registered_is_refused_before_any_key_is_stored(
        self, world: KeyWorld, store: FakeKeyStore
    ) -> None:
        await world.register()

        with pytest.raises(ConflictError, match="already registered"):
            await world.service.register(ACTOR, pasted())

        assert store.calls == []

    async def test_an_unknown_environment_is_refused_before_any_key_is_stored(
        self, world: KeyWorld, store: FakeKeyStore
    ) -> None:
        with pytest.raises(ValidationError, match="not defined"):
            await world.service.register(ACTOR, pasted(environment="nowhere"))

        assert store.calls == []

    async def test_without_a_vault_of_its_own_mosaic_takes_no_key(self) -> None:
        world = KeyWorld()

        with pytest.raises(ConflictError) as refused:
            await world.service.register(ACTOR, pasted())

        assert refused.value.details["reason"] == "keyStoreUnavailable"
        assert "give MOSAIC the secret's URI" in refused.value.message
        assert world.endpoint_repository.endpoints == {}

    async def test_a_vault_that_refuses_the_key_leaves_nothing_registered(
        self, world: KeyWorld, store: FakeKeyStore, logged: LogRecorder
    ) -> None:
        store.put_error = UpstreamAuthorizationError(
            "MOSAIC isn't allowed to store secrets in Key Vault kv-contoso-ai."
        )

        with pytest.raises(UpstreamAuthorizationError):
            await world.service.register(ACTOR, pasted())

        assert world.endpoint_repository.endpoints == {}
        assert world.endpoint_repository.credentials == {}
        assert store.versions == {}
        assert [call[0] for call in store.calls] == ["put", "delete"]
        assert not [
            entry for entry in logged.entries if entry["event"] == "stored_key_not_discarded"
        ]

    async def test_a_vault_write_that_commits_then_fails_is_deleted(
        self, world: KeyWorld, store: FakeKeyStore
    ) -> None:
        store.put_error_after_write = UpstreamError("Key Vault timed out.")

        with pytest.raises(UpstreamError, match="timed out"):
            await world.service.register(ACTOR, pasted())

        [(_, name)] = [call for call in store.calls if call[0] == "put"]
        assert ("delete", name) in store.calls
        assert store.versions == {}
        assert world.endpoint_repository.endpoints == {}

    async def test_a_cancelled_registration_deletes_the_stored_key(
        self, world: KeyWorld, store: FakeKeyStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def cancel(*_: Any, **__: Any) -> Any:
            raise asyncio.CancelledError()

        monkeypatch.setattr(world.endpoint_repository, "create_endpoint", cancel)

        with pytest.raises(asyncio.CancelledError):
            await world.service.register(ACTOR, pasted())

        [(_, name)] = [call for call in store.calls if call[0] == "put"]
        assert ("delete", name) in store.calls
        assert store.versions == {}
        assert world.endpoint_repository.endpoints == {}

    async def test_a_registration_that_fails_after_the_key_is_stored_deletes_it(
        self, world: KeyWorld, store: FakeKeyStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def refuse(*_: Any, **__: Any) -> Any:
            raise ConflictError("Someone registered it a moment earlier.")

        monkeypatch.setattr(world.endpoint_repository, "create_endpoint", refuse)

        with pytest.raises(ConflictError, match="a moment earlier"):
            await world.service.register(ACTOR, pasted())

        [(_, name)] = [call for call in store.calls if call[0] == "put"]
        assert ("delete", name) in store.calls
        assert store.versions == {}

    async def test_a_key_that_cant_be_deleted_again_is_reported_without_a_traceback(
        self,
        world: KeyWorld,
        store: FakeKeyStore,
        monkeypatch: pytest.MonkeyPatch,
        logged: LogRecorder,
    ) -> None:
        async def refuse(*_: Any, **__: Any) -> Any:
            raise ConflictError("Someone registered it a moment earlier.")

        monkeypatch.setattr(world.endpoint_repository, "create_endpoint", refuse)
        store.delete_error = UpstreamError("Key Vault is unreachable.")

        with pytest.raises(ConflictError, match="moment earlier"):
            await world.service.register(ACTOR, pasted())

        [warning] = [
            entry for entry in logged.entries if entry["event"] == "stored_key_not_discarded"
        ]
        assert warning["level"] == "warning"
        assert warning["vault"] == VAULT_NAME
        assert "exc_info" not in warning
        assert not any(entry["level"] == "exception" for entry in logged.entries)
        assert KEY not in json.dumps(logged.entries, default=str)

    async def test_a_secret_uri_registration_is_untouched(
        self, world: KeyWorld, store: FakeKeyStore
    ) -> None:
        endpoint = await world.register()

        assert store.calls == []
        assert endpoint.key_stored_by_mosaic is False
        assert "keyStoredByMosaic" not in endpoint.model_dump(mode="json", by_alias=True)
        assert stored_secret(world) == SECRET_URI


class TestReplacingTheKey:
    async def test_a_new_key_is_the_next_version_of_the_same_secret(
        self, world: KeyWorld, store: FakeKeyStore, logged: LogRecorder
    ) -> None:
        endpoint = await world.service.register(ACTOR, pasted())
        uri = stored_secret(world)

        updated = await world.service.update(
            ACTOR, endpoint.id, ModelEndpointUpdate(api_key=SecretStr(NEW_KEY))
        )

        [name] = store.versions
        assert store.versions[name] == [KEY, NEW_KEY]
        # Same identifier, so no publication needs a new plan.
        assert stored_secret(world) == uri
        assert world.probed_keys[-1] == NEW_KEY
        assert updated.status == ModelEndpointStatus.CONNECTED
        [audit] = [
            event
            for event in world.endpoint_repository.audit_events.values()
            if event.action == "credentialReference.keyReplaced"
        ]
        assert audit.details == {"modelEndpointId": endpoint.id}
        recorded = everything_recorded(world, logged.entries, updated.model_dump(mode="json"))
        assert NEW_KEY not in recorded
        assert KEY not in recorded

    async def test_a_key_write_that_cant_be_recorded_reports_the_key_is_live(
        self, world: KeyWorld, store: FakeKeyStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        endpoint = await world.service.register(ACTOR, pasted())

        async def fail(*_: Any, **__: Any) -> Any:
            raise RuntimeError("Cosmos is unreachable.")

        monkeypatch.setattr(world.endpoint_repository, "save_credential", fail)

        with pytest.raises(ChangeNotRecordedError) as refused:
            await world.service.update(
                ACTOR, endpoint.id, ModelEndpointUpdate(api_key=SecretStr(NEW_KEY))
            )

        assert "stored the new key" in refused.value.message
        assert refused.value.details == {"id": endpoint.id, "reason": "keyReplacedNotRecorded"}
        [name] = store.versions
        assert store.versions[name] == [KEY, NEW_KEY]
        recorded = json.dumps(
            {"message": refused.value.message, "details": refused.value.details}, default=str
        )
        assert KEY not in recorded
        assert NEW_KEY not in recorded

    async def test_a_key_write_with_status_recording_failure_reports_the_key_is_live(
        self, world: KeyWorld, store: FakeKeyStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        endpoint = await world.service.register(ACTOR, pasted())

        async def fail(*_: Any, **__: Any) -> Any:
            raise RuntimeError("Cosmos is unreachable.")

        monkeypatch.setattr(world.endpoint_repository, "record_endpoint_state", fail)

        with pytest.raises(ChangeNotRecordedError) as refused:
            await world.service.update(
                ACTOR, endpoint.id, ModelEndpointUpdate(api_key=SecretStr(NEW_KEY))
            )

        assert "stored the new key" in refused.value.message
        assert refused.value.details["reason"] == "keyReplacedNotRecorded"
        [name] = store.versions
        assert store.versions[name] == [KEY, NEW_KEY]

    async def test_a_save_endpoint_conflict_doesnt_break_a_key_replacement(
        self, world: KeyWorld, store: FakeKeyStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        endpoint = await world.service.register(ACTOR, pasted())

        async def conflict(*_: Any, **__: Any) -> Any:
            raise ConflictError("The endpoint changed.")

        monkeypatch.setattr(world.endpoint_repository, "save_endpoint", conflict)

        updated = await world.service.update(
            ACTOR, endpoint.id, ModelEndpointUpdate(api_key=SecretStr(NEW_KEY))
        )

        [name] = store.versions
        assert store.versions[name] == [KEY, NEW_KEY]
        assert world.probed_keys[-1] == NEW_KEY
        assert updated.status == ModelEndpointStatus.CONNECTED

    async def test_a_key_mosaic_doesnt_keep_is_changed_where_it_is_kept(
        self, world: KeyWorld, store: FakeKeyStore
    ) -> None:
        endpoint = await world.register()

        with pytest.raises(ConflictError, match="a Key Vault secret you manage"):
            await world.service.update(
                ACTOR, endpoint.id, ModelEndpointUpdate(api_key=SecretStr(NEW_KEY))
            )

        assert store.calls == []

    async def test_a_key_mosaic_keeps_isnt_swapped_for_a_secret_uri(
        self, world: KeyWorld
    ) -> None:
        endpoint = await world.service.register(ACTOR, pasted())

        with pytest.raises(ConflictError, match="replace the key instead"):
            await world.service.update(
                ACTOR,
                endpoint.id,
                ModelEndpointUpdate.model_validate({"credentialSecretUri": SECRET_URI}),
            )

        assert stored_secret(world) != SECRET_URI

    async def test_an_endpoint_without_a_key_takes_none(self, world: KeyWorld) -> None:
        by_id = await world.service.register(
            ACTOR, ModelEndpointCreate.model_validate({"azure_resource_id": AI_RESOURCE_ID})
        )

        with pytest.raises(ValidationError, match="takes a new key"):
            await world.service.update(
                ACTOR, by_id.id, ModelEndpointUpdate(api_key=SecretStr(NEW_KEY))
            )

    async def test_a_missing_secret_says_to_replace_the_key(
        self, world: KeyWorld, store: FakeKeyStore
    ) -> None:
        endpoint = await world.service.register(ACTOR, pasted())
        world.secret_error = ValidationError("That Key Vault secret does not exist.")

        checked = await world.service.preflight(ACTOR, endpoint.id)

        assert "Replace the API key" in (checked.access.message or "")


class TestRemoval:
    async def test_removing_the_endpoint_deletes_the_secret_mosaic_stored(
        self, world: KeyWorld, store: FakeKeyStore
    ) -> None:
        endpoint = await world.service.register(ACTOR, pasted())
        [name] = store.versions

        await world.service.delete(ACTOR, endpoint.id)

        assert ("delete", name) in store.calls
        assert store.versions == {}
        assert endpoint.id not in world.endpoint_repository.endpoints

    async def test_a_secret_already_gone_doesnt_block_removal(
        self, world: KeyWorld, store: FakeKeyStore
    ) -> None:
        endpoint = await world.service.register(ACTOR, pasted())
        store.versions.clear()

        await world.service.delete(ACTOR, endpoint.id)

        assert endpoint.id not in world.endpoint_repository.endpoints

    async def test_a_vault_that_refuses_the_delete_keeps_the_endpoint(
        self, world: KeyWorld, store: FakeKeyStore
    ) -> None:
        endpoint = await world.service.register(ACTOR, pasted())
        store.delete_error = UpstreamAuthorizationError(
            "MOSAIC isn't allowed to delete secrets in Key Vault kv-contoso-ai."
        )

        with pytest.raises(UpstreamAuthorizationError):
            await world.service.delete(ACTOR, endpoint.id)

        assert endpoint.id in world.endpoint_repository.endpoints
        assert store.versions != {}

    async def test_a_secret_uri_endpoint_leaves_its_secret_alone(
        self, world: KeyWorld, store: FakeKeyStore
    ) -> None:
        endpoint = await world.register()

        await world.service.delete(ACTOR, endpoint.id)

        assert store.calls == []

    async def test_a_published_endpoint_keeps_its_key(self) -> None:
        store = FakeKeyStore()
        harness = KeyHarness(world=KeyWorld(key_store=store))
        await harness.setup(credential_secret_uri=None, api_key=KEY)
        publication_id = await harness.publish()
        assert (await harness.apply(publication_id)).status == PublishRunStatus.SUCCEEDED

        with pytest.raises(ConflictError, match="Unpublish"):
            await harness.world.service.delete(ACTOR, harness.endpoint_id)

        assert not any(action == "delete" for action, _ in store.calls)


async def test_a_stored_key_publishes_exactly_like_one_the_administrator_stored() -> None:
    store = FakeKeyStore()
    harness = KeyHarness(world=KeyWorld(key_store=store))
    await harness.setup(credential_secret_uri=None, api_key=KEY)
    [name] = store.versions

    run = await harness.apply(await harness.publish())

    assert run.status == PublishRunStatus.SUCCEEDED
    [body] = [
        call["body"]
        for call in harness.apim.http_calls
        if call["method"] == "PUT" and call["path"] == CLAUDE_KEY
    ]
    assert body["properties"]["keyVault"] == {"secretIdentifier": f"{VAULT_URI}/secrets/{name}"}
    assert body["properties"]["displayName"] == f"{CLAUDE_API}-key"
    assert KEY not in json.dumps(harness.apim.http_calls)
    assert any(step.kind == PublishedResourceKind.NAMED_VALUE for step in run.steps)


class TestWriter:
    def writer(self, handler: Any) -> KeyVaultSecretWriter:
        return KeyVaultSecretWriter(
            FakeCredential(),  # type: ignore[arg-type]
            f"{VAULT_URI}/",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    async def test_it_writes_the_value_with_mosaics_identity_and_reads_nothing_back(
        self,
    ) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            # Key Vault repeats the value in its answer, which MOSAIC never reads.
            return httpx.Response(200, json={"value": KEY})

        writer = self.writer(handler)
        secret = writer.new_secret("mosaic-apikey-fabrikam-foundry-0a1b2c3d")

        await writer.put(secret, SecretStr(KEY), tags={MANAGED_BY_TAG: MANAGED_BY})

        [request] = seen
        assert request.method == "PUT"
        assert str(request.url) == (
            f"{VAULT_URI}/secrets/mosaic-apikey-fabrikam-foundry-0a1b2c3d?api-version=7.4"
        )
        assert request.headers["Authorization"].startswith("Bearer ")
        assert json.loads(request.content) == {"value": KEY, "tags": {MANAGED_BY_TAG: MANAGED_BY}}
        assert writer.vault_name == VAULT_NAME

    @pytest.mark.parametrize(
        ("status", "error", "message"),
        [
            (403, UpstreamAuthorizationError, "Key Vault Secrets Officer"),
            (401, UpstreamAuthorizationError, "Key Vault Secrets Officer"),
            (409, ConflictError, "deleted secret"),
            (500, UpstreamError, "HTTP 500"),
        ],
    )
    async def test_a_refusal_never_repeats_the_key_or_what_the_vault_said(
        self, status: int, error: type[Exception], message: str
    ) -> None:
        writer = self.writer(
            lambda _: httpx.Response(status, json={"error": {"message": f"echo {KEY}"}})
        )

        with pytest.raises(error) as refused:
            await writer.put(writer.new_secret("mosaic-apikey-x-0a1b2c3d"), SecretStr(KEY), tags={})

        assert message in str(refused.value)
        assert KEY not in str(refused.value)
        assert KEY not in json.dumps(getattr(refused.value, "details", {}))

    async def test_an_unreachable_vault_chains_nothing_that_held_the_key(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route", request=request)

        writer = self.writer(handler)

        with pytest.raises(UpstreamError) as refused:
            await writer.put(writer.new_secret("mosaic-apikey-x-0a1b2c3d"), SecretStr(KEY), tags={})

        assert "Nothing was stored" in str(refused.value)
        assert refused.value.__cause__ is None
        assert refused.value.__suppress_context__ is True

    @pytest.mark.parametrize(("status", "deleted"), [(200, True), (404, False)])
    async def test_deleting_a_secret_that_is_already_gone_is_fine(
        self, status: int, deleted: bool
    ) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(status, json={})

        writer = self.writer(handler)
        secret = KeyVaultSecretId.parse(f"{VAULT_URI}/secrets/mosaic-apikey-x-0a1b2c3d")

        assert await writer.delete(secret) is deleted
        assert seen[0].method == "DELETE"

    async def test_a_refused_delete_says_what_to_grant(self) -> None:
        writer = self.writer(lambda _: httpx.Response(403, json={}))
        secret = KeyVaultSecretId.parse(f"{VAULT_URI}/secrets/mosaic-apikey-x-0a1b2c3d")

        with pytest.raises(UpstreamAuthorizationError, match="delete secrets"):
            await writer.delete(secret)

    def test_only_a_key_vault_is_accepted_as_mosaics_vault(self) -> None:
        with pytest.raises(ValueError, match="MOSAIC_KEY_VAULT_URI"):
            KeyVaultSecretWriter(FakeCredential(), "https://storage.example.com")  # type: ignore[arg-type]


class TestHttpContract:
    @pytest.fixture
    def client(self, settings: Settings, store: FakeKeyStore) -> Any:
        world = KeyWorld(key_store=store)
        app = create_app(settings)
        with TestClient(app) as client:
            app.state.model_endpoint_repository = world.endpoint_repository
            app.state.model_endpoint_service = world.service
            yield client

    def test_a_key_goes_in_and_never_comes_back_out(
        self, client: TestClient, store: FakeKeyStore
    ) -> None:
        created = client.post(
            "/api/v1/model-endpoints",
            json={"endpoint": PROJECT_URL, "apiKey": KEY, "name": "Fabrikam partner Foundry"},
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["keyStoredByMosaic"] is True
        assert body["endpoint"].rstrip("/") == ORIGIN
        assert "apiKey" not in body

        replaced = client.patch(f"/api/v1/model-endpoints/{body['id']}", json={"apiKey": NEW_KEY})
        assert replaced.status_code == 200, replaced.text
        listed = client.get("/api/v1/model-endpoints")
        [name] = store.versions
        assert store.versions[name] == [KEY, NEW_KEY]
        for response in (created, replaced, listed):
            assert KEY not in response.text
            assert NEW_KEY not in response.text

    def test_a_key_replacement_with_other_changes_is_refused_without_repeating_the_key(
        self, client: TestClient
    ) -> None:
        created = client.post(
            "/api/v1/model-endpoints",
            json={"endpoint": PROJECT_URL, "apiKey": KEY, "name": "Fabrikam partner Foundry"},
        )
        assert created.status_code == 201, created.text
        body = created.json()

        refused = client.patch(
            f"/api/v1/model-endpoints/{body['id']}",
            json={"apiKey": NEW_KEY, "name": "Renamed"},
        )

        assert refused.status_code == 422
        assert "on its own" in refused.text
        assert NEW_KEY not in refused.text

    @pytest.mark.parametrize(
        "payload",
        [
            # The key itself is wrong.
            {"endpoint": PROJECT_URL, "apiKey": f"{KEY} {KEY}"},
            # Something else is wrong, and the rule spans the whole body, key included.
            {"endpoint": "http://fabrikam-foundry.services.ai.azure.com", "apiKey": KEY},
            {"endpoint": PROJECT_URL, "apiKey": KEY, "credentialSecretUri": SECRET_URI},
            {"endpoint": PROJECT_URL, "apiKey": KEY, "unexpected": True},
        ],
    )
    def test_a_refused_request_never_repeats_the_key(
        self, client: TestClient, store: FakeKeyStore, payload: dict[str, object]
    ) -> None:
        refused = client.post("/api/v1/model-endpoints", json=payload)

        assert refused.status_code == 422
        assert KEY not in refused.text
        errors = refused.json()["detail"]
        assert errors and all("input" not in error for error in errors)
        assert all({"loc", "msg", "type"} <= set(error) for error in errors)
        assert store.calls == []

    def test_a_refused_update_never_repeats_the_key(self, client: TestClient) -> None:
        refused = client.patch(
            "/api/v1/model-endpoints/anything", json={"apiKey": "two words", "name": None}
        )

        assert refused.status_code == 422
        assert "two words" not in refused.text

    def test_the_api_describes_the_key_as_write_only(self, client: TestClient) -> None:
        schema = client.get("/openapi.json").json()["components"]["schemas"]
        api_key = schema["ModelEndpointCreate"]["properties"]["apiKey"]

        assert json.dumps(api_key).count("writeOnly") >= 1
        assert "password" in json.dumps(api_key)


def test_a_logged_traceback_carries_no_local_variables() -> None:
    def read(key: str) -> None:
        raise RuntimeError("the vault answered oddly")

    try:
        read(KEY)
    except RuntimeError:
        event = render_exceptions(
            logging.getLogger("test"), "error", {"event": "failed", "exc_info": sys.exc_info()}
        )

    rendered = json.dumps(event, default=str)
    assert "the vault answered oddly" in rendered
    assert KEY not in rendered
    assert all("locals" not in frame for frame in event["exception"][0]["frames"])
