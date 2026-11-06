"""The fictional roster and desired estate, independent of any live tenant."""

import re
from dataclasses import asdict, dataclass
from uuid import UUID

ENVIRONMENTS = ("dev", "stg", "prod")
ENVIRONMENT_KEYS = {"dev": "development", "stg": "staging", "prod": "production"}
ALL = ENVIRONMENTS


@dataclass(frozen=True)
class Team:
    code: str
    name: str
    group: str
    budget: int
    environments: tuple[str, ...] = ALL


TEAMS = (
    Team("general", "General", "general", 25),
    Team("NASA-MISSION", "NASA Mission Systems", "nasa-mission", 100),
    Team("FAA-OPS", "FAA Airspace Operations", "faa-ops", 150, ("prod",)),
    Team("FAA-RND", "FAA Flight Systems Research", "faa-rnd", 75),
    Team("DOE-LABS", "DOE National Lab Research", "doe-labs", 150),
    Team("USDA-DATA", "USDA Agricultural Insights", "usda-data", 100),
)


@dataclass(frozen=True)
class Person:
    agency: str
    name: str
    center: str
    additional_centers: tuple[str, ...] = ()

    @property
    def local_part(self) -> str:
        return f"mosaic.demo.{self.agency.lower()}.{self.name.lower().replace(' ', '.')}"

    @property
    def centers(self) -> tuple[str, ...]:
        return ("general", self.center, *self.additional_centers)


PEOPLE = tuple(
    Person(agency, name, center, ("FAA-RND",) if name == "Carlos Rivera" else ())
    for agency, center, names in (
        (
            "NASA",
            "NASA-MISSION",
            (
                "Daniel Brooks",
                "Miguel Santos",
                "Ethan Park",
                "Aisha Carter",
                "Priya Shah",
            ),
        ),
        ("FAA", "FAA-OPS", ("James Wilson", "Carlos Rivera", "Marcus Bennett")),
        ("FAA", "FAA-RND", ("Emily Chen", "Fatima Hassan")),
        (
            "DOE",
            "DOE-LABS",
            (
                "Michael Reed",
                "David Nguyen",
                "Omar Rahman",
                "Sofia Martinez",
                "Grace Kim",
            ),
        ),
        (
            "USDA",
            "USDA-DATA",
            (
                "William Turner",
                "Andre Johnson",
                "Noah Patel",
                "Sarah Thompson",
                "Elena Garcia",
            ),
        ),
    )
    for name in names
)


@dataclass(frozen=True)
class Service:
    key: str
    short_key: str
    name: str
    center: str
    calls_model: bool = False

    def cost_center(self, environment: str) -> str:
        if environment not in ENVIRONMENTS:
            raise ValueError("Unknown environment")
        return "FAA-RND" if self.center == "FAA-OPS" and environment != "prod" else self.center

    def host_name(self, environment: str) -> str:
        self.cost_center(environment)
        name = f"mcp-mf-{environment}-{self.short_key}"
        if len(name) > 32:
            raise ValueError("Container Apps name exceeds 32 characters")
        return name


SERVICES = (
    Service("doe-energy-data", "doe-energy", "DOE Energy Data Desk", "DOE-LABS"),
    Service("doe-lab-research", "doe-research", "DOE Lab Research Library", "DOE-LABS", True),
    Service("faa-airport-reference", "faa-airports", "FAA Airport Reference", "FAA-OPS"),
    Service(
        "faa-regulatory-reference", "faa-guidance", "FAA Regulatory Reference", "FAA-RND", True
    ),
    Service("usda-crop-insights", "usda-crops", "USDA Crop Insights", "USDA-DATA"),
    Service(
        "usda-conservation-guide", "usda-conserve", "USDA Conservation Guide", "USDA-DATA", True
    ),
    Service("nasa-mission-archive", "nasa-missions", "NASA Mission Archive", "NASA-MISSION", True),
    Service("nasa-earthdata-explorer", "nasa-earthdata", "NASA Earthdata Explorer", "NASA-MISSION"),
)


def validate_grant(center: str, environment: str) -> None:
    team = next((team for team in TEAMS if team.code == center), None)
    if team is None or environment not in team.environments:
        raise ValueError("Cost center is not approved for that environment")


def validate_target(target: dict) -> None:
    identifiers = (
        "tenantId",
        "subscriptionId",
        "apiAppClientId",
        "apiServicePrincipalId",
        "userRoleId",
        "apiPrincipalId",
    )
    for field in identifiers:
        if field not in target:
            if field in ("tenantId", "subscriptionId"):
                raise ValueError(f"{field} is required")
            continue
        if str(UUID(target[field])) != target[field]:
            raise ValueError(f"{field} must be a canonical UUID")
    vault = target.get("onboardingVault")
    if vault is not None and not re.fullmatch(r"[a-z][a-z0-9-]{1,22}[a-z0-9]", vault):
        raise ValueError("onboardingVault must be an Azure Key Vault name")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", target["domain"]):
        raise ValueError("domain must be a DNS name")
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,30}", target["estate"]):
        raise ValueError("estate must be a short lowercase identifier")


def inventory() -> dict:
    return {
        "gateways": 3,
        "usableAiEndpoints": 5,
        "mcpDeployments": [
            {"environment": env, "host": service.host_name(env), **asdict(service)}
            for env in ENVIRONMENTS
            for service in SERVICES
        ],
        "people": [asdict(person) | {"localPart": person.local_part} for person in PEOPLE],
        "costCenters": [asdict(team) for team in TEAMS],
        "monthlyModelBudgetsUsd": sum(team.budget for team in TEAMS),
        "manualProductionJobs": 5,
    }
