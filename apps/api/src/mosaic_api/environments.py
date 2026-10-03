import json
import re
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field

from mosaic_api.domain import Entity, MosaicModel, deterministic_id
from mosaic_api.domain import EnvironmentVerdict as EnvironmentVerdict
from mosaic_api.domain import VerdictLevel as VerdictLevel
from mosaic_api.errors import ConflictError, ValidationError

ENVIRONMENT_CATALOG_ENTITY_TYPE = "environmentCatalog"
UNCLASSIFIED_KEY = "unclassified"
_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,31}$")


class EnvironmentColor(StrEnum):
    BRAND = "brand"
    DANGER = "danger"
    IMPORTANT = "important"
    INFORMATIVE = "informative"
    SEVERE = "severe"
    SUBTLE = "subtle"
    SUCCESS = "success"
    WARNING = "warning"


class EnvironmentDefinition(MosaicModel):
    key: str
    display_name: str
    description: str | None = None
    color: EnvironmentColor
    production: bool = False
    aliases: list[str] = Field(default_factory=list)
    accepts_endpoints_from: list[str] = Field(default_factory=list)
    order: int = 0
    built_in: bool = False


class EnvironmentCatalog(Entity):
    entity_type: Literal["environmentCatalog"] = "environmentCatalog"
    environments: list[EnvironmentDefinition]
    require_classification: bool = False

    @classmethod
    def new(cls, tenant_id: str) -> "EnvironmentCatalog":
        return cls(
            id=deterministic_id(ENVIRONMENT_CATALOG_ENTITY_TYPE, tenant_id),
            tenant_id=tenant_id,
            environments=built_in_environments(),
        )


def built_in_environments() -> list[EnvironmentDefinition]:
    return [
        EnvironmentDefinition(
            key="development",
            display_name="Development",
            description="Early engineering and integration work.",
            color=EnvironmentColor.BRAND,
            aliases=["dev", "develop"],
            order=10,
            built_in=True,
        ),
        EnvironmentDefinition(
            key="test",
            display_name="Test",
            description="Automated and manual test workloads.",
            color=EnvironmentColor.INFORMATIVE,
            aliases=["tst", "testing"],
            order=20,
            built_in=True,
        ),
        EnvironmentDefinition(
            key="qc",
            display_name="QC",
            description="Quality-control validation before staging.",
            color=EnvironmentColor.IMPORTANT,
            aliases=["qa", "quality"],
            order=30,
            built_in=True,
        ),
        EnvironmentDefinition(
            key="staging",
            display_name="Staging",
            description="Production-like readiness and pre-release testing.",
            color=EnvironmentColor.WARNING,
            aliases=["stage", "stg", "preprod", "pre-prod"],
            order=40,
            built_in=True,
        ),
        EnvironmentDefinition(
            key="production",
            display_name="Production",
            description="Live workloads serving production traffic.",
            color=EnvironmentColor.DANGER,
            production=True,
            aliases=["prod", "prd", "live"],
            order=50,
            built_in=True,
        ),
        EnvironmentDefinition(
            key="sandbox",
            display_name="Sandbox",
            description="Exploration, labs, proofs of concept, and demos.",
            color=EnvironmentColor.SUBTLE,
            aliases=["sbx", "lab", "poc"],
            order=60,
            built_in=True,
        ),
    ]


def _invalid(field: str, message: str, *, extra: Mapping[str, object] | None = None) -> None:
    details: dict[str, object] = {"reason": "invalidEnvironment", "field": field}
    if extra:
        details.update(extra)
    raise ValidationError(message, details=details)


def _normalized_alias(alias: str) -> str:
    return alias.strip().casefold()


