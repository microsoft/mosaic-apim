from mosaic_api.domain import Group
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
