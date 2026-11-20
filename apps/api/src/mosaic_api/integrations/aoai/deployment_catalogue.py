"""Normalize Azure's advertised model SKUs without inventing capacity or quota mappings."""

import math

from mosaic_api.errors import UpstreamError
from mosaic_api.integrations.aoai.inventory import _capability_map, _text
from mosaic_api.integrations.apim.client import JsonObject
from mosaic_api.model_deployments import (
    DeployableModel,
    DeploymentCapacity,
    DeploymentQuota,
    DeploymentSku,
)


def number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _capacity(raw: object) -> DeploymentCapacity:
    if not isinstance(raw, dict):
        raise UpstreamError("Azure did not provide verifiable SKU capacity constraints")
    for key in ("minimum", "maximum", "step", "default"):
        value = raw.get(key)
        if value is not None and (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            or (key == "step" and value == 0)
        ):
            raise UpstreamError("Azure returned invalid SKU capacity constraints")
    allowed = raw.get("allowedValues", [])
    if not isinstance(allowed, list) or any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in allowed
    ):
        raise UpstreamError("Azure returned invalid SKU capacity choices")
    if (
        raw.get("minimum") is not None
        and raw.get("maximum") is not None
        and raw["minimum"] > raw["maximum"]
    ):
        raise UpstreamError("Azure returned contradictory SKU capacity bounds")
    return DeploymentCapacity(
        minimum=raw.get("minimum"),
        maximum=raw.get("maximum"),
        step=raw.get("step"),
        default=raw.get("default"),
        allowed_values=allowed,
    )


def _deployment_sku_rows(raw: object, *, base_openai: bool) -> list[JsonObject]:
    groups: dict[str, list[JsonObject]] = {}
    for sku in raw if isinstance(raw, list) else []:
        name = _text(sku.get("name")) if isinstance(sku, dict) else None
        if name is None:
            raise UpstreamError("Azure returned an incomplete model SKU; retry the catalogue")
        groups.setdefault(name.casefold(), []).append(sku)

    rows: list[JsonObject] = []
    for sku_key, variants in groups.items():
        if len(variants) == 1:
            rows.append(variants[0])
            continue
        if base_openai and len(variants) == 2:
            # ARM takes only the SKU name. For a base model, keep Azure's base
            # meter when the second row is explicitly its fine-tuning counterpart.
            by_usage = {
                usage.casefold(): row
                for row in variants
                if (usage := _text(row.get("usageName"))) is not None
            }
            base_rows = [
                row
                for usage_name, row in by_usage.items()
                if usage_name.startswith(f"openai.{sku_key}.")
                and not usage_name.endswith("-finetune")
                and f"{usage_name}-finetune" in by_usage
            ]
            if base_rows:
                rows.extend(base_rows)
                continue
        raise UpstreamError("Azure returned ambiguous duplicate model deployment types")
    return rows


def deployment_models(items: list[JsonObject], usages: list[JsonObject]) -> list[DeployableModel]:
    quotas: dict[str, DeploymentQuota] = {}
    seen: set[str] = set()
    for usage in usages:
        name = usage.get("name")
        usage_name = _text(name.get("value")) if isinstance(name, dict) else None
        current, limit = number(usage.get("currentValue")), number(usage.get("limit"))
        if usage_name is not None:
            key = usage_name.casefold()
            if key in seen:
                quotas.pop(key, None)
                continue
            seen.add(key)
        if usage_name is not None and current is not None and limit is not None:
            quotas[usage_name.casefold()] = DeploymentQuota(
                usage_name=usage_name,
                current_value=current,
                limit=limit,
                available=max(0, limit - current),
                unit=_text(usage.get("unit")),
                blocked=str(usage.get("status", "")).casefold() in {"blocked", "unknown"},
            )

    models: list[DeployableModel] = []
    model_identities: set[tuple[str | None, ...]] = set()
    for item in items:
        raw = item.get("model")
        model = raw if isinstance(raw, dict) else item
        name = _text(model.get("name"))
        model_format = _text(model.get("format"))
        version = _text(model.get("version"))
        if not name or not model_format or not version:
            raise UpstreamError(
                "Azure returned a model without its full format, name, and version. "
                "The deployment catalogue cannot be verified; retry or inspect the account "
                "in Azure."
            )
        source = _text(model.get("source"))
        source_account = _text(model.get("sourceAccount"))
        identity = (
            model_format,
            name,
            version,
            _text(model.get("publisher")),
            source,
            source_account,
        )
        if identity in model_identities:
            raise UpstreamError("Azure returned an ambiguous duplicate model version")
        model_identities.add(identity)
        skus: list[DeploymentSku] = []
        raw_skus = model.get("skus", item.get("skus", []))
        for sku in _deployment_sku_rows(
            raw_skus,
            base_openai=model_format.casefold() == "openai" and not (source or source_account),
        ):
            usage_name = _text(sku.get("usageName"))
            skus.append(
                DeploymentSku(
                    name=sku["name"],
                    capacity=_capacity(sku.get("capacity")),
                    quota=quotas.get(usage_name.casefold()) if usage_name else None,
                    deprecation_date=_text(sku.get("deprecationDate")),
                )
            )
        deprecation = model.get("deprecation")
        models.append(
            DeployableModel(
                model_name=name,
                model_format=model_format,
                model_version=version,
                model_publisher=_text(model.get("publisher")),
                source=source,
                source_account=source_account,
                is_default_version=model.get("isDefaultVersion") is True,
                lifecycle_status=_text(model.get("lifecycleStatus")),
                deprecation_inference=(
                    _text(deprecation.get("inference")) if isinstance(deprecation, dict) else None
                ),
                capabilities=_capability_map(model.get("capabilities")),
                skus=skus,
            )
        )
    return sorted(
        models,
        key=lambda model: (
            model.model_name.casefold(),
            model.model_format.casefold(),
            model.model_version,
        ),
    )