def validate_catalog(environments: list[EnvironmentDefinition]) -> None:
    if len(environments) > 30:
        _invalid("key", "A catalog can contain at most 30 environments")
    keys: set[str] = set()
    names: set[str] = set()
    aliases: dict[str, str] = {}
    by_key = {environment.key: environment for environment in environments}
    for environment in environments:
        if not _KEY_PATTERN.match(environment.key):
            _invalid("key", "Environment keys must be slugs from 2 to 32 characters")
        if environment.key == UNCLASSIFIED_KEY:
            _invalid("key", "Unclassified is reserved and cannot be defined")
        if environment.key in keys:
            _invalid("key", "Environment keys must be unique")
        keys.add(environment.key)

        display_name = environment.display_name.strip()
        if not 1 <= len(display_name) <= 40:
            _invalid("displayName", "Environment display names must be 1 to 40 characters")
        name_key = display_name.casefold()
        if name_key == "unclassified":
            _invalid("displayName", "Unclassified is reserved for resources without an environment")
        if name_key in names:
            _invalid("displayName", "Environment display names must be unique")
        names.add(name_key)

        if environment.description is not None and len(environment.description) > 280:
            _invalid("description", "Environment descriptions must be 280 characters or fewer")
        if len(environment.aliases) > 10:
            _invalid("aliases", "An environment can have at most 10 aliases")
        if not 0 <= environment.order <= 10000:
            _invalid("order", "Environment order must be between 0 and 10000")

    reserved_aliases = {environment.key for environment in environments} | names
    for environment in environments:
        seen_aliases: set[str] = set()
        for alias in environment.aliases:
            normalized = _normalized_alias(alias)
            if not 1 <= len(normalized) <= 40:
                _invalid("aliases", "Aliases must be 1 to 40 characters")
            if normalized in reserved_aliases:
                _invalid("aliases", "Aliases cannot duplicate keys or display names")
            if normalized in seen_aliases or normalized in aliases:
                _invalid("aliases", "Aliases must be unique across the catalog")
            seen_aliases.add(normalized)
            aliases[normalized] = environment.key

        seen_accepts: set[str] = set()
        for accepted in environment.accepts_endpoints_from:
            if accepted not in by_key:
                _invalid("acceptsEndpointsFrom", "Accepted endpoint environments must exist")
            if accepted == environment.key:
                _invalid(
                    "acceptsEndpointsFrom",
                    "An environment cannot list itself as an exception",
                )
            if accepted in seen_accepts:
                _invalid("acceptsEndpointsFrom", "Accepted endpoint environments cannot repeat")
            seen_accepts.add(accepted)
            if environment.production and not by_key[accepted].production:
                _invalid(
                    "acceptsEndpointsFrom",
                    "Production environments can only accept production-class endpoints",
                )

    production_references = {
        accepted: environment.key
        for environment in environments
        if environment.production
        for accepted in environment.accepts_endpoints_from
    }
    for key, referenced_by in production_references.items():
        if not by_key[key].production:
            _invalid(
                "production",
                "An environment accepted by a production environment must stay production-class",
                extra={"environment": key, "referencedBy": referenced_by},
            )


def _definitions(catalog: EnvironmentCatalog) -> dict[str, EnvironmentDefinition]:
    return {environment.key: environment for environment in catalog.environments}


def _name(definitions: Mapping[str, EnvironmentDefinition], key: str | None) -> str:
    return "Unclassified" if key is None else definitions[key].display_name


def is_production(catalog: EnvironmentCatalog, key: str | None) -> bool:
    if key is None:
        return False
    definition = _definitions(catalog).get(key)
    return bool(definition and definition.production)


