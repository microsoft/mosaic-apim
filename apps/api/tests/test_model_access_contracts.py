import pytest
from mosaic_api.domain import (
    BindingSource,
    EntitlementBinding,
    EntitlementCreate,
    EntitlementUpdate,
    ModelAccessSettings,
    ModelApi,
    model_access_subscription_name,
)
from pydantic import ValidationError


@pytest.mark.parametrize("model", [EntitlementCreate, EntitlementUpdate])
def test_client_cannot_manufacture_an_orchestrated_binding(
    model: type[EntitlementCreate] | type[EntitlementUpdate],
) -> None:
    payload: dict[str, object] = {
        "binding": {"gatewayId": "gateway", "source": "orchestrated"},
    }
    if model is EntitlementCreate:
        payload.update(
            subject={"kind": "user", "id": "principal"},
            resource={"kind": "modelApi", "id": "model"},
        )
    with pytest.raises(ValidationError, match="Orchestrated"):
        model.model_validate(payload)


def test_manual_binding_remains_valid() -> None:
    update = EntitlementUpdate(
        binding=EntitlementBinding(gateway_id="gateway", source=BindingSource.MANUAL)
    )
    assert update.binding is not None
    assert update.binding.source == BindingSource.MANUAL


def test_subscription_identity_is_stable_and_isolated() -> None:
    first = model_access_subscription_name("tenant", "publication", "grant")
    assert first == model_access_subscription_name("tenant", "publication", "grant")
    assert first.startswith("mosaic-grant-")
    assert first != model_access_subscription_name("another", "publication", "grant")
    assert first != model_access_subscription_name("tenant", "another", "grant")
    assert first != model_access_subscription_name("tenant", "publication", "another")


def test_published_model_provenance_does_not_invent_a_snapshot() -> None:
    model = ModelApi(
        id="model", tenant_id="tenant", gateway_id="gateway", api_name="api",
        display_name="Model", path="model", publication_id="publication",
    )
    assert model.imported_from_snapshot_id is None
    assert model.publication_id == "publication"
    with pytest.raises(ValidationError, match="snapshot or a publication"):
        ModelApi(
            id="model", tenant_id="tenant", gateway_id="gateway", api_name="api",
            display_name="Model", path="model",
        )


def test_methods_are_independent_and_both_off_is_explicit() -> None:
    assert ModelAccessSettings().model_dump() == {"keysEnabled": True, "entraEnabled": True}
    assert not ModelAccessSettings(keys_enabled=False, entra_enabled=False).keys_enabled
