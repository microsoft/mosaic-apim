"""Build an observed model inventory for one endpoint.

Mirrors ``integrations.apim.inventory``: collection is partial-tolerant, and a section MOSAIC could
not read marks its entity types incomplete so the caller exempts them from the stale-document sweep.
Otherwise a transient throttling response would turn "MOSAIC could not read deployments" into "this
endpoint has no models", which for a governance control plane is worse than an outright error.
"""

import asyncio
from collections.abc import Callable, Coroutine
from functools import partial
from typing import Any

import structlog

from mosaic_api.domain import ModelInventorySummary, deterministic_id, new_id
from mosaic_api.errors import DomainError
from mosaic_api.integrations.aoai.client import CognitiveServicesClient
from mosaic_api.integrations.apim.client import JsonObject
from mosaic_api.observed import (
    ObservedAvailableModel,
    ObservedEndpointEntity,
    ObservedModelDeployment,
)

logger = structlog.get_logger()

DEPLOYMENT_TYPE = "observedModelDeployment"
AVAILABLE_MODEL_TYPE = "observedAvailableModel"

# Capability flags map to the request paths a deployment can serve. Used to describe a deployment
# in the UI, never to construct a call: MOSAIC does not invoke models.
_CAPABILITY_PATHS: tuple[tuple[str, str], ...] = (
    ("chatCompletion", "/chat/completions"),
    ("completion", "/completions"),
    ("embeddings", "/embeddings"),
    ("imageGenerations", "/images/generations"),
    ("audio", "/audio/speech"),
    ("realtime", "/realtime"),
)


class ModelInventorySnapshot:
    def __init__(self, snapshot_id: str) -> None:
        self.snapshot_id = snapshot_id
        self.deployments: list[ObservedModelDeployment] = []
        self.available_models: list[ObservedAvailableModel] = []
        self.errors: list[str] = []
        self.incomplete_types: set[str] = set()

    def entities(self) -> list[ObservedEndpointEntity]:
        entities: list[ObservedEndpointEntity] = []
        entities.extend(self.deployments)
        entities.extend(self.available_models)
        return entities

    def summary(self) -> ModelInventorySummary:
        return ModelInventorySummary(
            deployments=len(self.deployments),
            available_models=len(self.available_models),
            succeeded_deployments=sum(
                1
                for deployment in self.deployments
                if (deployment.provisioning_state or "").casefold() == "succeeded"
            ),
            deprecated_deployments=sum(
                1
                for model in self.available_models
                if (model.lifecycle_status or "").casefold() == "deprecated"
            ),
        )


def _no_items() -> list[JsonObject]:
    """A fresh, correctly typed empty fallback for a collection MOSAIC could not read."""

    return []


def _properties(item: JsonObject) -> JsonObject:
    properties = item.get("properties")
    return properties if isinstance(properties, dict) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _capability_map(value: object) -> dict[str, str]:
    """Normalise ARM's free-form capability map to strings so the model stays typed."""

    if not isinstance(value, dict):
        return {}
    return {
        str(key): str(item)
        for key, item in value.items()
        if item is not None and not isinstance(item, dict | list)
    }


def _request_paths(capabilities: dict[str, str]) -> list[str]:
    lowered = {key.casefold(): value for key, value in capabilities.items()}
    paths: list[str] = []
    for flag, path in _CAPABILITY_PATHS:
        raw = lowered.get(flag.casefold())
        if raw is not None and raw.casefold() not in {"false", "0", ""}:
            paths.append(path)
    return paths


class ModelInventoryCollector:
    def __init__(
        self,
        client: CognitiveServicesClient,
        *,
        tenant_id: str,
        endpoint_id: str,
    ) -> None:
        self._client = client
        self._tenant_id = tenant_id
        self._endpoint_id = endpoint_id
        self._snapshot_id = new_id("snapshot")
        self._snapshot = ModelInventorySnapshot(self._snapshot_id)

    def _id(self, prefix: str, *parts: str) -> str:
        return deterministic_id(prefix, self._tenant_id, self._endpoint_id, *parts)

    async def _guard[T](
        self,
        label: str,
        work: Callable[[], Coroutine[Any, Any, T]],
        fallback: T,
        *,
        affects: tuple[str, ...] = (),
    ) -> T:
        # ``work`` is a factory rather than a coroutine so nothing is constructed until it is about
        # to be awaited, matching the gateway collector's shutdown behaviour.
        try:
            return await work()
        except DomainError as error:
            self._snapshot.errors.append(f"{label}: {error.message}")
            self._snapshot.incomplete_types.update(affects)
            logger.warning("model_inventory_section_failed", section=label, reason=error.message)
            return fallback

    async def collect(self) -> ModelInventorySnapshot:
        deployments, models = await asyncio.gather(
            self._guard(
                "deployments",
                partial(self._client.list_deployments),
                _no_items(),
                affects=(DEPLOYMENT_TYPE,),
            ),
            self._guard(
                "available models",
                partial(self._client.list_models),
                _no_items(),
                affects=(AVAILABLE_MODEL_TYPE,),
            ),
        )
        self._collect_deployments(deployments)
        self._collect_available_models(models)
        return self._snapshot

    def _collect_deployments(self, items: list[JsonObject]) -> None:
        for item in items:
            name = _text(item.get("name"))
            if not name:
                continue
            properties = _properties(item)
            model = properties.get("model")
            model = model if isinstance(model, dict) else {}
            sku = item.get("sku")
            sku = sku if isinstance(sku, dict) else {}
            capabilities = _capability_map(properties.get("capabilities"))
            self._snapshot.deployments.append(
                ObservedModelDeployment(
                    id=self._id("obsdeployment", name),
                    tenant_id=self._tenant_id,
                    endpoint_id=self._endpoint_id,
                    snapshot_id=self._snapshot_id,
                    deployment_name=name,
                    model_name=_text(model.get("name")),
                    model_version=_text(model.get("version")),
                    model_format=_text(model.get("format")),
                    model_publisher=_text(model.get("publisher")),
                    sku_name=_text(sku.get("name")),
                    sku_capacity=_int_or_none(sku.get("capacity")),
                    provisioning_state=_text(properties.get("provisioningState")),
                    rai_policy_name=_text(properties.get("raiPolicyName")),
                    capabilities=capabilities,
                    request_paths=_request_paths(capabilities),
                )
            )

    def _collect_available_models(self, items: list[JsonObject]) -> None:
        for item in items:
            # ``accounts/{name}/models`` returns the model under a ``model`` envelope alongside the
            # kind and SKU it would need.
            model = item.get("model")
            model = model if isinstance(model, dict) else item
            name = _text(model.get("name"))
            if not name:
                continue
            version = _text(model.get("version"))
            deprecation = model.get("deprecation")
            deprecation = deprecation if isinstance(deprecation, dict) else {}
            self._snapshot.available_models.append(
                ObservedAvailableModel(
                    id=self._id("obsmodel", name, version or ""),
                    tenant_id=self._tenant_id,
                    endpoint_id=self._endpoint_id,
                    snapshot_id=self._snapshot_id,
                    model_name=name,
                    model_format=_text(model.get("format")),
                    model_version=version,
                    lifecycle_status=_text(model.get("lifecycleStatus")),
                    max_capacity=_int_or_none(model.get("maxCapacity")),
                    capabilities=_capability_map(model.get("capabilities")),
                    deprecation_inference=_text(deprecation.get("inference")),
                    deprecation_fine_tune=_text(deprecation.get("fineTune")),
                )
            )