def permits(
    catalog: EnvironmentCatalog, gateway_env: str | None, endpoint_env: str | None
) -> EnvironmentVerdict:
    definitions = _definitions(catalog)
    if gateway_env is not None and gateway_env not in definitions:
        return EnvironmentVerdict(
            level=VerdictLevel.BLOCKED,
            reason=f"Gateway environment {gateway_env!r} isn't defined in Settings → Environments.",
            gateway_environment=gateway_env,
            endpoint_environment=endpoint_env,
        )
    if endpoint_env is not None and endpoint_env not in definitions:
        return EnvironmentVerdict(
            level=VerdictLevel.BLOCKED,
            reason=(
                f"Endpoint environment {endpoint_env!r} isn't defined in "
                "Settings → Environments."
            ),
            gateway_environment=gateway_env,
            endpoint_environment=endpoint_env,
        )
    gateway_name = _name(definitions, gateway_env)
    endpoint_name = _name(definitions, endpoint_env)
    if gateway_env is not None and endpoint_env is not None:
        if gateway_env == endpoint_env:
            return EnvironmentVerdict(
                level=VerdictLevel.ALLOWED,
                reason=f"Both are {gateway_name}.",
                gateway_environment=gateway_env,
                endpoint_environment=endpoint_env,
            )
        gateway_def = definitions[gateway_env]
        if endpoint_env in gateway_def.accepts_endpoints_from:
            return EnvironmentVerdict(
                level=VerdictLevel.ALLOWED,
                reason=f"{gateway_name} gateways also accept endpoints from {endpoint_name}.",
                gateway_environment=gateway_env,
                endpoint_environment=endpoint_env,
                via_exception=True,
            )
        return EnvironmentVerdict(
            level=VerdictLevel.BLOCKED,
            reason=(
                f"A {gateway_name} gateway can't front a {endpoint_name} endpoint. "
                f"Classify the endpoint as {gateway_name}, or publish it through a "
                f"{endpoint_name} gateway."
            ),
            gateway_environment=gateway_env,
            endpoint_environment=endpoint_env,
        )
    if gateway_env is not None:
        if definitions[gateway_env].production:
            return EnvironmentVerdict(
                level=VerdictLevel.BLOCKED,
                reason=(
                    f"A {gateway_name} gateway can't front an unclassified endpoint. "
                    "Classify the endpoint first."
                ),
                gateway_environment=gateway_env,
                endpoint_environment=None,
            )
        if catalog.require_classification:
            return EnvironmentVerdict(
                level=VerdictLevel.BLOCKED,
                reason=(
                    "Require classification is on, and the endpoint is unclassified. "
                    "Classify it first."
                ),
                gateway_environment=gateway_env,
                endpoint_environment=None,
            )
        return EnvironmentVerdict(
            level=VerdictLevel.WARNING,
            reason=(
                "The endpoint is unclassified. Classify it so environment rules can protect "
                "this pairing."
            ),
            gateway_environment=gateway_env,
            endpoint_environment=None,
        )
    if endpoint_env is not None:
        if definitions[endpoint_env].production:
            return EnvironmentVerdict(
                level=VerdictLevel.BLOCKED,
                reason=(
                    f"An unclassified gateway can't front a {endpoint_name} endpoint. "
                    "Classify the gateway first."
                ),
                gateway_environment=None,
                endpoint_environment=endpoint_env,
            )
        if catalog.require_classification:
            return EnvironmentVerdict(
                level=VerdictLevel.BLOCKED,
                reason=(
                    "Require classification is on, and the gateway is unclassified. "
                    "Classify it first."
                ),
                gateway_environment=None,
                endpoint_environment=endpoint_env,
            )
        return EnvironmentVerdict(
            level=VerdictLevel.WARNING,
            reason=(
                "The gateway is unclassified. Classify it so environment rules can protect "
                "this pairing."
            ),
            gateway_environment=None,
            endpoint_environment=endpoint_env,
        )
    if catalog.require_classification:
        return EnvironmentVerdict(
            level=VerdictLevel.BLOCKED,
            reason=(
                "Require classification is on, and both resources are unclassified. "
                "Classify them first."
            ),
            gateway_environment=None,
            endpoint_environment=None,
        )
    return EnvironmentVerdict(
        level=VerdictLevel.WARNING,
        reason=(
            "Both resources are unclassified. Classify them so environment rules can protect "
            "this pairing."
        ),
        gateway_environment=None,
        endpoint_environment=None,
    )


