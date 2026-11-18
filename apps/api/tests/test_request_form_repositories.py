from copy import deepcopy
from dataclasses import dataclass
from typing import cast
from unittest.mock import patch

import pytest
from azure.cosmos.aio import CosmosClient
from mosaic_api.domain import AccessRequest, RequestFormPublishInput
from mosaic_api.errors import ConflictError
from mosaic_api.repositories.base import EntitlementRepository
from mosaic_api.repositories.cosmos_entitlements import CosmosEntitlementRepository
from mosaic_api.repositories.memory_entitlements import InMemoryEntitlementRepository
from mosaic_api.services.request_forms import RequestFormsService
from test_approval_workflows import ADMIN, RESOURCE, TENANT, USER, audit
from test_model_access_repositories import Cosmos
from test_request_forms import definition, submission, validated


@dataclass
class Store:
    repository: EntitlementRepository
    cosmos: Cosmos | None = None

    def contents(self):
        if self.cosmos:
            return deepcopy(
                (self.cosmos.containers["desired"].items, self.cosmos.containers["audit"].items)
            )
        repo = cast(InMemoryEntitlementRepository, self.repository)
        return deepcopy(
            (
                repo.access_requests,
                repo.request_form_heads,
                repo.request_form_revisions,
                repo.audit_events,
                repo.notification_events,
                repo.entitlements,
            )
        )


@pytest.fixture(params=["memory", "cosmos"])
def store(request: pytest.FixtureRequest) -> Store:
    if request.param == "cosmos":
        cosmos = Cosmos()
        return Store(
            CosmosEntitlementRepository(cast(CosmosClient, cosmos), "db", "desired", "audit"),
            cosmos,
        )
    return Store(InMemoryEntitlementRepository())


async def test_requester_point_reads_are_tenant_scoped_and_filter_before_parsing(
    store: Store,
) -> None:
    record = AccessRequest(
        id="owned-evidence", tenant_id=TENANT, requester_object_id=USER, resource=RESOURCE,
    )
    await store.repository.create_access_request(record, audit())
    own = await store.repository.get_access_request(
        TENANT, record.id, requester_object_id=USER
    )
    assert own is not None and own.id == record.id
    assert await store.repository.get_access_request(
        "other-tenant", record.id, requester_object_id=USER
    ) is None
    if store.cosmos:
        with patch.object(
            store.repository, "_model", side_effect=AssertionError("Foreign evidence was parsed")
        ):
            assert await store.repository.get_access_request(
                TENANT, record.id, requester_object_id="another-user"
            ) is None
    else:
        assert await store.repository.get_access_request(
            TENANT, record.id, requester_object_id="another-user"
        ) is None


async def test_guard_closes_first_publication_race_atomically(store: Store) -> None:
    service = RequestFormsService(store.repository)
    stale, _ = await service.current(TENANT)
    await service.publish(
        ADMIN,
        RequestFormPublishInput(
            expected_revision=0,
            definition=definition(),
            enable_confirmed=True,
        ),
    )
    before = store.contents()
    with pytest.raises(ConflictError) as failed:
        await store.repository.create_access_request(
            AccessRequest(
                id="raced",
                tenant_id=TENANT,
                requester_object_id=USER,
                resource=RESOURCE,
            ),
            audit(),
            form_guard=stale,
        )
    assert failed.value.details["reason"] == "requestFormWriteConflict"
    assert store.contents() == before
    assert await store.repository.get_access_request(TENANT, "raced") is None


async def test_revisions_are_cas_immutable_and_tenant_scoped(store: Store) -> None:
    service = RequestFormsService(store.repository)
    await service.publish(
        ADMIN,
        RequestFormPublishInput(
            expected_revision=0,
            definition=definition(),
            enable_confirmed=True,
        ),
    )
    original = await store.repository.get_request_form_revision(TENANT, 1)
    assert original is not None
    before = store.contents()
    with pytest.raises(ConflictError):
        await service.publish(
            ADMIN,
            RequestFormPublishInput(
                expected_revision=0,
                definition=definition(),
                enable_confirmed=True,
            ),
        )
    assert store.contents() == before
    assert await store.repository.get_request_form_revision("another-tenant", 1) is None
    changed = definition()
    changed.questions[0].label = "Revised intended use"
    await service.publish(ADMIN, RequestFormPublishInput(expected_revision=1, definition=changed))
    historical = await store.repository.get_request_form_revision(TENANT, 1)
    assert historical is not None and historical.definition.questions[0].label == "Intended use"


async def test_decisions_cannot_change_or_remove_the_recorded_form(store: Store) -> None:
    service = RequestFormsService(store.repository)
    await service.publish(
        ADMIN,
        RequestFormPublishInput(
            expected_revision=0,
            definition=definition(),
            enable_confirmed=True,
        ),
    )
    head, _ = await service.current(TENANT)
    snapshot = validated(definition(), submission(definition()))
    record = AccessRequest(
        id="recorded",
        tenant_id=TENANT,
        requester_object_id=USER,
        resource=RESOURCE,
        request_form=snapshot,
        cost_center_id=snapshot.cost_center_id if snapshot else None,
        requested_environment=snapshot.environment if snapshot else None,
    )
    await store.repository.create_access_request(record, audit(), form_guard=head)
    stored = await store.repository.get_access_request(TENANT, record.id)
    assert stored is not None
    before = store.contents()
    with pytest.raises(ConflictError) as changed:
        await store.repository.save_access_request(
            stored.model_copy(
                update={
                    "request_form": None,
                    "state": "denied",
                }
            ),
            audit(),
        )
    assert changed.value.details["reason"] == "requestFormImmutable"
    assert store.contents() == before
    saved = await store.repository.save_access_request(
        stored.model_copy(update={"state": "denied"}), audit()
    )
    assert saved.request_form == snapshot
