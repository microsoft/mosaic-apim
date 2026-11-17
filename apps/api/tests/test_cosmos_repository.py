import pytest
from mosaic_api.domain import AccessRequest, Group
from mosaic_api.repositories.cosmos import CosmosDirectoryRepository


def test_cosmos_system_properties_are_removed_before_validation() -> None:
    group = CosmosDirectoryRepository._model(
        Group,
        {
            "id": "group_example",
            "tenantId": "tenant-test",
            "entityType": "group",
            "name": "Example",
            "_etag": '"00000000-0000-0000-0000-000000000000"',
            "_rid": "resource-id",
            "_self": "dbs/example/colls/example/docs/example/",
            "_attachments": "attachments/",
            "_ts": 1,
        },
    )

    assert group.name == "Example"
    assert group.etag == '"00000000-0000-0000-0000-000000000000"'


@pytest.mark.parametrize(
    "snapshot",
    [
        {},
        {"requesterName": "Jordan Rowan", "requesterUsername": "jordan@example.invalid"},
    ],
)
def test_requester_snapshot_round_trips_without_adding_empty_fields(
    snapshot: dict[str, str],
) -> None:
    record = CosmosDirectoryRepository._model(
        AccessRequest,
        {
            "id": "request-example",
            "tenantId": "tenant-test",
            "requesterObjectId": "00000000-0000-4000-8000-000000000045",
            "resource": {"kind": "modelApi", "id": "model-example"},
            "_etag": '"example-etag"',
            **snapshot,
        },
    )
    document = CosmosDirectoryRepository._document(record)
    assert {key: document[key] for key in snapshot} == snapshot
    if not snapshot:
        assert "requesterName" not in document
        assert "requesterUsername" not in document
    loaded = CosmosDirectoryRepository._model(AccessRequest, document)
    assert loaded.requester_name == record.requester_name
    assert loaded.requester_username == record.requester_username
    assert loaded.requester_object_id == record.requester_object_id