def compatibility_fingerprint(
    catalog: EnvironmentCatalog, gateway_env: str | None, endpoint_env: str | None
) -> str:
    verdict = permits(catalog, gateway_env, endpoint_env)
    payload = {
        "endpointEnvironment": endpoint_env,
        "endpointProduction": is_production(catalog, endpoint_env),
        "gatewayEnvironment": gateway_env,
        "gatewayProduction": is_production(catalog, gateway_env),
        "level": verdict.level,
        "permittedByException": endpoint_env if verdict.via_exception else None,
        "requireClassification": catalog.require_classification,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


class EnvironmentCompatibilityCell(EnvironmentVerdict):
    pass


def compatibility_matrix(catalog: EnvironmentCatalog) -> list[EnvironmentCompatibilityCell]:
    values: list[str | None] = [environment.key for environment in sorted_environments(catalog)]
    values.append(None)
    return [
        EnvironmentCompatibilityCell.model_validate(
            permits(catalog, gateway_env, endpoint_env).model_dump()
        )
        for gateway_env in values
        for endpoint_env in values
    ]


def sorted_environments(catalog: EnvironmentCatalog) -> list[EnvironmentDefinition]:
    return sorted(catalog.environments, key=lambda item: (item.order, item.key))


def _compact(value: str) -> str:
    return re.sub(r"[-_\s]+", "", value.casefold().strip())


def suggest_environment(
    catalog: EnvironmentCatalog, value: str | None
) -> EnvironmentDefinition | None:
    if value is None:
        return None
    normalized = value.casefold().strip()
    if not normalized:
        return None
    for environment in catalog.environments:
        candidates = [environment.key, environment.display_name, *environment.aliases]
        if normalized in {candidate.casefold().strip() for candidate in candidates}:
            return environment
    compact = _compact(value)
    return next(
        (
            environment
            for environment in catalog.environments
            if compact
            in {
                _compact(environment.key),
                _compact(environment.display_name),
                *(_compact(alias) for alias in environment.aliases),
            }
        ),
        None,
    )


def azure_environment_tag(tags: Mapping[str, str] | None) -> str | None:
    if not tags:
        return None
    by_name = {name.casefold(): value for name, value in tags.items()}
    return by_name.get("environment") or by_name.get("env")


class EnvironmentUsage(MosaicModel):
    gateways: int = 0
    model_endpoints: int = 0
    mcp_endpoints: int = 0


class EnvironmentWithUsage(EnvironmentDefinition):
    usage: EnvironmentUsage = Field(default_factory=EnvironmentUsage)


class EnvironmentCatalogView(MosaicModel):
    environments: list[EnvironmentWithUsage]
    require_classification: bool = False
    unclassified: EnvironmentUsage = Field(default_factory=EnvironmentUsage)
    compatibility: list[EnvironmentCompatibilityCell] = Field(default_factory=list)
    updated_at: datetime | None = None


class EnvironmentCreate(MosaicModel):
    key: str
    display_name: str
    description: str | None = None
    color: EnvironmentColor
    production: bool = False
    aliases: list[str] = Field(default_factory=list)
    accepts_endpoints_from: list[str] = Field(default_factory=list)
    order: int | None = None


class EnvironmentUpdate(MosaicModel):
    display_name: str | None = None
    description: str | None = None
    color: EnvironmentColor | None = None
    production: bool | None = None
    aliases: list[str] | None = None
    accepts_endpoints_from: list[str] | None = None
    order: int | None = None


class EnvironmentSettingsUpdate(MosaicModel):
    require_classification: bool


EnvironmentResourceKind = Literal["gateway", "modelEndpoint", "mcpEndpoint"]
EnvironmentSuggestionSource = Literal["azureTag", "legacyLabel"]


class EnvironmentSuggestion(MosaicModel):
    resource_kind: EnvironmentResourceKind
    resource_id: str
    resource_name: str
    suggested_environment: str | None = None
    source: EnvironmentSuggestionSource | None = None
    evidence: str | None = None


class EnvironmentSuggestionList(MosaicModel):
    items: list[EnvironmentSuggestion]


class EnvironmentAssignment(MosaicModel):
    resource_kind: EnvironmentResourceKind
    resource_id: str
    environment: str | None = None


class EnvironmentAssignmentRequest(MosaicModel):
    assignments: list[EnvironmentAssignment]
    acknowledge_grants: bool = False


class EnvironmentAssignmentOutcome(MosaicModel):
    resource_kind: EnvironmentResourceKind
    resource_id: str
    resource_name: str
    previous_environment: str | None = None
    environment: str | None = None
    status: Literal["applied", "unchanged", "failed"]
    message: str | None = None


class EnvironmentAssignmentResult(MosaicModel):
    results: list[EnvironmentAssignmentOutcome]
    grants_carried: int = 0
    warnings: list[str] = Field(default_factory=list)


class PortalEnvironment(MosaicModel):
    key: str
    display_name: str
    description: str | None = None
    color: EnvironmentColor
    production: bool
    order: int


class BlockedPublication(MosaicModel):
    kind: Literal["model", "mcp", "pool"] = "model"
    publication_id: str
    display_name: str | None = None
    status: str
    gateway_id: str
    gateway_name: str
    gateway_environment: str | None
    model_endpoint_id: str | None = None
    model_endpoint_name: str | None = None
    deployment_name: str | None = None
    mcp_endpoint_id: str | None = None
    mcp_endpoint_name: str | None = None
    endpoint_environment: str | None
    verdict: EnvironmentVerdict


def refuse_blocked_pairing(verdict: EnvironmentVerdict) -> None:
    if verdict.level == VerdictLevel.BLOCKED:
        raise ConflictError(
            verdict.reason,
            details={
                "reason": "environmentBlocked",
                "verdict": verdict.model_dump(mode="json", by_alias=True),
            },
        )


def publications_blocked_error(
    publications: list[BlockedPublication],
    *,
    suggested_assignments: list[dict[str, object]] | None = None,
) -> ConflictError:
    return ConflictError(
        "This environment change would block applied publications",
        details={
            "reason": "publicationsBlocked",
            "publications": [
                item.model_dump(mode="json", by_alias=True) for item in publications
            ],
            "suggestedAssignments": suggested_assignments or [],
        },
    )
