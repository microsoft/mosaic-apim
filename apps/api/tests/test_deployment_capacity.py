"""Capacity type and processing scope, derived from a deployment's SKU (ADR 0018)."""

import pytest
from mosaic_api.deployment_capacity import CapacityType, ProcessingScope, classify_sku
from mosaic_api.observed import ObservedModelDeployment
from mosaic_api.repositories.cosmos_endpoints import CosmosModelEndpointRepository


def _deployment(**overrides: object) -> ObservedModelDeployment:
    payload: dict[str, object] = {
        "id": "obsdeployment_1",
        "tenant_id": "tenant-test",
        "endpoint_id": "endpoint_1",
        "snapshot_id": "snapshot_1",
        "deployment_name": "gpt-4o-ptu",
    }
    payload.update(overrides)
    return ObservedModelDeployment.model_validate(payload)


class TestClassifySku:
    @pytest.mark.parametrize(
        ("sku_name", "capacity_type", "processing_scope"),
        [
            ("Standard", CapacityType.PAY_AS_YOU_GO, ProcessingScope.REGIONAL),
            ("GlobalStandard", CapacityType.PAY_AS_YOU_GO, ProcessingScope.GLOBAL),
            ("DataZoneStandard", CapacityType.PAY_AS_YOU_GO, ProcessingScope.DATA_ZONE),
            ("ProvisionedManaged", CapacityType.PROVISIONED, ProcessingScope.REGIONAL),
            ("GlobalProvisionedManaged", CapacityType.PROVISIONED, ProcessingScope.GLOBAL),
            ("DataZoneProvisionedManaged", CapacityType.PROVISIONED, ProcessingScope.DATA_ZONE),
            ("GlobalBatch", CapacityType.BATCH, ProcessingScope.GLOBAL),
            ("DataZoneBatch", CapacityType.BATCH, ProcessingScope.DATA_ZONE),
        ],
    )
    def test_known_skus(
        self, sku_name: str, capacity_type: CapacityType, processing_scope: ProcessingScope
    ) -> None:
        assert classify_sku(sku_name) == (capacity_type, processing_scope)

    def test_case_and_surrounding_space_are_ignored(self) -> None:
        assert classify_sku(" globalprovisionedmanaged ") == (
            CapacityType.PROVISIONED,
            ProcessingScope.GLOBAL,
        )

    @pytest.mark.parametrize("sku_name", [None, "", "DeveloperTier", "S0", "Provisioned"])
    def test_anything_else_is_unknown_rather_than_a_guess(self, sku_name: str | None) -> None:
        assert classify_sku(sku_name) == (CapacityType.UNKNOWN, ProcessingScope.UNKNOWN)


class TestObservedDeploymentCapacity:
    def test_derived_from_the_sku(self) -> None:
        deployment = _deployment(sku_name="DataZoneProvisionedManaged", sku_capacity=100)

        assert deployment.capacity_type == CapacityType.PROVISIONED
        assert deployment.processing_scope == ProcessingScope.DATA_ZONE

    def test_no_sku_is_unknown(self) -> None:
        deployment = _deployment()

        assert deployment.capacity_type == CapacityType.UNKNOWN
        assert deployment.processing_scope == ProcessingScope.UNKNOWN
        assert deployment.spillover_deployment_name is None

    def test_a_supplied_value_cannot_contradict_the_sku(self) -> None:
        deployment = _deployment(
            sku_name="GlobalStandard",
            capacity_type=CapacityType.PROVISIONED,
            processing_scope=ProcessingScope.REGIONAL,
        )

        assert deployment.capacity_type == CapacityType.PAY_AS_YOU_GO
        assert deployment.processing_scope == ProcessingScope.GLOBAL

    def test_a_document_stored_before_the_fields_existed_reads_without_a_resync(self) -> None:
        deployment = CosmosModelEndpointRepository._model(
            ObservedModelDeployment,
            {
                "id": "obsdeployment_1",
                "tenantId": "tenant-test",
                "entityType": "observedModelDeployment",
                "endpointId": "endpoint_1",
                "snapshotId": "snapshot_1",
                "deploymentName": "gpt-4o-ptu",
                "skuName": "GlobalProvisionedManaged",
                "skuCapacity": 100,
                "_etag": '"etag"',
            },
        )

        assert deployment.capacity_type == CapacityType.PROVISIONED
        assert deployment.processing_scope == ProcessingScope.GLOBAL

    def test_stored_document_round_trips(self) -> None:
        stored = _deployment(
            sku_name="ProvisionedManaged",
            spillover_deployment_name="gpt-4o-standard",
        )

        document = CosmosModelEndpointRepository._document(stored)
        assert document["capacityType"] == "provisioned"
        assert document["processingScope"] == "regional"
        assert document["spilloverDeploymentName"] == "gpt-4o-standard"

        read = CosmosModelEndpointRepository._model(ObservedModelDeployment, document)
        assert read.capacity_type == CapacityType.PROVISIONED
        assert read.processing_scope == ProcessingScope.REGIONAL
        assert read.spillover_deployment_name == "gpt-4o-standard"
