import pytest
from mosaic_api.environments import (
    EnvironmentCatalog,
    EnvironmentDefinition,
    azure_environment_tag,
    built_in_environments,
    compatibility_fingerprint,
    compatibility_matrix,
    permits,
    suggest_environment,
    validate_catalog,
)
from mosaic_api.errors import ValidationError


def _catalog(**kwargs: object) -> EnvironmentCatalog:
    return EnvironmentCatalog.new("tenant-test").model_copy(update=kwargs)


def test_built_in_environment_seed_contract() -> None:
    seeds = built_in_environments()

    assert [item.key for item in seeds] == [
        "development",
        "test",
        "qc",
        "staging",
        "production",
        "sandbox",
    ]
    assert all(item.built_in for item in seeds)
    assert next(item for item in seeds if item.key == "production").production is True


@pytest.mark.parametrize(
    ("environment", "field"),
    [
        (EnvironmentDefinition(key="Unclassified", display_name="Custom", color="brand"), "key"),
        (
            EnvironmentDefinition(
                key="custom", display_name="Unclassified", color="brand"
            ),
            "displayName",
        ),
        (
            EnvironmentDefinition(
                key="custom", display_name="Custom", color="brand", aliases=["production"]
            ),
            "aliases",
        ),
        (
            EnvironmentDefinition(
                key="custom",
                display_name="Custom",
                color="brand",
                accepts_endpoints_from=["missing"],
            ),
            "acceptsEndpointsFrom",
        ),
    ],
)
def test_catalog_validation_reports_invalid_environment_reason(
    environment: EnvironmentDefinition, field: str
) -> None:
    with pytest.raises(ValidationError) as raised:
        validate_catalog([*built_in_environments(), environment])

    assert raised.value.details["reason"] == "invalidEnvironment"
    assert raised.value.details["field"] == field


def test_catalog_validation_enforces_production_isolation_both_directions() -> None:
    catalog = _catalog()
    production = next(item for item in catalog.environments if item.key == "production")
    staging = next(item for item in catalog.environments if item.key == "staging")

    with pytest.raises(ValidationError):
        validate_catalog(
            [
                *(item for item in catalog.environments if item.key != "production"),
                production.model_copy(update={"accepts_endpoints_from": ["staging"]}),
            ]
        )

    with pytest.raises(ValidationError):
        validate_catalog(
            [
                *(
                    item
                    for item in catalog.environments
                    if item.key not in {"production", "staging"}
                ),
                production.model_copy(update={"accepts_endpoints_from": ["staging"]}),
                staging.model_copy(update={"production": False}),
            ]
        )


def test_permits_contract_matrix_for_classified_unclassified_unknown_and_exceptions() -> None:
    staging_exception = next(item for item in built_in_environments() if item.key == "staging")
    catalog = _catalog(
        environments=[
            *(item for item in built_in_environments() if item.key != "staging"),
            staging_exception.model_copy(update={"accepts_endpoints_from": ["test"]}),
        ]
    )

    assert permits(catalog, "production", "production").level == "allowed"
    assert permits(catalog, "staging", "test").via_exception is True
    assert permits(catalog, "staging", "development").level == "blocked"
    assert permits(catalog, "production", None).level == "blocked"
    assert permits(catalog, None, "production").level == "blocked"
    assert permits(catalog, "development", None).level == "warning"
    assert permits(catalog, None, "development").level == "warning"
    assert permits(catalog, None, None).level == "warning"
    assert (
        permits(catalog.model_copy(update={"require_classification": True}), None, None).level
        == "blocked"
    )
    assert permits(catalog, "missing", "test").level == "blocked"


def test_compatibility_fingerprint_changes_only_for_rule_inputs() -> None:
    catalog = _catalog()
    base = compatibility_fingerprint(catalog, "staging", "test")
    cosmetic = catalog.model_copy(
        update={
            "environments": [
                environment.model_copy(update={"display_name": "Stage"})
                if environment.key == "staging"
                else environment
                for environment in catalog.environments
            ]
        }
    )
    exception = catalog.model_copy(
        update={
            "environments": [
                environment.model_copy(update={"accepts_endpoints_from": ["test"]})
                if environment.key == "staging"
                else environment
                for environment in catalog.environments
            ]
        }
    )

    assert compatibility_fingerprint(cosmetic, "staging", "test") == base
    assert compatibility_fingerprint(exception, "staging", "test") != base
    required = catalog.model_copy(update={"require_classification": True})
    assert compatibility_fingerprint(
        required, None, None
    ) != compatibility_fingerprint(catalog, None, None)


def test_matrix_suggestion_and_azure_tag_helpers() -> None:
    catalog = _catalog()

    assert len(compatibility_matrix(catalog)) == 49
    assert suggest_environment(catalog, "Pre Prod").key == "staging"  # type: ignore[union-attr]
    assert suggest_environment(catalog, "PROD").key == "production"  # type: ignore[union-attr]
    assert azure_environment_tag({"Owner": "team", "ENV": "Prod"}) == "Prod"
    assert azure_environment_tag({"environment": "raw value", "env": "ignored"}) == "raw value"
